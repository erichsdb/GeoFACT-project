"""Implements: FA48 (Rasterklassifikation: raster_classify).

Contract tests of `raster_classify` (builtin/operations/raster_classify.py):
right-open class intervals like the vector operation `classify`, nodata is
preserved, the result is a one-band raster.
"""

from __future__ import annotations

import numpy as np
import pytest
from affine import Affine
from pyproj import CRS

from geofact import api
from geofact.builtin.operations.raster_classify import (
    RasterClassifyStep,
    classify_raster,
    run_raster_classify,
)
from geofact.core.types import DataType, RasterLayer

TRANSFORM = Affine(10, 0, 350000, 0, -10, 5640000)
CRS33 = CRS.from_epsg(32633)


def _raster(rows, nodata=None, **fields) -> RasterLayer:
    return RasterLayer(
        data=np.asarray(rows),
        transform=TRANSFORM,
        crs=CRS33,
        nodata=nodata,
        path="ndvi.tif",
        **fields,
    )


def _run(raster, **params):
    return run_raster_classify({"raster": raster}, params)


# =====================================================================
# Intervals
# =====================================================================


def test_fa48_classes_are_right_open_like_the_vector_classify():
    """breaks [0.2, 0.5]: x < 0.2 -> 1, 0.2 <= x < 0.5 -> 2, x >= 0.5 -> 3."""
    raster = _raster([[0.0, 0.19999, 0.2, 0.49999, 0.5, 0.9]])
    result = _run(raster, breaks=[0.2, 0.5], values=[1, 2, 3])
    assert result.data.tolist() == [[1, 1, 2, 2, 3, 3]]


def test_fa48_result_is_a_uint8_one_band_raster_with_nodata_255():
    result = _run(
        _raster([[0.1, 0.9]]), breaks=[0.5], values=[0, 1], output_band="gruen"
    )
    assert result.data.dtype == np.uint8 and result.nodata == 255
    assert result.data.ndim == 2 and result.band_names == ["gruen"]
    assert (
        result.transform == TRANSFORM
        and result.crs == CRS33
        and result.path == "ndvi.tif"
    )


def test_fa48_default_output_band_name():
    assert _run(_raster([[1.0]]), breaks=[0.5], values=[0, 1]).band_names == ["class"]


def test_fa48_non_integer_values_give_float32_with_nan_nodata():
    result = _run(
        _raster([[0.1, 0.9, np.nan]], nodata=float("nan")),
        breaks=[0.5],
        values=[0.25, 0.75],
    )
    assert result.data.dtype == np.float32 and np.isnan(result.nodata)
    assert result.data[0, :2].tolist() == [0.25, 0.75] and np.isnan(result.data[0, 2])


@pytest.mark.parametrize("values", [[-1, 0, 1], [0, 1, 255], [0, 1, 300]])
def test_fa48_class_values_outside_uint8_range_give_float32(values):
    result = _run(_raster([[0.0, 1.0, 2.0]]), breaks=[0.5, 1.5], values=values)
    assert result.data.dtype == np.float32
    assert result.data[0].tolist() == [float(v) for v in values]


def test_fa48_class_values_need_not_be_ordered_or_distinct():
    result = _run(_raster([[0.1, 0.6, 0.9]]), breaks=[0.3, 0.7], values=[5, 1, 5])
    assert result.data.tolist() == [[5, 1, 5]]


def test_fa48_integer_input_raster():
    result = _run(
        _raster(np.array([[10, 20, 30, 40]])), breaks=[20, 40], values=[1, 2, 3]
    )
    assert result.data.tolist() == [[1, 2, 2, 3]]


# =====================================================================
# Nodata
# =====================================================================


def test_fa48_nodata_cells_stay_nodata():
    raster = _raster([[-200.0, 0.1, 0.9]], nodata=-200.0)
    result = _run(raster, breaks=[0.5], values=[1, 2])
    assert result.data.tolist() == [[255, 1, 2]]


def test_fa48_nan_cells_are_nodata_even_without_a_declared_nodata():
    """NaN has no class: digitize would put it into the top class - it must not."""
    raster = _raster([[0.1, np.nan, 0.9]])
    assert raster.nodata is None
    assert _run(raster, breaks=[0.5], values=[1, 2]).data.tolist() == [[1, 255, 2]]


def test_fa48_input_raster_is_not_modified():
    raster = _raster([[0.1, np.nan]], nodata=float("nan"))
    before = raster.data.copy()
    _run(raster, breaks=[0.5], values=[1, 2])
    assert np.array_equal(raster.data, before, equal_nan=True)


# =====================================================================
# Bands
# =====================================================================


