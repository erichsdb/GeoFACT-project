"""Implements: FA57 (CRS-Konsistenz je Schritt, Nachbedingung (5)).

Contract-Tests für ``engine/executor.py::check_input_crs``: ein Schritt bekommt
nur Eingänge in EINEM CRS - gemischte CRS scheitern VOR dem Aufruf der
Operation; ein Eingang außerhalb des Arbeits-CRS ist eine Warnung; Eingänge
ohne CRS werden nicht verglichen. ``load_layer`` ist durch ein Double ersetzt
(liefert die Daten unverändert, ohne Harmonisierung).
"""

from __future__ import annotations

import textwrap

import networkx as nx
import pandas as pd
import pytest
from pyproj import CRS

from _executor_doubles import BBOX_REGION, fake_loader, file_layer, points
from geofact.core.errors import StepExecutionError
from geofact.core.scenario import Scenario
from geofact.engine import events as ev
from geofact.engine import executor as executor_module
from geofact.engine.discovery import discover
from geofact.engine.executor import ForeignCrsWarning, check_input_crs, run_scenario

_RECORDING_PLUGIN = textwrap.dedent('''
    """Implements: FA57 (Test-Double: Operation, die ihren Aufruf protokolliert)."""
    from pathlib import Path
    from typing import ClassVar, Literal

    from geofact.plugin_api import DataType, Step, register_operation

    MARKER = Path(__file__).with_name("called.txt")


    class RecordStep(Step):
        op: Literal["record_pair"] = "record_pair"
        INPUT_PORTS: ClassVar[dict[str, DataType]] = {"left": DataType.VECTOR, "right": DataType.VECTOR}
        OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR


    @register_operation(RecordStep)
    def run(inputs, params):
        MARKER.write_text("aufgerufen", encoding="utf-8")
        return inputs["left"]
''')


def _scenario_and_registry(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "record_pair.py").write_text(_RECORDING_PLUGIN, encoding="utf-8")
    registry = discover(plugin_paths=[plugin_dir])
    config = {
        "scenario": {"name": "CRS-Mix", "region": BBOX_REGION},
        "layers": [file_layer("zones"), file_layer("points")],
        "steps": [
            {
                "id": "count",
                "op": "record_pair",
                "inputs": {"left": "zones", "right": "points"},
            }
        ],
        "output": [{"type": "geojson", "source": "count"}],
    }
    scenario = Scenario.model_validate(config, context={"registry": registry})
    return scenario, registry, plugin_dir / "called.txt"


def test_fa57_step_with_mixed_input_crs_fails_before_the_operation_runs(
    tmp_path, monkeypatch
):
    scenario, registry, marker = _scenario_and_registry(tmp_path)
    monkeypatch.setattr(
        executor_module,
        "load_layer",
        fake_loader({"zones": points("EPSG:32633"), "points": points("EPSG:4326")}),
    )
    events = []
    with pytest.raises(StepExecutionError) as excinfo:
        run_scenario(scenario, registry=registry, on_event=events.append)
    error = excinfo.value
    assert error.step_id == "count"
    assert isinstance(error.__cause__, TypeError)
    message = str(error)
    assert "verschiedenen CRS" in message
    assert "left=EPSG:32633, right=EPSG:4326" in message  # Ports alphabetisch
    assert "FA5-Nachbedingung verletzt" in message
    assert not marker.exists(), "die Operation darf bei gemischten CRS nicht laufen"
    assert any(e.type == ev.STEP_ERROR and e.step == "count" for e in events)


def test_fa57_step_input_in_foreign_crs_warns_but_runs(tmp_path, monkeypatch):
    scenario, registry, marker = _scenario_and_registry(tmp_path)
    monkeypatch.setattr(
        executor_module,
        "load_layer",
        fake_loader({"zones": points("EPSG:25833"), "points": points("EPSG:25833")}),
    )
    events = []
    result = run_scenario(scenario, registry=registry, on_event=events.append)
    assert marker.exists()
    assert "count" in result.store
    foreign = [w for w in result.warnings if w.category == "ForeignCrsWarning"]
    assert len(foreign) == 1 and foreign[0].step == "count"
    assert "EPSG:25833" in foreign[0].message and "EPSG:32633" in foreign[0].message
    assert any(
        e.type == ev.STEP_WARNING and e.detail.get("category") == "ForeignCrsWarning"
        for e in events
    )


def test_fa57_inputs_without_crs_are_not_compared(tmp_path, monkeypatch):
    scenario, registry, marker = _scenario_and_registry(tmp_path)
    monkeypatch.setattr(
        executor_module,
        "load_layer",
        fake_loader({"zones": points(None), "points": points("EPSG:32633")}),
    )
    result = run_scenario(scenario, registry=registry)
    assert marker.exists()
    assert not [w for w in result.warnings if w.category == "ForeignCrsWarning"]

    # Tabellen (auch mit einer Spalte 'crs') und Graphen ohne CRS zählen nicht.
    table = pd.DataFrame({"crs": ["EPSG:4326"], "wert": [1]})
    graph = nx.Graph()
    working = CRS.from_epsg(32633)
    check_input_crs({"a": table, "b": graph, "c": points("EPSG:32633")}, working)


def test_fa57_graph_crs_takes_part_in_the_comparison():
    graph = nx.Graph(crs="EPSG:4326")
    with pytest.raises(TypeError, match="graph=EPSG:4326"):
        check_input_crs(
            {"graph": graph, "origins": points("EPSG:32633")}, CRS.from_epsg(32633)
        )


def test_fa57_empty_layer_passes_the_check():
    empty = points("EPSG:32633").iloc[0:0]
    with pytest.warns(ForeignCrsWarning, match="EPSG:25833"):
        check_input_crs({"a": points("EPSG:25833").iloc[0:0]}, CRS.from_epsg(32633))
    check_input_crs({"a": empty, "b": points("EPSG:32633")}, CRS.from_epsg(32633))
