"""Implements: FA9 (beobachtbarer Lauf: Fortschritt, Warnungen, Abbruch), FA55/FA56 (Ereignisse region_ready, region_warning).

Beobachtbarer Lauf, ohne die Ausführungssemantik zu ändern: ``run_scenario()``
meldet über einen optionalen ``on_event``-Callback (Default: No-op) den
Lebenszyklus jedes Layers und Schritts; die Web-Schicht visualisiert damit den DAG live.

- ``ProgressEvent`` und die Ereignis-Konstanten: stabile Strings (Wire-Contract
  zur Web-Schicht), neue Typen kommen nur additiv hinzu.
- ``CancelToken``: kooperativer Abbruch, geprüft an jeder Layer- und
  Schrittgrenze; der Lauf endet mit ``RunCancelled``.
- ``collect_warnings()`` fängt die Warnungen eines Laufabschnitts ab. Ein
  prozessweiter ``warnings.showwarning``-Router leitet sie an den Sammler des
  aktuellen Kontexts (``ContextVar``), ohne Sammler an den vorherigen Hook. Es
  gibt bewusst kein ``warnings.catch_warnings``: es schaltet Prozesszustand um
  und ist mit parallelen Läufen in Threads nicht sicher.

Prozessweite Warnungskonfiguration:

1. Der Router, bei Bedarf installiert.
2. Ein ``always``-Filter für ``geofact.*`` und ``geofact_plugin_*``
   (``GEOFACT_WARNING_MODULES``) ganz vorn. Damit erreichen Warnungen der
   Bausteine den Router auch bei ``PYTHONWARNINGS=ignore``, der
   Wiederholungsschutz gilt nicht (ein zweiter Lauf meldet erneut) und ``-W error``
   macht sie nicht zu Ausnahmen. ``collect_warnings()`` prüft nur, ob der Eintrag
   noch vorn steht, und setzt ihn sonst neu. Warnungen fremder Module bleiben
   unter den Filtern des Hosts.
3. Reparatur: ein nach der Installation hinzugekommenes nacktes ``ignore``
   (geleaktes ``simplefilter``) wird an der nächsten Abschnittsgrenze entfernt,
   einmal je Prozess mit ``WarningFilterRepaired``; Einträge des Hosts davor bleiben.
"""

from __future__ import annotations

import threading
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterator, Optional


# Ereignis-Typen (stabile Strings, Wire-Contract zur Web-Schicht).
PLAN = "plan"
LAYER_LOADING = "layer_loading"
LAYER_LOADED = "layer_loaded"
LAYER_WARNING = "layer_warning"
LAYER_ERROR = "layer_error"
STEP_RUNNING = "step_running"
STEP_WARNING = "step_warning"
STEP_DONE = "step_done"
STEP_ERROR = "step_error"
RUN_DONE = "run_done"
RUN_ERROR = "run_error"
RUN_CANCELLED = "run_cancelled"
# FA55/FA56: Region aufgelöst und Arbeits-CRS bestimmt (detail = RegionInfo.to_dict());
# je Plausibilitätswarnung der Region ein Ereignis.
REGION_READY = "region_ready"
REGION_WARNING = "region_warning"


@dataclass
class ProgressEvent:
    """Ein einzelnes Fortschritts-Ereignis.

    type ist einer der Modul-Konstanten oben. Die übrigen Felder sind
    je nach Ereignis gesetzt (None-Felder werden in to_dict() entfernt,
    damit die JSON-Nachricht schlank bleibt)."""

    type: str
    step: Optional[str] = None
    op: Optional[str] = None
    layer: Optional[str] = None
    status: Optional[str] = None
    output_type: Optional[str] = None
    duration_ms: Optional[float] = None
    message: Optional[str] = None
    error: Optional[str] = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        return {k: v for k, v in raw.items() if v is not None and v != {}}


ProgressCallback = Callable[[ProgressEvent], None]


def _noop(_event: ProgressEvent) -> None:
    """Standard-Callback: verwirft Ereignisse."""
    return None


# --- Abbruch ---


