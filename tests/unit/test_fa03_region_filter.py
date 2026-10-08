"""Implements: FA3 (gemeinsamer Regionsfilter builtin/_region_filter.py::within_region).

Ein Prädikat für osm, wfs, ckan, gtfs und rest: ``intersects`` gegen die
vereinigte Region (Randpunkte zählen), Punkte über ``shapely.intersects_xy``,
der Rest gegen die vorbereitete (``shapely.prepare``) Region.
"""

from __future__ import annotations

import ast
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

import geofact.builtin.sources as sources_pkg
from geofact.builtin._region_filter import within_region

L_SHAPE = Polygon([(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)])
REGION = gpd.GeoDataFrame(geometry=[L_SHAPE], crs="EPSG:4326")
CALLERS = ("osm", "wfs", "ckan", "gtfs", "rest")


def test_fa03_region_filter_matches_the_previous_intersects_predicate():
    data = gpd.GeoDataFrame(
        {"n": list(range(7))},
        geometry=[
            Point(0.5, 0.5),  # innen
            Point(1.5, 1.5),  # im Ausschnitt des L -> raus
            Point(2, 0.5),  # auf dem Rand -> zählt
            LineString([(1.5, 1.5), (1.5, 0.5)]),  # schneidet -> drin
            LineString([(3, 3), (4, 4)]),  # außen
            Polygon([(1.2, 1.2), (1.8, 1.2), (1.8, 1.8)]),  # im Ausschnitt
            None,  # fehlende Geometrie -> raus
        ],
        crs="EPSG:4326",
    )
    expected = data[data.intersects(REGION.union_all())].reset_index(drop=True)
    result = within_region(data, REGION)
    assert result["n"].tolist() == expected["n"].tolist() == [0, 2, 3]
    assert list(result.index) == [0, 1, 2]


def test_fa03_region_filter_multi_part_region_is_unioned():
    region = gpd.GeoDataFrame(
        geometry=[Polygon([(0, 0), (1, 0), (1, 1)]), Polygon([(5, 5), (6, 5), (6, 6)])],
        crs="EPSG:4326",
    )
    data = gpd.GeoDataFrame(
        geometry=[Point(0.9, 0.1), Point(5.9, 5.1), Point(3, 3)], crs="EPSG:4326"
    )
    assert len(within_region(data, region)) == 2


def test_fa03_region_filter_empty_input_returns_empty():
    empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    assert within_region(empty, REGION).empty


def test_fa03_region_filter_rejects_crs_mismatch():
    data = gpd.GeoDataFrame(geometry=[Point(0.5, 0.5)], crs="EPSG:3857")
    with pytest.raises(ValueError, match="CRS"):
        within_region(data, REGION)


def test_fa03_region_filter_has_no_second_copy_in_the_connectors():
    """AST-Prüfung: kein Konnektor definiert eine eigene Regionsverfeinerung
    oder ruft ``.intersects(...)`` gegen die Region selbst auf; alle fünf
    Aufrufer importieren within_region."""
    root = Path(sources_pkg.__file__).parent
    for name in CALLERS:
        tree = ast.parse((root / f"{name}.py").read_text(encoding="utf-8"))
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "_refine_to_region" not in defined, name
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "intersects"
        ]
        assert calls == [], f"{name}.py ruft .intersects() selbst auf"
        imported = {
            alias.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
            and n.module == "geofact.builtin._region_filter"
            for alias in n.names
        }
        assert "within_region" in imported, name
