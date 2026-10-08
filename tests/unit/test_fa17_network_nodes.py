"""Implements: FA17 (Netzknoten aus Linientopologie ableiten).

Contract-Tests für src/geofact/builtin/operations/network_nodes.py: Happy Path,
Typfehler-Fall, Randfall leerer Layer (Testregel für Operationen) plus
Schema-Validierung des NetworkNodesStep.
"""

import geopandas as gpd
import pytest
from shapely.geometry import LineString, MultiLineString, Polygon

from geofact.builtin.operations import graph as graph_module
from geofact.builtin.operations import network_nodes
from geofact.builtin.operations.network_nodes import NetworkNodesStep


def empty_lines() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")


# =====================================================================
# Happy Path
# =====================================================================


def test_fa17_extracts_unique_endpoints():
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "bc"]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            LineString([(100, 0), (200, 0)]),
        ],
        crs="EPSG:32633",
    )
    result = network_nodes.run_network_nodes({"lines": lines}, {})
    # 3 eindeutige Endpunkte: (0,0), (100,0) - geteilt von beiden Segmenten -, (200,0)
    assert len(result) == 3
    assert (result.geometry.geom_type == "Point").all()
    assert result.crs == lines.crs
    coords = {(round(p.x), round(p.y)) for p in result.geometry}
    assert coords == {(0, 0), (100, 0), (200, 0)}


def test_fa17_tolerance_merges_nearby_endpoints():
    # Zwei Segmente, deren Enden 3m auseinanderliegen statt exakt gleich
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "bc"]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            LineString([(103, 0), (200, 0)]),
        ],
        crs="EPSG:32633",
    )
    result_no_tol = network_nodes.run_network_nodes(
        {"lines": lines}, {"tolerance_m": 0}
    )
    assert len(result_no_tol) == 4  # (0,0),(100,0),(103,0),(200,0) - alle eindeutig

    result_tol = network_nodes.run_network_nodes({"lines": lines}, {"tolerance_m": 5})
    assert len(result_tol) == 3  # (100,0) und (103,0) fallen zusammen


def test_fa17_multilinestring_each_part_contributes_endpoints():
    mls = MultiLineString([[(0, 0), (50, 0)], [(50, 0), (100, 0)]])
    lines = gpd.GeoDataFrame({"id": ["mls"]}, geometry=[mls], crs="EPSG:32633")
    result = network_nodes.run_network_nodes({"lines": lines}, {})
    coords = {(round(p.x), round(p.y)) for p in result.geometry}
    assert coords == {(0, 0), (50, 0), (100, 0)}


def test_fa17_output_usable_as_build_network_nodes():
    """Nachbedingung FA17: ein build_network-Schritt mit dem network_nodes-
    Ergebnis als 'nodes' und derselben Toleranz verliert kein Segment -
    Kantenzahl entspricht der Segmentanzahl, keine Fragmentierungswarnung."""
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "bc", "cd"]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            LineString([(100, 0), (200, 50)]),
            LineString([(200, 50), (300, 50)]),
        ],
        crs="EPSG:32633",
    )
    nodes = network_nodes.run_network_nodes({"lines": lines}, {"tolerance_m": 1})

    with _no_warning_context():
        result = graph_module.run_build_network(
            {"lines": lines, "nodes": nodes}, {"tolerance_m": 1}
        )
    assert result.nx_graph.number_of_edges() == 3
    assert result.nx_graph.number_of_nodes() == 4


class _no_warning_context:
    """Kleiner Kontextmanager, der sicherstellt, dass KEINE
    DisconnectedNetworkWarning ausgelöst wird (pytest.warns(None) ist in
    neueren pytest-Versionen entfernt)."""

    def __enter__(self):
        import warnings

        self._catch = warnings.catch_warnings(record=True)
        self._records = self._catch.__enter__()
        warnings.simplefilter("always")
        return self._records

    def __exit__(self, exc_type, exc, tb):
        self._catch.__exit__(exc_type, exc, tb)
        disconnected = [
            w
            for w in self._records
            if issubclass(w.category, graph_module.DisconnectedNetworkWarning)
        ]
        assert not disconnected, f"unerwartete Fragmentierungswarnung: {disconnected}"
        return False


# =====================================================================
# Typfehler-Fall
# =====================================================================


def test_fa17_non_linear_geometry_skipped_with_warning():
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "poly"]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
        ],
        crs="EPSG:32633",
    )
    with pytest.warns(network_nodes.NonLinearGeometryWarning):
        result = network_nodes.run_network_nodes({"lines": lines}, {})
    # nur die LineString-Endpunkte, Polygon übersprungen statt Absturz
    assert len(result) == 2


# =====================================================================
# Randfall: leerer Layer
# =====================================================================


def test_fa17_empty_lines_returns_empty_points():
    result = network_nodes.run_network_nodes({"lines": empty_lines()}, {})
    assert result.empty
    assert result.crs == empty_lines().crs


# =====================================================================
# Schema-Validierung (NetworkNodesStep)
# =====================================================================


def test_fa17_schema_requires_lines_port():
    with pytest.raises(ValueError, match="fehlende Eingänge"):
        NetworkNodesStep(id="n", op="network_nodes", inputs={}, params={})


def test_fa17_schema_valid_minimal_step():
    step = NetworkNodesStep(
        id="n", op="network_nodes", inputs={"lines": "tramgleise"}, params={}
    )
    assert step.OUTPUT_TYPE.value == "vector"
    assert step.INPUT_PORTS == {"lines": step.INPUT_PORTS["lines"]}
