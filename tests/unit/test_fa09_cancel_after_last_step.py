"""Implements: FA9 (Abbruchprüfung nach dem letzten Schritt und der letzten Ladung).

Contract-Tests für ``engine/executor.py``: ein Abbruch, der während des
letzten Schritts oder der letzten (nur von einer Ausgabe gelesenen) Ladung
angefordert wird, endet als ``RunCancelled`` - nie als fertiger Lauf mit
``run_done``.
"""

from __future__ import annotations

import pytest

from _executor_doubles import BBOX_REGION, file_layer
from geofact.core.errors import RunCancelled
from geofact.core.scenario import Scenario
from geofact.engine import events as ev
from geofact.engine.events import CancelToken
from geofact.engine.executor import run_scenario


def test_fa09_cancel_during_the_last_step_ends_as_cancelled():
    scenario = Scenario(
        **{
            "scenario": {"name": "Abbruch", "region": BBOX_REGION},
            "layers": [file_layer("substations")],
            "steps": [
                {
                    "id": "letzter",
                    "op": "buffer",
                    "inputs": {"geometry": "substations"},
                    "params": {"radius_km": 1},
                }
            ],
            "output": [{"type": "geojson", "source": "letzter"}],
        }
    )
    token = CancelToken()
    events: list = []

    def observe(event) -> None:
        events.append(event)
        if event.type == ev.STEP_RUNNING and event.step == "letzter":
            token.cancel()  # während des letzten Schritts angefordert

    with pytest.raises(RunCancelled):
        run_scenario(scenario, on_event=observe, cancel=token)
    types = [e.type for e in events]
    assert ev.STEP_DONE in types  # der Schritt selbst lief zu Ende
    assert ev.RUN_DONE not in types
    assert types[-1] == ev.RUN_CANCELLED


def test_fa09_cancel_during_the_final_load_ends_as_cancelled():
    scenario = Scenario(
        **{
            "scenario": {"name": "Abbruch", "region": BBOX_REGION},
            "layers": [file_layer("substations"), file_layer("roh")],
            "steps": [
                {
                    "id": "s1",
                    "op": "buffer",
                    "inputs": {"geometry": "substations"},
                    "params": {"radius_km": 1},
                }
            ],
            "output": [
                {"type": "geojson", "source": "s1"},
                {"type": "geojson", "source": "roh", "path": "roh.geojson"},
            ],  # final_loads
        }
    )
    token = CancelToken()
    events: list = []

    def observe(event) -> None:
        events.append(event)
        if event.type == ev.LAYER_LOADING and event.layer == "roh":
            token.cancel()

    with pytest.raises(RunCancelled):
        run_scenario(scenario, on_event=observe, cancel=token)
    types = [e.type for e in events]
    assert any(e.type == ev.LAYER_LOADED and e.layer == "roh" for e in events)
    assert ev.RUN_DONE not in types
    assert types[-1] == ev.RUN_CANCELLED
