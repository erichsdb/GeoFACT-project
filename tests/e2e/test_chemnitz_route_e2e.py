"""Implements: FA50, FA51, FA52, FA53 (Beispiel chemnitz_route_hbf_tu, offline Ende zu Ende).

Das Beispielszenario `examples/chemnitz_route_hbf_tu.yaml` ohne Netz: die Layer-
Deklarationen (OSM-Straßen, zwei geocode-Orte) werden durch das Straßenraster
`tests/fixtures/chemnitz_street_grid.geojson` (source: file) und zwei `source: points`
ersetzt; Netz, Route und Ausgaben laufen unverändert über `api.run`.

Das Raster (tests/fixtures/make_street_grid_fixture.py) hat einen Kreisverkehr als
Polygon in der Hauptstrasse: die kürzeste Route führt geradeaus durch ihn (ca. 2802 m);
ohne die Umrandung (polygons: skip) müsste sie eine Spalte seitwärts ausweichen
(ca. 3343 m).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
import yaml
from shapely.geometry import LineString

from geofact import api

pytestmark = pytest.mark.e2e

EXAMPLE = (
    Path(__file__).resolve().parents[2] / "examples" / "chemnitz_route_hbf_tu.yaml"
)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

HBF = {"name": "Chemnitz Hauptbahnhof", "lon": 12.9306, "lat": 50.8396}
TU = {"name": "Reichenhainer Straße 70, Chemnitz", "lon": 12.9295, "lat": 50.8158}
RING_NORTH = (12.9300, 50.8353)
RING_SOUTH = (12.9300, 50.8347)


def region_fetcher(name: str) -> list[dict]:
    assert name == "Chemnitz, Deutschland"
    return json.loads(
        (FIXTURES / "nominatim_chemnitz.json").read_text(encoding="utf-8")
    )


def offline_config(**route_params) -> dict:
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    raw = copy.deepcopy(raw)
    raw["layers"] = [
        {
            "id": "strassen",
            "source": "file",
            "data_type": "vector",
            "path": str(FIXTURES / "chemnitz_street_grid.geojson"),
        },
        {"id": "start", "source": "points", "features": [HBF]},
        {"id": "ziel", "source": "points", "features": [TU]},
    ]
    route = next(step for step in raw["steps"] if step["id"] == "route")
    route["params"].update(route_params)
    return raw


def run(config: dict, tmp_path: Path) -> api.RunReport:
    document = api.load_scenario(config)
    return api.run(document, out_dir=tmp_path / "out", region_fetcher=region_fetcher)


@pytest.fixture(autouse=True)
def _snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))


# =====================================================================
# Die Route
# =====================================================================


def test_fa51_example_route_goes_straight_through_the_roundabout(tmp_path):
    report = run(offline_config(), tmp_path)
    routes = report.result.store["route"]
    assert len(routes) == 1
    row = routes.iloc[0]
    assert (row["origin"], row["destination"]) == (HBF["name"], TU["name"])
    assert row["length_m"] == pytest.approx(2802, rel=0.01)
    assert row["straight_line_m"] == pytest.approx(2646.7, rel=0.002)
    assert row["detour_factor"] == pytest.approx(
        row["length_m"] / row["straight_line_m"]
    )
    assert 1.0 < row["detour_factor"] < 1.15
    assert row["travel_time_min"] == pytest.approx(
        row["length_m"] * 60 / 30_000
    )  # 30 km/h
    assert row["origin_snap_m"] == pytest.approx(61, abs=3)
    assert row["destination_snap_m"] == pytest.approx(96, abs=3)
    assert routes.crs == "EPSG:32633"


def test_fa51_route_line_follows_the_edge_geometry_through_the_ring_vertices(tmp_path):
    report = run(offline_config(), tmp_path)
    line = report.result.store["route"].to_crs("EPSG:4326").geometry.iloc[0]
    assert isinstance(line, LineString)
    coords = [(round(x, 6), round(y, 6)) for x, y in line.coords]
    # Start- und Zielknoten (Kreuzungen der Hauptstrasse), die Ringecken N, E oder W, S und
    # die Kreuzungen dazwischen - die echte Folge der Kantenvertices, nicht Start und Ende allein
    assert coords[:2] == [(12.93, 50.84), RING_NORTH]
    assert coords[2] in [(12.9296, 50.835), (12.9304, 50.835)]
    assert coords[3:] == [
        RING_SOUTH,
        (12.93, 50.83),
        (12.93, 50.825),
        (12.93, 50.82),
        (12.93, 50.815),
    ]


def test_fa50_without_the_ring_boundary_the_route_has_to_detour(tmp_path):
    config = offline_config()
    next(step for step in config["steps"] if step["id"] == "netz")["params"][
        "polygons"
    ] = "skip"
    report = run(config, tmp_path)
    assert report.result.store["route"]["length_m"].iloc[0] == pytest.approx(
        3343, rel=0.01
    )
    skipped = [w for w in report.warnings if "Fläche(n)" in w.message]
    assert len(skipped) == 1 and skipped[0].step == "netz"
    assert "polygons: boundary" in skipped[0].message


def test_fa50_example_network_has_the_expected_nodes_and_one_component(tmp_path):
    import networkx as nx

    report = run(offline_config(), tmp_path)
    graph = report.result.store["netz"]
    assert nx.number_connected_components(graph.nx_graph) == 1
    # 5 Spalten x 8 Zeilen, minus der Kreuzung im Kreisverkehr (ersetzt durch 4 Ringknoten)
    assert graph.nx_graph.number_of_nodes() == 5 * 8 - 1 + 4
    assert {d["highway"] for _, _, d in graph.nx_graph.edges(data=True)} == {
        "primary",
        "residential",
    }
    assert report.warnings == []


# =====================================================================
# Ausgaben (FA53) und Lauf
# =====================================================================


def test_fa53_example_outputs_are_written(tmp_path):
    report = run(offline_config(), tmp_path)
    assert report.outputs.skipped == []
    assert {p.name for p in report.outputs.written} == {
        "chemnitz_route_hbf_tu.html",
        "chemnitz_route_hbf_tu.geojson",
    }
    geojson = gpd.read_file(tmp_path / "out" / "chemnitz_route_hbf_tu.geojson")
    assert len(geojson) == 1 and geojson.geom_type.iloc[0] == "LineString"
    assert geojson.crs == "EPSG:4326"
    assert {
        "origin",
        "destination",
        "length_m",
        "travel_time_min",
        "straight_line_m",
        "detour_factor",
    } <= set(geojson.columns)
    html = (tmp_path / "out" / "chemnitz_route_hbf_tu.html").read_text(encoding="utf-8")
    assert "leaflet" in html.lower() and "Chemnitz Hauptbahnhof" in html


def test_fa53_the_street_network_itself_can_be_written_as_edges_nodes_and_map(tmp_path):
    config = offline_config()
    config["output"] += [
        {"type": "geojson", "source": "netz", "path": "netz_kanten.geojson"},
        {
            "type": "csv",
            "source": "netz",
            "graph_part": "nodes",
            "path": "netz_knoten.csv",
        },
        {"type": "map", "source": "netz", "path": "netz.html", "color_field": "length"},
    ]
    report = run(config, tmp_path)
    assert report.outputs.skipped == []
    graph = report.result.store["netz"].nx_graph
    edges = gpd.read_file(tmp_path / "out" / "netz_kanten.geojson")
    assert len(edges) == graph.number_of_edges()
    assert set(edges["highway"]) == {"primary", "residential"}
    nodes = pd.read_csv(tmp_path / "out" / "netz_knoten.csv")
    assert len(nodes) == graph.number_of_nodes()
    assert (tmp_path / "out" / "netz.html").stat().st_size > 0


def test_fa51_weight_travel_time_gives_the_same_route_at_constant_speed(tmp_path):
    by_length = run(offline_config(), tmp_path / "a").result.store["route"].iloc[0]
    by_time = (
        run(offline_config(weight="travel_time_min"), tmp_path / "b")
        .result.store["route"]
        .iloc[0]
    )
    assert by_time["length_m"] == pytest.approx(by_length["length_m"])


def test_fa09_only_the_needed_layers_are_loaded_in_the_example(tmp_path):
    report = run(offline_config(), tmp_path)
    assert sorted(report.result.loaded_layers) == ["start", "strassen", "ziel"]
    assert report.result.order == ["netz", "route"]


def test_fa51_two_runs_give_identical_geojson(tmp_path):
    run(offline_config(), tmp_path / "a")
    run(offline_config(), tmp_path / "b")
    first = (tmp_path / "a" / "out" / "chemnitz_route_hbf_tu.geojson").read_bytes()
    second = (tmp_path / "b" / "out" / "chemnitz_route_hbf_tu.geojson").read_bytes()
    assert first == second


def test_fa52_a_place_outside_the_region_stops_the_run_with_its_name(tmp_path):
    config = offline_config()
    config["layers"][2] = {
        "id": "ziel",
        "source": "points",
        "features": [{"name": "Dresden Hauptbahnhof", "lon": 13.7320, "lat": 51.0403}],
    }
    with pytest.raises(api.LayerLoadError, match="Dresden Hauptbahnhof"):
        run(config, tmp_path)
