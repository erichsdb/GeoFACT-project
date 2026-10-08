"""Implements: FA50 (Netz aus Linien mit Knotenbildung).

Contract-Tests für src/geofact/builtin/operations/line_network.py: Knotenregel
(Linienenden + gemeinsame Koordinaten, keine Knoten an reinen Kreuzungen), Teilung
der Linien, Kantenattribute, Fahrzeit, Fangtoleranz, Flächen, parallele Stücke,
leerer Layer, Typfehler.
"""

import warnings

import geopandas as gpd
import numpy as np
import pytest
from pydantic import ValidationError
from shapely.geometry import LineString, MultiLineString, Point, Polygon

from geofact.builtin.operations import line_network as ln
from geofact.builtin.operations.graph import (
    DisconnectedNetworkWarning,
    NonLinearGeometryWarning,
)
from geofact.core.types import Graph, RasterLayer
from geofact.engine.catalog import params_catalog

CRS = "EPSG:32633"


def frame(geoms, **columns) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        columns or {"id": list(range(len(geoms)))}, geometry=geoms, crs=CRS
    )


def build(geoms, params=None, **columns) -> Graph:
    return ln.run_line_network({"lines": frame(geoms, **columns)}, params or {})


def node_coords(graph: Graph) -> set[tuple[float, float]]:
    return {(g.x, g.y) for _, g in graph.nx_graph.nodes(data="geometry")}


def edge_between(graph: Graph, a: tuple, b: tuple) -> dict:
    ids = {(g.x, g.y): n for n, g in graph.nx_graph.nodes(data="geometry")}
    return graph.nx_graph.edges[ids[a], ids[b]]


# =====================================================================
# Happy Path: Knoten und Kanten
# =====================================================================


def test_fa50_chain_of_two_lines_has_three_nodes_and_two_edges():
    graph = build([LineString([(0, 0), (100, 0)]), LineString([(100, 0), (100, 50)])])
    assert isinstance(graph, Graph)
    assert node_coords(graph) == {(0, 0), (100, 0), (100, 50)}
    assert graph.nx_graph.number_of_edges() == 2
    assert graph.crs == CRS
    assert edge_between(graph, (0, 0), (100, 0))["length"] == pytest.approx(100)


def test_fa50_shared_vertex_in_the_middle_splits_the_line_into_two_edges():
    """T-Kreuzung: die Koordinate (100, 0) liegt mitten in der ersten Linie und am Ende der
    zweiten - die erste Linie wird dort geteilt."""
    graph = build(
        [
            LineString([(0, 0), (100, 0), (200, 0)]),
            LineString([(100, 0), (100, 100)]),
        ]
    )
    assert node_coords(graph) == {(0, 0), (100, 0), (200, 0), (100, 100)}
    assert graph.nx_graph.number_of_edges() == 3
    assert edge_between(graph, (0, 0), (100, 0))["length"] == pytest.approx(100)
    assert edge_between(graph, (100, 0), (200, 0))["length"] == pytest.approx(100)


def test_fa50_vertices_used_by_a_single_line_are_not_nodes():
    """Kurvenpunkte einer einzelnen Linie bleiben Teil der Kantengeometrie."""
    curve = LineString([(0, 0), (50, 20), (100, 0), (150, 20), (200, 0)])
    graph = build([curve])
    assert node_coords(graph) == {(0, 0), (200, 0)}
    ((u, v, data),) = graph.nx_graph.edges(data=True)
    assert data["geometry"].equals(curve)
    assert data["length"] == pytest.approx(curve.length)


def test_fa50_crossing_without_a_shared_coordinate_stays_unconnected():
    """Bruecke/Tunnel: die Linien kreuzen sich bei (50, 50), teilen aber keine Koordinate."""
    with pytest.warns(DisconnectedNetworkWarning, match="2 getrennten"):
        graph = build(
            [LineString([(0, 50), (100, 50)]), LineString([(50, 0), (50, 100)])]
        )
    assert graph.nx_graph.number_of_nodes() == 4
    assert graph.nx_graph.number_of_edges() == 2
    assert (50, 50) not in node_coords(graph)


