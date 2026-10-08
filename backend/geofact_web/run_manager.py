"""Ausführung von Szenarien mit Live-Fortschritt (in-memory).

Jeder Lauf läuft in einem eigenen Thread (geofact ist synchron); die
ProgressEvents gehen in eine thread-sichere Queue, aus der der SSE-Endpunkt
liest. Der Manager hält den LayerStore, damit fertige Knoten schon während
des Laufs abrufbar sind. Läufe liegen nur im Prozessspeicher und gehen beim
Neustart verloren; der einzige geteilte Zustand ist das Lock-geschützte
``_runs``-Dict.

Nutzer-Isolation (FA24): ``get_for_owner()`` gibt fremde Läufe nicht zurück
(der Router meldet 404 statt 403, damit eine geratene run_id nichts verrät);
``create()`` lehnt einen Lauf ab, sobald der Besitzer sein Limit gleichzeitiger
Läufe erreicht hat. Nach GEOFACT_WEB_MAX_RETAINED_RUNS abgeschlossenen Läufen
wird der älteste entfernt (Eviction), laufende nie.

Ausführung (FA44, FA54, FA59, FA58/FA60/FA65/FA56, FA66/FA33): ``api.run`` mit
``out_dir`` schreibt die Ausgaben einmal am Ende des Laufs (kein Dienstaufruf je
Download). Ohne ``release_intermediates`` bleibt jeder Knoten abrufbar. Das
Laufverzeichnis verschwindet mit der Eviction; ``purge_orphaned_run_dirs()``
räumt beim Start nur alte Reste fremder Prozesse auf.

Liste der Läufe (FA89): ``list_for_owner`` nennt die Läufe eines Besitzers, die
der Manager noch hält, neuester zuerst; was die Eviction entfernt hat, fehlt.
"""

from __future__ import annotations

import math
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid
import warnings
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from geofact import api
from geofact.api import events as ev

from . import scenario_service
from .models import (
    AttributionOut,
    LayerProvenanceOut,
    NodeOriginOut,
    RegionInfoOut,
    RunStatus,
    RunWarningOut,
    StepState,
)
from .settings import get_settings

_SENTINEL = object()  # markiert das Ende des Event-Streams

_FINISHED_STATUSES = ("done", "error", "cancelled")

# Anzahl abgeschlossener Läufe, die maximal im Speicher bleiben.
_DEFAULT_MAX_RETAINED_RUNS = 20


def _max_retained_runs() -> int:
    raw = os.environ.get("GEOFACT_WEB_MAX_RETAINED_RUNS")
    if not raw:
        return _DEFAULT_MAX_RETAINED_RUNS
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"GEOFACT_WEB_MAX_RETAINED_RUNS muss eine Ganzzahl sein, nicht '{raw}'"
        ) from None
    if value < 1:
        raise ValueError(f"GEOFACT_WEB_MAX_RETAINED_RUNS muss >= 1 sein, nicht {value}")
    return value


# Alter, ab dem ein fremdes Laufverzeichnis als Rest gilt (FA54); muss deutlich
# über der längsten Laufzeit liegen, damit ein schreibender anderer Prozess nicht trifft.
_DEFAULT_RUN_DIR_MAX_AGE_HOURS = 6.0


def _run_dir_max_age_hours() -> float:
    raw = os.environ.get("GEOFACT_WEB_RUN_DIR_MAX_AGE_HOURS")
    if not raw:
        return _DEFAULT_RUN_DIR_MAX_AGE_HOURS
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"GEOFACT_WEB_RUN_DIR_MAX_AGE_HOURS muss eine Zahl (Stunden) sein, nicht '{raw}'"
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"GEOFACT_WEB_RUN_DIR_MAX_AGE_HOURS muss > 0 sein, nicht '{raw}'"
        )
    return value


