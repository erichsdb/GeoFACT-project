"""Implements: FA66 (Nachbedingung 4: die Web-Demo gibt Zwischenergebnisse nie frei), FA33.

Die Freigabe nach dem letzten Leser ist opt-in (``release_intermediates``). Die
Web-Demo braucht jeden Knoten für die Per-Node-Ansicht und nach einem Fehler
(FA33) - der Lauf ruft ``api.run`` deshalb nie mit der Freigabe auf.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from geofact import api  # noqa: E402
from geofact_web import run_manager as run_manager_module  # noqa: E402
from geofact_web.run_manager import RunManager  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _document() -> api.ScenarioDocument:
    # Kette: substations -> catchment -> shrunk; catchment hat nach "shrunk"
    # keinen Leser mehr und wäre mit release_intermediates freigegeben.
    return api.load_scenario(
        {
            "scenario": {"name": "keep", "region": BBOX_REGION},
            "layers": [
                {
                    "id": "substations",
                    "source": "file",
                    "data_type": "vector",
                    "path": str(FIXTURES_DIR / "substations.geojson"),
                },
            ],
            "steps": [
                {
                    "id": "catchment",
                    "op": "buffer",
                    "inputs": {"geometry": "substations"},
                    "params": {"radius_km": 2},
                },
                {
                    "id": "shrunk",
                    "op": "buffer",
                    "inputs": {"geometry": "catchment"},
                    "params": {"radius_km": 0.5},
                },
            ],
            "output": [
                {"type": "geojson", "source": "shrunk", "path": "shrunk.geojson"}
            ],
        }
    )


def test_fa66_web_never_passes_release_intermediates(monkeypatch):
    seen: list[dict] = []
    real_run = run_manager_module.api.run

    def spy(document, **kwargs):
        seen.append(dict(kwargs))
        return real_run(document, **kwargs)

    monkeypatch.setattr(run_manager_module.api, "run", spy)
    manager = RunManager(max_retained_runs=5)
    state = manager.create(_document(), refresh_snapshots=False)
    manager._run(state)
    assert state.status == "done", state.error
    assert len(seen) == 1
    assert not seen[0].get("release_intermediates", False)


def test_fa66_web_keeps_every_intermediate_result():
    manager = RunManager(max_retained_runs=5)
    state = manager.create(_document(), refresh_snapshots=False)
    manager._run(state)
    assert state.status == "done", state.error
    for node in ("substations", "catchment", "shrunk"):
        assert state.store.get(node) is not None, node
    assert state.to_status().released == []
