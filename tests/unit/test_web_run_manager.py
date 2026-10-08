"""Tests für backend/geofact_web/run_manager.py: Eviction-Policy.

Ohne Eviction wächst RunManager._runs unbegrenzt - jeder abgeschlossene
Lauf behält seinen kompletten LayerStore für die Prozesslebensdauer.
Diese Tests prüfen die Eviction isoliert gegen eine eigene RunManager-
Instanz (NICHT den Prozess-Singleton `manager`, den test_web_backend.py
mitbenutzt) und mit synchroner Ausführung (kein Thread/Polling nötig).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from geofact import api  # noqa: E402
from geofact_web.run_manager import RunManager  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _scenario(name: str) -> api.ScenarioDocument:
    return api.load_scenario(
        {
            "scenario": {"name": name, "region": BBOX_REGION},
            "layers": [
                {
                    "id": "substations",
                    "source": "file",
                    "path": str(FIXTURES_DIR / "substations.geojson"),
                    "data_type": "vector",
                },
            ],
            "steps": [
                {
                    "id": "catchment",
                    "op": "buffer",
                    "inputs": {"geometry": "substations"},
                    "params": {"radius_km": 1},
                },
            ],
            "output": [
                {"type": "geojson", "source": "catchment", "path": "catchment.geojson"}
            ],
        }
    )


def _create_and_run_sync(manager: RunManager, name: str) -> str:
    """Legt einen Lauf an und führt ihn synchron im aktuellen Thread aus
    (manager._run statt manager.start(), damit der Test nicht auf einen
    Hintergrund-Thread warten muss)."""
    state = manager.create(_scenario(name), refresh_snapshots=False)
    manager._run(state)
    return state.run_id


def test_finished_runs_beyond_limit_are_evicted():
    """Vorbedingung: mehr abgeschlossene Läufe als max_retained_runs.
    Nachbedingung: die ältesten abgeschlossenen Läufe sind entfernt,
    genau max_retained_runs bleiben erhalten."""
    manager = RunManager(max_retained_runs=3)
    run_ids = [_create_and_run_sync(manager, f"run-{i}") for i in range(5)]

    remaining = set(manager._runs.keys())
    assert len(remaining) == 3
    # Die zuletzt erzeugten/abgeschlossenen Läufe bleiben erhalten.
    assert remaining == set(run_ids[-3:])
    for old_id in run_ids[:2]:
        assert manager.get(old_id) is None


def test_most_recently_finished_run_is_never_evicted():
    """Nachbedingung: der Lauf, der die Eviction auslöst (der zuletzt
    fertiggewordene), ist selbst nie das Evictionsziel - sonst könnte ein
    Client, der gerade auf sein Ergebnis pollt, es sofort wieder verlieren."""
    manager = RunManager(max_retained_runs=1)
    run_ids = [_create_and_run_sync(manager, f"run-{i}") for i in range(4)]

    last_id = run_ids[-1]
    assert manager.get(last_id) is not None
    assert manager.get(last_id).status == "done"
    assert len(manager._runs) == 1


def test_running_runs_are_never_evicted_regardless_of_limit():
    """Vorbedingung: ein Lauf ist noch nicht abgeschlossen (pending/running).
    Nachbedingung: er zählt nicht zum Limit und wird nie evictet, auch wenn
    danach mehr abgeschlossene Läufe als das Limit entstehen."""
    manager = RunManager(max_retained_runs=1)
    pending_state = manager.create(_scenario("still-pending"), refresh_snapshots=False)
    assert pending_state.status == "pending"

    for i in range(3):
        _create_and_run_sync(manager, f"finished-{i}")

    # Der pending Lauf ist weiterhin da, obwohl das Limit 1 abgeschlossenen
    # Lauf erlaubt - insgesamt sind es also 2 Einträge (1 pending + 1 done).
    assert manager.get(pending_state.run_id) is not None
    assert manager.get(pending_state.run_id).status == "pending"
    finished = [s for s in manager._runs.values() if s.status == "done"]
    assert len(finished) == 1


def test_eviction_limit_below_default_via_constructor_override():
    """Randfall: Limit 1 - nach jedem weiteren fertigen Lauf bleibt genau
    einer erhalten."""
    manager = RunManager(max_retained_runs=1)
    first_id = _create_and_run_sync(manager, "first")
    assert manager.get(first_id) is not None

    second_id = _create_and_run_sync(manager, "second")
    assert manager.get(first_id) is None
    assert manager.get(second_id) is not None


def test_max_retained_runs_reads_env_when_not_overridden(monkeypatch):
    monkeypatch.setenv("GEOFACT_WEB_MAX_RETAINED_RUNS", "7")
    manager = RunManager()
    assert manager.max_retained_runs == 7


def test_max_retained_runs_rejects_invalid_env(monkeypatch):
    monkeypatch.setenv("GEOFACT_WEB_MAX_RETAINED_RUNS", "not-a-number")
    manager = RunManager()
    with pytest.raises(ValueError, match="Ganzzahl"):
        _ = manager.max_retained_runs


# ---------------------------------------------------------------------------
# FA9: Abbruch über CancelToken, Layer-Fehler am Schritt
# ---------------------------------------------------------------------------


def _drain(subscription) -> list:
    from geofact_web.run_manager import _SENTINEL

    items = []
    while True:
        item = subscription.get(timeout=5)
        if item is _SENTINEL:
            return items
        items.append(item)


def test_fa09_cancelled_run_ends_as_cancelled_with_a_single_event():
    """Der Abbruch läuft über das CancelToken des Laufs; der Executor meldet
    run_cancelled selbst - der Manager veröffentlicht es nicht ein zweites Mal."""
    manager = RunManager()
    state = manager.create(_scenario("abbruch"), refresh_snapshots=False)
    state.cancel.cancel()
    manager._run(state)

    assert state.status == "cancelled"
    assert [e["type"] for e in state.events].count("run_cancelled") == 1
    assert state.loaded_layers == []
    # ein spät verbundener Client bekommt Replay und Ende (kein Hängenbleiben)
    replay = _drain(state.subscribe())
    assert [e["type"] for e in replay][-1] == "run_cancelled"


def test_fa09_cancel_marks_a_pending_run_and_rejects_a_finished_one():
    manager = RunManager()
    pending = manager.create(_scenario("noch-nicht-gestartet"), refresh_snapshots=False)
    assert manager.cancel(pending.run_id) is True
    assert pending.cancel.cancelled is True
    manager._run(pending)
    assert manager.cancel(pending.run_id) is False  # abgeschlossen


def test_fa09_layer_load_failure_marks_the_step_that_needed_the_layer(tmp_path):
    # Eine fehlende Datei fängt seit W2-02 der Vorab-Check (FA44) als
    # ConfigError; ein Ladefehler entsteht durch eine vorhandene, unlesbare Datei.
    broken = tmp_path / "kaputt.geojson"
    broken.write_text("kein GeoJSON", encoding="utf-8")
    scenario = api.load_scenario(
        {
            "scenario": {"name": "kaputt", "region": BBOX_REGION},
            "layers": [
                {
                    "id": "substations",
                    "source": "file",
                    "path": str(broken),
                    "data_type": "vector",
                },
            ],
            "steps": [
                {
                    "id": "catchment",
                    "op": "buffer",
                    "inputs": {"geometry": "substations"},
                    "params": {"radius_km": 1},
                },
            ],
            "output": [{"type": "geojson", "source": "catchment"}],
        }
    )
    manager = RunManager()
    state = manager.create(scenario, refresh_snapshots=False)
    manager._run(state)

    assert state.status == "error"
    assert "substations" in (state.error or "") and "file" in (state.error or "")
    assert state.steps["catchment"].status == "error"
    assert "substations" in (state.steps["catchment"].error or "")
    types = [e["type"] for e in state.events]
    assert "layer_error" in types and "run_error" in types
    assert "step_running" not in types


def test_fa06_layer_validation_report_shows_up_at_the_step_that_needed_the_layer():
    """Die gesammelte FA6-Meldung (layer_warning) landet in den Warnungen des
    Schritts, der den Layer braucht - sichtbar in der Ausführungsansicht."""
    scenario = api.load_scenario(
        {
            "scenario": {"name": "FA6", "region": BBOX_REGION},
            "layers": [
                {
                    "id": "substations",
                    "source": "file",
                    "path": str(FIXTURES_DIR / "substations.geojson"),
                    "data_type": "vector",
                    "validation": {
                        "required_fields": ["voltage"],
                        "on_violation": "warn",
                    },
                },
            ],
            "steps": [
                {
                    "id": "catchment",
                    "op": "buffer",
                    "inputs": {"geometry": "substations"},
                    "params": {"radius_km": 1},
                },
            ],
            "output": [{"type": "geojson", "source": "catchment"}],
        }
    )
    manager = RunManager()
    state = manager.create(scenario, refresh_snapshots=False)
    manager._run(state)

    assert state.status == "done"
    warnings_at_step = state.steps["catchment"].warnings
    assert any(
        "'required_fields'" in text and "substations" in text
        for text in warnings_at_step
    )
    assert any(
        e["type"] == "layer_warning" and e["layer"] == "substations"
        for e in state.events
    )
