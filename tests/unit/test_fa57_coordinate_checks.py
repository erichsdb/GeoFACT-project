"""Implements: FA57 (Typen/Helfer: Koordinatenplausibilität, nicht endliche Stützpunkte).

Contract-Tests für ``core/crs.py::coordinate_plausibility`` und
``nonfinite_vertices`` - die Engine-Seite (Harmonisierer, Executor) folgt in
Welle 2 (W2-01/W2-02)."""

from __future__ import annotations

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import LineString, Point

from geofact.core.crs import coordinate_plausibility, nonfinite_vertices


def test_fa57_metric_coordinates_under_geographic_crs_are_an_error():
    errors, warnings = coordinate_plausibility(
        (300000, 5600000, 310000, 5610000), CRS.from_epsg(4326)
    )
    assert len(errors) == 1 and warnings == []
    assert "Koordinaten passen nicht zu EPSG:4326" in errors[0]
    assert "crs_override" in errors[0]


def test_fa57_plausible_geographic_coordinates_pass():
    assert coordinate_plausibility((12.2, 51.2, 12.5, 51.4), CRS.from_epsg(4326)) == (
        [],
        [],
    )


def test_fa57_projected_crs_outside_area_of_use_warns():
    # Leipzig in UTM 33N passt, dieselben Zahlen als Gauss-Krueger Zone 2 nicht
    assert coordinate_plausibility(
        (300000, 5680000, 330000, 5700000), CRS.from_epsg(25833)
    ) == ([], [])
    errors, warnings = coordinate_plausibility(
        (300000, 5680000, 330000, 5700000), CRS.from_epsg(31466)
    )
    assert errors == []
    assert (
        len(warnings) == 1
        and "außerhalb des Gültigkeitsbereichs von EPSG:31466" in warnings[0]
    )


def test_fa57_empty_layer_and_missing_crs_pass_all_checks():
    nan = float("nan")
    assert coordinate_plausibility((nan, nan, nan, nan), CRS.from_epsg(4326)) == (
        [],
        [],
    )
    assert coordinate_plausibility(None, CRS.from_epsg(4326)) == ([], [])
    assert coordinate_plausibility((0, 0, 1, 1), None) == ([], [])
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    assert nonfinite_vertices(empty) == (0, [])


def test_fa57_nonfinite_vertices_counts_and_names_rows():
    inf = float("inf")
    gdf = gpd.GeoDataFrame(
        geometry=[
            Point(1, 1),
            LineString([(0, 0), (inf, 1), (2, inf)]),
            Point(2, 2),
            Point(inf, 0),
        ],
        crs="EPSG:3035",
        index=[10, 11, 12, 13],
    )
    count, rows = nonfinite_vertices(gdf)
    assert count == 3
    assert rows == [1, 3]  # 0-basierte Zeilennummern, nicht der Index


def test_fa57_nonfinite_vertices_reports_at_most_ten_rows():
    inf = float("inf")
    gdf = gpd.GeoDataFrame(geometry=[Point(inf, 0)] * 15, crs="EPSG:3035")
    count, rows = nonfinite_vertices(gdf)
    assert count == 15
    assert rows == list(range(10))
