"""Implements: FA33 (Zwischenergebnisse bleiben nach einem Fehler abrufbar).

Contract-Tests dafür, dass ein fehlgeschlagener Lauf die Ergebnisse
seiner bereits fertigen Schritte behält und weiter ausliefert - das ist
der Weg zur Fehlerursache (welche Daten liefen in den kaputten Schritt?).

Motivierender Fall: brach ein Schritt ab, war an die Zwischenergebnisse
nicht mehr herankommen, obwohl sie im Prozessspeicher noch lagen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from geofact import api  # noqa: E402
from geofact_web.run_manager import RunManager  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _failing_scenario() -> api.ScenarioDocument:
    """Erster Schritt läuft durch (buffer), zweiter scheitert
    zwangsläufig: filter auf einer Spalte, die der Layer nicht hat."""
    return api.load_scenario(
        {
            "scenario": {"name": "partial", "region": BBOX_REGION},
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
                {
                    "id": "broken",
                    "op": "filter",
                    "inputs": {"features": "catchment"},
                    "params": {"condition": "spalte_gibt_es_nicht > 1"},
                },
            ],
            "output": [{"type": "geojson", "source": "broken", "path": "out.geojson"}],
        }
    )


def _run_sync(manager: RunManager, document: api.ScenarioDocument):
    state = manager.create(document, refresh_snapshots=False)
    manager._run(state)
    return state


# =====================================================================
# Nachbedingung: der Lauf ist fehlerhaft, die fertigen Schritte bleiben
# =====================================================================


def test_fa33_failed_run_is_marked_error():
    """Vorbedingung des Rests: der Lauf scheitert tatsächlich."""
    manager = RunManager(max_retained_runs=5)
    state = _run_sync(manager, _failing_scenario())
    assert state.status == "error"
    assert state.error


def test_fa33_completed_step_result_survives_the_failure():
    """Kernfall: das Ergebnis des VOR dem Fehler fertigen Schritts liegt
    weiterhin im Store und ist abrufbar."""
    manager = RunManager(max_retained_runs=5)
    state = _run_sync(manager, _failing_scenario())
    assert state.store.get("catchment") is not None
    assert len(state.store.get("catchment")) > 0


def test_fa33_failing_step_has_no_result_but_an_error_message():
    """Abgrenzung: der gescheiterte Schritt selbst hat kein Ergebnis -
    stattdessen steht sein Fehlertext im Status (Regel 7)."""
    manager = RunManager(max_retained_runs=5)
    state = _run_sync(manager, _failing_scenario())
    assert state.store.get("broken") is None
    status = state.to_status()
    assert status.steps["broken"].status == "error"
    assert status.steps["broken"].error


def test_fa33_status_keeps_completed_step_marked_done():
    """Der Graph muss nach dem Fehler weiter zeigen, was fertig wurde -
    sonst ist nicht erkennbar, wo die Pipeline stand."""
    manager = RunManager(max_retained_runs=5)
    state = _run_sync(manager, _failing_scenario())
    status = state.to_status()
    assert status.steps["catchment"].status == "done"


def test_fa33_store_is_not_cleared_on_failure():
    """Regression: ein Aufräumen des Stores im Fehlerpfad würde genau
    die Nachbedingung dieser FA brechen."""
    manager = RunManager(max_retained_runs=5)
    state = _run_sync(manager, _failing_scenario())
    # Layer UND fertiger Schritt liegen weiterhin im Store.
    assert len(state.store) >= 1
    assert "catchment" in state.store
    assert "substations" in state.store


# =====================================================================
# HTTP-Ebene: der Knoten-Endpunkt liefert Zwischenergebnisse weiter aus,
# auch wenn der Lauf als fehlerhaft gilt
# =====================================================================


def test_fa33_node_endpoint_serves_partial_result_of_failed_run(monkeypatch):
    from fastapi.testclient import TestClient

    from geofact_web import run_manager as run_manager_module
    from geofact_web import settings as settings_module
    from geofact_web.main import app

    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    settings_module.get_settings.cache_clear()

    state = _run_sync(run_manager_module.manager, _failing_scenario())
    assert state.status == "error"

    with TestClient(app) as http:
        ok = http.get(f"/api/runs/{state.run_id}/nodes/catchment")
        assert ok.status_code == 200
        assert ok.json()["node_id"] == "catchment"

        # Der gescheiterte Schritt hat kein Ergebnis - 404 mit klarer
        # Meldung, nicht ein leeres Ergebnis (Regel 7).
        missing = http.get(f"/api/runs/{state.run_id}/nodes/broken")
        assert missing.status_code == 404

        # Der Lauf-Status bleibt abrufbar und trägt den Fehler.
        run = http.get(f"/api/runs/{state.run_id}")
        assert run.status_code == 200
        body = run.json()
        assert body["status"] == "error"
        assert body["steps"]["catchment"]["status"] == "done"
        assert body["steps"]["broken"]["status"] == "error"

    settings_module.get_settings.cache_clear()
