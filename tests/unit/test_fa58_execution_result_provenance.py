"""Implements: FA58 (CRS-Provenienz im Ausführungsergebnis, Nachbedingung (1)).

Contract-Tests für ``engine/executor.py``: ``ExecutionResult.provenance``
enthält genau ``plan.required_layers`` (Lazy Loading: unbenutzte Layer haben
keine Provenienz), ``layer_loaded.detail["provenance"]`` trägt sie, und eine
Lücke ist ein expliziter Fehler. ``load_layer`` ist durch ein Double mit der
Signatur aus NACHT_SPEC 2.9 ersetzt (Senke ``provenance_sink``).
"""

from __future__ import annotations

import pytest

from _executor_doubles import BBOX_REGION, fake_loader, file_layer, points
from geofact.core.scenario import Scenario
from geofact.engine import events as ev
from geofact.engine import executor as executor_module
from geofact.engine.executor import run_scenario
from geofact.engine.run import load_scenario, run


def _config() -> dict:
    return {
        "scenario": {"name": "Provenienz", "region": BBOX_REGION},
        "layers": [
            file_layer("zones"),
            file_layer("points"),
            file_layer("unbenutzt"),
            file_layer("nur_ausgabe"),
        ],
        "steps": [
            {
                "id": "join",
                "op": "spatial_join",
                "inputs": {"left": "zones", "right": "points"},
                "params": {"predicate": "intersects"},
            }
        ],
        "output": [
            {"type": "geojson", "source": "join"},
            {"type": "geojson", "source": "nur_ausgabe", "path": "roh.geojson"},
        ],
    }


def _data() -> dict:
    return {
        layer: points("EPSG:32633")
        for layer in ("zones", "points", "unbenutzt", "nur_ausgabe")
    }


def test_fa58_execution_result_provenance_covers_exactly_the_loaded_layers(monkeypatch):
    monkeypatch.setattr(executor_module, "load_layer", fake_loader(_data()))
    events: list = []
    report = run(load_scenario(_config()), observer=events.append)
    result = report.result
    assert result.plan is not None
    assert set(result.provenance) == set(result.plan.required_layers)
    assert set(result.provenance) == {"zones", "points", "nur_ausgabe"}
    assert report.provenance == result.provenance
    loaded = {
        e.layer: e.detail.get("provenance") for e in events if e.type == ev.LAYER_LOADED
    }
    assert set(loaded) == {"zones", "points", "nur_ausgabe"}
    assert loaded["zones"]["source_crs"] == "EPSG:4326"
    assert loaded["zones"]["target_crs"] == "EPSG:32633"


def test_fa58_missing_provenance_of_a_loaded_layer_is_an_error(monkeypatch):
    monkeypatch.setattr(
        executor_module,
        "load_layer",
        fake_loader(
            _data(),
            skip_provenance={"points"},
        ),
    )
    with pytest.raises(RuntimeError, match="Provenienz inkonsistent"):
        run_scenario(Scenario(**_config()))