class CancelToken:
    """Kooperativer Abbruch eines Laufs. Ein Thread ruft ``cancel()``, der
    Executor liest ``cancelled`` an jeder Layer- und Schrittgrenze. Thread-
    sicher (``threading.Event``); ein Token gilt für einen Lauf."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


# --- Warnungen ---


@dataclass(frozen=True)
class RunWarning:
    """Eine während eines Laufs aufgetretene Warnung. layer bzw. step
    nennen, wo sie auftrat (Layer laden bzw. Schritt ausführen). severity:
    ``info`` für Hinweise (Kategorie endet auf ``Notice``), sonst ``warning``
    (engine/loading.py::warning_severity)."""

    message: str
    category: str
    layer: Optional[str] = None
    step: Optional[str] = None
    severity: str = "warning"


_collector: ContextVar["list[tuple[str, str]] | None"] = ContextVar(
    "geofact_warning_collector", default=None
)
_install_lock = threading.Lock()
_previous_showwarning: Callable[..., Any] | None = None


def _route_warning(message, category, filename, lineno, file=None, line=None) -> None:
    """Ersatz für ``warnings.showwarning``: leitet die Warnung an den Sammler
    des aktuellen Kontexts, sonst an den vorherigen Hook."""
    collector = _collector.get()
    if collector is not None:
        collector.append((str(message), category.__name__))
        return
    assert _previous_showwarning is not None
    _previous_showwarning(message, category, filename, lineno, file, line)


# Module, deren Warnungen ein Lauf immer sammelt: das Paket geofact und die
# Plugin-Module geofact_plugin_<Name> (engine/discovery.py), nicht ``geofact_web``.
GEOFACT_WARNING_MODULES = r"(?:geofact|geofact_plugin_\w+)(?:\..*)?$"


def _always_filter_leads() -> bool:
    """Steht unser ``always``-Eintrag ganz vorn in der Filterliste?"""
    filters = warnings.filters
    if not filters:
        return False
    action, _message, category, module, _lineno = filters[0]
    return (
        action == "always"
        and category is Warning
        and getattr(module, "pattern", None) == GEOFACT_WARNING_MODULES
    )


def install_warning_router() -> None:
    """Installiert den Router und den ``always``-Filter (idempotent, thread-sicher).
    Läuft bei jedem ``collect_warnings()`` und ist dann nur ein Vergleich; unter
    ``warnings.catch_warnings`` (z. B. pytest) setzt es Hook und Filter nach dem
    Verlassen wieder. ``filterwarnings`` verschiebt einen gleichen Eintrag nach
    vorn; ein gleichzeitig warnender Thread kann ihn in diesem Moment verpassen."""
    global _previous_showwarning, _baseline_ignores
    with _install_lock:
        if warnings.showwarning is not _route_warning:
            _previous_showwarning = warnings.showwarning
            warnings.showwarning = _route_warning
            # Ausgangslage merken: nackte ``ignore``-Einträge des Hosts vor dem
            # Router sind gewollt.
            _baseline_ignores = sum(
                1 for entry in warnings.filters if entry == _BARE_IGNORE
            )
        if not _always_filter_leads():
            warnings.filterwarnings("always", module=GEOFACT_WARNING_MODULES)


# --- Reparatur geleakter Warnungsfilter ---


class WarningFilterRepaired(UserWarning):
    """Ein globales ``warnings.simplefilter('ignore')`` (z. B. aus einer
    Bibliothek) war nach dem Start des Routers hinzugekommen und wurde entfernt."""


_BARE_IGNORE = ("ignore", None, Warning, None, 0)
_baseline_ignores = 0
_repair_reported = False


def _notify_filters_mutated() -> None:
    """Invalidiert den Filter-Cache von ``warnings`` nach direkter Listenänderung
    (privater Hook, je nach Python-Version anders benannt)."""
    for name in ("_filters_mutated", "_filters_mutated_lock_held"):
        hook = getattr(warnings, name, None)
        if callable(hook):
            try:
                hook()
            except Exception:  # noqa: BLE001 - Cache-Invalidierung ist best effort
                continue
            return


def repair_leaked_filters() -> bool:
    """Entfernt nackte ``ignore``-Einträge, die nach dem Router hinzukamen: ein
    geleaktes ``simplefilter('ignore')`` unterdrückte sonst still Warnungen eines
    Laufs (Goldene Regel 7). Beim ersten Eingriff im Prozess erscheint einmal
    ``WarningFilterRepaired``. Liefert True, wenn repariert wurde."""
    global _repair_reported
    with _install_lock:
        count = sum(1 for entry in warnings.filters if entry == _BARE_IGNORE)
        excess = count - _baseline_ignores
        if excess <= 0:
            return False
        kept: list = []
        for entry in warnings.filters:
            if excess > 0 and entry == _BARE_IGNORE:
                excess -= 1
                continue
            kept.append(entry)
        warnings.filters[:] = kept
        _notify_filters_mutated()
        report = not _repair_reported
        _repair_reported = True
    if report:
        warnings.warn(
            "Ein globaler Warnungsfilter 'ignore' (z. B. aus einer Bibliothek) wurde entfernt, "
            "damit die Warnungen des Laufs sichtbar bleiben",
            WarningFilterRepaired,
            stacklevel=2,
        )
    return True


@contextmanager
def collect_warnings() -> Iterator[list[tuple[str, str]]]:
    """Fängt die Warnungen des aktuellen Kontexts ab: eine Liste von
    ``(Text, Kategoriename)``, die sich im ``with``-Block füllt. Verschachtelt
    bekommt der innere Sammler seine Warnungen allein; andere Threads haben
    einen eigenen Kontext. Ein geleaktes ``ignore`` wird vorher entfernt."""
    install_warning_router()
    collected: list[tuple[str, str]] = []
    token = _collector.set(collected)
    try:
        repair_leaked_filters()
        yield collected
    finally:
        _collector.reset(token)