def test_fa50_a_coordinate_visited_twice_by_one_line_is_a_node():
    """Eine Linie, die sich selbst berührt (Acht): der Berührpunkt ist Knoten, die Schleife
    bleibt als Kante an diesem Knoten erhalten."""
    figure_eight = LineString(
        [(0, 0), (50, 50), (100, 0), (100, 100), (50, 50), (0, 100)]
    )
    graph = build([figure_eight])
    assert node_coords(graph) == {(0, 0), (50, 50), (0, 100)}
    assert graph.nx_graph.number_of_edges() == 3
    center = next(
        n for n, g in graph.nx_graph.nodes(data="geometry") if (g.x, g.y) == (50, 50)
    )
    assert graph.nx_graph.has_edge(center, center)


def test_fa50_edge_geometry_runs_between_the_nodes_of_the_edge():
    graph = build(
        [
            LineString([(0, 0), (60, 30), (100, 0)]),
            LineString([(100, 0), (100, 100)]),
        ]
    )
    ids = {(g.x, g.y): n for n, g in graph.nx_graph.nodes(data="geometry")}
    for u, v, data in graph.nx_graph.edges(data=True):
        ends = {(data["geometry"].coords[0]), (data["geometry"].coords[-1])}
        assert ends == {
            (
                graph.nx_graph.nodes[u]["geometry"].x,
                graph.nx_graph.nodes[u]["geometry"].y,
            ),
            (
                graph.nx_graph.nodes[v]["geometry"].x,
                graph.nx_graph.nodes[v]["geometry"].y,
            ),
        }
    assert ids[(0, 0)] == 0  # Knoten heißen 0..n-1 nach erstem Auftreten


def test_fa50_node_numbers_follow_first_appearance_and_are_deterministic():
    geoms = [LineString([(0, 0), (10, 0)]), LineString([(10, 0), (10, 10)])]
    first = build(geoms)
    second = build(geoms)
    assert [(n, g.wkt) for n, g in first.nx_graph.nodes(data="geometry")] == [
        (0, "POINT (0 0)"),
        (1, "POINT (10 0)"),
        (2, "POINT (10 10)"),
    ]
    assert list(first.nx_graph.edges) == list(second.nx_graph.edges)


def test_fa50_multilinestring_parts_are_separate_lines():
    multi = MultiLineString([[(0, 0), (100, 0)], [(100, 0), (100, 100)]])
    graph = build([multi])
    assert node_coords(graph) == {(0, 0), (100, 0), (100, 100)}
    assert graph.nx_graph.number_of_edges() == 2


def test_fa50_repeated_consecutive_coordinates_do_not_create_edges_of_length_zero():
    graph = build([LineString([(0, 0), (0, 0), (100, 0), (100, 0)])])
    assert graph.nx_graph.number_of_edges() == 1
    assert all(d["length"] > 0 for _, _, d in graph.nx_graph.edges(data=True))


def test_fa50_does_not_mutate_its_input():
    lines = frame([LineString([(0, 0), (100, 0)])], highway=["residential"])
    before = lines.copy()
    ln.run_line_network({"lines": lines}, {"attributes": ["highway"]})
    assert lines.equals(before)


# =====================================================================
# Attribute und Fahrzeit
# =====================================================================


def test_fa50_selected_attributes_are_carried_to_every_piece_of_the_line():
    graph = build(
        [LineString([(0, 0), (100, 0), (200, 0)]), LineString([(100, 0), (100, 50)])],
        {"attributes": ["highway", "name"]},
        highway=["primary", "residential"],
        name=["Hauptstrasse", None],
    )
    pieces = [d for _, _, d in graph.nx_graph.edges(data=True)]
    assert sorted(d["highway"] for d in pieces) == ["primary", "primary", "residential"]
    assert [d["name"] for d in pieces if d["highway"] == "primary"] == [
        "Hauptstrasse"
    ] * 2
    assert all(set(d) == {"length", "geometry", "highway", "name"} for d in pieces)


