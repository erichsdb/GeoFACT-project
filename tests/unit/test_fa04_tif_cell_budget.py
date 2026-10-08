"""Implements: FA4 (Zellbudget beim Lesen von GeoTIFF), NFA2.

The file format `tif` (builtin/formats/raster.py) checks the cell budget
(`_raster_io.MAX_CELLS`, bands x rows x cols of the read window) BEFORE
`dataset.read`: an oversized window fails with a message naming the layer, the
cell count and the remedy instead of filling the memory. The path may come as a
`str` (e.g. a `/vsizip/` path from the ZIP raster support).
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from shapely.geometry import box

from geofact.builtin import _raster_io
from geofact.builtin.formats import raster as raster_format
from geofact.builtin.sources import file as files
from geofact.builtin.sources.file import FileLayer
from geofact.plugin_api import LoadContext

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "population.tif"
)  # 10 x 10, 1 band


def _layer(**fields) -> FileLayer:
    return FileLayer(
        id="pop", path=str(FIXTURE), data_type="raster", format="tif", **fields
    )


def test_fa04_tif_over_the_cell_budget_fails_before_reading(monkeypatch):
    monkeypatch.setattr(_raster_io, "MAX_CELLS", 99)

    def must_not_read(*args, **kwargs):  # pragma: no cover - the check comes first
        raise AssertionError("dataset.read was called before the budget check")

    monkeypatch.setattr(rasterio.io.DatasetReader, "read", must_not_read)
    with pytest.raises(
        ValueError, match="Layer 'pop'.*100 Zellen.*99.*Region verkleinern"
    ):
        files.load(_layer(), LoadContext())


def test_fa04_tif_budget_counts_the_window_and_the_bands(monkeypatch):
    monkeypatch.setattr(_raster_io, "MAX_CELLS", 100)
    full = files.load(_layer(), LoadContext())
    assert full.shape == (10, 10)
    # two bands of the same file: 200 cells > 100
    with pytest.raises(ValueError, match="200 Zellen"):
        files.load(_layer(bands=[1, 1], band_names=["a", "b"]), LoadContext())
    # half the region window fits again
    region = gpd.GeoDataFrame(
        geometry=[box(13.70, 51.03, 13.74, 51.07)], crs="EPSG:4326"
    )
    monkeypatch.setattr(_raster_io, "MAX_CELLS", 60)
    windowed = files.load(_layer(), LoadContext(region=region))
    assert windowed.data.size <= 60


def test_fa04_tif_reader_accepts_a_str_path():
    data = raster_format._read_geotiff(str(FIXTURE), _layer(), LoadContext())
    assert data.path == str(FIXTURE) and float(np.sum(data.data)) == 10000
