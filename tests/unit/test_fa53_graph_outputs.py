"""Implements: FA53 (Graphen als Ausgabe: Kanten und Knoten als GeoJSON, CSV und Karte).

Contract-Tests für src/geofact/builtin/_graph_frames.py und die Graph-Fähigkeit der
Ausgabeformate geojson/csv/map (export.py, map.py): Graphen aus build_network,
line_network und centrality, Kanten (Standard) und Knoten (graph_part), Kanten ohne
Geometrie, leerer Graph, graph_part an einer Vektor-Quelle, ungültige Optionen.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from geofact import api
from geofact.builtin import _graph_frames as frames
from geofact.core.types import Graph

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


@pytest.fixture(autouse=True)
def _snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))


def handmade_graph() -> Graph:
    nx_graph = nx.Graph()
    for node, (x, y) in {"A": (0, 0), "B": (100, 0), "C": (100, 100)}.items():
        nx_graph.add_node(node, geometry=Point(x, y), rolle="knoten")
    nx_graph.add_edge(
        "A",
        "B",
        length=100.0,
        highway="primary",
        geometry=LineString([(0, 0), (50, 20), (100, 0)]),
    )
    nx_graph.add_edge(
        "B",
        "C",
        length=100.0,
        highway="residential",
        geometry=LineString([(100, 0), (100, 100)]),
    )
    return Graph(nx_graph, crs="EPSG:32633")


# =====================================================================
# Kanten- und Knoten-Layer
# =====================================================================


def test_fa53_edges_frame_has_one_line_per_edge_with_its_attributes():
    edges = frames.edges_frame(handmade_graph())
    assert len(edges) == 2
    assert edges.crs == "EPSG:32633"
    assert set(edges.columns) == {"node_u", "node_v", "length", "highway", "geometry"}
    first = edges[edges["highway"] == "primary"].iloc[0]
    assert (first["node_u"], first["node_v"]) == ("A", "B")
    assert first.geometry.equals(LineString([(0, 0), (50, 20), (100, 0)]))
    assert set(edges.geom_type) == {"LineString"}


def test_fa53_nodes_frame_has_one_point_per_node_with_id_and_attributes():
    nodes = frames.nodes_frame(handmade_graph())
    assert list(nodes["id"]) == ["A", "B", "C"]
    assert set(nodes.columns) == {"id", "rolle", "geometry"}
    assert set(nodes.geom_type) == {"Point"}
    assert nodes.crs == "EPSG:32633"


def test_fa53_edge_without_geometry_is_drawn_straight_with_a_counted_warning():
    graph = handmade_graph()
    del graph.nx_graph.edges["A", "B"]["geometry"]
    with pytest.warns(frames.EdgeWithoutGeometryWarning, match="1 von 2 Kanten"):
        edges = frames.edges_frame(graph)
    straight = edges[edges["highway"] == "primary"].geometry.iloc[0]
    assert list(straight.coords) == [(0, 0), (100, 0)]


def test_fa53_empty_graph_gives_empty_frames_with_the_graph_crs():
    empty = Graph(nx.Graph(), crs="EPSG:32633")
    assert (
        frames.edges_frame(empty).empty
        and frames.edges_frame(empty).crs == "EPSG:32633"
    )
    assert (
        frames.nodes_frame(empty).empty
        and frames.nodes_frame(empty).crs == "EPSG:32633"
    )


def test_fa53_frame_for_output_leaves_vector_layers_untouched():
    layer = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:32633")

    class Spec:
        options = None
        model_extra: dict = {}
        source = "x"

    assert frames.frame_for_output(layer, Spec()) is layer


# =====================================================================
# Im Lauf: Graphen aus build_network, line_network und centrality
# =====================================================================


def config(outputs: list[dict]) -> dict:
    return {
        "scenario": {"name": "FA53", "region": BBOX_REGION},
        "layers": [
            {
                "id": "leitungen",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "power_lines.geojson"),
            },
            {
                "id": "umspannwerke",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "substations.geojson"),
            },
        ],
        "steps": [
            {
                "id": "netz",
                "op": "build_network",
                "inputs": {"lines": "leitungen", "nodes": "umspannwerke"},
                "params": {"tolerance_m": 500},
            },
            {"id": "zentral", "op": "centrality", "inputs": {"graph": "netz"}},
            {
                "id": "linien_netz",
                "op": "line_network",
                "inputs": {"lines": "leitungen"},
                "params": {"attributes": ["id"], "speed_kmh": 50},
            },
        ],
        "output": outputs,
    }


def run(outputs: list[dict], tmp_path: Path) -> api.RunReport:
    document = api.load_scenario(config(outputs))
    return api.run(document, out_dir=tmp_path / "out")


def test_fa53_build_network_graph_as_geojson_edges(tmp_path):
    report = run(
        [{"type": "geojson", "source": "netz", "path": "kanten.geojson"}], tmp_path
    )
    assert report.outputs.skipped == []
    edges = gpd.read_file(tmp_path / "out" / "kanten.geojson")
    nx_graph = report.result.store["netz"].nx_graph
    assert len(edges) == nx_graph.number_of_edges() > 0
    assert set(edges.geom_type) == {"LineString"}
    assert {"node_u", "node_v", "length"} <= set(edges.columns)
    assert edges.crs == "EPSG:4326"


def test_fa53_centrality_graph_as_geojson_nodes_carries_the_metrics(tmp_path):
    report = run(
        [
            {
                "type": "geojson",
                "source": "zentral",
                "graph_part": "nodes",
                "path": "knoten.geojson",
            }
        ],
        tmp_path,
    )
    assert report.outputs.skipped == []
    nodes = gpd.read_file(tmp_path / "out" / "knoten.geojson")
    assert len(nodes) == report.result.store["zentral"].nx_graph.number_of_nodes()
    assert {"betweenness", "is_articulation_point", "component"} <= set(nodes.columns)
    assert set(nodes.geom_type) == {"Point"}


def test_fa53_centrality_graph_as_geojson_edges_carries_the_bridge_flag(tmp_path):
    report = run(
        [{"type": "geojson", "source": "zentral", "path": "kanten.geojson"}], tmp_path
    )
    edges = gpd.read_file(tmp_path / "out" / "kanten.geojson")
    assert "is_bridge" in edges.columns
    assert len(edges) == report.result.store["zentral"].nx_graph.number_of_edges()


def test_fa53_line_network_graph_as_csv_edges_has_wkt_and_attributes(tmp_path):
    report = run(
        [{"type": "csv", "source": "linien_netz", "path": "kanten.csv"}], tmp_path
    )
    assert report.outputs.skipped == []
    table = pd.read_csv(tmp_path / "out" / "kanten.csv")
    assert (
        len(table) == report.result.store["linien_netz"].nx_graph.number_of_edges() > 0
    )
    assert {"node_u", "node_v", "length", "travel_time_min", "id", "wkt"} <= set(
        table.columns
    )
    assert table["wkt"].str.startswith("LINESTRING").all()
    assert (table["travel_time_min"] > 0).all()


def test_fa53_graph_as_csv_nodes(tmp_path):
    report = run(
        [
            {
                "type": "csv",
                "source": "zentral",
                "graph_part": "nodes",
                "path": "knoten.csv",
            }
        ],
        tmp_path,
    )
    table = pd.read_csv(tmp_path / "out" / "knoten.csv")
    assert len(table) == report.result.store["zentral"].nx_graph.number_of_nodes()
    assert table["wkt"].str.startswith("POINT").all()
    assert "betweenness" in table.columns


def test_fa53_graph_as_map_edges_and_nodes(tmp_path):
    report = run(
        [
            {
                "type": "map",
                "source": "linien_netz",
                "path": "kanten.html",
                "color_field": "length",
            },
            {
                "type": "map",
                "source": "zentral",
                "graph_part": "nodes",
                "color_field": "betweenness",
                "path": "knoten.html",
            },
        ],
        tmp_path,
    )
    assert report.outputs.skipped == []
    edges_html = (tmp_path / "out" / "kanten.html").read_text(encoding="utf-8")
    nodes_html = (tmp_path / "out" / "knoten.html").read_text(encoding="utf-8")
    assert "leaflet" in edges_html.lower() and "length" in edges_html
    assert "betweenness" in nodes_html


def test_fa53_default_part_is_edges_for_every_graph_format(tmp_path):
    run(
        [
            {"type": "geojson", "source": "netz", "path": "a.geojson"},
            {
                "type": "geojson",
                "source": "netz",
                "graph_part": "edges",
                "path": "b.geojson",
            },
        ],
        tmp_path,
    )
    a = json.loads((tmp_path / "out" / "a.geojson").read_text(encoding="utf-8"))
    b = json.loads((tmp_path / "out" / "b.geojson").read_text(encoding="utf-8"))
    assert a["features"] == b["features"]
    assert {f["geometry"]["type"] for f in a["features"]} == {"LineString"}


def test_fa53_graph_part_on_a_vector_source_is_skipped_with_a_reason_not_ignored(
    tmp_path,
):
    report = run(
        [
            {
                "type": "geojson",
                "source": "leitungen",
                "graph_part": "nodes",
                "path": "falsch.geojson",
            },
            {"type": "geojson", "source": "leitungen", "path": "richtig.geojson"},
        ],
        tmp_path,
    )
    assert [p.name for p in report.outputs.written] == ["richtig.geojson"]
    (skipped,) = report.outputs.skipped
    assert skipped.source == "leitungen"
    assert "graph_part: nodes gilt nur für eine Graph-Quelle" in skipped.reason


def test_fa53_unknown_graph_part_value_is_a_located_configuration_error():
    with pytest.raises(api.ConfigError) as caught:
        api.load_scenario(
            config([{"type": "geojson", "source": "netz", "graph_part": "beide"}])
        )
    assert [location for location, _ in caught.value.issues] == [
        "output -> 0 -> graph_part"
    ]


def test_fa53_formats_declare_that_they_accept_graphs():
    accepts = {
        entry["name"]: entry["accepts"] for entry in api.extension_catalog()["outputs"]
    }
    for name in ("geojson", "csv"):
        assert accepts[name] == ["graph", "vector"], name
    assert accepts["map"] == ["graph", "raster", "vector"]  # raster: FA49
    assert accepts["rdf"] == ["vector"]


def test_fa53_empty_graph_is_written_without_an_error(tmp_path):
    """Randfall: ein Netz ohne Kanten ergibt eine leere, aber gültige Ausgabe."""
    from geofact.builtin.outputs import export, map as map_module

    empty = Graph(nx.Graph(), crs="EPSG:32633")

    class Spec:
        options = export.GraphExportOptions()
        model_extra: dict = {}
        source = "leer"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        export._write_csv_output(empty, tmp_path / "leer.csv", Spec())
        map_module._write_output(
            empty,
            tmp_path / "leer.html",
            type(
                "S",
                (),
                {
                    "options": map_module.MapOptions(),
                    "model_extra": {},
                    "source": "leer",
                },
            )(),
        )
    assert (tmp_path / "leer.csv").is_file()
    assert (tmp_path / "leer.html").is_file()


def test_fa53_web_serialization_of_a_line_network_graph_leaves_out_the_edge_geometry():
    """Die Web-Ansicht eines Graphen (Node-Link-JSON) kennt die Kantengeometrie nicht und
    bekommt sie nicht: Kantenattribute ja, 'geometry' nein (JSON-sicher)."""
    from geofact_web.presentation import serialize

    nx_graph = nx.Graph()
    nx_graph.add_node(0, geometry=Point(500000, 5630000))
    nx_graph.add_node(1, geometry=Point(500100, 5630000))
    nx_graph.add_edge(
        0,
        1,
        length=100.0,
        travel_time_min=0.2,
        highway="residential",
        name=None,
        geometry=LineString([(500000, 5630000), (500100, 5630000)]),
    )
    payload = serialize.serialize_result(Graph(nx_graph, crs="EPSG:32633"))
    (edge,) = payload["graph"]["edges"]
    assert set(edge) == {
        "source",
        "target",
        "length",
        "travel_time_min",
        "highway",
        "name",
    }
    assert (
        payload["describe"]["edge_count"] == 1
        and payload["describe"]["node_count"] == 2
    )
    json.dumps(payload)  # serialisierbar
