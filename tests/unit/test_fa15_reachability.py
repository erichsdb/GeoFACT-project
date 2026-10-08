"""Implements: FA15 (Erreichbarkeitsanalyse).

Contract-Tests für src/geofact/builtin/operations/reachability.py:
Happy Path, Typfehler-Fall, Randfall leerer Layer (Testregel für Operationen)
plus Schema-Validierung von ShortestPathStep/IsochroneStep.
"""

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point

from geofact.builtin.operations import graph as graph_module
from geofact.builtin.operations import reachability as reach_module
from geofact.builtin.operations.reachability import IsochroneStep, ShortestPathStep


def path_nodes() -> gpd.GeoDataFrame:
    # A-B-C-D in einer Reihe, 100m Abstand (metrisches CRS: EPSG:32633)
    return gpd.GeoDataFrame(
        {"id": ["A", "B", "C", "D"]},
        geometry=[Point(0, 0), Point(100, 0), Point(200, 0), Point(300, 0)],
        crs="EPSG:32633",
    )


def path_lines() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": ["ab", "bc", "cd"]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            LineString([(100, 0), (200, 0)]),
            LineString([(200, 0), (300, 0)]),
        ],
        crs="EPSG:32633",
    )


def built_path_graph() -> graph_module.Graph:
    return graph_module.run_build_network(
        {"lines": path_lines(), "nodes": path_nodes()}, {}
    )


# =====================================================================
# shortest_path
# =====================================================================


def test_fa15_shortest_path_happy_path():
    graph = built_path_graph()
    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    destinations = gpd.GeoDataFrame(
        {"id": ["d1"]}, geometry=[Point(300, 0)], crs="EPSG:32633"
    )
    result = reach_module.run_shortest_path(
        {"graph": graph, "origins": origins, "destinations": destinations},
        {"max_snap_m": 5},
    )
    assert list(result["network_distance"]) == pytest.approx([300.0])
    assert len(result) == 1  # origins-Zeilenzahl bleibt erhalten


def test_fa15_shortest_path_picks_nearest_of_multiple_destinations():
    graph = built_path_graph()
    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    destinations = gpd.GeoDataFrame(
        {"id": ["far", "near"]},
        geometry=[Point(300, 0), Point(200, 0)],
        crs="EPSG:32633",
    )
    result = reach_module.run_shortest_path(
        {"graph": graph, "origins": origins, "destinations": destinations},
        {"max_snap_m": 5},
    )
    assert result["network_distance"].iloc[0] == pytest.approx(200.0)


def test_fa15_shortest_path_unreachable_destination_is_nan_with_warning():
    # Zwei getrennte Paare: A-B und C-D, keine Verbindung dazwischen -
    # o1 bei A kann d1 bei D im Netz nicht erreichen.
    nodes = gpd.GeoDataFrame(
        {"id": ["A", "B", "C", "D"]},
        geometry=[Point(0, 0), Point(100, 0), Point(1000, 0), Point(1100, 0)],
        crs="EPSG:32633",
    )
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "cd"]},
        geometry=[LineString([(0, 0), (100, 0)]), LineString([(1000, 0), (1100, 0)])],
        crs="EPSG:32633",
    )
    with pytest.warns(graph_module.DisconnectedNetworkWarning):
        graph = graph_module.run_build_network({"lines": lines, "nodes": nodes}, {})

    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    destinations = gpd.GeoDataFrame(
        {"id": ["d1"]}, geometry=[Point(1100, 0)], crs="EPSG:32633"
    )
    with pytest.warns(reach_module.UnreachableFeatureWarning):
        result = reach_module.run_shortest_path(
            {"graph": graph, "origins": origins, "destinations": destinations},
            {"max_snap_m": 5},
        )
    assert result["network_distance"].isna().all()


def test_fa15_shortest_path_unsnappable_origin_is_nan_with_warning():
    graph = built_path_graph()
    # 50m abseits, aber max_snap_m=5 -> nicht fangbar
    origins = gpd.GeoDataFrame(
        {"id": ["far"]}, geometry=[Point(0, 50)], crs="EPSG:32633"
    )
    destinations = gpd.GeoDataFrame(
        {"id": ["d1"]}, geometry=[Point(300, 0)], crs="EPSG:32633"
    )
    with pytest.warns(reach_module.UnsnappableFeatureWarning):
        result = reach_module.run_shortest_path(
            {"graph": graph, "origins": origins, "destinations": destinations},
            {"max_snap_m": 5},
        )
    assert result["network_distance"].isna().all()