def test_fa50_missing_attribute_values_become_none():
    graph = build(
        [LineString([(0, 0), (100, 0)])],
        {"attributes": ["name"]},
        name=[np.nan],
    )
    ((_, _, data),) = graph.nx_graph.edges(data=True)
    assert data["name"] is None


def test_fa50_without_attributes_edges_carry_only_length_and_geometry():
    graph = build([LineString([(0, 0), (100, 0)])], highway=["residential"])
    ((_, _, data),) = graph.nx_graph.edges(data=True)
    assert set(data) == {"length", "geometry"}


def test_fa50_unknown_attribute_column_is_an_explicit_error():
    with pytest.raises(ValueError, match=r"attributes \['gibts_nicht'\].*vorhanden"):
        build(
            [LineString([(0, 0), (100, 0)])],
            {"attributes": ["gibts_nicht"]},
            highway=["x"],
        )


def test_fa50_reserved_attribute_names_are_rejected_at_configuration_time():
    with pytest.raises(ValidationError, match="length"):
        ln.LineNetworkStep(
            id="n",
            op="line_network",
            inputs={"lines": "l"},
            params={"attributes": ["length"]},
        )


def test_fa50_speed_sets_travel_time_in_minutes():
    """500 m bei 30 km/h = 500 m/min -> genau 1 Minute."""
    graph = build([LineString([(0, 0), (500, 0)])], {"speed_kmh": 30})
    ((_, _, data),) = graph.nx_graph.edges(data=True)
    assert data["travel_time_min"] == pytest.approx(1.0)


def test_fa50_without_speed_there_is_no_travel_time():
    graph = build([LineString([(0, 0), (500, 0)])])
    ((_, _, data),) = graph.nx_graph.edges(data=True)
    assert "travel_time_min" not in data


@pytest.mark.parametrize("speed", [0, -5])
def test_fa50_speed_must_be_positive(speed):
    with pytest.raises(ValidationError, match="speed_kmh"):
        ln.LineNetworkStep(
            id="n",
            op="line_network",
            inputs={"lines": "l"},
            params={"speed_kmh": speed},
        )


# =====================================================================
# Fangtoleranz
# =====================================================================


def n_components(graph: Graph) -> int:
    import networkx as nx

    return nx.number_connected_components(graph.nx_graph)


def test_fa50_tolerance_joins_ends_that_miss_each_other_slightly():
    geoms = [LineString([(0, 0), (100, 0)]), LineString([(100.8, 0), (200, 0)])]
    with pytest.warns(DisconnectedNetworkWarning):
        separate = build(geoms)
    joined = build(geoms, {"tolerance_m": 1.0})
    assert separate.nx_graph.number_of_nodes() == 4
    assert joined.nx_graph.number_of_nodes() == 3
    assert n_components(joined) == 1


def test_fa50_tolerance_zero_keeps_near_coordinates_apart():
    with pytest.warns(DisconnectedNetworkWarning):
        graph = build(
            [LineString([(0, 0), (100, 0)]), LineString([(100.8, 0), (200, 0)])]
        )
    assert graph.nx_graph.number_of_nodes() == 4


def test_fa50_tolerance_does_not_join_coordinates_farther_apart():
    with pytest.warns(DisconnectedNetworkWarning):
        graph = build(
            [LineString([(0, 0), (100, 0)]), LineString([(103, 0), (200, 0)])],
            {"tolerance_m": 1.0},
        )
    assert graph.nx_graph.number_of_nodes() == 4


