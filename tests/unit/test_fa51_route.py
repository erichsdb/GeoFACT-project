"""Implements: FA51 (Route zwischen Orten).

Contract-Tests für src/geofact/builtin/operations/route.py: Happy Path (Route folgt den
Kantengeometrien), Gewicht, Paarbildung, Fangen (größte Komponente), nicht fangbare/
nicht erreichbare Paare, leere Layer, Typfehler, Vertrag im Katalog.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import networkx as nx
import numpy as np
import pytest
from affine import Affine
from pydantic import ValidationError
from pyproj import CRS
from shapely.geometry import LineString, Point, Polygon

from geofact.builtin.operations import graph as graph_module
from geofact.builtin.operations import line_network as ln
from geofact.builtin.operations import route as route_module
from geofact.builtin.operations.reachability import (
    UnreachableFeatureWarning,
    UnsnappableFeatureWarning,
)
from geofact.core.types import Graph, RasterLayer
from geofact.engine.catalog import params_catalog

CRS_M = "EPSG:32633"


def places(coords, names=None, crs=CRS_M) -> gpd.GeoDataFrame:
    data = {"name": names} if names is not None else {"id": list(range(len(coords)))}
    return gpd.GeoDataFrame(data, geometry=[Point(*c) for c in coords], crs=crs)


def network(geoms, **params) -> Graph:
    lines = gpd.GeoDataFrame({"id": list(range(len(geoms)))}, geometry=geoms, crs=CRS_M)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ln.run_line_network({"lines": lines}, params)


def grid_lines(size: int = 3, step: float = 100.0) -> list[LineString]:
    coords = [i * step for i in range(size)]
    horizontals = [LineString([(x, y) for x in coords]) for y in coords]
    verticals = [LineString([(x, y) for y in coords]) for x in coords]
    return horizontals + verticals


def manual_graph(nodes: dict, edges: list, crs=CRS_M) -> Graph:
    nx_graph = nx.Graph()
    for node, (x, y) in nodes.items():
        nx_graph.add_node(node, geometry=Point(x, y))
    for u, v, attrs in edges:
        nx_graph.add_edge(u, v, **attrs)
    return Graph(nx_graph, crs=crs)


def route(graph, origins, destinations, **params) -> gpd.GeoDataFrame:
    params.setdefault("max_snap_m", 20)
    return route_module.run_route(
        {"graph": graph, "origins": origins, "destinations": destinations}, params
    )


# =====================================================================
# Happy Path
# =====================================================================


def test_fa51_route_on_a_grid_has_length_straight_line_and_detour_factor():
    graph = network(grid_lines())
    result = route(graph, places([(0, 0)], ["A"]), places([(200, 200)], ["B"]))
    assert len(result) == 1
    row = result.iloc[0]
    assert (row["origin"], row["destination"]) == ("A", "B")
    assert row["length_m"] == pytest.approx(400)
    assert row["straight_line_m"] == pytest.approx(200 * np.sqrt(2))
    assert row["detour_factor"] == pytest.approx(400 / (200 * np.sqrt(2)))
    assert row["origin_snap_m"] == pytest.approx(0)
    assert row["destination_snap_m"] == pytest.approx(0)
    line = row.geometry
    assert line.geom_type == "LineString"
    assert line.length == pytest.approx(400)
    assert line.coords[0] == (0, 0) and line.coords[-1] == (200, 200)
    assert result.crs == CRS_M


def test_fa51_route_follows_the_edge_geometry_not_the_straight_line_between_nodes():
    """Die Kante A-B verläuft über (50, 60); die Route enthält diesen Kurvenpunkt."""
    graph = network(
        [
            LineString([(0, 0), (50, 60), (100, 0)]),
            LineString([(100, 0), (200, 0)]),
        ]
    )
    result = route(graph, places([(0, 0)]), places([(200, 0)]))
    line = result.geometry.iloc[0]
    assert (50, 60) in list(line.coords)
    curve = np.hypot(50, 60) * 2
    assert result["length_m"].iloc[0] == pytest.approx(curve + 100)
    assert line.length == pytest.approx(curve + 100)


def test_fa51_route_through_an_edge_in_reverse_direction_is_oriented_along_the_travel():
    graph = network(
        [
            LineString([(0, 0), (50, 60), (100, 0)]),
            LineString([(100, 0), (200, 0)]),
        ]
    )
    result = route(graph, places([(200, 0)]), places([(0, 0)]))
    coords = list(result.geometry.iloc[0].coords)
    assert coords[0] == (200, 0) and coords[-1] == (0, 0)
    assert coords == [(200, 0), (100, 0), (50, 60), (0, 0)]


def test_fa51_route_works_on_a_build_network_graph_with_edge_geometry():
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "bc"]},
        geometry=[
            LineString([(0, 0), (50, 60), (100, 0)]),
            LineString([(100, 0), (200, 0)]),
        ],
        crs=CRS_M,
    )
    nodes = places([(0, 0), (100, 0), (200, 0)])
    nodes["id"] = ["A", "B", "C"]
    graph = graph_module.run_build_network({"lines": lines, "nodes": nodes}, {})
    result = route(graph, places([(0, 0)]), places([(200, 0)]))
    assert (50, 60) in list(result.geometry.iloc[0].coords)


def test_fa51_labels_come_from_the_name_column_else_from_the_index():
    graph = network(grid_lines())
    named = route(graph, places([(0, 0)], ["Start"]), places([(200, 0)], ["Ziel"]))
    assert (named["origin"].iloc[0], named["destination"].iloc[0]) == ("Start", "Ziel")
    unnamed = route(graph, places([(0, 0)]), places([(200, 0)]))
    assert (unnamed["origin"].iloc[0], unnamed["destination"].iloc[0]) == ("0", "0")


def test_fa51_points_are_snapped_to_the_nearest_node_and_the_snap_distance_is_reported():
    graph = network(grid_lines())
    result = route(graph, places([(3, 4)]), places([(200, 197)]), max_snap_m=10)
    assert result["origin_snap_m"].iloc[0] == pytest.approx(5)
    assert result["destination_snap_m"].iloc[0] == pytest.approx(3)
    assert result["length_m"].iloc[0] == pytest.approx(400)
    # Luftlinie und Umwegfaktor gelten für die Orte, nicht für die Knoten
    assert result["straight_line_m"].iloc[0] == pytest.approx(np.hypot(197, 193))


def test_fa51_polygon_origins_route_from_their_centroid():
    graph = network(grid_lines())
    square = gpd.GeoDataFrame(
        {"name": ["Feld"]},
        geometry=[Polygon([(-5, -5), (5, -5), (5, 5), (-5, 5)])],
        crs=CRS_M,
    )
    result = route(graph, square, places([(200, 0)]))
    assert result["length_m"].iloc[0] == pytest.approx(200)


def test_fa51_route_does_not_mutate_the_graph_or_the_inputs():
    graph = network(grid_lines())
    before_edges = {(u, v): dict(d) for u, v, d in graph.nx_graph.edges(data=True)}
    origins, destinations = places([(0, 0)]), places([(200, 200)])
    origins_before = origins.copy()
    route(graph, origins, destinations)
    assert {
        (u, v): dict(d) for u, v, d in graph.nx_graph.edges(data=True)
    } == before_edges
    assert origins.equals(origins_before)


# =====================================================================
# Gewicht
# =====================================================================


def weight_graph() -> Graph:
    """A-B direkt: 1000 m, 10 min. A-C-B über die Autobahn: 1200 m, 6 min."""
    return manual_graph(
        {"A": (0, 0), "B": (1000, 0), "C": (500, 300)},
        [
            (
                "A",
                "B",
                {
                    "length": 1000.0,
                    "travel_time_min": 10.0,
                    "geometry": LineString([(0, 0), (1000, 0)]),
                },
            ),
            (
                "A",
                "C",
                {
                    "length": 600.0,
                    "travel_time_min": 3.0,
                    "geometry": LineString([(0, 0), (500, 300)]),
                },
            ),
            (
                "C",
                "B",
                {
                    "length": 600.0,
                    "travel_time_min": 3.0,
                    "geometry": LineString([(500, 300), (1000, 0)]),
                },
            ),
        ],
    )


def test_fa51_weight_length_takes_the_shortest_road():
    result = route(weight_graph(), places([(0, 0)]), places([(1000, 0)]))
    assert result["length_m"].iloc[0] == pytest.approx(1000)
    assert result["travel_time_min"].iloc[0] == pytest.approx(10)


def test_fa51_weight_travel_time_takes_the_fastest_road_and_still_reports_the_length():
    result = route(
        weight_graph(), places([(0, 0)]), places([(1000, 0)]), weight="travel_time_min"
    )
    assert result["travel_time_min"].iloc[0] == pytest.approx(6)
    assert result["length_m"].iloc[0] == pytest.approx(1200)
    assert (500, 300) in list(result.geometry.iloc[0].coords)


def test_fa51_travel_time_column_exists_only_when_every_edge_has_a_time():
    with_time = route(
        network(grid_lines(), speed_kmh=30), places([(0, 0)]), places([(200, 0)])
    )
    assert with_time["travel_time_min"].iloc[0] == pytest.approx(200 / 500)
    without_time = route(network(grid_lines()), places([(0, 0)]), places([(200, 0)]))
    assert "travel_time_min" not in without_time.columns


def test_fa51_weight_travel_time_without_travel_times_is_an_explicit_error():
    with pytest.raises(ValueError, match="'travel_time_min'.*speed_kmh"):
        route(
            network(grid_lines()),
            places([(0, 0)]),
            places([(200, 0)]),
            weight="travel_time_min",
        )


def test_fa51_edges_without_length_are_an_explicit_error_not_a_silent_weight_of_one():
    graph = manual_graph({"A": (0, 0), "B": (100, 0)}, [("A", "B", {})])
    with pytest.raises(
        ValueError, match="1 von 1 Kanten tragen kein Attribut 'length'"
    ):
        route(graph, places([(0, 0)]), places([(100, 0)]))


# =====================================================================
# Paarbildung
# =====================================================================


def test_fa51_pairwise_connects_row_i_with_row_i():
    graph = network(grid_lines())
    result = route(
        graph,
        places([(0, 0), (0, 200)], ["o1", "o2"]),
        places([(200, 0), (200, 200)], ["d1", "d2"]),
    )
    assert list(zip(result["origin"], result["destination"])) == [
        ("o1", "d1"),
        ("o2", "d2"),
    ]
    assert result["length_m"].tolist() == pytest.approx([200, 200])


def test_fa51_cross_connects_every_origin_with_every_destination():
    graph = network(grid_lines())
    result = route(
        graph,
        places([(0, 0), (0, 200)], ["o1", "o2"]),
        places([(200, 0), (200, 100), (100, 100)], ["d1", "d2", "d3"]),
        pairing="cross",
    )
    assert len(result) == 6
    assert list(zip(result["origin"], result["destination"]))[:3] == [
        ("o1", "d1"),
        ("o1", "d2"),
        ("o1", "d3"),
    ]
    assert result.set_index(["origin", "destination"])["length_m"][
        "o2", "d2"
    ] == pytest.approx(300)


def test_fa51_pairwise_with_different_row_counts_is_an_explicit_error():
    graph = network(grid_lines())
    with pytest.raises(
        ValueError, match=r"gleich viele Zeilen \(origins: 2, destinations: 1\).*cross"
    ):
        route(graph, places([(0, 0), (0, 100)]), places([(200, 0)]))


# =====================================================================
# Fangen: größte Komponente, nicht fangbar, nicht erreichbar
# =====================================================================


def grid_with_island() -> Graph:
    """Gitter 0..200 und daneben, ohne gemeinsame Koordinate, eine kurze Straße bei
    (130, 100)-(130, 110): sie kreuzt die Gitterlinie y=100 nur geometrisch."""
    return network(grid_lines() + [LineString([(130, 100), (130, 110)])])


def test_fa51_a_place_near_only_a_small_component_is_not_snapped_into_it_by_default():
    graph = network(grid_lines() + [LineString([(1000, 0), (1100, 0)])])
    with pytest.raises(ValueError, match="1 nicht gefangen"):
        route(graph, places([(1000, 5)]), places([(0, 0)]), max_snap_m=50)


def test_fa51_diversion_to_the_largest_component_is_warned_and_reports_the_larger_snap_distance():
    """Ort (130, 105): der nächste Knoten liegt mit 5 m in der Insel, der nächste des
    Hauptnetzes ist (100, 100) mit 30,4 m."""
    with pytest.warns(
        route_module.OtherComponentWarning, match="kleineren Zusammenhangskomponente"
    ):
        result = route(
            grid_with_island(), places([(130, 105)]), places([(0, 0)]), max_snap_m=40
        )
    assert result["origin_snap_m"].iloc[0] == pytest.approx(np.hypot(30, 5))
    assert result["length_m"].iloc[0] == pytest.approx(200)


def test_fa51_snap_to_any_node_uses_the_nearest_node_even_in_a_small_component():
    with pytest.raises(ValueError, match="1 ohne Weg"):
        route(
            grid_with_island(),
            places([(130, 105)]),
            places([(0, 0)]),
            max_snap_m=40,
            snap_to="any_node",
        )


def test_fa51_unsnappable_pair_gets_a_row_without_geometry_and_a_warning_while_others_route():
    graph = network(grid_lines())
    origins = places([(0, 0), (900, 900)], ["nah", "fern"])
    destinations = places([(200, 0), (200, 200)], ["d1", "d2"])
    with pytest.warns(UnsnappableFeatureWarning, match="1 von 2 Paaren"):
        result = route(graph, origins, destinations, max_snap_m=30)
    assert len(result) == 2
    assert result["length_m"].iloc[0] == pytest.approx(200)
    assert result.geometry.iloc[0] is not None
    assert np.isnan(result["length_m"].iloc[1])
    assert result.geometry.iloc[1] is None
    assert np.isnan(result["origin_snap_m"].iloc[1])


@pytest.mark.filterwarnings(
    "ignore::geofact.builtin.operations.route.EdgeGeometryMissingWarning"
)
def test_fa51_unreachable_pair_gets_a_row_without_geometry_and_a_warning():
    graph = manual_graph(
        {"A": (0, 0), "B": (100, 0), "C": (500, 0), "D": (600, 0)},
        [("A", "B", {"length": 100.0}), ("C", "D", {"length": 100.0})],
    )
    origins = places([(0, 0), (0, 0)], ["a1", "a2"])
    destinations = places([(100, 0), (600, 0)], ["b", "d"])
    with pytest.warns(UnreachableFeatureWarning, match="1 von 2 Paaren"):
        result = route(graph, origins, destinations, snap_to="any_node")
    assert result["length_m"].iloc[0] == pytest.approx(100)
    assert np.isnan(result["length_m"].iloc[1])
    assert result.geometry.iloc[1] is None


def test_fa51_nothing_routable_is_an_error_with_the_reasons():
    graph = network(grid_lines())
    with pytest.raises(
        ValueError, match=r"kein einziges der 1 Paare.*1 nicht gefangen"
    ):
        route(graph, places([(900, 900)]), places([(0, 0)]), max_snap_m=30)


def test_fa51_origin_and_destination_at_the_same_node_give_length_zero_without_a_line():
    graph = network(grid_lines())
    with pytest.warns(route_module.SameNodeWarning):
        result = route(graph, places([(1, 1)]), places([(2, 2)]))
    assert result["length_m"].iloc[0] == 0
    assert result.geometry.iloc[0] is None
    assert np.isnan(result["detour_factor"].iloc[0])


def test_fa51_identical_places_have_no_detour_factor():
    graph = network(grid_lines())
    with pytest.warns(route_module.SameNodeWarning):
        result = route(graph, places([(0, 0)]), places([(0, 0)]))
    assert result["straight_line_m"].iloc[0] == 0
    assert np.isnan(result["detour_factor"].iloc[0])


def test_fa51_edge_without_geometry_is_drawn_straight_with_a_warning():
    graph = manual_graph(
        {"A": (0, 0), "B": (100, 0), "C": (100, 100)},
        [("A", "B", {"length": 100.0}), ("B", "C", {"length": 100.0})],
    )
    with pytest.warns(route_module.EdgeGeometryMissingWarning, match="2 Kante"):
        result = route(graph, places([(0, 0)]), places([(100, 100)]))
    assert list(result.geometry.iloc[0].coords) == [(0, 0), (100, 0), (100, 100)]


def test_fa51_a_complete_route_gives_no_warning():
    graph = network(grid_lines())
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        route(graph, places([(0, 0)]), places([(200, 200)]))


# =====================================================================
# Leere Layer, Typ- und CRS-Fehler, Konfiguration
# =====================================================================


def test_fa51_empty_origins_or_destinations_are_an_explicit_error():
    graph = network(grid_lines())
    empty = gpd.GeoDataFrame({"id": []}, geometry=[], crs=CRS_M)
    with pytest.raises(ValueError, match="'origins' ist leer"):
        route(graph, empty, places([(0, 0)]))
    with pytest.raises(ValueError, match="'destinations' ist leer"):
        route(graph, places([(0, 0)]), empty)


def test_fa51_empty_graph_is_an_explicit_error():
    empty_graph = Graph(nx.Graph(), crs=CRS_M)
    with pytest.raises(ValueError, match="keine Kanten"):
        route(empty_graph, places([(0, 0)]), places([(100, 0)]))


def test_fa51_type_error_when_graph_is_not_a_graph():
    with pytest.raises(TypeError, match="Eingang 'graph' muss ein Graph"):
        route(places([(0, 0)]), places([(0, 0)]), places([(1, 1)]))


def test_fa51_type_error_when_origins_is_not_a_vector_layer():
    raster = RasterLayer(
        data=np.zeros((2, 2)), transform=Affine.identity(), crs=CRS.from_epsg(32633)
    )
    with pytest.raises(TypeError, match="Eingang 'origins' muss ein Vektor-Layer"):
        route(network(grid_lines()), raster, places([(0, 0)]))


def test_fa51_different_crs_is_an_explicit_error():
    graph = network(grid_lines())
    with pytest.raises(ValueError, match="unterschiedliche CRS"):
        route(graph, places([(12.9, 50.8)], crs="EPSG:4326"), places([(0, 0)]))


def test_fa51_nodes_without_geometry_are_an_explicit_error():
    nx_graph = nx.Graph()
    nx_graph.add_edge("A", "B", length=1.0)
    with pytest.raises(ValueError, match="Knoten des Graphen tragen keine Geometrie"):
        route(Graph(nx_graph, crs=CRS_M), places([(0, 0)]), places([(1, 0)]))


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"max_snap_m": 0},
        {"max_snap_m": 5, "weight": "cost"},
        {"max_snap_m": 5, "pairing": "all"},
        {"max_snap_m": 5, "snap_to": "x"},
        {"max_snap_m": 5, "radius": 3},
    ],
)
def test_fa51_invalid_configuration_is_rejected(params):
    with pytest.raises(ValidationError):
        route_module.RouteStep(
            id="r",
            op="route",
            inputs={"graph": "g", "origins": "o", "destinations": "d"},
            params=params,
        )


# =====================================================================
# Vertrag im Katalog
# =====================================================================


def test_fa51_contract_ports_output_and_params():
    step = route_module.RouteStep
    assert {k: v.value for k, v in step.INPUT_PORTS.items()} == {
        "graph": "graph",
        "origins": "vector",
        "destinations": "vector",
    }
    assert step.OUTPUT_TYPE.value == "vector"
    specs, required = params_catalog(step.Params)
    assert required == ["max_snap_m"]
    assert specs["pairing"]["choices"] == ["pairwise", "cross"]
    assert specs["weight"]["choices"] == ["length", "travel_time_min"]
    assert specs["snap_to"]["choices"] == ["largest_component", "any_node"]
