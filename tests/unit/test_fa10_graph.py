"""Implements: FA10 (Graphmodul).

Contract-Tests für src/geofact/builtin/operations/graph.py.
"""

import geopandas as gpd
import pytest
from shapely.geometry import LineString, MultiLineString, Point, Polygon

from geofact.builtin.operations import graph as graph_module
from geofact.engine.catalog import params_catalog


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


# =====================================================================
# build_network
# =====================================================================


def test_fa10_build_network_tolerance_matching():
    nodes = path_nodes()
    # Linienenden liegen 3m neben den echten Knoten - inn erhalb Toleranz
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "bc", "cd"]},
        geometry=[
            LineString([(0, 0), (103, 0)]),
            LineString([(97, 0), (200, 0)]),
            LineString([(200, 0), (303, 0)]),
        ],
        crs="EPSG:32633",
    )
    result = graph_module.run_build_network(
        {"lines": lines, "nodes": nodes}, {"tolerance_m": 5}
    )
    assert result.nx_graph.number_of_nodes() == 4
    assert result.nx_graph.number_of_edges() == 3


def test_fa10_build_network_outside_tolerance_drops_edge():
    nodes = path_nodes()
    lines = gpd.GeoDataFrame(
        {"id": ["ab"]},
        geometry=[LineString([(0, 0), (150, 0)])],  # 50m daneben, außerhalb Toleranz
        crs="EPSG:32633",
    )
    with pytest.warns(graph_module.DroppedLinePieceWarning, match="1 am Ende"):
        result = graph_module.run_build_network(
            {"lines": lines, "nodes": nodes}, {"tolerance_m": 5}
        )
    assert result.nx_graph.number_of_edges() == 0


def test_fa10_build_network_empty_lines():
    nodes = path_nodes()
    empty_lines = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    result = graph_module.run_build_network({"lines": empty_lines, "nodes": nodes}, {})
    assert result.nx_graph.number_of_nodes() == 4
    assert result.nx_graph.number_of_edges() == 0


def test_fa10_build_network_handles_multilinestring():
    # Reale OSM-Relationen liefern gelegentlich MultiLineString statt
    # LineString - .coords würde dafür eine NotImplementedError werfen.
    nodes = path_nodes()
    lines = gpd.GeoDataFrame(
        {"id": ["ab_multi"]},
        geometry=[MultiLineString([[(0, 0), (100, 0)]])],
        crs="EPSG:32633",
    )
    result = graph_module.run_build_network({"lines": lines, "nodes": nodes}, {})
    assert result.nx_graph.number_of_edges() == 1
    assert result.nx_graph.has_edge("A", "B")


def test_fa10_build_network_skips_non_linear_geometry():
    # PostgisBackend vereinigt Tabellen je Tag-Filter - dieselbe Tag-
    # Kombination kann daher gelegentlich eine Fläche statt einer Linie
    # liefern (real gegen Sachsen-Datensatz reproduziert: 2 MultiPolygon
    # unter power=line). Solche Objekte werden übersprungen, nicht die
    # ganze Operation abgebrochen.
    nodes = path_nodes()
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "stray_polygon"]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
        ],
        crs="EPSG:32633",
    )
    with pytest.warns(graph_module.NonLinearGeometryWarning):
        result = graph_module.run_build_network({"lines": lines, "nodes": nodes}, {})
    assert result.nx_graph.number_of_edges() == 1
    assert result.nx_graph.has_edge("A", "B")


