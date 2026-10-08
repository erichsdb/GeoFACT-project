"""Implements: FA19 (Vektor-Layer verlustfrei zusammenführen).

Contract-Tests für src/geofact/builtin/operations/vector_union.py: Happy Path,
Typfehler-Fälle (CRS-Mismatch, Geometriefamilien-Mismatch), Randfall
leerer Layer (Testregel für Operationen) plus Schema-Validierung des
VectorUnionStep und ein Regressionstest, der die leipzig10-Motivation
(Tram + S-Bahn) direkt gegen network_nodes/build_network durchspielt.
"""

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Polygon

from geofact.builtin.operations import graph as graph_module
from geofact.builtin.operations import network_nodes
from geofact.builtin.operations import vector_union
from geofact.builtin.operations.vector_union import VectorUnionStep


def empty_lines(crs="EPSG:32633") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"id": []}, geometry=[], crs=crs)


# =====================================================================
# Happy Path
# =====================================================================


def test_fa19_concatenates_rows_with_mismatched_columns():
    """Mimikt tram vs. rail: 'a' hat eine 'voltage'-Spalte, die 'b' fehlt;
    'b' hat eine 'gauge'-Spalte, die 'a' fehlt; beide teilen 'railway'
    mit unterschiedlichen Werten."""
    a = gpd.GeoDataFrame(
        {
            "id": ["tram1", "tram2"],
            "railway": ["tram", "tram"],
            "voltage": ["600", "600"],
        },
        geometry=[
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (20, 0)]),
        ],
        crs="EPSG:32633",
    )
    b = gpd.GeoDataFrame(
        {
            "id": ["rail1", "rail2", "rail3"],
            "railway": ["rail", "rail", "rail"],
            "gauge": ["1435", "1435", "1435"],
        },
        geometry=[
            LineString([(0, 100), (10, 100)]),
            LineString([(10, 100), (20, 100)]),
            LineString([(20, 100), (30, 100)]),
        ],
        crs="EPSG:32633",
    )

    result = vector_union.run_vector_union({"a": a, "b": b}, {})

    assert len(result) == len(a) + len(b) == 5
    assert set(result["id"]) == {"tram1", "tram2", "rail1", "rail2", "rail3"}

    by_id = result.set_index("id")
    # Spalte 'voltage' nur bei a-Zeilen gesetzt, NaN bei b-Zeilen
    assert by_id.loc["tram1", "voltage"] == "600"
    assert by_id.loc["tram2", "voltage"] == "600"
    assert gpd.pd.isna(by_id.loc["rail1", "voltage"])
    assert gpd.pd.isna(by_id.loc["rail2", "voltage"])
    # Spalte 'gauge' nur bei b-Zeilen gesetzt, NaN bei a-Zeilen
    assert gpd.pd.isna(by_id.loc["tram1", "gauge"])
    assert by_id.loc["rail1", "gauge"] == "1435"
    # gemeinsame Spalte 'railway' korrekt je Herkunft
    assert by_id.loc["tram1", "railway"] == "tram"
    assert by_id.loc["tram2", "railway"] == "tram"
    assert by_id.loc["rail1", "railway"] == "rail"
    assert by_id.loc["rail3", "railway"] == "rail"


def test_fa19_single_geometry_family_both_sides_no_error():
    a = gpd.GeoDataFrame(
        {"id": ["p1"]},
        geometry=[Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
        crs="EPSG:32633",
    )
    b = gpd.GeoDataFrame(
        {"id": ["p2"]},
        geometry=[Polygon([(20, 0), (30, 0), (30, 10), (20, 10)])],
        crs="EPSG:32633",
    )
    result = vector_union.run_vector_union({"a": a, "b": b}, {})
    assert len(result) == 2
    assert (result.geometry.geom_type == "Polygon").all()


# =====================================================================
# Typfehler-Fall: CRS-Mismatch
# =====================================================================


def test_fa19_crs_mismatch_raises_value_error():
    a = gpd.GeoDataFrame(
        {"id": ["l1"]}, geometry=[LineString([(0, 0), (10, 0)])], crs="EPSG:32633"
    )
    b = gpd.GeoDataFrame(
        {"id": ["l2"]}, geometry=[LineString([(0, 0), (0.1, 0)])], crs="EPSG:4326"
    )
    with pytest.raises(ValueError, match="EPSG:32633") as excinfo:
        vector_union.run_vector_union({"a": a, "b": b}, {})
    assert "4326" in str(excinfo.value)


# =====================================================================
# Typfehler-Fall: Geometriefamilien-Mismatch
# =====================================================================


def test_fa19_geometry_family_mismatch_raises_value_error():
    a = gpd.GeoDataFrame(
        {"id": ["p1"]},
        geometry=[Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
        crs="EPSG:32633",
    )
    b = gpd.GeoDataFrame(
        {"id": ["l1"]},
        geometry=[LineString([(0, 0), (10, 0)])],
        crs="EPSG:32633",
    )
    with pytest.raises(ValueError, match="Fläche") as excinfo:
        vector_union.run_vector_union({"a": a, "b": b}, {})
    assert "Linie" in str(excinfo.value)


# =====================================================================
# Randfall: leerer Layer
# =====================================================================


def test_fa19_one_side_empty_returns_only_other_sides_rows():
    a = empty_lines()
    b = gpd.GeoDataFrame(
        {"id": ["l1", "l2", "l3"]},
        geometry=[
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (20, 0)]),
            LineString([(20, 0), (30, 0)]),
        ],
        crs="EPSG:32633",
    )
    result = vector_union.run_vector_union({"a": a, "b": b}, {})
    assert len(result) == 3
    assert result.crs == b.crs
    assert set(result["id"]) == {"l1", "l2", "l3"}


