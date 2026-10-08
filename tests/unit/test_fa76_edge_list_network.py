"""Implements: FA76 (graph from an explicit edge list: edge_list_network).

Contract tests of `edge_list_network` (builtin/operations/edge_list_network.py):
happy path (exact topology, attributes, length in metres), compatibility with
centrality and graph_to_vector, type error (raster on a vector port), empty
layers, dangling edges (error and drop), duplicate/missing node keys, self
loops, parallel edges, length fallback, and the contrast to build_network
(nearest-node snapping merges nodes that share a coordinate).
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pytest
from shapely.geometry import LineString, MultiLineString, Point, Polygon

from geofact import api
from geofact.builtin.operations.edge_list_network import (
    DanglingEdgeWarning,
    EdgeAttributeOverwrittenWarning,
    EdgeGeometryWarning,
    EdgeListNetworkStep,
    EdgeLengthFallbackWarning,
    ParallelEdgesMergedWarning,
    SelfLoopWarning,
    run_edge_list_network,
)
from geofact.builtin.operations.graph import (
    DisconnectedNetworkWarning,
    run_build_network,
    run_centrality,
    run_graph_to_vector,
)
from geofact.builtin.operations.route import run_route
from geofact.core.types import DataType

CRS = "EPSG:3035"
PARAMS = {"node_key": "bus_id", "from_column": "bus0", "to_column": "bus1"}


def _buses() -> gpd.GeoDataFrame:
    # B1 and B2 share one coordinate (two voltage levels of one substation),
    # like PyPSA-Eur buses of the same station.
    return gpd.GeoDataFrame(
        {"bus_id": ["B1", "B2", "B3", "B4"], "voltage": [380, 220, 220, 380]},
        geometry=[Point(0, 0), Point(0, 0), Point(1000, 0), Point(0, 2000)],
        crs=CRS,
    )


def _lines(rows=None) -> gpd.GeoDataFrame:
    rows = rows or [
        ("T1", "B1", "B2", None, None),  # transformer: no length, short geometry
        ("L1", "B2", "B3", 1.2, LineString([(0, 0), (500, 100), (1000, 0)])),
        ("L2", "B1", "B4", 2.0, LineString([(0, 0), (0, 2000)])),
    ]
    return gpd.GeoDataFrame(
        {
            "line_id": [r[0] for r in rows],
            "bus0": [r[1] for r in rows],
            "bus1": [r[2] for r in rows],
            "length_km": [r[3] for r in rows],
        },
        geometry=[r[4] for r in rows],
        crs=CRS,
    )


def _run(nodes=None, edges=None, **params):
    return run_edge_list_network(
        {
            "nodes": _buses() if nodes is None else nodes,
            "edges": _lines() if edges is None else edges,
        },
        {**PARAMS, **params},
    )


# =====================================================================
# Happy path
# =====================================================================


def test_fa76_edge_list_keeps_the_exact_topology():
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # one component, no fallback: no warning at all
        graph = _run()
    g = graph.nx_graph
    assert sorted(g.nodes) == ["B1", "B2", "B3", "B4"]
    assert sorted(tuple(sorted(e)) for e in g.edges) == [
        ("B1", "B2"),
        ("B1", "B4"),
        ("B2", "B3"),
    ]
    assert graph.crs == CRS
    # node attributes come from the node layer, the key is the node id
    assert g.nodes["B3"]["voltage"] == 220
    assert g.nodes["B3"]["geometry"] == Point(1000, 0)
    # edge attributes come from the edge layer, geometry is kept
    assert g.edges["B2", "B3"]["line_id"] == "L1"
    assert g.edges["B2", "B3"]["geometry"].equals(
        LineString([(0, 0), (500, 100), (1000, 0)])
    )
    assert g.edges["B2", "B3"]["parallel_edges"] == 1


def test_fa76_edge_length_is_the_geometry_length_in_metres_without_length_column():
    g = _run().nx_graph
    assert g.edges["B1", "B4"]["length"] == pytest.approx(2000.0)
    assert g.edges["B1", "B2"]["length"] == pytest.approx(
        0.0
    )  # no geometry: straight line


def test_fa76_length_column_in_km_is_stored_in_metres_with_one_fallback_warning():
    with pytest.warns(EdgeLengthFallbackWarning, match="1 von 3 Kante"):
        g = _run(length_column="length_km", length_unit="km").nx_graph
    assert g.edges["B2", "B3"]["length"] == pytest.approx(1200.0)
    assert g.edges["B1", "B4"]["length"] == pytest.approx(2000.0)


def test_fa76_output_works_with_centrality_and_graph_to_vector():
    scored = run_centrality({"graph": _run()}, {})
    nodes = run_graph_to_vector({"graph": scored}, {})
    assert sorted(nodes["id"]) == ["B1", "B2", "B3", "B4"]
    assert {"betweenness", "component", "is_articulation_point", "voltage"} <= set(
        nodes.columns
    )
    by_id = nodes.set_index("id")
    assert by_id.loc["B1", "betweenness"] > 0  # B1 lies between B4 and B2/B3
    edges = run_graph_to_vector({"graph": scored}, {"part": "edges"})
    assert len(edges) == 3 and {"node_u", "node_v", "length", "is_bridge"} <= set(
        edges.columns
    )


def test_fa76_edges_without_geometry_from_a_plain_table():
    table = gpd.GeoDataFrame(
        {"bus0": ["B1", "B3"], "bus1": ["B4", "B2"]}, geometry=[None, None], crs=None
    )
    g = _run(edges=table).nx_graph
    assert g.edges["B1", "B4"]["length"] == pytest.approx(2000.0)  # straight line
    assert g.edges["B1", "B4"]["geometry"] is None


def test_fa76_shared_coordinates_stay_separate_nodes_unlike_build_network():
    """B1 and B2 share a coordinate: the edge list keeps both nodes and the
    transformer between them; nearest-node snapping cannot tell them apart."""
    edges = _lines(
        [
            ("T1", "B1", "B2", None, LineString([(0, 0), (0.5, 0)])),
            ("L1", "B2", "B3", 1.2, LineString([(0, 0), (1000, 0)])),
            ("L2", "B1", "B4", 2.0, LineString([(0, 0), (0, 2000)])),
        ]
    )
    exact = _run(edges=edges).nx_graph
    assert exact.number_of_nodes() == 4 and exact.has_edge("B1", "B2")
    assert exact.has_edge("B2", "B3") and exact.has_edge("B1", "B4")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        snapped = run_build_network(
            {"lines": edges, "nodes": _buses()},
            {"tolerance_m": 1, "node_key": "bus_id"},
        ).nx_graph
    # every line end snaps to the first of the two co-located buses: the 220 kV
    # line L1 hangs at the 380 kV bus B1, B2 stays isolated, the transformer vanishes
    assert not snapped.has_edge("B1", "B2")
    assert snapped.degree("B2") == 0


# =====================================================================
# Dangling edges, keys, self loops, parallel edges
# =====================================================================


def _with_dangling() -> gpd.GeoDataFrame:
    return _lines(
        [
            ("L1", "B2", "B3", 1.0, LineString([(0, 0), (1000, 0)])),
            ("L9", "B3", "X9", 1.0, LineString([(1000, 0), (5000, 0)])),
            ("L8", None, "B4", 1.0, LineString([(9, 9), (0, 2000)])),
        ]
    )


def test_fa76_dangling_edge_is_an_error_by_default():
    with pytest.raises(
        ValueError, match=r"2 von 3 Kante\(n\) nennen einen unbekannten Knoten"
    ) as exc:
        _run(edges=_with_dangling())
    assert "'X9'" in str(exc.value) and "unknown_nodes: drop" in str(exc.value)


def test_fa76_dangling_edge_with_drop_is_left_out_with_counts():
    with pytest.warns(DanglingEdgeWarning, match=r"'bus0': 1, 'bus1': 1"):
        g = _run(edges=_with_dangling(), unknown_nodes="drop").nx_graph
    assert g.number_of_edges() == 1 and "X9" not in g


@pytest.mark.parametrize(
    "keys, message",
    [
        (["B1", "B1", "B3", "B4"], "nicht eindeutig"),
        (["B1", None, "B3", "B4"], "leere Werte"),
        (["B1", "", "B3", "B4"], "leere Werte"),
        (["B1", "  ", "B3", "B4"], "leere Werte"),
    ],
)
def test_fa76_bad_node_keys_are_an_error(keys, message):
    nodes = _buses().assign(bus_id=keys)
    with pytest.raises(ValueError, match=message):
        _run(nodes=nodes, edges=_lines([("L", "B3", "B4", 1.0, None)]))


def test_fa76_missing_columns_are_an_error_with_the_present_columns():
    with pytest.raises(ValueError, match=r"Spalte 'bus0' fehlt im Layer 'edges'"):
        _run(edges=_lines().rename(columns={"bus0": "von"}))
    with pytest.raises(ValueError, match=r"node_key|Spalte 'bus_id' fehlt"):
        _run(nodes=_buses().rename(columns={"bus_id": "name"}))


def test_fa76_self_loop_is_left_out_with_a_warning():
    edges = _lines([("L1", "B1", "B4", 1.0, None), ("L2", "B3", "B3", 1.0, None)])
    with pytest.warns(SelfLoopWarning, match="1 Kante"):
        g = _run(edges=edges).nx_graph
    assert not g.has_edge("B3", "B3")


def test_fa76_parallel_edges_keep_the_shortest_and_count_the_merged_rows():
    edges = _lines(
        [
            ("L1", "B1", "B4", 3.0, None),
            ("L2", "B4", "B1", 2.0, None),
            ("L3", "B1", "B4", 5.0, None),
        ]
    )
    with pytest.warns(ParallelEdgesMergedWarning, match="2 Kante"):
        g = _run(edges=edges, length_column="length_km", length_unit="km").nx_graph
    edge = g.edges["B1", "B4"]
    assert edge["line_id"] == "L2" and edge["length"] == pytest.approx(2000.0)
    assert edge["parallel_edges"] == 3


def test_fa76_several_components_are_reported_with_the_largest():
    edges = _lines([("L1", "B1", "B4", 1.0, None)])
    with pytest.warns(
        DisconnectedNetworkWarning, match=r"3 getrennten .*größte: 2 von 4"
    ):
        _run(edges=edges)


# =====================================================================
# Type error and parameter checks at config time
# =====================================================================


def test_fa76_contract_ports_and_types():
    assert EdgeListNetworkStep.INPUT_PORTS == {
        "nodes": DataType.VECTOR,
        "edges": DataType.VECTOR,
    }
    assert EdgeListNetworkStep.OUTPUT_TYPE == DataType.GRAPH


def _scenario(edges_layer: dict) -> dict:
    return {
        "scenario": {"name": "t", "region": "5.0,45.0,15.0,55.0"},
        "layers": [
            {
                "id": "buses",
                "source": "file",
                "path": "x.geojson",
                "data_type": "vector",
            },
            edges_layer,
        ],
        "steps": [
            {
                "id": "netz",
                "op": "edge_list_network",
                "inputs": {"nodes": "buses", "edges": edges_layer["id"]},
                "params": PARAMS,
            },
            {"id": "knoten", "op": "graph_to_vector", "inputs": {"graph": "netz"}},
        ],
        "output": [{"type": "geojson", "source": "knoten"}],
    }


def test_fa76_raster_input_is_a_type_error_at_config_time():
    issues = api.validate(
        _scenario(
            {
                "id": "r",
                "source": "file",
                "path": "x.tif",
                "data_type": "raster",
                "format": "tif",
            }
        )
    ).issues
    assert issues and issues[0][0] == "steps -> 0 -> inputs -> edges"
    assert "vector" in issues[0][1]


def test_fa76_table_edges_validate():
    assert api.validate(
        _scenario(
            {"id": "lines", "source": "table", "path": "lines.csv", "quotechar": "'"}
        )
    ).valid


@pytest.mark.parametrize(
    "params, field",
    [
        ({"from_column": "a", "to_column": "b"}, "node_key"),
        ({**PARAMS, "to_column": "bus0"}, "verschieden"),
        ({**PARAMS, "unknown_nodes": "ignore"}, "unknown_nodes"),
        ({**PARAMS, "length_unit": "mi"}, "length_unit"),
    ],
)
def test_fa76_params_are_validated(params, field):
    with pytest.raises(ValueError, match=field):
        EdgeListNetworkStep(
            id="n",
            op="edge_list_network",
            inputs={"nodes": "a", "edges": "b"},
            params=params,
        )


# =====================================================================
# Edge case: empty layers
# =====================================================================


def test_fa76_empty_edges_give_isolated_nodes():
    empty = _lines().iloc[0:0]
    with pytest.warns(DisconnectedNetworkWarning):
        g = _run(edges=empty).nx_graph
    assert g.number_of_nodes() == 4 and g.number_of_edges() == 0


def test_fa76_empty_nodes_and_edges_give_an_empty_graph():
    graph = _run(nodes=_buses().iloc[0:0], edges=_lines().iloc[0:0])
    assert graph.nx_graph.number_of_nodes() == 0
    assert len(run_graph_to_vector({"graph": graph}, {})) == 0


def test_fa76_is_visible_in_the_catalog():
    entry = {e["op"]: e for e in api.operation_catalog()}["edge_list_network"]
    assert entry["input_ports"] == {"edges": "vector", "nodes": "vector"} or entry[
        "input_ports"
    ] == {"nodes": "vector", "edges": "vector"}
    assert entry["output_type"] == "graph"
    assert sorted(entry["required_params"]) == ["from_column", "node_key", "to_column"]


# =====================================================================
# Review findings 03.10.2026: no NaN lengths, line geometry, reserved
# columns, CRS precondition
# =====================================================================


def _keyed(n: int, geometry: bool = True) -> gpd.GeoDataFrame:
    keys = ["x", "y", "z"][:n]
    points = [Point(i * 1000, 0) for i in range(n)] if geometry else [None] * n
    return gpd.GeoDataFrame({"bus_id": keys}, geometry=points, crs=CRS)


def test_fa76_undefined_edge_length_is_an_error_not_nan():
    edges = _lines([("L1", "x", "y", None, None), ("L2", "y", "z", None, None)])
    with pytest.raises(ValueError, match="2 Kante.*ohne bestimmbare Länge"):
        _run(nodes=_keyed(3, geometry=False), edges=edges)
    # with length_column and a missing value the same: no NaN weight
    edges = _lines([("L1", "x", "y", 1.0, None), ("L2", "y", "z", None, None)])
    with (
        pytest.raises(ValueError, match="1 Kante.*ohne bestimmbare Länge.*'y', 'z'"),
        pytest.warns(EdgeLengthFallbackWarning),
    ):
        _run(
            nodes=_keyed(3, geometry=False),
            edges=edges,
            length_column="length_km",
            length_unit="km",
        )


def test_fa76_parallel_edge_without_length_never_stays_as_nan():
    edges = _lines([("L1", "x", "y", None, None), ("L2", "x", "y", 5.0, None)])
    with (
        pytest.raises(ValueError, match="ohne bestimmbare Länge"),
        pytest.warns(EdgeLengthFallbackWarning),
    ):
        _run(nodes=_keyed(2, geometry=False), edges=edges, length_column="length_km")
    # with node geometry the missing value falls back to the straight line
    # (1000 m) and the shorter parallel edge (5 m) is kept
    with (
        pytest.warns(EdgeLengthFallbackWarning),
        pytest.warns(ParallelEdgesMergedWarning),
    ):
        g = _run(nodes=_keyed(2), edges=edges, length_column="length_km").nx_graph
    assert g.edges["x", "y"]["length"] == pytest.approx(5.0)
    assert g.edges["x", "y"]["line_id"] == "L2"


def test_fa76_multilinestring_edges_are_merged_and_work_with_route():
    edges = _lines(
        [
            (
                "L1",
                "x",
                "y",
                None,
                MultiLineString([[(0, 0), (500, 0)], [(500, 0), (1000, 0)]]),
            ),
            ("L2", "y", "z", None, LineString([(1000, 0), (2000, 0)])),
        ]
    )
    graph = _run(nodes=_keyed(3), edges=edges)
    assert graph.nx_graph.edges["x", "y"]["geometry"].geom_type == "LineString"
    origins = gpd.GeoDataFrame({"n": ["o"]}, geometry=[Point(0, 1)], crs=CRS)
    destinations = gpd.GeoDataFrame({"n": ["d"]}, geometry=[Point(2000, 1)], crs=CRS)
    routes = run_route(
        {"graph": graph, "origins": origins, "destinations": destinations},
        {"max_snap_m": 50},
    )
    assert routes.geometry.iloc[0].length == pytest.approx(2000.0)


def test_fa76_disjoint_multilinestring_keeps_its_length_without_geometry():
    edges = _lines(
        [
            (
                "L1",
                "x",
                "y",
                None,
                MultiLineString([[(0, 0), (400, 0)], [(600, 0), (1000, 0)]]),
            ),
        ]
    )
    with pytest.warns(EdgeGeometryWarning, match="1 Kante"):
        g = _run(nodes=_keyed(2), edges=edges).nx_graph
    assert g.edges["x", "y"]["geometry"] is None
    assert g.edges["x", "y"]["length"] == pytest.approx(800.0)


def test_fa76_non_line_edge_geometry_is_an_error():
    edges = _lines([("L1", "x", "y", None, Polygon([(0, 0), (1, 0), (1, 1)]))])
    with pytest.raises(ValueError, match="Kantengeometrie vom Typ Polygon"):
        _run(nodes=_keyed(2), edges=edges)


def test_fa76_reserved_edge_columns_are_overwritten_with_a_warning():
    edges = _lines([("L1", "x", "y", None, None)]).assign(length=7.0, parallel_edges=9)
    with pytest.warns(
        EdgeAttributeOverwrittenWarning, match=r"\['length', 'parallel_edges'\]"
    ):
        g = _run(nodes=_keyed(2), edges=edges).nx_graph
    assert g.edges["x", "y"]["length"] == pytest.approx(1000.0)
    # length_column: length uses the column - nothing is lost, no warning
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        g = _run(
            nodes=_keyed(2),
            edges=edges.drop(columns="parallel_edges"),
            length_column="length",
        ).nx_graph
    assert g.edges["x", "y"]["length"] == pytest.approx(7.0)


def test_fa76_nodes_and_edges_in_different_crs_are_an_error():
    with pytest.raises(ValueError, match="verschiedene CRS"):
        _run(edges=_lines().to_crs("EPSG:4326"))


def test_fa76_nodes_without_crs_and_edges_with_crs_are_an_error():
    nodes = _buses().set_crs(None, allow_override=True)
    with pytest.raises(ValueError, match="verschiedene CRS"):
        _run(nodes=nodes)
