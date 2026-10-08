"""Implements: FA8 (zonal_stats: min/count/std/share, output_field, band), FA47 (Anteil je Zone).

Contract tests of the extended `zonal_stats` (builtin/operations/zonal_stats.py):
the new statistics, the result column name, band selection on multi-band rasters
and the handling of zones without a value (outside the raster, only nodata,
empty geometry): NaN plus ONE aggregated warning instead of a crash
("width and height must be > 0" for a zone completely outside the raster).
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import numpy as np
import pytest
from affine import Affine
from pyproj import CRS
from shapely.geometry import Polygon, box

from geofact import api
from geofact.builtin.operations.zonal_stats import (
    RasterCoverageWarning,
    ZonalStatsStep,
    run_zonal_stats,
)
from geofact.core.types import RasterLayer

TRANSFORM = Affine(
    0.008, 0, 13.70, 0, -0.004, 51.07
)  # 10 x 10 cells: x 13.70-13.78, y 51.03-51.07
WGS84 = CRS.from_epsg(4326)
WHOLE = box(13.70, 51.03, 13.78, 51.07)
OUTSIDE = box(14.5, 52.0, 14.6, 52.1)


def _raster(data, **fields) -> RasterLayer:
    return RasterLayer(data=np.asarray(data), transform=TRANSFORM, crs=WGS84, **fields)


def _zones(*geometries, **columns) -> gpd.GeoDataFrame:
    columns = {"id": [f"z{i}" for i in range(len(geometries))], **columns}
    return gpd.GeoDataFrame(columns, geometry=list(geometries), crs="EPSG:4326")


def _run(zones, raster, **params):
    return run_zonal_stats({"zones": zones, "values": raster}, params)


def _caught(function):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = function()
    return result, [w for w in caught if issubclass(w.category, RasterCoverageWarning)]


RAMP = np.arange(100, dtype="float64").reshape(10, 10)  # 0 .. 99


# =====================================================================
# New statistics
# =====================================================================


@pytest.mark.parametrize(
    "stat, expected",
    [
        ("sum", 4950.0),
        ("mean", 49.5),
        ("max", 99.0),
        ("min", 0.0),
        ("count", 100.0),
    ],
)
def test_fa08_zonal_stats_known_values_of_every_statistic(stat, expected):
    result = _run(_zones(WHOLE), _raster(RAMP), stat=stat, decimals=3)
    assert result[f"{stat}_value"].iloc[0] == pytest.approx(expected)


def test_fa08_zonal_stats_std_is_the_population_standard_deviation():
    result = _run(_zones(WHOLE), _raster(RAMP), stat="std", decimals=4)
    assert result["std_value"].iloc[0] == pytest.approx(np.std(RAMP), abs=1e-4)


def test_fa08_zonal_stats_count_counts_valid_cells_only():
    data = RAMP.copy()
    data[:, 5:] = -200.0
    result = _run(_zones(WHOLE), _raster(data, nodata=-200.0), stat="count")
    assert result["count_value"].iloc[0] == 50


def test_fa08_zonal_stats_share_of_a_zero_one_raster_is_the_mean():
    data = np.zeros((10, 10), dtype="uint8")
    data[:, :3] = 1  # 30 of 100 cells
    result = _run(_zones(WHOLE), _raster(data), stat="share")
    assert result["share_value"].iloc[0] == pytest.approx(0.3)


def test_fa08_zonal_stats_share_ignores_nodata_cells():
    """share = share of the VALID cells: 20 nodata cells (255) are neither green nor not-green."""
    data = np.zeros((10, 10), dtype="uint8")
    data[:, :3] = 1  # 30 green
    data[:2, :] = 255  # 20 nodata cells, 6 of them in the green columns
    result = _run(_zones(WHOLE), _raster(data, nodata=255), stat="share", decimals=4)
    assert result["share_value"].iloc[0] == pytest.approx(24 / 80)


def test_fa08_zonal_stats_share_of_a_class_value():
    data = np.tile(np.array([1, 1, 1, 2, 2, 2, 3, 3, 3, 3], dtype="uint8"), (10, 1))
    result = _run(_zones(WHOLE), _raster(data), stat="share", class_value=3, decimals=2)
    assert result["share_value"].iloc[0] == pytest.approx(0.4)


def test_fa08_zonal_stats_share_defaults_to_three_decimals_not_zero_or_one():
    """Without `decimals` the default 0 would round every share to 0 or 1."""
    data = np.zeros((10, 10), dtype="uint8")
    data[:, :3] = 1
    value = _run(_zones(WHOLE), _raster(data), stat="share")["share_value"].iloc[0]
    assert value == 0.3
    assert ZonalStatsStep.Params(stat="share").decimals == 3
    assert ZonalStatsStep.Params(stat="share", decimals=1).decimals == 1
    assert (
        ZonalStatsStep.Params(stat="mean").decimals == 0
    )  # other statistics unchanged
    assert ZonalStatsStep.Params().stat == "sum"


def test_fa08_zonal_stats_nan_nodata_cells_are_excluded():
    data = RAMP.copy()
    data[:, 5:] = np.nan
    result = _run(
        _zones(WHOLE), _raster(data, nodata=float("nan")), stat="mean", decimals=3
    )
    assert result["mean_value"].iloc[0] == pytest.approx(np.mean(RAMP[:, :5]))


def test_fa08_zonal_stats_class_value_only_for_share():
    with pytest.raises(ValueError, match="class_value gilt nur für stat: share"):
        ZonalStatsStep.Params(stat="mean", class_value=1)
    assert ZonalStatsStep.Params(stat="share", class_value=2).class_value == 2


def test_fa08_zonal_stats_unknown_stat_is_rejected():
    with pytest.raises(ValueError):
        ZonalStatsStep.Params(stat="median")


# =====================================================================
# Result column
# =====================================================================


def test_fa08_zonal_stats_default_column_name_is_unchanged():
    result = _run(_zones(WHOLE), _raster(RAMP), stat="max")
    assert "max_value" in result.columns


def test_fa08_zonal_stats_output_field_names_the_result_column():
    result = _run(
        _zones(WHOLE), _raster(RAMP), stat="mean", output_field="mittel", decimals=1
    )
    assert "mittel" in result.columns and "mean_value" not in result.columns
    assert result["mittel"].iloc[0] == 49.5


def test_fa08_zonal_stats_output_field_must_not_overwrite_a_column():
    with pytest.raises(ValueError, match="output_field 'name'.*schon eine Spalte"):
        _run(_zones(WHOLE, name=["a"]), _raster(RAMP), stat="sum", output_field="name")


def test_fa08_zonal_stats_declares_its_produced_field_for_the_web_view():
    step = ZonalStatsStep(
        id="z",
        op="zonal_stats",
        inputs={"zones": "a", "values": "b"},
        params={"stat": "share", "output_field": "gruenanteil"},
    )
    assert step.produced_fields() == {"gruenanteil"}
    default = ZonalStatsStep(
        id="z",
        op="zonal_stats",
        inputs={"zones": "a", "values": "b"},
        params={"stat": "min"},
    )
    assert default.produced_fields() == {"min_value"}


def test_fa08_zonal_stats_existing_params_are_unchanged():
    step = ZonalStatsStep(
        id="z",
        op="zonal_stats",
        inputs={"zones": "a", "values": "b"},
        params={"stat": "sum"},
    )
    assert step.params == {
        "stat": "sum",
        "decimals": 0,
        "output_field": None,
        "band": None,
        "class_value": None,
        # FA8 Nenner-Quote (W1-05): new params, defaults keep the old behaviour
        "valid_share_field": None,
        "min_valid_share": 0.0,
    }
    entry = {e["op"]: e for e in api.operation_catalog()}["zonal_stats"]["params"]
    assert entry["stat"]["choices"][:3] == ["sum", "mean", "max"]
    assert set(entry["stat"]["choices"]) == {
        "sum",
        "mean",
        "max",
        "min",
        "count",
        "std",
        "share",
    }
    assert entry["stat"]["default"] == "sum" and entry["decimals"]["default"] == 0


# =====================================================================
# Band selection
# =====================================================================


def _multi_band() -> RasterLayer:
    data = np.stack([np.full((10, 10), 1.0), np.full((10, 10), 5.0)])
    return RasterLayer(
        data=data, transform=TRANSFORM, crs=WGS84, band_names=["red", "nir"]
    )


def test_fa08_zonal_stats_band_by_name_or_number():
    by_name = _run(_zones(WHOLE), _multi_band(), stat="mean", band="nir")
    by_number = _run(_zones(WHOLE), _multi_band(), stat="mean", band=1)
    assert (
        by_name["mean_value"].iloc[0] == 5.0 and by_number["mean_value"].iloc[0] == 1.0
    )


def test_fa08_zonal_stats_multi_band_raster_without_band_is_an_error_naming_the_bands():
    with pytest.raises(ValueError, match=r"2 Bänder \['red', 'nir'\].*'band'"):
        _run(_zones(WHOLE), _multi_band(), stat="mean")


def test_fa08_zonal_stats_unknown_band_is_an_error_naming_the_bands():
    with pytest.raises(ValueError, match=r"Band 'swir'.*\['red', 'nir'\]"):
        _run(_zones(WHOLE), _multi_band(), stat="mean", band="swir")


def test_fa08_zonal_stats_single_band_raster_needs_no_band():
    assert _run(_zones(WHOLE), _raster(RAMP), stat="max")["max_value"].iloc[0] == 99


# =====================================================================
# Zones without a value: NaN plus ONE aggregated warning
# =====================================================================


def test_fa08_zonal_stats_zone_completely_outside_the_raster_is_nan_with_a_warning():
    """Was a crash ("width and height must be > 0") found in the safety net."""
    result, caught = _caught(
        lambda: _run(_zones(WHOLE, OUTSIDE), _raster(RAMP), stat="sum")
    )
    assert result["sum_value"].iloc[0] == 4950 and np.isnan(result["sum_value"].iloc[1])
    assert len(caught) == 1
    message = str(caught[0].message)
    assert (
        "1 von 2 Zonen" in message
        and "außerhalb des Rasters" in message
        and "[1]" in message
    )


def test_fa08_zonal_stats_many_outside_zones_give_one_warning_with_the_first_indices():
    outside = [box(20 + i, 40, 20.5 + i, 40.5) for i in range(8)]
    result, caught = _caught(
        lambda: _run(_zones(WHOLE, *outside), _raster(RAMP), stat="mean")
    )
    assert result["mean_value"].isna().sum() == 8
    assert len(caught) == 1
    message = str(caught[0].message)
    assert "8 von 9 Zonen" in message and "[1, 2, 3, 4, 5, ...]" in message


def test_fa08_zonal_stats_outside_zones_in_every_direction_do_not_crash():
    around = [
        box(13.70, 51.10, 13.78, 51.20),  # north
        box(13.70, 50.90, 13.78, 51.00),  # south
        box(13.60, 51.03, 13.68, 51.07),  # west
        box(13.80, 51.03, 13.90, 51.07),  # east
    ]
    result, caught = _caught(lambda: _run(_zones(*around), _raster(RAMP), stat="sum"))
    assert result["sum_value"].isna().all() and len(caught) == 1


def test_fa08_zonal_stats_zone_partly_outside_uses_the_overlap_without_warning():
    half = box(13.74, 51.03, 13.90, 51.07)  # right half of the raster + beyond
    result, caught = _caught(lambda: _run(_zones(half), _raster(RAMP), stat="count"))
    assert result["count_value"].iloc[0] == 50 and caught == []


def test_fa08_zonal_stats_zone_with_only_nodata_cells_is_nan_and_reported():
    data = np.full((10, 10), 7.0)
    data[:, 5:] = -200.0
    only_nodata = box(13.74, 51.03, 13.78, 51.07)
    result, caught = _caught(
        lambda: _run(
            _zones(only_nodata, WHOLE), _raster(data, nodata=-200.0), stat="sum"
        )
    )
    assert np.isnan(result["sum_value"].iloc[0]) and result["sum_value"].iloc[1] == 350
    assert len(caught) == 1 and "nur mit Nodata-Zellen" in str(caught[0].message)


def test_fa08_zonal_stats_empty_geometry_is_nan_and_reported():
    result, caught = _caught(
        lambda: _run(_zones(WHOLE, Polygon()), _raster(RAMP), stat="sum")
    )
    assert np.isnan(result["sum_value"].iloc[1]) and len(caught) == 1
    assert "leerer Geometrie" in str(caught[0].message)


def test_fa08_zonal_stats_all_causes_share_one_warning():
    data = np.full((10, 10), 7.0)
    data[:, 5:] = -200.0
    zones = _zones(WHOLE, OUTSIDE, box(13.74, 51.03, 13.78, 51.07), Polygon())
    result, caught = _caught(
        lambda: _run(zones, _raster(data, nodata=-200.0), stat="sum")
    )
    assert len(caught) == 1
    message = str(caught[0].message)
    assert "3 von 4 Zonen" in message
    for fragment in (
        "außerhalb des Rasters",
        "nur mit Nodata-Zellen",
        "leerer Geometrie",
    ):
        assert fragment in message


def test_fa08_zonal_stats_complete_coverage_gives_no_warning():
    _, caught = _caught(lambda: _run(_zones(WHOLE), _raster(RAMP), stat="sum"))
    assert caught == []


def test_fa08_zonal_stats_empty_zone_layer_gives_an_empty_result_without_warning():
    empty = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:4326")
    result, caught = _caught(
        lambda: _run(empty, _raster(RAMP), stat="share", output_field="anteil")
    )
    assert len(result) == 0 and "anteil" in result.columns and caught == []


def test_fa08_zonal_stats_warning_travels_as_a_layer_step_warning_through_the_executor():
    """The aggregated warning reaches the run report (rule 7: never silent)."""
    from geofact.engine.events import collect_warnings

    with collect_warnings() as caught:
        _run(_zones(WHOLE, OUTSIDE), _raster(RAMP), stat="sum")
    assert len(caught) == 1 and caught[0][1] == "RasterCoverageWarning"