def test_fa19_both_sides_empty_returns_empty_no_crash():
    a = empty_lines()
    b = empty_lines()
    result = vector_union.run_vector_union({"a": a, "b": b}, {})
    assert result.empty
    assert result.crs == a.crs


# =====================================================================
# Schema-Validierung (VectorUnionStep)
# =====================================================================


def test_fa19_schema_requires_a_and_b_ports():
    with pytest.raises(ValueError, match="fehlende Eingänge"):
        VectorUnionStep(id="u", op="vector_union", inputs={}, params={})
    with pytest.raises(ValueError, match="fehlende Eingänge"):
        VectorUnionStep(
            id="u", op="vector_union", inputs={"a": "tramgleise"}, params={}
        )


def test_fa19_schema_valid_minimal_step():
    step = VectorUnionStep(
        id="u",
        op="vector_union",
        inputs={"a": "tramgleise", "b": "sbahn_gleise"},
        params={},
    )
    assert step.OUTPUT_TYPE.value == "vector"
    assert set(step.INPUT_PORTS.keys()) == {"a", "b"}


# =====================================================================
# Regression: leipzig10-Motivation (Tram + S-Bahn -> network_nodes -> build_network)
# =====================================================================


def test_fa19_regression_combined_tram_and_rail_feeds_build_network():
    """Spiegelt den motivierenden Fall der Anforderung FA19:
    ein Tram- und ein S-Bahn-Linien-Layer mit gemeinsamem 'railway'-
    Attribut und teils nicht-gemeinsamen Spalten werden zusammengeführt,
    dann durch network_nodes (FA17) und build_network (FA10) geschickt -
    der kombinierte Layer muss in genau dieser Pipeline nutzbar sein und
    Kanten aus BEIDEN Eingaben im resultierenden Graphen erzeugen."""
    tram = gpd.GeoDataFrame(
        {
            "id": [f"tram{i}" for i in range(5)],
            "railway": ["tram"] * 5,
            "voltage": ["600"] * 5,
        },
        geometry=[
            LineString([(0, 0), (10, 0)]),
            LineString([(10, 0), (20, 0)]),
            LineString([(20, 0), (30, 0)]),
            LineString([(30, 0), (40, 0)]),
            LineString([(40, 0), (50, 0)]),
        ],
        crs="EPSG:32633",
    )
    rail = gpd.GeoDataFrame(
        {
            "id": [f"rail{i}" for i in range(6)],
            "railway": ["rail"] * 6,
            "gauge": ["1435"] * 6,
        },
        geometry=[
            LineString([(0, 500), (20, 500)]),
            LineString([(20, 500), (40, 500)]),
            LineString([(40, 500), (60, 500)]),
            LineString([(60, 500), (80, 500)]),
            LineString([(80, 500), (100, 500)]),
            LineString([(100, 500), (120, 500)]),
        ],
        crs="EPSG:32633",
    )

    combined = vector_union.run_vector_union({"a": tram, "b": rail}, {})
    assert len(combined) == len(tram) + len(rail) == 11

    nodes = network_nodes.run_network_nodes({"lines": combined}, {"tolerance_m": 1})
    result = graph_module.run_build_network(
        {"lines": combined, "nodes": nodes}, {"tolerance_m": 1}
    )

    # 5 Tram- + 6 Rail-Segmente ergeben 11 Kanten insgesamt, verteilt über
    # zwei getrennte Zusammenhangskomponenten (Tram bei y=0, Rail bei
    # y=500 - beide Netze sind räumlich getrennt, aber beide Kantengruppen
    # müssen im Graphen vorhanden sein).
    assert result.nx_graph.number_of_edges() == 11
    components = list(graph_module.nx.connected_components(result.nx_graph))
    assert len(components) == 2
