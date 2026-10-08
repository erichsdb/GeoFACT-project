"""Implements: FA8 (spatial_join, nearest_distance: Eingänge aus einem früheren Join).

Regression zur LLM-Vorstudie (03.10.2026): spatial_join hinterlässt die Spalte
``index_right``; ein folgendes nearest_distance (oder ein zweiter spatial_join)
brach mit "'index_right' cannot be a column name in the frames being joined"
ab, einem GeoPandas-Fehler ohne Bezug zur Konfiguration.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from geofact.builtin.operations.nearest_distance import run_nearest_distance
from geofact.builtin.operations.spatial_join import run_spatial_join

CRS = "EPSG:32633"


def _zones() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"zone": ["a", "b"]},
        geometry=[
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]),
        ],
        crs=CRS,
    )


def _points() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["p1", "p2", "p3"]},
        geometry=[Point(1, 1), Point(25, 5), Point(50, 50)],
        crs=CRS,
    )


def _hospitals() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"kind": ["h"]}, geometry=[Point(1, 4)], crs=CRS)


def test_fa08_nearest_distance_after_spatial_join_does_not_crash():
    joined = run_spatial_join(
        {"left": _points(), "right": _zones()}, {"predicate": "within"}
    )
    assert "index_right" in joined.columns

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # kein Umbenennen nötig, keine Warnung
        result = run_nearest_distance({"from": joined, "to": _hospitals()}, {})

    assert list(result["distance"]) == pytest.approx(
        [3.0, (24**2 + 1) ** 0.5, (49**2 + 46**2) ** 0.5]
    )
    # alle Spalten der Eingabe bleiben unverändert erhalten
    assert list(result.columns) == [*joined.columns, "distance"]
    assert result["index_right"].equals(joined["index_right"])


def test_fa08_nearest_distance_target_with_index_right_does_not_crash():
    targets = run_spatial_join(
        {"left": _hospitals(), "right": _zones()}, {"predicate": "within"}
    )
    result = run_nearest_distance({"from": _points(), "to": targets}, {})
    assert result["distance"].iloc[0] == pytest.approx(3.0)
    assert "index_right" not in result.columns


def test_fa08_second_spatial_join_keeps_the_first_index_column_with_a_warning():
    first = run_spatial_join(
        {"left": _points(), "right": _zones()}, {"predicate": "within"}
    )
    districts = gpd.GeoDataFrame(
        {"district": ["west"]},
        geometry=[Polygon([(0, 0), (40, 0), (40, 40), (0, 40)])],
        crs=CRS,
    )

    with pytest.warns(UserWarning, match="'index_right' -> 'index_right_1'"):
        second = run_spatial_join(
            {"left": first, "right": districts}, {"predicate": "within"}
        )

    assert second["index_right_1"].equals(first["index_right"])
    assert second["district"].tolist()[:2] == ["west", "west"]
    assert second["index_right"].tolist()[:2] == [0, 0]


def test_fa08_spatial_join_without_index_columns_is_unchanged():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = run_spatial_join(
            {"left": _points(), "right": _zones()}, {"predicate": "within"}
        )
    assert list(result.columns) == ["name", "geometry", "index_right", "zone"]


def test_fa08_join_index_columns_on_an_empty_layer():
    empty = run_spatial_join(
        {"left": _points(), "right": _zones()}, {"predicate": "within"}
    ).iloc[0:0]
    result = run_nearest_distance({"from": empty, "to": _hospitals()}, {})
    assert len(result) == 0 and "distance" in result.columns
