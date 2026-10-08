"""Implements: FA8 (Zusatz: stille Faelle in Attributoperationen melden).

Contract tests: ``filter`` with ``on_missing: exclude`` counts and reports the
objects it drops, ``aggregate`` reports features that fall into no zone and
features that are counted in several zones (``predicate: intersects``) -
nothing disappears or doubles silently (Golden Rule 7).
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

from geofact.builtin.operations.aggregate import run_aggregate
from geofact.builtin.operations.filter import run_filter

CRS = "EPSG:25833"


def points(values) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"score": values}, geometry=[Point(i, i) for i in range(len(values))], crs=CRS
    )


def two_zones() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["west", "ost"]},
        geometry=[box(0, 0, 10, 10), box(10, 0, 20, 10)],
        crs=CRS,
    )


def _messages(record) -> list[str]:
    return [str(w.message) for w in record]


def test_fa08_filter_exclude_reports_dropped_count():
    gdf = points([0.9, None, 0.7, None])
    with pytest.warns(UserWarning) as record:
        result = run_filter({"features": gdf}, {"condition": "score > 0.5"})
    assert len(result) == 2
    dropped = [m for m in _messages(record) if "on_missing: exclude" in m]
    assert len(dropped) == 1
    assert "2 von 4 Objekt(en)" in dropped[0] and "'score'" in dropped[0]


def test_fa08_filter_exclude_empty_column_reports_everything_dropped():
    gdf = points([None, None, None])
    with pytest.warns(UserWarning) as record:
        result = run_filter({"features": gdf}, {"condition": "score > 0.5"})
    assert result.empty
    dropped = [m for m in _messages(record) if "on_missing: exclude" in m]
    assert len(dropped) == 1 and "3 von 3 Objekt(en)" in dropped[0]
    assert "alle" in dropped[0]


def test_fa08_filter_exclude_no_missing_no_warning():
    gdf = points([0.9, 0.1])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = run_filter({"features": gdf}, {"condition": "score > 0.5"})
    assert len(result) == 1


def test_fa08_aggregate_reports_unassigned_features():
    features = gpd.GeoDataFrame(
        {"kind": ["a", "b", "c"]},
        geometry=[Point(5, 5), Point(15, 5), Point(50, 50)],
        crs=CRS,
    )
    with pytest.warns(UserWarning) as record:
        result = run_aggregate({"features": features, "zones": two_zones()}, {})
    assert list(result["count"]) == [1, 1]
    unassigned = [m for m in _messages(record) if "keiner Zone" in m]
    assert len(unassigned) == 1 and "1 von 3 Objekt(en)" in unassigned[0]


def test_fa08_aggregate_reports_multi_zone_features_with_intersects():
    crossing = LineString([(5, 5), (15, 5)])
    features = gpd.GeoDataFrame({"kind": ["x"]}, geometry=[crossing], crs=CRS)
    with pytest.warns(UserWarning) as record:
        result = run_aggregate(
            {"features": features, "zones": two_zones()}, {"predicate": "intersects"}
        )
    assert list(result["count"]) == [1, 1]
    multi = [m for m in _messages(record) if "mehreren Zonen" in m]
    assert len(multi) == 1 and "1 Objekt(e)" in multi[0] and "within" in multi[0]


def test_fa08_aggregate_within_does_not_double_count():
    crossing = LineString([(5, 5), (15, 5)])
    inside = LineString([(1, 1), (3, 3)])
    features = gpd.GeoDataFrame(
        {"kind": ["x", "y"]}, geometry=[crossing, inside], crs=CRS
    )
    with pytest.warns(UserWarning) as record:
        result = run_aggregate(
            {"features": features, "zones": two_zones()}, {"predicate": "within"}
        )
    assert list(result["count"]) == [1, 0]
    messages = _messages(record)
    assert not [m for m in messages if "mehreren Zonen" in m]
    # the crossing line lies within no zone - reported, not silently lost
    assert [m for m in messages if "keiner Zone" in m and "1 von 2" in m]


def test_fa08_aggregate_all_assigned_once_no_warning():
    features = gpd.GeoDataFrame({"kind": ["a"]}, geometry=[Point(5, 5)], crs=CRS)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = run_aggregate({"features": features, "zones": two_zones()}, {})
    assert list(result["count"]) == [1, 0]
