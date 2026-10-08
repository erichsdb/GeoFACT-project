"""Implements: FA49 (Raster in der Web-Ansicht: deklariertes Nodata wird ausgeblendet), FA12.

The raster preview of the web demo (backend/geofact_web/presentation/serialize.py
`describe`, raster_png.py `render_png`) hid NaN and a hard-coded sentinel threshold only.
A 0/1 mask with nodata 255 or Sentinel-2 digital numbers with nodata 0 showed their
nodata as the maximum / minimum of the legend; the declared nodata of the layer is now
excluded as well.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from affine import Affine
from pyproj import CRS

from geofact.core.types import RasterLayer

pytest.importorskip("fastapi")

from geofact_web.presentation.serialize import describe  # noqa: E402

TRANSFORM = Affine(10, 0, 350000, 0, -10, 5640000)


def _raster(data, **fields) -> RasterLayer:
    return RasterLayer(
        data=np.asarray(data), transform=TRANSFORM, crs=CRS.from_epsg(32633), **fields
    )


def test_fa49_stats_of_a_mask_leave_out_the_declared_nodata():
    mask = _raster([[0, 1, 255], [1, 255, 0]], nodata=255, band_names=["gruen"])
    stats = describe(mask)["stats"]
    assert stats["min"] == 0 and stats["max"] == 1 and stats["valid_count"] == 4


def test_fa49_stats_of_digital_numbers_leave_out_nodata_zero():
    scene = _raster(
        np.stack([np.array([[0, 1200], [1500, 0]]), np.full((2, 2), 7)]),
        nodata=0,
        band_names=["red", "scl"],
    )
    stats = describe(scene)["stats"]  # first band
    assert stats["min"] == 1200 and stats["max"] == 1500 and stats["valid_count"] == 2


def test_fa49_stats_without_nodata_are_unchanged():
    stats = describe(_raster([[1.0, 2.0], [3.0, np.nan]]))["stats"]
    assert (stats["min"], stats["max"], stats["valid_count"]) == (1.0, 3.0, 3)


def test_fa49_stats_of_a_raster_without_valid_cells_are_none():
    assert describe(_raster([[255, 255]], nodata=255))["stats"] is None


def test_fa49_preview_png_is_transparent_where_the_declared_nodata_is():
    pillow = pytest.importorskip("PIL.Image")
    from geofact_web.raster_png import render_png

    png = render_png(_raster([[0, 1], [255, 1]], nodata=255))
    image = pillow.open(io.BytesIO(png)).convert("RGBA")
    assert image.getpixel((0, 1))[3] == 0  # the 255 cell
    assert image.getpixel((0, 0))[3] == 255 and image.getpixel((1, 0))[3] == 255