def test_fa10_build_network_normalizes_polygon_nodes_to_centroid():
    # Reale OSM-Umspannwerke sind gelegentlich als eingezäunte Fläche
    # (Polygon) statt als Punkt gemappt. graph_to_vector muss trotzdem
    # eine Punkt-Geometrie liefern (Netzwerkmodell: Knoten = Punkt).
    nodes = gpd.GeoDataFrame(
        {"id": ["A", "B"]},
        geometry=[
            Polygon([(0, 0), (20, 0), (20, 20), (0, 20)]),  # Centroid (10, 10)
            Point(100, 0),
        ],
        crs="EPSG:32633",
    )
    lines = gpd.GeoDataFrame(
        {"id": ["ab"]}, geometry=[LineString([(10, 10), (100, 0)])], crs="EPSG:32633"
    )
    built = graph_module.run_build_network({"lines": lines, "nodes": nodes}, {})
    node_geom = built.nx_graph.nodes["A"]["geometry"]
    assert node_geom.geom_type == "Point"
    assert (node_geom.x, node_geom.y) == pytest.approx((10, 10))


# =====================================================================
# centrality (Betweenness, Bridges, Artikulationspunkte, Komponenten)
# =====================================================================


def test_fa10_centrality_on_known_graph():
    built = graph_module.run_build_network(
        {"lines": path_lines(), "nodes": path_nodes()}, {}
    )
    result = graph_module.run_centrality({"graph": built}, {})
    betweenness = dict(result.nx_graph.nodes(data="betweenness"))
    assert betweenness["A"] == pytest.approx(0.0)
    assert betweenness["B"] == pytest.approx(2 / 3)
    assert betweenness["C"] == pytest.approx(2 / 3)
    assert betweenness["D"] == pytest.approx(0.0)


def test_fa10_bridges_on_known_graph():
    built = graph_module.run_build_network(
        {"lines": path_lines(), "nodes": path_nodes()}, {}
    )
    result = graph_module.run_centrality({"graph": built}, {})
    # In einem Pfadgraphen ist JEDE Kante eine Brücke.
    for _, _, is_bridge in result.nx_graph.edges(data="is_bridge"):
        assert is_bridge is True
    articulation = dict(result.nx_graph.nodes(data="is_articulation_point"))
    assert articulation["B"] is True
    assert articulation["C"] is True
    assert articulation["A"] is False
    assert articulation["D"] is False


def test_fa10_centrality_single_node_graph():
    nodes = gpd.GeoDataFrame({"id": ["only"]}, geometry=[Point(0, 0)], crs="EPSG:32633")
    empty_lines = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    built = graph_module.run_build_network({"lines": empty_lines, "nodes": nodes}, {})
    result = graph_module.run_centrality({"graph": built}, {})
    assert result.nx_graph.nodes["only"]["betweenness"] == pytest.approx(0.0)


# =====================================================================
# graph_to_vector
# =====================================================================


def test_fa10_graph_to_vector_attributes():
    built = graph_module.run_build_network(
        {"lines": path_lines(), "nodes": path_nodes()}, {}
    )
    scored = graph_module.run_centrality({"graph": built}, {})
    result = graph_module.run_graph_to_vector({"graph": scored}, {})

    assert isinstance(result, gpd.GeoDataFrame)
    assert len(result) == 4
    assert "betweenness" in result.columns
    assert "is_articulation_point" in result.columns
    assert result.crs.to_epsg() == 32633
    row_b = result[result["id"] == "B"].iloc[0]
    assert row_b["betweenness"] == pytest.approx(2 / 3)


def test_fa10_graph_to_vector_empty_graph():
    empty_nodes = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    empty_lines = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    built = graph_module.run_build_network(
        {"lines": empty_lines, "nodes": empty_nodes}, {}
    )
    result = graph_module.run_graph_to_vector({"graph": built}, {})
    assert len(result) == 0


# =====================================================================
# FA10-Vorbedingung: getrennte Komponenten
# =====================================================================


def test_fa10_disconnected_lines_raise_or_warn():
    nodes = gpd.GeoDataFrame(
        {"id": ["A", "B", "C", "D"]},
        geometry=[Point(0, 0), Point(100, 0), Point(1000, 0), Point(1100, 0)],
        crs="EPSG:32633",
    )
    # zwei getrennte Paare: A-B und C-D, keine Verbindung dazwischen
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "cd"]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            LineString([(1000, 0), (1100, 0)]),
        ],
        crs="EPSG:32633",
    )
    with pytest.warns(graph_module.DisconnectedNetworkWarning):
        result = graph_module.run_build_network({"lines": lines, "nodes": nodes}, {})
    assert result.nx_graph.number_of_nodes() == 4


