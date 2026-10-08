"""Implements: FA94 (Fach-Plugin power_grid_osm: Hochspannungsnetz aus OSM-Rohdaten).

Contract-Tests für examples/plugins/power_grid_osm.py. Das Plugin gehört nicht zum
Kern: die Tests bauen sich per ``discover(plugin_paths=[...])`` eine eigene Registry
und laden die Funktion aus der Datei.
"""

from __future__ import annotations

import importlib.util
import os
import warnings
from pathlib import Path

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import LineString, Point, box

from geofact.core.scenario import Scenario
from geofact.engine.discovery import discover

REPO = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO / "examples" / "plugins"
PLUGIN_FILE = PLUGIN_DIR / "power_grid_osm.py"
EXAMPLE = REPO / "examples" / "sachsen_netz_resilienz.yaml"
FIXTURES_DIR = REPO / "tests" / "fixtures"
UTM = "EPSG:25833"


@pytest.fixture(scope="module")
def plugin():
    spec = importlib.util.spec_from_file_location("fa94_power_grid_osm", PLUGIN_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def registry():
    return discover(plugin_paths=[PLUGIN_DIR])


def _substations(rows: list[tuple[object, str]]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"voltage": [voltage for _, voltage in rows]},
        geometry=[geometry for geometry, _ in rows],
        crs=UTM,
    )


def _lines(rows: list[tuple[list[tuple[float, float]], str]]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"voltage": [voltage for _, voltage in rows]},
        geometry=[LineString(coords) for coords, _ in rows],
        crs=UTM,
    )


def _config(**step_params) -> dict:
    return {
        "scenario": {"name": "Fach-Plugin", "region": "13.60,51.01,13.94,51.15"},
        "layers": [
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
            {
                "id": "power_lines",
                "source": "file",
                "path": str(FIXTURES_DIR / "power_lines.geojson"),
                "data_type": "vector",
            },
            {
                "id": "population",
                "source": "file",
                "path": str(FIXTURES_DIR / "population.tif"),
                "data_type": "raster",
            },
        ],
        "steps": [
            {
                "id": "grid",
                "op": "power_grid_osm",
                "inputs": {"substations": "substations", "power_lines": "power_lines"},
                "params": {"min_voltage": 110000, **step_params},
            },
            {"id": "nodes", "op": "graph_to_vector", "inputs": {"graph": "grid"}},
        ],
        "output": [{"type": "geojson", "source": "nodes", "path": "nodes.geojson"}],
    }


# --- Registrierung -----------------------------------------------------------


def test_fa94_plugin_is_found_on_the_plugin_path_and_not_in_the_core(registry):
    scenario = Scenario.model_validate(_config(), context={"registry": registry})
    assert scenario.execution_order() == ["grid", "nodes"]
    with pytest.raises(ValidationError, match="power_grid_osm"):
        Scenario.model_validate(_config(), context={"registry": discover()})


def test_fa94_example_of_the_thesis_is_valid_with_the_plugin(registry):
    import yaml

    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    scenario = Scenario.model_validate(raw, context={"registry": registry})
    assert scenario.execution_order()[0] == "grid"
    assert raw["steps"][0]["op"] == "power_grid_osm"


# --- Happy Path: die drei fachlichen Regeln ----------------------------------


def test_fa94_line_pieces_meeting_at_a_mast_connect_two_substations(plugin):
    # Station A is an area, B a point; the line consists of two pieces that meet
    # at a mast (their ends lie 10 m apart, inside the tolerance of 50 m).
    substations = _substations(
        [(box(0, 0, 100, 100), "110000"), (Point(5000, 0), "110000")]
    )
    lines = _lines(
        [
            ([(90, 50), (2500, 0)], "110000"),  # starts inside the area of A
            ([(2510, 0), (5000, 0)], "110000"),
        ]
    )
    graph = plugin.run_power_grid_osm(
        {"substations": substations, "power_lines": lines},
        {"min_voltage": 110000, "tolerance_m": 50},
    ).nx_graph
    kinds = sorted(data["kind"] for _, data in graph.nodes(data=True))
    assert kinds == ["junction", "substation", "substation"]
    assert graph.number_of_edges() == 2
    junction = next(
        n for n, data in graph.nodes(data=True) if data["kind"] == "junction"
    )
    # The substation is a through node: both stations are reached via the mast.
    assert sorted(graph.neighbors(junction)) == ["uw0", "uw1"]
    assert graph.edges["uw1", junction]["length"] == pytest.approx(2490.0)
    assert graph.nodes["uw0"]["geometry"].equals(Point(50, 50))  # centroid of the area


def test_fa94_highest_voltage_of_a_multiple_value_counts(plugin):
    assert plugin.max_voltage("110000;20000") == 110000
    assert plugin.max_voltage("20000, 380000") == 380000
    assert plugin.max_voltage("unbekannt") != plugin.max_voltage("unbekannt")  # NaN
    substations = _substations(
        [
            (Point(0, 0), "110000;20000"),
            (Point(1000, 0), "20000"),
            (Point(2000, 0), "x"),
        ]
    )
    lines = _lines([([(0, 0), (900, 0)], "110000")])
    graph = plugin.run_power_grid_osm(
        {"substations": substations, "power_lines": lines}, {"min_voltage": 110000}
    ).nx_graph
    stations = [n for n, data in graph.nodes(data=True) if data["kind"] == "substation"]
    assert stations == ["uw0"]


