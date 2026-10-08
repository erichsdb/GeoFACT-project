"""Implements: FA10/FA53 (graph_to_vector.part: nodes | edges).

Contract tests for the `part` parameter of graph_to_vector in
src/geofact/builtin/operations/graph.py: default `nodes` is unchanged, `edges`
returns one line per edge (same shape as the graph outputs of FA53), an empty
graph gives an empty layer, an unknown value is rejected with position.
"""

from __future__ import annotations

import networkx as nx
import pytest
from pydantic import ValidationError
from shapely.geometry import LineString, Point

from geofact.builtin.operations import graph as graph_module
from geofact.builtin.operations.graph import GraphToVectorStep
from geofact.core.types import Graph

CRS = "EPSG:32633"


def _graph() -> Graph:
    nx_graph = nx.Graph()
    for node, (x, y) in {"A": (0, 0), "B": (100, 0), "C": (100, 100)}.items():
        nx_graph.add_node(node, geometry=Point(x, y), betweenness=0.5)
    nx_graph.add_edge(
        "A",
        "B",
        length=100.0,
        is_bridge=True,
        geometry=LineString([(0, 0), (50, 20), (100, 0)]),
    )
    nx_graph.add_edge(
        "B",
        "C",
        length=100.0,
        is_bridge=False,
        geometry=LineString([(100, 0), (100, 100)]),
    )
    return Graph(nx_graph, crs=CRS)


def _run(graph: Graph, **params):
    validated = GraphToVectorStep.Params(**params).model_dump()
    return graph_module.run_graph_to_vector({"graph": graph}, validated)


def test_fa53_graph_to_vector_default_part_is_nodes_unchanged():
    step = GraphToVectorStep(id="g", inputs={"graph": "net"})
    assert step.params == {"part": "nodes"}
    nodes = _run(_graph())
    assert len(nodes) == 3
    assert set(nodes.columns) == {"id", "betweenness", "geometry"}
    assert set(nodes.geom_type) == {"Point"}
    assert nodes.crs == CRS
    assert "betweenness" in step.produced_fields()


def test_fa53_graph_to_vector_edges_part_returns_one_line_per_edge():
    edges = _run(_graph(), part="edges")
    assert len(edges) == 2
    assert set(edges.columns) == {"node_u", "node_v", "length", "is_bridge", "geometry"}
    first = edges[edges["node_u"] == "A"].iloc[0]
    assert first.geometry.equals(LineString([(0, 0), (50, 20), (100, 0)]))
    assert edges.crs == CRS
    step = GraphToVectorStep(id="g", inputs={"graph": "net"}, params={"part": "edges"})
    assert {"node_u", "node_v", "length", "is_bridge"} <= step.produced_fields()


def test_fa53_graph_to_vector_edges_of_empty_graph_is_empty_layer():
    edges = _run(Graph(nx.Graph(), crs=CRS), part="edges")
    assert edges.empty
    assert {"node_u", "node_v"} <= set(edges.columns)
    nodes = _run(Graph(nx.Graph(), crs=CRS))
    assert nodes.empty


def test_fa53_graph_to_vector_unknown_part_is_rejected_with_position():
    with pytest.raises(ValidationError) as caught:
        GraphToVectorStep(id="g", inputs={"graph": "net"}, params={"part": "kanten"})
    assert ("params", "part") in [error["loc"] for error in caught.value.errors()]


def test_fa53_edge_attributes_do_not_overwrite_the_node_ids():
    """Nachtreview 20: a line column named node_u/node_v replaced the real node id
    in the edges layer."""
    from shapely.geometry import LineString as Line

    import geopandas as gpd

    nodes = gpd.GeoDataFrame(
        {"id": [1, 2]}, geometry=[Point(0, 0), Point(10, 0)], crs=CRS
    )
    lines = gpd.GeoDataFrame(
        {"node_u": ["foo"]}, geometry=[Line([(0, 0), (10, 0)])], crs=CRS
    )
    graph = graph_module.run_build_network(
        {"lines": lines, "nodes": nodes}, {"node_key": "id"}
    )
    with pytest.warns(UserWarning, match="node_u"):
        edges = _run(graph, part="edges")
    assert edges["node_u"].tolist() == [1] and edges["node_v"].tolist() == [2]