# =====================================================================
# FA10: centrality verändert den Eingabegraphen nicht
# =====================================================================


def test_fa10_centrality_does_not_mutate_its_input_graph():
    """Der Eingabegraph gehört einem anderen Knoten des DAG (im Store
    referenzierbar). centrality darf ihn nicht mit Metriken anreichern."""
    built = graph_module.run_build_network(
        {"lines": path_lines(), "nodes": path_nodes()}, {}
    )
    nodes_before = {n: dict(a) for n, a in built.nx_graph.nodes(data=True)}
    edges_before = {(u, v): dict(a) for u, v, a in built.nx_graph.edges(data=True)}

    result = graph_module.run_centrality({"graph": built}, {})

    assert result.nx_graph is not built.nx_graph
    assert {n: dict(a) for n, a in built.nx_graph.nodes(data=True)} == nodes_before
    assert {
        (u, v): dict(a) for u, v, a in built.nx_graph.edges(data=True)
    } == edges_before
    assert "betweenness" not in built.nx_graph.nodes["B"]
    assert "betweenness" in result.nx_graph.nodes["B"]


def test_fa10_two_centrality_steps_on_the_same_graph_are_independent():
    built = graph_module.run_build_network(
        {"lines": path_lines(), "nodes": path_nodes()}, {}
    )
    first = graph_module.run_centrality({"graph": built}, {})
    second = graph_module.run_centrality({"graph": built}, {})
    assert first.nx_graph is not second.nx_graph
    assert dict(first.nx_graph.nodes(data="betweenness")) == dict(
        second.nx_graph.nodes(data="betweenness")
    )


# =====================================================================
# FA10: Knotenschlüssel
# =====================================================================


def _star_lines(coords: list[tuple[float, float]]) -> gpd.GeoDataFrame:
    lines = [LineString([coords[0], target]) for target in coords[1:]]
    return gpd.GeoDataFrame(
        {"id": [f"l{i}" for i in range(len(lines))]}, geometry=lines, crs="EPSG:32633"
    )


def test_fa10_node_key_unique_id_column_is_used_without_warning():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = graph_module.run_build_network(
            {"lines": path_lines(), "nodes": path_nodes()}, {}
        )
    assert sorted(result.nx_graph.nodes) == ["A", "B", "C", "D"]


def test_fa10_node_key_duplicate_ids_fall_back_instead_of_merging():
    """Bugfix: doppelte 'id'-Werte (z. B. aus PostGIS-Daten) verschmolzen
    früher stumm mehrere Knoten zu einem."""
    nodes = gpd.GeoDataFrame(
        {"id": ["X", "X", "Y", "Y"]},
        geometry=[Point(0, 0), Point(100, 0), Point(200, 0), Point(300, 0)],
        crs="EPSG:32633",
    )
    with pytest.warns(graph_module.NodeKeyWarning, match="nicht eindeutig.*'X'"):
        result = graph_module.run_build_network(
            {"lines": path_lines(), "nodes": nodes}, {}
        )
    assert sorted(result.nx_graph.nodes) == [0, 1, 2, 3]  # Zeilenindex 0..3
    assert result.nx_graph.number_of_edges() == 3
    assert result.nx_graph.nodes[1]["id"] == "X"  # das Attribut bleibt erhalten


def test_fa10_node_key_empty_id_values_fall_back_to_the_row_index():
    nodes = gpd.GeoDataFrame(
        {"id": ["A", None, "C", "D"]},
        geometry=[Point(0, 0), Point(100, 0), Point(200, 0), Point(300, 0)],
        crs="EPSG:32633",
    )
    with pytest.warns(graph_module.NodeKeyWarning, match="1 leere Werte"):
        result = graph_module.run_build_network(
            {"lines": path_lines(), "nodes": nodes}, {}
        )
    assert sorted(result.nx_graph.nodes) == [0, 1, 2, 3]


