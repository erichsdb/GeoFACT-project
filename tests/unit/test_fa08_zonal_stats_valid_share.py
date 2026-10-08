"""Implements: FA8 (zonal_stats: Nenner-Quote je Zone - valid_share_field, min_valid_share).

The statistic of a zone rests on its VALID cells; when part of the zone is nodata
or lies outside the raster, the denominator is smaller than the zone. `zonal_stats`
(builtin/operations/zonal_stats.py) makes this visible: `valid_share_field` writes
the share of the zone's cells that carry a value, ONE `RasterValidShareWarning`
names the zones below 1.0, and `min_valid_share` turns a statistic resting on too
few cells into NaN instead of a value that looks complete.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import numpy as np
import pytest
from affine import Affine
from pyproj import CRS
from shapely.geometry import box

from geofact.builtin.operations.zonal_stats import (
    RasterValidShareWarning,
    ZonalStatsStep,
    run_zonal_stats,
    statistic_for_geometry,
)
from geofact.core.types import RasterLayer

TRANSFORM = Affine(10, 0, 0, 0, -10, 100)  # 10 x 10 cells of 10 m: x 0-100, y 0-100
UTM33 = CRS.from_epsg(32633)
LEFT = box(0, 0, 50, 100)  # columns 0-4
RIGHT = box(50, 0, 100, 100)  # columns 5-9
HALF_OUTSIDE = box(50, 0, 150, 100)  # columns 5-9 inside, 5 more outside


def _raster() -> RasterLayer:
    data = np.ones((10, 10), dtype="float64")
    data[:5, 5:] = -1  # top half of the right side is nodata
    return RasterLayer(data=data, transform=TRANSFORM, crs=UTM33, nodata=-1)


def _zones(*geometries) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": [f"z{i}" for i in range(len(geometries))]},
        geometry=list(geometries),
        crs=UTM33,
    )


def _run(zones, **params):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_zonal_stats({"zones": zones, "values": _raster()}, params)
    return result, [
        w for w in caught if issubclass(w.category, RasterValidShareWarning)
    ]


def test_fa08_zonal_stats_valid_share_field_counts_nodata_and_cells_outside_the_raster():
    result, caught = _run(
        _zones(LEFT, RIGHT, HALF_OUTSIDE),
        stat="sum",
        valid_share_field="anteil_gueltig",
    )
    assert result["anteil_gueltig"].tolist() == pytest.approx([1.0, 0.5, 0.25])
    assert result["sum_value"].tolist() == [
        50.0,
        25.0,
        25.0,
    ]  # values unchanged (refactoring)
    assert len(caught) == 1
    message = str(caught[0].message)
    assert "2 von 3 Zonen" in message and "0.25" in message
    assert ZonalStatsStep(
        id="s",
        op="zonal_stats",
        inputs={"zones": "a", "values": "b"},
        params={"valid_share_field": "anteil_gueltig"},
    ).produced_fields() == {"sum_value", "anteil_gueltig"}


def test_fa08_zonal_stats_min_valid_share_turns_thin_statistics_into_nan():
    result, caught = _run(
        _zones(LEFT, RIGHT, HALF_OUTSIDE), stat="mean", decimals=2, min_valid_share=0.5
    )
    values = result["mean_value"].tolist()
    assert values[0] == 1.0 and values[1] == 1.0 and np.isnan(values[2])
    assert len(caught) == 1 and "min_valid_share 0.5" in str(caught[0].message)
    assert "1 Zone" in str(caught[0].message)


def test_fa08_zonal_stats_complete_zones_raise_no_share_warning_and_params_are_checked():
    result, caught = _run(_zones(LEFT), stat="sum")
    assert caught == [] and "valid_share" not in " ".join(result.columns)
    value, share = statistic_for_geometry(RIGHT, _raster(), "sum", 1.0)
    assert (value, share) == (25.0, 0.5)
    assert (
        statistic_for_geometry(box(500, 500, 600, 600), _raster(), "sum", 1.0)[1] == 0.0
    )
    with pytest.raises(ValueError):
        ZonalStatsStep.Params(min_valid_share=1.5)
    with pytest.raises(ValueError, match="output_field"):
        ZonalStatsStep.Params(output_field="x", valid_share_field="x")