def test_fa50_tolerance_snaps_the_edge_geometry_to_the_node_coordinates():
    graph = build(
        [LineString([(0, 0), (100, 0)]), LineString([(100.8, 0), (200, 0)])],
        {"tolerance_m": 1.0},
    )
    nodes = node_coords(graph)
    for _, _, data in graph.nx_graph.edges(data=True):
        assert data["geometry"].coords[0] in nodes
        assert data["geometry"].coords[-1] in nodes


def test_fa50_tolerance_collapsing_a_short_line_removes_it_with_a_counted_warning():
    """Eine Linie, die kürzer als die Toleranz ist, fällt auf einen Punkt zusammen: sie entfällt,
    das gemeldet wird - keine Kante der Länge 0, kein stilles Verschwinden."""
    with pytest.warns(
        NonLinearGeometryWarning,
        match=r"1 Linie\(n\) sind nicht länger als tolerance_m=1 m",
    ):
        graph = build(
            [LineString([(0, 0), (0.4, 0)]), LineString([(0.4, 0), (100, 0)])],
            {"tolerance_m": 1.0},
        )
    assert graph.nx_graph.number_of_edges() == 1
    assert all(d["length"] > 0 for _, _, d in graph.nx_graph.edges(data=True))


# =====================================================================
# Flächen (geschlossene OSM-Wege), unbrauchbare Geometrien
# =====================================================================


def roundabout_scene():
    ring = Polygon([(100, 0), (110, 10), (100, 20), (90, 10)])
    arm_west = LineString([(0, 10), (90, 10)])
    arm_east = LineString([(110, 10), (200, 10)])
    return [arm_west, ring, arm_east]


@pytest.mark.filterwarnings(
    "ignore::geofact.builtin.operations.graph.DisconnectedNetworkWarning"
)
def test_fa50_polygons_are_skipped_with_a_counted_warning_by_default():
    with pytest.warns(
        NonLinearGeometryWarning, match=r"1 Objekt\(e\).*1 Fläche\(n\).*boundary"
    ):
        graph = build(roundabout_scene())
    assert graph.nx_graph.number_of_edges() == 2


def test_fa50_polygon_boundary_connects_the_arms_through_the_ring():
    import networkx as nx

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        graph = build(roundabout_scene(), {"polygons": "boundary"})
    assert nx.number_connected_components(graph.nx_graph) == 1
    ids = {(g.x, g.y): n for n, g in graph.nx_graph.nodes(data="geometry")}
    length = nx.shortest_path_length(
        graph.nx_graph, ids[(0, 10)], ids[(200, 10)], weight="length"
    )
    ring_half = 2 * np.hypot(10, 10)
    assert length == pytest.approx(90 + ring_half + 90)


@pytest.mark.filterwarnings(
    "ignore::geofact.builtin.operations.graph.DisconnectedNetworkWarning"
)
def test_fa50_multipolygon_boundaries_and_holes_become_lines():
    outer = Polygon(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        holes=[[(40, 40), (60, 40), (60, 60), (40, 60)]],
    )
    graph = build([outer], {"polygons": "boundary"})
    lengths = sorted(d["length"] for _, _, d in graph.nx_graph.edges(data=True))
    assert lengths == pytest.approx([80, 400])


def test_fa50_points_and_empty_geometries_are_skipped_with_a_warning():
    geoms = [LineString([(0, 0), (100, 0)]), Point(5, 5), LineString()]
    with pytest.warns(NonLinearGeometryWarning) as caught:
        graph = build(geoms)
    messages = " | ".join(str(w.message) for w in caught)
    assert "1 Objekt(e) in 'lines' ohne Geometrie" in messages
    assert "1 Objekt(e) in 'lines' sind keine (Multi)LineString-Geometrie" in messages
    assert graph.nx_graph.number_of_edges() == 1


# =====================================================================
# Parallele Stücke, Zusammenhang
# =====================================================================


