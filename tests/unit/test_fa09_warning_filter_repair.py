"""Implements: FA9 (Zusatz: Reparatur geleakter Warnungsfilter, Ereignisse region_ready/region_warning).

Contract-Tests für ``engine/events.py``: ein nach dem Start des Routers
geleaktes ``simplefilter('ignore')`` wird an der nächsten Abschnittsgrenze
entfernt (einmalige ``WarningFilterRepaired``-Meldung je Prozess); ein
``ignore`` der Ausgangslage (Host, ``-W ignore``) bleibt."""

from __future__ import annotations

import warnings

import pytest

from geofact.engine import events

BARE_IGNORE = ("ignore", None, Warning, None, 0)


@pytest.fixture
def fresh_router(monkeypatch):
    """Eigene Filterliste und frischer Router je Test (catch_warnings stellt
    Hook und Filter danach wieder her)."""
    monkeypatch.setattr(events, "_repair_reported", False)
    monkeypatch.setattr(events, "_baseline_ignores", events._baseline_ignores)
    with warnings.catch_warnings():
        warnings.resetwarnings()
        # Router neu installieren lassen: Ausgangslage wird jetzt gemerkt
        warnings.showwarning = warnings._showwarning_orig  # type: ignore[attr-defined]
        yield


def _foreign_warning() -> None:
    """Eine Warnung, die einem fremden Modul zugerechnet wird (Bibliothek)."""
    exec(
        compile(
            "import warnings\nwarnings.warn('fremd', UserWarning)",
            "fremdlib.py",
            "exec",
        ),
        {"__name__": "fremdlib"},
    )


def test_fa09_leaked_ignore_is_removed_and_reported_once(fresh_router):
    events.install_warning_router()
    warnings.simplefilter("ignore")  # Leck, z. B. aus einer Bibliothek
    with events.collect_warnings() as first:
        _foreign_warning()
    assert BARE_IGNORE not in warnings.filters
    categories = [category for _, category in first]
    assert categories.count("WarningFilterRepaired") == 1
    assert ("fremd", "UserWarning") in first

    warnings.simplefilter("ignore")  # zweites Leck im selben Prozess
    with events.collect_warnings() as second:
        pass
    assert BARE_IGNORE not in warnings.filters
    assert [c for _, c in second if c == "WarningFilterRepaired"] == []


def test_fa09_baseline_ignore_of_the_host_is_kept(fresh_router):
    warnings.simplefilter(
        "ignore"
    )  # Host stellt Warnungen ab, BEVOR der Router startet
    events.install_warning_router()
    with events.collect_warnings() as collected:
        pass
    assert BARE_IGNORE in warnings.filters
    assert collected == []


def test_fa09_without_leak_the_filters_stay_untouched(fresh_router):
    events.install_warning_router()
    before = list(warnings.filters)
    with events.collect_warnings() as collected:
        pass
    assert warnings.filters == before
    assert collected == []
    assert events.repair_leaked_filters() is False


def test_fa09_region_events_are_additive_constants():
    assert events.REGION_READY == "region_ready"
    assert events.REGION_WARNING == "region_warning"
