"""Implements: FA9 (Web-Teil: Abbruch während des letzten Schritts endet als Abbruch).

Der Executor prüft den Abbruch nach dem letzten Schritt und der letzten Ladung
(FA9-Zusatz). Die Web-Demo muss das als ``cancelled`` zeigen - nicht als
fertigen Lauf - und darf kein ``run_done`` veröffentlichen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from geofact import api  # noqa: E402
from geofact.api import events as ev  # noqa: E402
from geofact_web.run_manager import RunManager, RunState  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _document() -> api.ScenarioDocument:
    return api.load_scenario(
        {
            "scenario": {"name": "cancel-last", "region": BBOX_REGION},
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
                    "params": {"radius_km": 1},
                },
                {
                    "id": "last",
                    "op": "buffer",
                    "inputs": {"geometry": "catchment"},
                    "params": {"radius_km": 0.2},
                },
            ],
            "output": [{"type": "geojson", "source": "last", "path": "last.geojson"}],
        }
    )


class _CancelWhileLastStepRuns(RunManager):
    """Drückt 'Abbrechen', während der letzte Schritt läuft (nach der
    Abbruchprüfung an seinem Anfang)."""

    def _on_event(self, state: RunState, event: ev.ProgressEvent) -> None:
        super()._on_event(state, event)
        if event.type == ev.STEP_RUNNING and event.step == "last":
            self.cancel(state.run_id)


def test_fa09_web_cancel_during_last_step_ends_as_cancelled():
    manager = _CancelWhileLastStepRuns(max_retained_runs=5)
    state = manager.create(_document(), refresh_snapshots=False)
    manager._run(state)

    assert state.status == "cancelled"
    types = [event["type"] for event in state.events]
    assert ev.RUN_CANCELLED in types
    assert ev.RUN_DONE not in types
    # FA93: der Abbruch gilt sofort - der Schritt, der gerade lief, ist abgebrochen,
    # der fertige davor bleibt abrufbar (FA33)
    assert state.to_status().steps["last"].status == "cancelled"
    assert state.to_status().steps["catchment"].status == "done"
    assert state.report is None