class RunLimitExceeded(Exception):
    """FA24: Besitzer hat bereits GEOFACT_WEB_MAX_CONCURRENT_RUNS_PER_USER
    gleichzeitig laufende Läufe - neuer Lauf wird abgelehnt statt
    stillschweigend eingereiht."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(
            f"Limit von {limit} gleichzeitig laufenden Läufen je Nutzer erreicht."
        )


@dataclass
class RunState:
    run_id: str
    document: api.ScenarioDocument
    refresh_snapshots: bool
    owner: str = "anonymous"
    # FA82: Konnektor-Optionen dieses Laufs (z. B. OSM-Quelle); None = Werte des Servers.
    connector_options: Optional[dict[str, str]] = None
    status: str = "pending"  # pending | running | done | error | cancelled
    order: list[str] = field(default_factory=list)
    steps: dict[str, StepState] = field(default_factory=dict)
    layers: list[str] = field(default_factory=list)
    loaded_layers: list[str] = field(default_factory=list)
    layer_load_ms: dict[str, float] = field(default_factory=dict)
    error: Optional[str] = None
    # FA59: Überschreibungen deklarierter Parameter (None = keine).
    parameters: Optional[dict[str, Any]] = None
    # FA60/FA61: Herkunft je Knoten-id aus der Expansion.
    origins: dict[str, NodeOriginOut] = field(default_factory=dict)
    # FA58: CRS-Herkunft je geladenem Layer.
    layer_provenance: dict[str, LayerProvenanceOut] = field(default_factory=dict)
    # FA55/FA56: Region und Arbeits-CRS.
    region: Optional[RegionInfoOut] = None
    # FA65: Lizenzen je Knoten (Plan) und der Ausgabequellen (Lauf).
    node_attribution: dict[str, Optional[AttributionOut]] = field(default_factory=dict)
    attribution: Optional[AttributionOut] = None
    # Warnungen ohne Schritt (region_warning).
    warnings: list[RunWarningOut] = field(default_factory=list)
    # FA66: freigegebene Knoten (bleibt leer - die Web-Demo gibt nie frei).
    released: list[str] = field(default_factory=list)
    # Status vor dem ersten Ereignis; der aktuelle Status enthielte die Ereignisse
    # schon, das Replay wendete sie doppelt an.
    initial_status: Optional[RunStatus] = None

    store: api.LayerStore = field(default_factory=api.LayerStore)
    result: Optional[api.ExecutionResult] = None
    report: Optional[api.RunReport] = None
    cancel: api.CancelToken = field(default_factory=api.CancelToken)
    # FA93: der Arbeits-Thread rechnet noch (auch nach einem Abbruch, bis der laufende
    # Baustein endet); solange zählt der Lauf gegen das Limit je Nutzer (FA24).
    worker_active: bool = False
    # FA93: der Abbruch ist dem Nutzer schon gemeldet; was der Thread danach noch
    # meldet, gehört zu keinem sichtbaren Lauf mehr.
    detached: bool = False
    # Verzeichnis der geschriebenen Ausgaben (FA44); wird mit dem Lauf entfernt.
    run_dir: Optional[Path] = None
    # FA89: Anlage und Abschluss (Unix-Zeit); finished_at bleibt None, solange er läuft.
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    # Alle bisherigen Events, für spät verbundene SSE-Clients.
    events: list[dict[str, Any]] = field(default_factory=list)
    _subscribers: list["queue.Queue"] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # FA93: ordnet Ereignisse des Arbeits-Threads und den Abbruch; nie zusammen mit _lock gehalten.
    _event_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def scenario(self) -> api.Scenario:
        return self.document.scenario

    def remove_run_dir(self) -> None:
        """Löscht die geschriebenen Ausgaben dieses Laufs."""
        if self.run_dir is not None:
            shutil.rmtree(self.run_dir, ignore_errors=True)
            self.run_dir = None

    def to_status(self) -> RunStatus:
        return RunStatus(
            run_id=self.run_id,
            status=self.status,  # type: ignore[arg-type]
            order=self.order,
            steps=self.steps,
            layers=self.layers,
            loaded_layers=self.loaded_layers,
            layer_load_ms=self.layer_load_ms,
            error=self.error,
            origins=self.origins,
            layer_provenance=self.layer_provenance,
            region=self.region,
            node_attribution=self.node_attribution,
            attribution=self.attribution,
            warnings=self.warnings,
            released=self.released,
        )

    # --- Pub/Sub für SSE --------------------------------------------
    def subscribe(self) -> "queue.Queue":
        q: queue.Queue = queue.Queue()
        with self._lock:
            # Vergangene Events sofort nachliefern (Replay), dann live.
            for event in self.events:
                q.put(event)
            if self.status in _FINISHED_STATUSES:
                q.put(_SENTINEL)
            else:
                self._subscribers.append(q)
        return q

    def _publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self.events.append(event)
            for q in self._subscribers:
                q.put(event)

    def _close(self) -> None:
        with self._lock:
            for q in self._subscribers:
                q.put(_SENTINEL)
            self._subscribers.clear()


class RunManager:
    def __init__(
        self,
        max_retained_runs: int | None = None,
        run_dir_max_age_hours: float | None = None,
    ) -> None:
        # Einfügereihenfolge = Erstellungsreihenfolge, Kriterium der Eviction.
        self._runs: OrderedDict[str, RunState] = OrderedDict()
        self._lock = threading.Lock()
        # Erst bei Gebrauch ausgewertet, damit Tests die Variable je Test setzen können.
        self._max_retained_runs = max_retained_runs
        self._run_dir_max_age_hours = run_dir_max_age_hours

    @property
    def max_retained_runs(self) -> int:
        return (
            self._max_retained_runs
            if self._max_retained_runs is not None
            else _max_retained_runs()
        )

    @property
    def run_dir_max_age_hours(self) -> float:
        if self._run_dir_max_age_hours is not None:
            return self._run_dir_max_age_hours
        return _run_dir_max_age_hours()

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(run_id)

    def get_for_owner(self, run_id: str, owner: str) -> RunState | None:
        """Wie get(), aber ohne Läufe anderer Besitzer (FA24); der Router meldet 404."""
        state = self.get(run_id)
        if state is None or state.owner != owner:
            return None
        return state

    def list_for_owner(self, owner: str) -> list[RunState]:
        """FA89: die Läufe dieses Besitzers, die der Manager noch hält, neuester
        zuerst (Einfügereihenfolge = Erstellungsreihenfolge)."""
        with self._lock:
            return [
                state for state in reversed(self._runs.values()) if state.owner == owner
            ]

    def _running_count_for_owner_locked(self, owner: str) -> int:
        """Muss unter self._lock aufgerufen werden."""
        return sum(
            1
            for state in self._runs.values()
            if state.owner == owner
            and (state.status not in _FINISHED_STATUSES or state.worker_active)
        )

    def _evict_if_needed_locked(self) -> list[RunState]:
        """Unter self._lock aufrufen. Entfernt die ältesten abgeschlossenen Läufe
        über max_retained_runs und liefert sie; der Aufrufer löscht deren
        Verzeichnisse nach Freigabe der Sperre, damit das andere Anfragen nicht blockiert."""
        limit = self.max_retained_runs
        finished_ids = [
            run_id
            for run_id, state in self._runs.items()
            if state.status in _FINISHED_STATUSES
        ]
        overflow = len(finished_ids) - limit
        evicted: list[RunState] = []
        for run_id in finished_ids[: max(overflow, 0)]:
            evicted.append(self._runs.pop(run_id))
        return evicted

    def purge_orphaned_run_dirs(self) -> list[Path]:
        """Entfernt Laufverzeichnisse, die zu keinem Lauf dieses Prozesses gehören und
        älter als ``run_dir_max_age_hours`` sind (FA54); beim Start einmal aufgerufen.
        Das Alter schützt die Ausgaben eines zweiten, noch lebenden Prozesses auf
        demselben Datenverzeichnis. Liefert die entfernten Pfade."""
        max_age_s = self.run_dir_max_age_hours * 3600
        now = time.time()
        with self._lock:
            live = {
                state.run_dir
                for state in self._runs.values()
                if state.run_dir is not None
            }
        runs_dir = get_settings().runs_dir
        removed: list[Path] = []
        if not runs_dir.is_dir():
            return removed
        for entry in sorted(runs_dir.iterdir()):
            if entry in live:
                continue
            try:
                age_s = now - entry.stat().st_mtime
            except OSError:
                continue  # inzwischen verschwunden (ein anderer Prozess hat es entfernt)
            if age_s < max_age_s:
                continue  # frisch: Lauf eines anderen Prozesses oder gerade erst beendet
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                pass  # unten geprüft: steht der Eintrag noch, wird das gemeldet
            if entry.exists():
                # Goldene Regel 7: nicht entfernbare Reste werden gemeldet.
                warnings.warn(
                    f"Laufverzeichnis '{entry}' eines früheren Prozesses konnte nicht "
                    "entfernt werden - bitte von Hand löschen",
                    stacklevel=2,
                )
                continue
            removed.append(entry)
        return removed

    def cancel(self, run_id: str) -> bool:
        """FA93: der Abbruch gilt sofort. Der Lauf ist für den Nutzer beendet
        (Status, Ereignis, Ende des Ereignisstroms), auch wenn ein Baustein noch
        rechnet oder lädt; der Kern hält an seiner nächsten Grenze an (FA9), was er
        bis dahin meldet, wird verworfen."""
        state = self.get(run_id)
        if state is None:
            return False
        with state._event_lock:
            if state.status in _FINISHED_STATUSES:
                return False
            state.cancel.cancel()
            state.detached = True
            for step in state.steps.values():
                if step.status in ("loading", "running"):
                    step.status = "cancelled"
            state.status = "cancelled"
            state.finished_at = time.time()
            state._publish({"type": ev.RUN_CANCELLED})
        state._close()
        return True

    def create(
        self,
        document: api.ScenarioDocument,
        refresh_snapshots: bool,
        owner: str = "anonymous",
        parameters: dict[str, Any] | None = None,
        connector_options: dict[str, str] | None = None,
    ) -> RunState:
        run_id = uuid.uuid4().hex[:12]
        state = RunState(
            run_id=run_id,
            document=document,
            refresh_snapshots=refresh_snapshots,
            owner=owner,
            connector_options=connector_options,
            # typisierte Werte des Kerns, sonst die Rohwerte; leer = keine Überschreibung
            parameters=dict(getattr(document, "parameters", None) or parameters or {})
            or None,
        )
        # Plan schon vor dem Start ableiten, damit das Frontend den DAG sofort zeichnet.
        execution_plan = api.plan(document)
        scenario = document.scenario
        order = list(execution_plan.order)
        step_by_id = {s.id: s for s in scenario.steps}
        state.order = order
        state.layers = sorted(execution_plan.required_layers)
        state.steps = {
            sid: StepState(
                id=sid,
                op=step_by_id[sid].op,
                output_type=step_by_id[sid].output_type().value,
                inputs=dict(step_by_id[sid].inputs),
                status="pending",
            )
            for sid in order
        }
        state.origins = {
            node: NodeOriginOut.model_validate(origin)
            for node, origin in scenario_service.node_origins(document).items()
        }
        state.node_attribution = {
            node: scenario_service.attribution_out(attribution)
            for node, attribution in execution_plan.attribution.items()
        }
        state.attribution = scenario_service.attribution_out(
            api.output_attribution(document, execution_plan)
        )
        state.initial_status = state.to_status().model_copy(deep=True)
        # Limit-Check und Insert unter einem Lock, sonst könnten parallele
        # create()-Aufrufe desselben Besitzers das Limit umgehen.
        with self._lock:
            limit = get_settings().max_concurrent_runs_per_user
            if self._running_count_for_owner_locked(owner) >= limit:
                raise RunLimitExceeded(limit)
            self._runs[run_id] = state
        return state

    def start(self, state: RunState) -> None:
        state.worker_active = True
        thread = threading.Thread(target=self._run, args=(state,), daemon=True)
        thread.start()

    # --- Ereignisse ---
    def _on_event(self, state: RunState, event: ev.ProgressEvent) -> None:
        # Abbruch läuft über state.cancel, nicht über den Beobachter.
        with state._event_lock:
            if state.detached:
                return  # FA93: nach dem gemeldeten Abbruch ändert nichts mehr den Lauf
            self._apply(state, event)
            state._publish(event.to_dict())

    @staticmethod
    def _apply(state: RunState, event: ev.ProgressEvent) -> None:
        etype = event.type
        if etype == ev.PLAN:
            state.status = "running"
            if isinstance(event.detail.get("attribution"), dict):
                state.node_attribution = {
                    node: scenario_service.attribution_out(raw)
                    for node, raw in event.detail["attribution"].items()
                }
        elif etype == ev.REGION_READY and event.detail:
            state.region = RegionInfoOut.model_validate(event.detail)
        elif etype == ev.REGION_WARNING and event.message:
            state.warnings.append(_warning_out(event))
        elif etype == ev.LAYER_LOADING and event.step in state.steps:
            state.steps[event.step].status = "loading"
        elif etype == ev.LAYER_LOADED and event.layer:
            # Ein Layer wird genau einmal geladen (FA9), daher setzen statt aufsummieren.
            if event.duration_ms is not None:
                state.layer_load_ms[event.layer] = event.duration_ms
            provenance = event.detail.get("provenance")
            if isinstance(provenance, dict):
                state.layer_provenance[event.layer] = LayerProvenanceOut.model_validate(
                    provenance
                )
        elif etype == ev.STEP_RUNNING and event.step in state.steps:
            state.steps[event.step].status = "running"
        elif etype in (ev.STEP_WARNING, ev.LAYER_WARNING) and event.message:
            # Am Schritt, der den Layer braucht; ohne Schritt auf Lauf-Ebene, nie verworfen.
            if event.step in state.steps:
                step = state.steps[event.step]
                step.warnings.append(event.message)
                step.warning_details.append(_warning_out(event))
            else:
                state.warnings.append(_warning_out(event))
        elif etype == ev.LAYER_ERROR and event.step in state.steps:
            step = state.steps[event.step]
            step.status = "error"
            step.error = event.error
        elif etype == ev.STEP_DONE and event.step in state.steps:
            step = state.steps[event.step]
            step.status = "done"
            step.duration_ms = event.duration_ms
            state.released.extend(event.detail.get("released") or ())
        elif etype == ev.RUN_DONE and isinstance(event.detail.get("attribution"), dict):
            state.attribution = scenario_service.attribution_out(
                event.detail["attribution"]
            )
        elif etype == ev.STEP_ERROR and event.step in state.steps:
            step = state.steps[event.step]
            step.status = "error"
            step.duration_ms = event.duration_ms
            step.error = event.error
        elif etype == ev.RUN_ERROR:
            state.status = "error"
            state.error = event.error
        elif etype == ev.RUN_CANCELLED:
            state.status = "cancelled"

    def _run(self, state: RunState) -> None:
        try:
            runs_dir = get_settings().runs_dir
            runs_dir.mkdir(parents=True, exist_ok=True)
            state.run_dir = Path(
                tempfile.mkdtemp(prefix=f"geofact_run_{state.run_id}_", dir=runs_dir)
            )
            # Kein release_intermediates (FA66): jedes Zwischenergebnis bleibt abrufbar (FA33).
            report = api.run(
                state.document,
                out_dir=state.run_dir,
                observer=lambda e: self._on_event(state, e),
                cancel=state.cancel,
                store=state.store,
                refresh=state.refresh_snapshots,
                connector_options=state.connector_options,
            )
            if state.detached:
                # FA93: der Abbruch kam, als nur noch Ausgaben geschrieben wurden.
                # Der Lauf bleibt abgebrochen und ohne Bericht.
                return
            state.report = report
            state.result = report.result
            state.loaded_layers = report.result.loaded_layers
            if report.skipped_outputs:
                # Goldene Regel 7: Serverlog, dazu SKIPPED_OUTPUTS.txt im Download.
                details = "; ".join(
                    f"output[{s.index}] ({s.type}, source={s.source}): {s.reason}"
                    for s in report.skipped_outputs
                )
                warnings.warn(
                    f"Lauf '{state.run_id}': nicht alle deklarierten Ausgaben geschrieben "
                    f"({len(report.skipped_outputs)} übersprungen): {details}",
                    stacklevel=1,
                )
            if state.status != "error":
                state.status = "done"
        except api.RunCancelled:
            # RUN_CANCELLED hat der Executor schon gemeldet.
            state.status = "cancelled"
        except Exception as exc:  # noqa: BLE001 - in Status gespiegelt
            if state.detached:
                return  # FA93: ein Fehler nach dem Abbruch ändert den Lauf nicht mehr
            if state.status != "error":
                state.status = "error"
                state.error = f"{type(exc).__name__}: {exc}"
                # Fehler vor oder außerhalb eines Schritts.
                state._publish({"type": ev.RUN_ERROR, "error": state.error})
        finally:
            state.worker_active = False
            if state.finished_at is None:
                state.finished_at = time.time()
            state._close()
            # Erst nach Abschluss evicten; der gerade fertige Lauf ist der neueste und nie das Ziel.
            with self._lock:
                evicted = self._evict_if_needed_locked()
            for old in evicted:
                old.remove_run_dir()


def _warning_out(event: ev.ProgressEvent) -> RunWarningOut:
    """Warnung mit Kategorie und Schweregrad (``detail.severity``, Standard ``warning``)."""
    severity = event.detail.get("severity")
    return RunWarningOut(
        message=event.message or "",
        category=event.detail.get("category"),
        severity=severity if severity in ("info", "warning") else "warning",
        layer=event.layer,
        step=event.step,
    )


# Prozessweiter Singleton-Manager, Läufe sind per owner isoliert (FA24).
manager = RunManager()