def test_fa15_shortest_path_empty_origins():
    graph = built_path_graph()
    origins = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    destinations = gpd.GeoDataFrame(
        {"id": ["d1"]}, geometry=[Point(300, 0)], crs="EPSG:32633"
    )
    result = reach_module.run_shortest_path(
        {"graph": graph, "origins": origins, "destinations": destinations},
        {"max_snap_m": 5},
    )
    assert len(result) == 0
    assert "network_distance" in result.columns


def test_fa15_shortest_path_empty_destinations_warns():
    graph = built_path_graph()
    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    destinations = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    with pytest.warns(reach_module.UnreachableFeatureWarning):
        result = reach_module.run_shortest_path(
            {"graph": graph, "origins": origins, "destinations": destinations},
            {"max_snap_m": 5},
        )
    assert result["network_distance"].isna().all()


def test_fa15_shortest_path_type_error_on_non_graph():
    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    with pytest.raises(TypeError):
        reach_module.run_shortest_path(
            {"graph": origins, "origins": origins, "destinations": origins},
            {"max_snap_m": 5},
        )


def test_fa15_shortest_path_type_error_on_non_vector_origins():
    graph = built_path_graph()
    destinations = gpd.GeoDataFrame(
        {"id": ["d1"]}, geometry=[Point(300, 0)], crs="EPSG:32633"
    )
    with pytest.raises(TypeError):
        reach_module.run_shortest_path(
            {"graph": graph, "origins": "not-a-layer", "destinations": destinations},
            {"max_snap_m": 5},
        )


def test_fa15_shortest_path_does_not_mutate_graph():
    graph = built_path_graph()
    edges_before = graph.nx_graph.number_of_edges()
    nodes_before = graph.nx_graph.number_of_nodes()
    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    destinations = gpd.GeoDataFrame(
        {"id": ["d1"]}, geometry=[Point(300, 0)], crs="EPSG:32633"
    )
    reach_module.run_shortest_path(
        {"graph": graph, "origins": origins, "destinations": destinations},
        {"max_snap_m": 5},
    )
    assert graph.nx_graph.number_of_edges() == edges_before
    assert graph.nx_graph.number_of_nodes() == nodes_before
    assert "betweenness" not in graph.nx_graph.nodes["A"]


# =====================================================================
# isochrone
# =====================================================================


def test_fa15_isochrone_happy_path_one_row_per_break():
    graph = built_path_graph()
    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    result = reach_module.run_isochrone(
        {"graph": graph, "origins": origins},
        {"breaks_m": [50, 150, 350], "max_snap_m": 5, "buffer_m": 10},
    )
    assert list(result["break_m"]) == [50, 150, 350]
    assert len(result) == 3
    assert result.crs.to_epsg() == 32633


def test_fa15_isochrone_breaks_cumulative_containment():
    graph = built_path_graph()
    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    result = reach_module.run_isochrone(
        {"graph": graph, "origins": origins},
        {"breaks_m": [50, 150, 350], "max_snap_m": 5, "buffer_m": 10},
    )
    small = result[result["break_m"] == 50].geometry.iloc[0]
    medium = result[result["break_m"] == 150].geometry.iloc[0]
    large = result[result["break_m"] == 350].geometry.iloc[0]
    assert small.area < medium.area < large.area
    assert medium.covers(small.buffer(-1))  # kumulative Fläche wächst mit dem break


def test_fa15_isochrone_small_break_still_covers_origin_node():
    # Der Origin-Knoten selbst hat Distanz 0 - auch ein sehr kleiner
    # break_m liefert daher immer mindestens eine Fläche (den
    # gepufferten Startknoten), nie eine leere Geometrie für diese Stufe.
    graph = built_path_graph()
    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    result = reach_module.run_isochrone(
        {"graph": graph, "origins": origins},
        {"breaks_m": [1, 2000], "max_snap_m": 5, "buffer_m": 1},
    )
    assert list(result["break_m"]) == [1, 2000]
    assert (
        result[result["break_m"] == 1].geometry.iloc[0].area
        < result[result["break_m"] == 2000].geometry.iloc[0].area
    )