def test_fa94_point_inside_a_substation_area_is_a_duplicate(plugin):
    substations = _substations(
        [(box(0, 0, 100, 100), "110000"), (Point(50, 50), "110000")]
    )
    lines = _lines([([(100, 50), (3000, 50)], "110000")])
    graph = plugin.run_power_grid_osm(
        {"substations": substations, "power_lines": lines}, {"min_voltage": 110000}
    ).nx_graph
    assert [n for n, d in graph.nodes(data=True) if d["kind"] == "substation"] == [
        "uw0"
    ]


def test_fa94_dropped_line_pieces_are_reported_once_with_counts(plugin):
    substations = _substations([(Point(0, 0), "110000"), (Point(5000, 0), "110000")])
    lines = _lines(
        [
            ([(0, 0), (5000, 0)], "110000"),
            ([(0, 10), (2500, 400), (5000, 10)], "110000"),  # parallel, longer
            ([(2000, 2000), (2020, 2000)], "110000"),  # both ends on one node
            ([(0, 0), (5000, 0)], "20000"),  # below min_voltage
            ([(0, 0), (5000, 0)], "?"),  # no readable voltage
        ]
    )
    with pytest.warns(plugin.DroppedLinePieceWarning) as caught:
        graph = plugin.run_power_grid_osm(
            {"substations": substations, "power_lines": lines},
            {"min_voltage": 110000, "tolerance_m": 50},
        ).nx_graph
    assert len(caught) == 1
    message = str(caught[0].message)
    assert "2 von 3 Wegstueck(en)" in message
    assert "1 Schleife(n)" in message and "1 parallel" in message
    assert (
        "1 Leitung(en) unter 110000 V" in message
        and "1 ohne lesbare Spannung" in message
    )
    assert graph.number_of_edges() == 1
    assert graph.edges["uw0", "uw1"]["length"] == pytest.approx(
        5000.0
    )  # the shortest stays


def test_fa94_no_warning_when_every_piece_becomes_an_edge(plugin):
    substations = _substations([(Point(0, 0), "110000"), (Point(5000, 0), "110000")])
    lines = _lines([([(0, 0), (5000, 0)], "110000")])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        graph = plugin.run_power_grid_osm(
            {"substations": substations, "power_lines": lines}, {"min_voltage": 110000}
        ).nx_graph
    assert graph.number_of_edges() == 1


# --- Typfehler-Fall ----------------------------------------------------------


def test_fa94_missing_voltage_attribute_is_an_explicit_error(plugin):
    substations = _substations([(Point(0, 0), "110000")]).drop(columns="voltage")
    lines = _lines([([(0, 0), (5000, 0)], "110000")])
    with pytest.raises(ValueError, match="voltage"):
        plugin.run_power_grid_osm(
            {"substations": substations, "power_lines": lines}, {"min_voltage": 110000}
        )


def test_fa94_raster_on_a_vector_port_is_rejected_before_the_run(registry):
    config = _config()
    config["steps"][0]["inputs"]["power_lines"] = "population"
    with pytest.raises(ValidationError, match="power_lines"):
        Scenario.model_validate(config, context={"registry": registry})


def test_fa94_parameters_are_checked(registry):
    for params in ({"min_voltage": -1}, {"tolerance_m": -5}, {"unbekannt": 1}):
        with pytest.raises(ValidationError):
            Scenario.model_validate(_config(**params), context={"registry": registry})
    config = _config()
    del config["steps"][0]["params"]["min_voltage"]
    with pytest.raises(ValidationError, match="min_voltage"):
        Scenario.model_validate(config, context={"registry": registry})


# --- Randfall: leerer Layer --------------------------------------------------


def test_fa94_empty_layers_give_an_empty_graph(plugin):
    substations = _substations([])
    lines = _lines([])
    result = plugin.run_power_grid_osm(
        {"substations": substations, "power_lines": lines}, {"min_voltage": 110000}
    )
    assert result.nx_graph.number_of_nodes() == 0
    assert result.nx_graph.number_of_edges() == 0
    assert result.crs == substations.crs


def test_fa94_substations_without_lines_stay_isolated_nodes(plugin):
    substations = _substations([(Point(0, 0), "110000"), (Point(5000, 0), "380000")])
    graph = plugin.run_power_grid_osm(
        {"substations": substations, "power_lines": _lines([])}, {"min_voltage": 110000}
    ).nx_graph
    assert graph.number_of_nodes() == 2
    assert graph.number_of_edges() == 0


# --- Web-Demo: mitgelieferte Plugins ------------------------------------------


def test_fa94_web_demo_loads_the_shipped_plugins_unless_a_path_is_set(monkeypatch):
    pytest.importorskip("fastapi")
    from geofact.api import PLUGIN_PATH_ENV
    from geofact_web import main

    monkeypatch.delenv(PLUGIN_PATH_ENV, raising=False)
    main.ship_example_plugins()
    assert Path(os.environ[PLUGIN_PATH_ENV]) == PLUGIN_DIR

    monkeypatch.setenv(PLUGIN_PATH_ENV, "eigene/plugins")
    main.ship_example_plugins()
    assert os.environ[PLUGIN_PATH_ENV] == "eigene/plugins"

    monkeypatch.setenv(PLUGIN_PATH_ENV, "  ")
    main.ship_example_plugins()
    assert Path(os.environ[PLUGIN_PATH_ENV]) == PLUGIN_DIR