def test_fa48_band_of_a_multi_band_raster_by_name_or_number():
    data = np.stack([np.array([[0.1, 0.9]]), np.array([[0.9, 0.1]])])
    raster = RasterLayer(
        data=data, transform=TRANSFORM, crs=CRS33, band_names=["a", "b"]
    )
    assert _run(raster, breaks=[0.5], values=[0, 1], band="b").data.tolist() == [[1, 0]]
    assert _run(raster, breaks=[0.5], values=[0, 1], band=1).data.tolist() == [[0, 1]]


def test_fa48_multi_band_raster_without_band_is_an_explicit_error():
    data = np.zeros((2, 1, 2))
    raster = RasterLayer(
        data=data, transform=TRANSFORM, crs=CRS33, band_names=["a", "b"]
    )
    with pytest.raises(ValueError, match=r"2 Bänder \['a', 'b'\].*'band'"):
        _run(raster, breaks=[0.5], values=[0, 1])


def test_fa48_unknown_band_is_an_explicit_error():
    raster = RasterLayer(
        data=np.zeros((1, 1, 2)), transform=TRANSFORM, crs=CRS33, band_names=["a"]
    )
    with pytest.raises(ValueError, match=r"Band 'z'.*\['a'\]"):
        _run(raster, breaks=[0.5], values=[0, 1], band="z")


# =====================================================================
# Edge cases and contract
# =====================================================================


def test_fa48_empty_raster_gives_an_empty_result():
    """Edge case 'empty layer'."""
    result = _run(_raster(np.zeros((0, 0))), breaks=[0.5], values=[0, 1])
    assert result.data.shape == (0, 0) and result.nodata == 255


def test_fa48_classify_raster_function_checks_the_value_count():
    with pytest.raises(ValueError, match="2 values passen nicht zu 2 breaks"):
        classify_raster(_raster([[1.0]]), breaks=[0.1, 0.2], values=[1, 2])


@pytest.mark.parametrize(
    "params, fragment",
    [
        ({"breaks": [0.5, 0.2], "values": [1, 2, 3]}, "aufsteigend"),
        ({"breaks": [0.5, 0.5], "values": [1, 2, 3]}, "aufsteigend"),
        ({"breaks": [0.5], "values": [1]}, "breaks \\+ 1"),
        ({"breaks": [0.5], "values": [1, 2, 3]}, "breaks \\+ 1"),
        ({"breaks": [], "values": [1]}, "breaks"),
        ({"breaks": [0.5], "values": [1, 2], "output_band": "no valid"}, "output_band"),
        ({"breaks": ["abc"], "values": [1, 2]}, "breaks"),
        ({"breaks": [0.5], "values": [1, 2], "labels": ["a", "b"]}, "labels"),
        ({"values": [1, 2]}, "breaks"),
    ],
)
def test_fa48_invalid_params_are_rejected(params, fragment):
    with pytest.raises(ValueError, match=fragment):
        RasterClassifyStep.Params(**params)


def test_fa48_contract_ports_and_types():
    assert RasterClassifyStep.INPUT_PORTS == {"raster": DataType.RASTER}
    assert RasterClassifyStep.OUTPUT_TYPE == DataType.RASTER
    entry = {e["op"]: e for e in api.operation_catalog()}["raster_classify"]
    assert (
        entry["input_ports"] == {"raster": "raster"}
        and entry["output_type"] == "raster"
    )
    assert entry["required_params"] == ["breaks", "values"]


def _scenario(source: dict, params: dict) -> dict:
    return {
        "scenario": {"name": "t", "region": "13.0,50.9,13.1,51.0"},
        "layers": [source],
        "steps": [
            {
                "id": "k",
                "op": "raster_classify",
                "inputs": {"raster": source["id"]},
                "params": params,
            }
        ],
        "output": [{"type": "geotiff", "source": "k"}],
    }


RASTER_LAYER = {
    "id": "r",
    "source": "file",
    "path": "x.tif",
    "data_type": "raster",
    "format": "tif",
}


def test_fa48_valid_scenario_validates():
    assert api.validate(
        _scenario(RASTER_LAYER, {"breaks": [0.3], "values": [0, 1]})
    ).valid


def test_fa48_vector_layer_on_the_raster_port_is_a_type_error():
    vector = {"id": "v", "source": "file", "path": "x.geojson", "data_type": "vector"}
    issues = api.validate(_scenario(vector, {"breaks": [0.3], "values": [0, 1]})).issues
    assert (
        issues
        and issues[0][0] == "steps -> 0 -> inputs -> raster"
        and "raster" in issues[0][1]
    )


def test_fa48_invalid_breaks_are_reported_with_their_position():
    issues = api.validate(
        _scenario(RASTER_LAYER, {"breaks": [0.5, 0.2], "values": [1, 2, 3]})
    ).issues
    assert issues and issues[0][0] == "steps -> 0 -> params -> breaks"
