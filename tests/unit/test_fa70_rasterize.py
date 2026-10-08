"""Implements: FA70 (Rasterisierung: rasterize; Raster-Helfer warp_raster, write_geotiff mit Tags).

Contract tests of `rasterize` (builtin/operations/rasterize.py): vector objects are
burnt into a raster in the working CRS on a grid aligned to multiples of the cell
size, with a cell budget (FA4/NFA2) and explicit errors for an empty layer, a
geographic CRS and a missing value field. Plus the two raster helpers of
builtin/_raster_io.py that the GeoTIFF output (W2-05) consumes.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import CRS
from shapely.geometry import LineString, Point, box

from geofact import api
from geofact.builtin import _raster_io
from geofact.builtin.operations.rasterize import (
    RasterizeStep,
    RasterizeWarning,
    run_rasterize,
)
from geofact.core.types import DataType, RasterLayer

UTM33 = "EPSG:32633"


def _features(*geometries, crs=UTM33, **columns) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(dict(columns), geometry=list(geometries), crs=crs)


def _run(features, **params) -> RasterLayer:
    return run_rasterize({"features": features}, {"cell_size_m": 10, **params})


# =====================================================================
# Happy path
# =====================================================================


def test_fa70_rasterize_burns_a_zero_one_mask_on_an_aligned_grid():
    # 12..33 x 103..117 -> aligned to 10 m: 10..40 x 100..120 = 3 cols x 2 rows
    result = _run(_features(box(12, 103, 33, 117)), output_band="gruen")
    assert result.data.dtype == np.uint8 and result.data.ndim == 2
    assert result.shape == (2, 3)
    assert result.transform == Affine(10, 0, 10, 0, -10, 120)
    assert result.band_names == ["gruen"] and result.crs == CRS.from_user_input(UTM33)
    assert result.nodata is None
    # cell centres 15/25/35 x 115/105: x=35 lies outside the box
    assert result.data.tolist() == [[1, 1, 0], [1, 1, 0]]


def test_fa70_rasterize_with_value_field_gives_float32_and_respects_fill_and_nodata():
    features = _features(box(0, 0, 10, 10), box(20, 0, 30, 10), wert=[2.5, 7])
    result = _run(features, value_field="wert", fill=-1, nodata=-1)
    assert result.data.dtype == np.float32
    assert result.data.tolist() == [[2.5, -1.0, 7.0]]
    assert result.nodata == -1


def test_fa70_rasterize_all_touched_burns_every_touched_cell():
    line = _features(LineString([(0, 0), (30, 20)]))
    thin = _run(line)
    touched = _run(line, all_touched=True)
    assert touched.data.sum() > thin.data.sum()


def test_fa70_rasterize_result_is_a_copy_not_a_view_of_the_input():
    features = _features(box(0, 0, 10, 10))
    before = features.copy()
    result = _run(features)
    assert result.data.base is None or result.data.flags.owndata
    assert features.equals(before)


# =====================================================================
# Type error, edge case, preconditions
# =====================================================================


def _scenario(layer: dict) -> dict:
    return {
        "scenario": {"name": "t", "region": "13.0,50.9,13.1,51.0"},
        "layers": [layer],
        "steps": [
            {
                "id": "r",
                "op": "rasterize",
                "inputs": {"features": layer["id"]},
                "params": {"cell_size_m": 10},
            }
        ],
        "output": [{"type": "geotiff", "source": "r"}],
    }


def test_fa70_rasterize_raster_on_the_features_port_is_a_type_error_at_config_time():
    issues = api.validate(
        _scenario(
            {
                "id": "t",
                "source": "file",
                "path": "x.tif",
                "data_type": "raster",
                "format": "tif",
            }
        )
    ).issues
    assert issues and issues[0][0] == "steps -> 0 -> inputs -> features"
    assert "vector" in issues[0][1]


def test_fa70_rasterize_valid_scenario_validates():
    assert api.validate(
        _scenario(
            {"id": "v", "source": "file", "path": "x.geojson", "data_type": "vector"}
        )
    ).valid


def test_fa70_rasterize_empty_layer_is_an_error_with_a_hint():
    empty = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs=UTM33)
    with pytest.raises(ValueError, match="leer.*kein Gitter"):
        _run(empty)


def test_fa70_rasterize_geographic_crs_is_an_error_naming_fa5():
    with pytest.raises(ValueError, match="kein projiziertes CRS.*FA5"):
        _run(_features(box(13.0, 50.9, 13.1, 51.0), crs="EPSG:4326"))


def test_fa70_rasterize_cell_budget_is_checked_before_allocating(monkeypatch):
    monkeypatch.setattr(_raster_io, "MAX_CELLS", 5)
    with pytest.raises(ValueError, match="cell_size_m vergrößern"):
        _run(_features(box(0, 0, 100, 100)))


def test_fa70_rasterize_unknown_value_field_names_the_columns():
    with pytest.raises(ValueError, match="'hoehe'.*\\['name'\\]"):
        _run(_features(box(0, 0, 10, 10), name=["a"]), value_field="hoehe")


def test_fa70_rasterize_non_numeric_value_field_is_an_error():
    with pytest.raises(ValueError, match="nicht numerisch"):
        _run(_features(box(0, 0, 10, 10), name=["a"]), value_field="name")


def test_fa70_rasterize_skipped_objects_are_reported_in_one_warning():
    features = _features(
        box(0, 0, 10, 10), box(10, 0, 20, 10), None, wert=[1.0, np.nan, 3.0]
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = _run(features, value_field="wert")
    messages = [str(w.message) for w in caught if "rasterize" in str(w.message)]
    assert len(messages) == 1 and "2 von 3" in messages[0]
    assert result.data.tolist() == [[1.0, 0.0]]


def test_fa70_rasterize_params_are_checked_at_config_time():
    with pytest.raises(ValueError):
        RasterizeStep.Params(cell_size_m=0)
    with pytest.raises(ValueError, match="uint8"):
        RasterizeStep.Params(cell_size_m=10, fill=300)
    with pytest.raises(ValueError, match="output_band"):
        RasterizeStep.Params(cell_size_m=10, output_band="nicht ok")
    RasterizeStep.Params(cell_size_m=10, value_field="w", fill=-9999.5)


def test_fa70_rasterize_is_visible_in_the_catalog():
    assert RasterizeStep.INPUT_PORTS == {"features": DataType.VECTOR}
    assert RasterizeStep.OUTPUT_TYPE == DataType.RASTER
    entry = {e["op"]: e for e in api.operation_catalog()}["rasterize"]
    assert (
        entry["input_ports"] == {"features": "vector"}
        and entry["output_type"] == "raster"
    )
    assert entry["required_params"] == ["cell_size_m"]


def test_fa70_rasterize_single_point_gets_one_cell():
    result = _run(_features(Point(20, 20)), all_touched=True)
    assert result.shape[0] >= 1 and result.shape[1] >= 1
    assert result.data.sum() >= 1


# =====================================================================
# Raster helpers (consumed by the geotiff output, W2-05)
# =====================================================================


def test_fa70_check_cell_budget_names_context_and_hint(monkeypatch):
    monkeypatch.setattr(_raster_io, "MAX_CELLS", 10)
    _raster_io.check_cell_budget(10, context="Layer 'x'", hint="kleiner")
    with pytest.raises(ValueError, match="Layer 'x'.*11.*10.*kleiner"):
        _raster_io.check_cell_budget(11, context="Layer 'x'", hint="kleiner")


def test_fa70_warp_raster_keeps_nodata_and_band_names():
    data = np.arange(2 * 20 * 20, dtype="float32").reshape(2, 20, 20)
    data[0, 0, 0] = -9999
    raster = RasterLayer(
        data=data,
        transform=Affine(10, 0, 350000, 0, -10, 5640000),
        crs=CRS.from_epsg(32633),
        nodata=-9999,
        band_names=["red", "nir"],
        path="s",
    )
    warped = _raster_io.warp_raster(raster, "EPSG:3035")
    assert warped.crs == CRS.from_epsg(3035)
    assert warped.band_names == ["red", "nir"] and warped.nodata == -9999
    assert warped.data.ndim == 3 and warped.data.shape[0] == 2
    assert warped.data.dtype == np.float32
    # cells outside the source footprint carry the declared nodata
    assert (warped.data == -9999).any()
    valid = warped.data[1][warped.data[1] != -9999]
    assert valid.min() >= data[1].min() and valid.max() <= data[1].max()
    # same CRS: unchanged copy
    same = _raster_io.warp_raster(raster, "EPSG:32633")
    assert np.array_equal(same.data, data) and same.data is not data


def test_fa70_warp_raster_unknown_resampling_is_an_error():
    raster = RasterLayer(
        data=np.ones((2, 2), "float32"),
        transform=Affine(10, 0, 0, 0, -10, 20),
        crs=CRS.from_epsg(32633),
    )
    with pytest.raises(ValueError, match="Resampling 'cubic'"):
        _raster_io.warp_raster(raster, "EPSG:3035", resampling="cubic")


def test_fa70_write_geotiff_tags_are_readable(tmp_path):
    raster = RasterLayer(
        data=np.ones((2, 2), "uint8"),
        transform=Affine(10, 0, 0, 0, -10, 20),
        crs=CRS.from_epsg(32633),
        band_names=["mask"],
    )
    target = tmp_path / "t.tif"
    _raster_io.write_geotiff(
        raster,
        target,
        tags={"TIFFTAG_COPYRIGHT": "(c) OSM", "GEOFACT_ATTRIBUTION": "OSM ODbL"},
    )
    with rasterio.open(target) as dataset:
        tags = dataset.tags()
    assert tags["TIFFTAG_COPYRIGHT"] == "(c) OSM"
    assert tags["GEOFACT_ATTRIBUTION"] == "OSM ODbL"
    assert _raster_io.read_geotiff(target).band_names == ["mask"]


# =====================================================================
# Nachtreview 02.10.2026
# =====================================================================


def test_fa70_point_on_the_right_grid_edge_is_burned():
    """Nachtreview 14: a point whose x is a multiple of the cell size fell at
    col == cols, outside the array, and was dropped silently."""
    result = _run(_features(Point(10, 15), Point(30, 15)))
    assert int(result.data.sum()) == 2
    valued = _run(
        _features(Point(10, 15), Point(30, 15), v=[1.0, 2.0]), value_field="v"
    )
    assert sorted(valued.data[valued.data > 0].tolist()) == [1.0, 2.0]


def test_fa70_point_on_the_bottom_grid_edge_is_burned():
    result = _run(_features(Point(15, 10), Point(15, 30)))
    assert int(result.data.sum()) == 2


def test_fa70_vertical_line_on_the_right_grid_edge_is_burned():
    result = _run(_features(Point(5, 5), LineString([(30, 0), (30, 20)])))
    assert int(result.data[:, -1].sum()) > 0


def test_fa70_polygon_grid_is_unchanged_on_cell_multiples():
    result = _run(_features(box(0, 0, 30, 20)))
    assert result.data.shape == (2, 3)
    assert int(result.data.sum()) == 6


def test_fa70_polygon_without_a_cell_centre_is_reported():
    """Nachtreview 19: a polygon smaller than a cell (or off the cell centres)
    burned into no cell without a warning."""
    with pytest.warns(RasterizeWarning, match="keine Zelle"):
        result = _run(_features(box(1, 1, 4, 4), v=[5.0]), value_field="v")
    assert float(result.data.sum()) == 0.0


def test_fa70_small_polygon_with_all_touched_is_not_reported():
    with warnings.catch_warnings():
        warnings.simplefilter("error", RasterizeWarning)
        result = _run(_features(box(1, 1, 4, 4)), all_touched=True)
    assert int(result.data.sum()) == 1


def test_fa70_polygons_covering_cell_centres_are_not_reported():
    with warnings.catch_warnings():
        warnings.simplefilter("error", RasterizeWarning)
        _run(_features(box(0, 0, 30, 20), box(40, 0, 100, 60)))
