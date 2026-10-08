"""Implements: FA47 (raster_calc blockweise, precision).

Contract tests of the block-wise evaluation of `raster_calc`
(builtin/operations/raster_calc.py): the expression is evaluated in row blocks of
``BLOCK_ROWS`` rows, so only one block of float64 copies lives at a time. The
result is bit-identical to a single-block evaluation; ``precision: float32``
halves the working memory at the cost of float32 rounding.
"""

from __future__ import annotations

import numpy as np
import pytest
from affine import Affine
from pyproj import CRS

from geofact.builtin.operations import raster_calc
from geofact.builtin.operations.raster_calc import RasterCalcStep, run_raster_calc
from geofact.core.types import RasterLayer

TRANSFORM = Affine(10, 0, 350000, 0, -10, 5640000)


def _raster(rows: int = 37, cols: int = 11, nodata=0) -> RasterLayer:
    rng = np.random.default_rng(42)
    red = rng.integers(0, 3000, size=(rows, cols)).astype("uint16")
    nir = rng.integers(0, 5000, size=(rows, cols)).astype("uint16")
    return RasterLayer(
        data=np.stack([red, nir]),
        transform=TRANSFORM,
        crs=CRS.from_epsg(32633),
        nodata=nodata,
        band_names=["red", "nir"],
    )


def _calc(raster, expression, **params):
    return run_raster_calc({"raster": raster}, {"expression": expression, **params})


@pytest.mark.parametrize(
    "expression",
    [
        "(nir - red) / (nir + red)",
        "((nir - red) / (nir + red)) > 0.3",
        "where(nir > 2000, sqrt(nir), log(red))",
    ],
)
def test_fa47_blockwise_result_is_bit_identical_to_one_block(monkeypatch, expression):
    raster = _raster()
    monkeypatch.setattr(raster_calc, "BLOCK_ROWS", 10_000)
    whole = _calc(raster, expression)
    monkeypatch.setattr(raster_calc, "BLOCK_ROWS", 4)
    blocks = _calc(raster, expression)
    assert (
        blocks.data.dtype == whole.data.dtype and blocks.data.shape == whole.data.shape
    )
    assert blocks.data.tobytes() == whole.data.tobytes()
    assert blocks.nodata == whole.nodata or (
        np.isnan(blocks.nodata) and np.isnan(whole.nodata)
    )


def test_fa47_blockwise_evaluation_only_sees_blocks_of_block_rows(monkeypatch):
    shapes: list[tuple[int, int]] = []
    original = raster_calc._Evaluator.__init__

    def recording(self, expression, bands, shape):
        shapes.append(shape)
        original(self, expression, bands, shape)

    monkeypatch.setattr(raster_calc._Evaluator, "__init__", recording)
    monkeypatch.setattr(raster_calc, "BLOCK_ROWS", 10)
    result = _calc(_raster(rows=25), "nir - red")
    assert shapes == [(10, 11), (10, 11), (5, 11)]
    assert result.shape == (25, 11)


def test_fa47_precision_float32_is_close_to_float64_and_keeps_the_output_types():
    raster = _raster()
    exact = _calc(raster, "(nir - red) / (nir + red)")
    single = _calc(raster, "(nir - red) / (nir + red)", precision="float32")
    assert single.data.dtype == np.float32
    assert np.allclose(single.data, exact.data, equal_nan=True, atol=1e-6)
    assert np.array_equal(np.isnan(single.data), np.isnan(exact.data))
    mask = _calc(raster, "nir > red", precision="float32")
    assert mask.data.dtype == np.uint8 and mask.nodata == 255


def test_fa47_precision_must_be_a_known_value():
    assert RasterCalcStep.Params(expression="a + 1").precision == "float64"
    with pytest.raises(ValueError, match="float64|float32"):
        RasterCalcStep.Params(expression="a + 1", precision="float16")