def test_fa10_node_key_missing_id_column_uses_the_row_index_without_warning():
    """OSM-Daten tragen meist keine 'id'-Spalte: die Knoten heißen wie die
    Zeilen des Layers (Zeilenindex, wie bisher) - auch wenn Zeilen vorher
    entfernt wurden und der Index Lücken hat. Keine Warnung: nichts wird
    verschmolzen."""
    import warnings

    nodes = gpd.GeoDataFrame(
        {"name": ["a", "b", "c", "d"]},
        geometry=[Point(0, 0), Point(100, 0), Point(200, 0), Point(300, 0)],
        crs="EPSG:32633",
        index=[10, 20, 30, 40],
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = graph_module.run_build_network(
            {"lines": path_lines(), "nodes": nodes}, {}
        )
    assert sorted(result.nx_graph.nodes) == [10, 20, 30, 40]
    assert result.nx_graph.has_edge(10, 20) and result.nx_graph.has_edge(30, 40)


def test_fa10_node_key_duplicate_id_with_unique_index_falls_back_to_the_row_index():
    nodes = gpd.GeoDataFrame(
        {"id": ["X", "X", "Y", "Y"]},
        geometry=[Point(0, 0), Point(100, 0), Point(200, 0), Point(300, 0)],
        crs="EPSG:32633",
        index=[3, 4, 5, 6],
    )
    with pytest.warns(graph_module.NodeKeyWarning, match="Zeilenindex"):
        result = graph_module.run_build_network(
            {"lines": path_lines(), "nodes": nodes}, {}
        )
    assert sorted(result.nx_graph.nodes) == [3, 4, 5, 6]


def test_fa10_node_key_non_unique_row_index_falls_back_to_position_with_warning():
    """Ein nicht eindeutiger Zeilenindex (z. B. nach dem Zusammenfügen zweier
    Layer) hätte Knoten verschmolzen - dann gilt die Zeilenposition."""
    nodes = gpd.GeoDataFrame(
        {"name": ["a", "b", "c", "d"]},
        geometry=[Point(0, 0), Point(100, 0), Point(200, 0), Point(300, 0)],
        crs="EPSG:32633",
        index=[0, 1, 0, 1],
    )
    with pytest.warns(
        graph_module.NodeKeyWarning, match="Zeilenindex.*nicht eindeutig"
    ):
        result = graph_module.run_build_network(
            {"lines": path_lines(), "nodes": nodes}, {}
        )
    assert sorted(result.nx_graph.nodes) == [0, 1, 2, 3]
    assert result.nx_graph.number_of_edges() == 3


def test_fa10_node_key_param_selects_an_explicit_column():
    import warnings

    nodes = gpd.GeoDataFrame(
        {"id": ["X", "X", "Y", "Y"], "ref": ["r1", "r2", "r3", "r4"]},
        geometry=[Point(0, 0), Point(100, 0), Point(200, 0), Point(300, 0)],
        crs="EPSG:32633",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # ausdrückliche Wahl: keine Warnung
        result = graph_module.run_build_network(
            {"lines": path_lines(), "nodes": nodes}, {"node_key": "ref"}
        )
    assert sorted(result.nx_graph.nodes) == ["r1", "r2", "r3", "r4"]
    assert result.nx_graph.has_edge("r1", "r2")


def test_fa10_node_key_param_must_exist_and_be_unique():
    nodes = gpd.GeoDataFrame(
        {"id": ["X", "X", "Y", "Y"]},
        geometry=[Point(0, 0), Point(100, 0), Point(200, 0), Point(300, 0)],
        crs="EPSG:32633",
    )
    with pytest.raises(ValueError, match="node_key 'gibt_es_nicht' ist keine Spalte"):
        graph_module.run_build_network(
            {"lines": path_lines(), "nodes": nodes}, {"node_key": "gibt_es_nicht"}
        )
    with pytest.raises(ValueError, match="node_key 'id' ist nicht eindeutig"):
        graph_module.run_build_network(
            {"lines": path_lines(), "nodes": nodes}, {"node_key": "id"}
        )


def test_fa10_node_key_empty_node_layer_needs_no_key_and_no_warning():
    import warnings

    empty_nodes = gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:32633")
    empty_lines = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = graph_module.run_build_network(
            {"lines": empty_lines, "nodes": empty_nodes}, {}
        )
    assert result.nx_graph.number_of_nodes() == 0


def test_fa10_build_network_declares_node_key_in_its_contract():
    specs, _ = params_catalog(graph_module.BuildNetworkStep.Params)
    spec = specs["node_key"]
    assert spec["type"] == "string" and spec["required"] is False


# =====================================================================
# Kantengeometrie (FA10-Änderung für FA51/FA53)
# =====================================================================


def test_fa10_build_network_edges_carry_their_line_geometry():
    """Jede Kante trägt das Linienstück, aus dem sie entstand - neben 'length'."""
    result = graph_module.run_build_network(
        {"lines": path_lines(), "nodes": path_nodes()}, {}
    )
    for u, v, data in result.nx_graph.edges(data=True):
        geometry = data["geometry"]
        assert geometry.geom_type == "LineString"
        assert data["length"] == pytest.approx(geometry.length)
    assert result.nx_graph.edges["A", "B"]["geometry"].equals(
        LineString([(0, 0), (100, 0)])
    )


def test_fa10_build_network_multilinestring_part_becomes_one_edge_geometry_each():
    nodes = path_nodes()
    multi = gpd.GeoDataFrame(
        {"id": ["ab_cd"]},
        geometry=[MultiLineString([[(0, 0), (100, 0)], [(200, 0), (300, 0)]])],
        crs="EPSG:32633",
    )
    result = graph_module.run_build_network({"lines": multi, "nodes": nodes}, {})
    assert result.nx_graph.edges["A", "B"]["geometry"].equals(
        LineString([(0, 0), (100, 0)])
    )
    assert result.nx_graph.edges["C", "D"]["geometry"].equals(
        LineString([(200, 0), (300, 0)])
    )


def test_fa10_build_network_empty_layers_give_an_empty_graph_without_edges():
    empty_nodes = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    empty_lines = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")
    result = graph_module.run_build_network(
        {"lines": empty_lines, "nodes": empty_nodes}, {}
    )
    assert result.nx_graph.number_of_edges() == 0


def test_fa10_centrality_keeps_edge_geometry():
    graph = graph_module.run_build_network(
        {"lines": path_lines(), "nodes": path_nodes()}, {}
    )
    centred = graph_module.run_centrality({"graph": graph}, {})
    assert centred.nx_graph.edges["B", "C"]["geometry"].equals(
        LineString([(100, 0), (200, 0)])
    )
    assert centred.nx_graph.edges["B", "C"]["is_bridge"] is True


# =====================================================================
# Nachtreview 02.10.2026: parallele Linien, reservierte Spaltennamen
# =====================================================================


def _two_nodes() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": [1, 2]}, geometry=[Point(0, 0), Point(10, 0)], crs="EPSG:32633"
    )


def test_fa10_parallel_lines_between_the_same_nodes_are_reported():
    """Nachtreview 16: a second line between the same node pair overwrote the first
    silently (nx.Graph has one edge per pair). The shorter line is kept, with a warning."""
    lines = gpd.GeoDataFrame(
        {"name": ["y", "x"]},
        geometry=[LineString([(0, 0), (5, 5), (10, 0)]), LineString([(0, 0), (10, 0)])],
        crs="EPSG:32633",
    )
    with pytest.warns(graph_module.ParallelEdgeWarning, match="1 .*parallel"):
        graph = graph_module.run_build_network(
            {"lines": lines, "nodes": _two_nodes()}, {}
        )
    assert graph.nx_graph.number_of_edges() == 1
    assert graph.nx_graph.edges[1, 2]["name"] == "x"
    assert graph.nx_graph.edges[1, 2]["length"] == pytest.approx(10.0)


def test_fa10_parallel_longer_line_does_not_replace_the_shorter_one():
    lines = gpd.GeoDataFrame(
        {"name": ["x", "y"]},
        geometry=[LineString([(0, 0), (10, 0)]), LineString([(0, 0), (5, 5), (10, 0)])],
        crs="EPSG:32633",
    )
    with pytest.warns(graph_module.ParallelEdgeWarning):
        graph = graph_module.run_build_network(
            {"lines": lines, "nodes": _two_nodes()}, {}
        )
    assert graph.nx_graph.edges[1, 2]["name"] == "x"


def test_fa10_lines_with_a_length_column_do_not_crash():
    """Nachtreview 16 (related): a 'length' column (e.g. from op length) raised
    'got multiple values for keyword argument length'."""
    lines = gpd.GeoDataFrame(
        {"length": [99.0]}, geometry=[LineString([(0, 0), (10, 0)])], crs="EPSG:32633"
    )
    with pytest.warns(UserWarning, match="length"):
        graph = graph_module.run_build_network(
            {"lines": lines, "nodes": _two_nodes()}, {}
        )
    assert graph.nx_graph.edges[1, 2]["length"] == pytest.approx(10.0)


# =====================================================================
# Verworfene Linienstücke (Goldene Regel 7, 03.10.2026)
# =====================================================================


def test_fa10_dropped_line_pieces_are_reported_with_counts_per_reason():
    """A line piece whose start, end or both ends find no node within tolerance_m
    used to vanish silently; one warning now names the count per reason."""
    nodes = path_nodes()
    lines = gpd.GeoDataFrame(
        {"id": ["ab", "start", "end", "both"]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            LineString([(50, 50), (100, 0)]),
            LineString([(200, 0), (250, 50)]),
            LineString([(50, 50), (250, 50)]),
        ],
        crs="EPSG:32633",
    )
    with pytest.warns(graph_module.DroppedLinePieceWarning) as caught:
        graph = graph_module.run_build_network(
            {"lines": lines, "nodes": nodes}, {"tolerance_m": 5}
        )
    message = str(caught[0].message)
    assert "3 von 4 Linienstück(en)" in message
    assert "1 am Anfang, 1 am Ende, 1 an beiden Enden" in message
    assert graph.nx_graph.number_of_edges() == 1


def test_fa10_dropped_line_pieces_count_multilinestring_parts():
    """Each part of a MultiLineString is one line piece in the count."""
    lines = gpd.GeoDataFrame(
        geometry=[MultiLineString([[(0, 0), (100, 0)], [(500, 0), (600, 0)]])],
        crs="EPSG:32633",
    )
    with pytest.warns(
        graph_module.DroppedLinePieceWarning, match="1 von 2 Linienstück"
    ):
        graph = graph_module.run_build_network(
            {"lines": lines, "nodes": path_nodes()}, {"tolerance_m": 5}
        )
    assert graph.nx_graph.number_of_edges() == 1


def test_fa10_no_dropped_line_piece_warning_when_every_piece_becomes_an_edge(recwarn):
    graph_module.run_build_network({"lines": path_lines(), "nodes": path_nodes()}, {})
    assert not [
        w for w in recwarn if w.category is graph_module.DroppedLinePieceWarning
    ]


def test_fa10_self_loop_line_is_kept_and_not_reported_as_dropped(recwarn):
    """Unchanged behaviour: a piece with both ends on the same node stays a self-loop edge."""
    lines = gpd.GeoDataFrame(geometry=[LineString([(0, 0), (2, 0)])], crs="EPSG:32633")
    graph = graph_module.run_build_network(
        {"lines": lines, "nodes": path_nodes()}, {"tolerance_m": 5}
    )
    assert graph.nx_graph.number_of_edges() == 1
    assert not [
        w for w in recwarn if w.category is graph_module.DroppedLinePieceWarning
    ]