def test_fa15_isochrone_break_with_no_reachable_node_skipped_with_warning():
    # Alle origins unsnappable -> überhaupt kein Knoten als Quelle,
    # jede breaks_m-Stufe bleibt leer und wird übersprungen statt eine
    # leere/kaputte Geometrie zu liefern.
    graph = built_path_graph()
    origins = gpd.GeoDataFrame(
        {"id": ["far"]}, geometry=[Point(0, 5000)], crs="EPSG:32633"
    )
    with pytest.warns(reach_module.UnreachableFeatureWarning):
        result = reach_module.run_isochrone(
            {"graph": graph, "origins": origins},
            {"breaks_m": [150], "max_snap_m": 5, "buffer_m": 10},
        )
    assert len(result) == 0


def test_fa15_isochrone_empty_origins_warns():
    graph = built_path_graph()
    origins = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    with pytest.warns(reach_module.UnsnappableFeatureWarning):
        result = reach_module.run_isochrone(
            {"graph": graph, "origins": origins},
            {"breaks_m": [150], "max_snap_m": 5, "buffer_m": 10},
        )
    assert len(result) == 0


def test_fa15_isochrone_type_error_on_non_graph():
    origins = gpd.GeoDataFrame({"id": ["o1"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    with pytest.raises(TypeError):
        reach_module.run_isochrone(
            {"graph": origins, "origins": origins}, {"breaks_m": [150], "max_snap_m": 5}
        )


# =====================================================================
# Schema-Validierung
# =====================================================================


def test_fa15_shortest_path_max_snap_required():
    with pytest.raises(ValueError):
        ShortestPathStep(
            id="sp",
            inputs={"graph": "g", "origins": "o", "destinations": "d"},
            params={},
        )


def test_fa15_shortest_path_max_snap_must_be_positive():
    with pytest.raises(ValueError):
        ShortestPathStep(
            id="sp",
            inputs={"graph": "g", "origins": "o", "destinations": "d"},
            params={"max_snap_m": 0},
        )


def test_fa15_isochrone_breaks_must_ascend():
    with pytest.raises(ValueError):
        IsochroneStep(
            id="iso",
            inputs={"graph": "g", "origins": "o"},
            params={"breaks_m": [150, 50], "max_snap_m": 5},
        )


def test_fa15_isochrone_breaks_must_be_positive():
    with pytest.raises(ValueError):
        IsochroneStep(
            id="iso",
            inputs={"graph": "g", "origins": "o"},
            params={"breaks_m": [0, 50], "max_snap_m": 5},
        )


def test_fa15_isochrone_max_snap_required():
    with pytest.raises(ValueError):
        IsochroneStep(
            id="iso", inputs={"graph": "g", "origins": "o"}, params={"breaks_m": [50]}
        )


def test_fa15_isochrone_ignores_edge_geometry():
    """Kanten tragen seit der FA10-Änderung ihre Geometrie; isochrone liest sie
    nicht - die Fläche ist mit und ohne Kantengeometrie identisch (bestehende
    Ergebnisse bleiben unverändert). Der Verlauf ist hier bewusst kurvig, damit
    ein Lesen der Geometrie die Fläche sichtbar verändern würde."""
    curved = gpd.GeoDataFrame(
        {"id": ["ab", "bc"]},
        geometry=[
            LineString([(0, 0), (50, 80), (100, 0)]),
            LineString([(100, 0), (150, -80), (200, 0)]),
        ],
        crs="EPSG:32633",
    )
    nodes = gpd.GeoDataFrame(
        {"id": ["A", "B", "C"]},
        geometry=[Point(0, 0), Point(100, 0), Point(200, 0)],
        crs="EPSG:32633",
    )
    graph = graph_module.run_build_network({"lines": curved, "nodes": nodes}, {})
    assert all("geometry" in data for _, _, data in graph.nx_graph.edges(data=True))
    stripped = graph_module.Graph(graph.nx_graph.copy(), crs=graph.crs)
    for _, _, data in stripped.nx_graph.edges(data=True):
        del data["geometry"]

    origins = gpd.GeoDataFrame({"id": ["o"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    params = {"breaks_m": [300], "max_snap_m": 5, "buffer_m": 20}
    with_geometry = reach_module.run_isochrone(
        {"graph": graph, "origins": origins}, params
    )
    without_geometry = reach_module.run_isochrone(
        {"graph": stripped, "origins": origins}, params
    )
    assert with_geometry.geometry.iloc[0].equals(without_geometry.geometry.iloc[0])
