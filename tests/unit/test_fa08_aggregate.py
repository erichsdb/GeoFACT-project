"""Implements: FA8 (räumliche Operationen, Erweiterung aggregate).

Contract-Tests für src/geofact/builtin/operations/aggregate.py: Happy Path,
Typfehler-Fall, Randfall leerer Layer (Testregel für Operationen) plus
Schema-Validierung des AggregateStep.
"""

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from geofact.builtin.operations import aggregate
from geofact.builtin.operations.aggregate import AggregateStep


def zones() -> gpd.GeoDataFrame:
    """Zwei quadratische Zonen nebeneinander: A = (0..10, 0..10),
    B = (10..20, 0..10)."""
    return gpd.GeoDataFrame(
        {"name": ["A", "B"]},
        geometry=[
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            Polygon([(10, 0), (20, 0), (20, 10), (10, 10)]),
        ],
        crs="EPSG:32633",
    )


def doctors() -> gpd.GeoDataFrame:
    """Drei Aerzte in Zone A (2x Hausarzt, 1x Zahnarzt), keiner in B."""
    return gpd.GeoDataFrame(
        {
            "speciality": ["general", "general", "dentist"],
            "staff": [2.0, 4.0, 6.0],
        },
        geometry=[Point(1, 1), Point(2, 2), Point(3, 3)],
        crs="EPSG:32633",
    )


def empty_points() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"speciality": [], "staff": []}, geometry=[], crs="EPSG:32633"
    )


# =====================================================================
# Happy Path
# =====================================================================


def test_fa08_aggregate_count_happy_path():
    result = aggregate.run_aggregate({"features": doctors(), "zones": zones()}, {})
    assert list(result["count"]) == [3, 0]
    # Nachbedingung: Zonen-Zeilen bleiben erhalten, Lücke ist explizit 0.
    assert list(result["name"]) == ["A", "B"]


def test_fa08_aggregate_group_by_creates_category_columns():
    result = aggregate.run_aggregate(
        {"features": doctors(), "zones": zones()},
        {"statistic": "count", "group_by": "speciality"},
    )
    assert list(result["count_general"]) == [2, 0]
    assert list(result["count_dentist"]) == [1, 0]
    assert list(result["count"]) == [3, 0]


def test_fa08_aggregate_sum_and_mean():
    summed = aggregate.run_aggregate(
        {"features": doctors(), "zones": zones()},
        {"statistic": "sum", "value_field": "staff"},
    )
    assert list(summed["sum_staff"]) == [12.0, 0.0]

    mean = aggregate.run_aggregate(
        {"features": doctors(), "zones": zones()},
        {"statistic": "mean", "value_field": "staff"},
    )
    assert mean["mean_staff"].iloc[0] == pytest.approx(4.0)
    # mean ohne Objekte ist undefiniert -> NaN, nicht 0.
    assert np.isnan(mean["mean_staff"].iloc[1])


# =====================================================================
# Typfehler-Fälle
# =====================================================================


def test_fa08_aggregate_missing_value_field_raises():
    with pytest.raises(ValueError, match="value_field"):
        aggregate.run_aggregate(
            {"features": doctors(), "zones": zones()},
            {"statistic": "sum", "value_field": "gibts_nicht"},
        )


def test_fa08_aggregate_missing_group_by_raises():
    with pytest.raises(ValueError, match="group_by"):
        aggregate.run_aggregate(
            {"features": doctors(), "zones": zones()},
            {"statistic": "count", "group_by": "gibts_nicht"},
        )


def test_fa08_aggregate_step_rejects_sum_without_value_field():
    with pytest.raises(ValueError, match="value_field"):
        AggregateStep(
            id="s",
            op="aggregate",
            inputs={"features": "a", "zones": "b"},
            params={"statistic": "sum"},
        )


def test_fa08_aggregate_step_rejects_group_by_with_sum():
    with pytest.raises(ValueError, match="group_by"):
        AggregateStep(
            id="s",
            op="aggregate",
            inputs={"features": "a", "zones": "b"},
            params={
                "statistic": "sum",
                "value_field": "staff",
                "group_by": "speciality",
            },
        )


# =====================================================================
# Randfall leerer Layer
# =====================================================================


def test_fa08_aggregate_empty_features_yields_zero_counts():
    result = aggregate.run_aggregate({"features": empty_points(), "zones": zones()}, {})
    assert list(result["count"]) == [0, 0]


def test_fa08_aggregate_empty_zones_returns_empty():
    result = aggregate.run_aggregate(
        {"features": doctors(), "zones": zones().iloc[0:0]}, {}
    )
    assert len(result) == 0
