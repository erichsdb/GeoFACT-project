"""Implements: FA12 (example data of the web demo: scripts/prepare_dem_sachsen.py for sachsen_nachthimmel).

The helper that builds the elevation raster of the night-sky example from ten Copernicus
DEM tiles. The offline part is tested: tile names, the merge into one GeoTIFF, reuse of a
tile that is already there. The download itself needs the network (and was checked once
against the reference raster: same 2400 x 4000 cells, identical values).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prepare_dem_sachsen.py"


@pytest.fixture(scope="module")
def prepare_dem():
    spec = importlib.util.spec_from_file_location("prepare_dem_sachsen", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tile(path: Path, west: float, north: float, value: float) -> Path:
    data = np.full((1, 4, 4), value, dtype="float32")
    profile = {
        "driver": "GTiff",
        "height": 4,
        "width": 4,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": from_origin(west, north, 0.25, 0.25),
        "tiled": True,
        "blockxsize": 16,
        "blockysize": 16,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data)
    return path


def test_fa12_dem_tile_names_cover_the_ten_saxony_tiles(prepare_dem):
    names = prepare_dem.tile_names()
    assert len(names) == len(set(names)) == 10
    assert names[0] == "Copernicus_DSM_COG_30_N50_00_E011_00_DEM"
    assert names[-1] == "Copernicus_DSM_COG_30_N51_00_E015_00_DEM"
    assert {name.split("_")[4] for name in names} == {"N50", "N51"}
    assert {name.split("_")[6] for name in names} == {
        f"E0{lon}" for lon in range(11, 16)
    }


def test_fa12_dem_merge_joins_adjacent_tiles_into_one_deflate_geotiff(
    prepare_dem, tmp_path
):
    west = _tile(tmp_path / "west.tif", 11.0, 51.0, 100.0)
    east = _tile(tmp_path / "east.tif", 12.0, 51.0, 200.0)
    out = tmp_path / "nested" / "dem.tif"

    shape = prepare_dem.merge_tiles([west, east], out)

    assert shape == (1, 4, 8)
    with rasterio.open(out) as merged:
        assert merged.shape == (4, 8) and str(merged.crs) == "EPSG:4326"
        assert merged.transform.c == 11.0 and merged.transform.f == 51.0
        assert merged.compression.name == "deflate"
        data = merged.read(1)
    # the tiles (1 degree = 4 cells of 0.25 degrees) sit side by side, west left of east
    assert np.all(data[:, :4] == 100.0) and np.all(data[:, 4:] == 200.0)


def test_fa12_dem_merge_without_tiles_is_an_explicit_error(prepare_dem, tmp_path):
    with pytest.raises(ValueError, match="no tiles"):
        prepare_dem.merge_tiles([], tmp_path / "dem.tif")
    assert not (tmp_path / "dem.tif").exists()


def test_fa12_dem_existing_tile_is_reused_without_a_download(prepare_dem, tmp_path):
    existing = tmp_path / "Copernicus_DSM_COG_30_N50_00_E011_00_DEM.tif"
    existing.write_bytes(b"already here")
    # the offline guard of the test run would fail loudly on any connection
    assert (
        prepare_dem.download_tile("Copernicus_DSM_COG_30_N50_00_E011_00_DEM", tmp_path)
        == existing
    )
    assert existing.read_bytes() == b"already here"