def test_fa50_parallel_pieces_keep_the_shorter_edge_and_warn():
    short = LineString([(0, 0), (100, 0)])
    long_way = LineString([(0, 0), (50, 80), (100, 0)])
    with pytest.warns(ln.ParallelEdgesWarning, match=r"1 Stück\(e\)"):
        graph = build([long_way, short])
    assert graph.nx_graph.number_of_edges() == 1
    ((_, _, data),) = graph.nx_graph.edges(data=True)
    assert data["length"] == pytest.approx(100)


def test_fa50_disconnected_network_warns_with_the_size_of_the_largest_component():
    geoms = [
        LineString([(0, 0), (100, 0)]),
        LineString([(100, 0), (200, 0)]),
        LineString([(1000, 0), (1100, 0)]),
    ]
    with pytest.warns(
        DisconnectedNetworkWarning, match=r"2 getrennten.*größte: 3 von 5 Knoten"
    ):
        build(geoms)


def test_fa50_connected_network_gives_no_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        build([LineString([(0, 0), (100, 0)]), LineString([(100, 0), (100, 50)])])


# =====================================================================
# Randfälle und Typfehler
# =====================================================================


def test_fa50_empty_layer_gives_an_empty_graph_with_a_warning():
    empty = gpd.GeoDataFrame({"id": []}, geometry=[], crs=CRS)
    with pytest.warns(ln.NoLinesWarning, match="Netz ist leer"):
        graph = ln.run_line_network({"lines": empty}, {})
    assert isinstance(graph, Graph)
    assert graph.nx_graph.number_of_nodes() == 0
    assert graph.crs == CRS


@pytest.mark.filterwarnings(
    "ignore::geofact.builtin.operations.graph.NonLinearGeometryWarning"
)
def test_fa50_layer_without_any_usable_line_is_an_empty_graph():
    with pytest.warns(ln.NoLinesWarning):
        graph = build([Point(0, 0)])
    assert graph.nx_graph.number_of_nodes() == 0


def test_fa50_type_error_when_lines_is_not_a_vector_layer():
    raster = RasterLayer(
        data=np.zeros((2, 2)),
        transform=__import__("affine").Affine.identity(),
        crs=__import__("pyproj").CRS.from_epsg(32633),
    )
    with pytest.raises(TypeError, match="Eingang 'lines' muss ein Vektor-Layer"):
        ln.run_line_network({"lines": raster}, {})


def test_fa50_missing_crs_is_an_explicit_error():
    lines = gpd.GeoDataFrame({"id": [0]}, geometry=[LineString([(0, 0), (1, 0)])])
    with pytest.raises(ValueError, match="kein CRS"):
        ln.run_line_network({"lines": lines}, {})


def test_fa50_geographic_crs_is_an_explicit_error():
    lines = gpd.GeoDataFrame(
        {"id": [0]},
        geometry=[LineString([(12.9, 50.8), (12.91, 50.8)])],
        crs="EPSG:4326",
    )
    with pytest.raises(ValueError, match="geographischen CRS"):
        ln.run_line_network({"lines": lines}, {})


def test_fa50_unknown_parameters_are_rejected():
    with pytest.raises(ValidationError, match="geschwindigkeit"):
        ln.LineNetworkStep(
            id="n",
            op="line_network",
            inputs={"lines": "l"},
            params={"geschwindigkeit": 30},
        )


# =====================================================================
# Vertrag im Katalog
# =====================================================================


def test_fa50_contract_ports_output_and_params():
    step = ln.LineNetworkStep
    assert {k: v.value for k, v in step.INPUT_PORTS.items()} == {"lines": "vector"}
    assert step.OUTPUT_TYPE.value == "graph"
    specs, required = params_catalog(step.Params)
    assert required == []
    assert set(specs) == {"speed_kmh", "tolerance_m", "attributes", "polygons"}
    assert specs["polygons"]["choices"] == ["skip", "boundary"]
    assert specs["speed_kmh"]["required"] is False
