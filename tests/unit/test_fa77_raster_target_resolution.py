"""Implements: FA77 (read a raster at a target resolution with explicit aggregation).

Contract tests of `resolution_m` + `aggregation` of the `tif` format
(builtin/formats/raster.py): the total of a count raster is preserved exactly
(nodata and NaN do not count), `mean` averages the valid cells, the result
matches GDAL's own `sum` resampling on an aligned grid, the file's (averaged)
overviews are never used, the target grid is aligned to whole blocks of the
file grid, the cell budget applies to the target raster, and every
precondition fails with an explicit message.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from pydantic import ValidationError
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import reproject
from shapely.geometry import box

from geofact.builtin import _raster_io
from geofact.builtin.formats import raster as raster_format
from geofact.builtin.sources import file as files
from geofact.builtin.sources.file import FileLayer
from geofact.plugin_api import LoadContext

NODATA = -200.0
ORIGIN = (4_000_000.0, 3_000_000.0)  # EPSG:3035, upper left corner


def _write(
    path: Path,
    data: np.ndarray,
    *,
    crs="EPSG:3035",
    cell=100.0,
    nodata=NODATA,
    origin=ORIGIN,
) -> Path:
    bands = data if data.ndim == 3 else data[np.newaxis]
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=bands.shape[1],
        width=bands.shape[2],
        count=bands.shape[0],
        dtype="float64",
        crs=crs,
        transform=from_origin(origin[0], origin[1], cell, cell),
        nodata=nodata,
    ) as dst:
        dst.write(bands)
    return path


def _population(rows=25, cols=37, seed=7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    data = rng.uniform(0, 50, size=(rows, cols)).round(3)
    data[3:6, 4:9] = NODATA  # nodata cells
    data[20:, 30:] = NODATA  # a whole partial block of nodata
    data[1, 1] = np.nan  # NaN does not count either
    return data


def _layer(path: Path, **fields) -> FileLayer:
    fields.setdefault("resolution_m", 1000)
    fields.setdefault("aggregation", "sum")
    fields.setdefault(
        "resampling", "sum" if fields["aggregation"] == "sum" else "average"
    )
    return FileLayer(
        id="pop", path=str(path), data_type="raster", format="tif", **fields
    )


def _valid(data: np.ndarray) -> np.ndarray:
    return ~np.isnan(data) & (data != NODATA)


# =====================================================================
# Happy path: sum preserved, mean, grid
# =====================================================================


def test_fa77_sum_preserves_the_total_of_the_valid_cells(tmp_path):
    data = _population()
    result = files.load(_layer(_write(tmp_path / "pop.tif", data)), LoadContext())
    assert result.data.shape == (3, 4)  # ceil(25/10) x ceil(37/10)
    out = result.data
    assert result.nodata == NODATA
    expected_total = data[_valid(data)].sum()
    assert out[out != NODATA].sum() == pytest.approx(expected_total, rel=1e-12)
    # block (0, 0) is the sum of its valid source cells
    block = data[0:10, 0:10]
    assert out[0, 0] == pytest.approx(block[_valid(block)].sum())
    # the partial block at the edge (rows 20-24, cols 30-36) is all nodata -> nodata
    assert out[2, 3] == NODATA
    # target grid: 1000 m cells starting at the file origin
    assert result.transform.a == 1000 and result.transform.e == -1000
    assert (result.transform.c, result.transform.f) == ORIGIN


def test_fa77_mean_averages_the_valid_cells(tmp_path):
    data = _population()
    result = files.load(
        _layer(_write(tmp_path / "pop.tif", data), aggregation="mean"), LoadContext()
    )
    block = data[0:10, 10:20]
    assert result.data[0, 1] == pytest.approx(block[_valid(block)].mean())
    edge = data[20:25, 0:10]  # partial block: only 5 rows exist
    assert result.data[2, 0] == pytest.approx(edge[_valid(edge)].mean())


def test_fa77_matches_gdal_sum_resampling_on_an_aligned_grid(tmp_path):
    rng = np.random.default_rng(1)
    data = rng.uniform(0, 10, size=(30, 40))
    path = _write(tmp_path / "aligned.tif", data, nodata=None)
    ours = files.load(_layer(path), LoadContext()).data
    # rasterio allows GDAL's 'sum' only for warping, not for reads: the reference
    # is a warp of the full-resolution array onto the 1 km grid
    gdal = np.zeros((3, 4))
    reproject(
        data,
        gdal,
        src_transform=from_origin(*ORIGIN, 100, 100),
        src_crs="EPSG:3035",
        dst_transform=from_origin(*ORIGIN, 1000, 1000),
        dst_crs="EPSG:3035",
        resampling=Resampling.sum,
    )
    np.testing.assert_allclose(ours, gdal, rtol=1e-9)
    assert ours.sum() == pytest.approx(data.sum(), rel=1e-12)


def test_fa77_averaged_overviews_are_never_used(tmp_path):
    """The file has averaged overviews (like GHS-POP's .ovr): a downsampled
    read could pick them; FA77 always reads full resolution."""
    rng = np.random.default_rng(2)
    data = rng.uniform(0, 10, size=(80, 80))
    path = _write(tmp_path / "with_ovr.tif", data, nodata=None)
    with rasterio.open(path, "r+") as dataset:
        dataset.build_overviews([2, 4, 8], Resampling.average)
    with rasterio.open(path) as dataset:
        assert dataset.overviews(1) == [2, 4, 8]

    reads: list = []
    original = rasterio.io.DatasetReader.read

    def spy(self, *args, **kwargs):
        reads.append(kwargs)
        return original(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rasterio.io.DatasetReader, "read", spy)
        result = files.load(_layer(path, resolution_m=800), LoadContext())
    assert result.data.shape == (10, 10)
    assert result.data.sum() == pytest.approx(data.sum(), rel=1e-12)
    np.testing.assert_allclose(result.data[0, 0], data[0:8, 0:8].sum())
    assert reads and all("out_shape" not in kw for kw in reads)  # full resolution only


def test_fa77_region_window_is_aligned_to_whole_blocks_of_the_file_grid(tmp_path):
    data = np.ones((40, 40))
    path = _write(tmp_path / "ones.tif", data, nodata=None)
    # a region inside the raster, starting in the middle of a 1 km block
    with rasterio.open(path) as dataset:
        left, top = dataset.transform * (15, 12)  # col 15, row 12
        right, bottom = dataset.transform * (27, 26)
    region_3035 = gpd.GeoDataFrame(
        geometry=[box(left, bottom, right, top)], crs="EPSG:3035"
    )
    region = region_3035.to_crs("EPSG:4326")
    result = files.load(_layer(path), LoadContext(region=region))
    # blocks start on multiples of 10 source cells from the file origin
    assert (result.transform.c - ORIGIN[0]) % 1000 == 0
    assert (ORIGIN[1] - result.transform.f) % 1000 == 0
    # interior blocks are full: 100 source cells of value 1
    assert np.all(result.data[1:-1, 1:-1] == 100)


def test_fa77_multiband_aggregates_every_band(tmp_path):
    data = np.stack([np.ones((20, 20)), np.full((20, 20), 2.0)])
    path = _write(tmp_path / "two.tif", data, nodata=None)
    result = files.load(
        _layer(path, bands=[1, 2], band_names=["a", "b"]), LoadContext()
    )
    assert result.data.shape == (2, 2, 2)
    assert result.data[0].sum() == 400 and result.data[1].sum() == 800
    assert result.band_names == ["a", "b"]


def test_fa77_without_options_the_read_is_unchanged(tmp_path):
    data = _population()
    path = _write(tmp_path / "pop.tif", data)
    plain = files.load(
        FileLayer(id="pop", path=str(path), data_type="raster", format="tif"),
        LoadContext(),
    )
    assert plain.data.shape == data.shape


# =====================================================================
# Cell budget applies to the target raster
# =====================================================================


def test_fa77_budget_counts_the_target_cells_not_the_source_window(
    tmp_path, monkeypatch
):
    path = _write(tmp_path / "pop.tif", _population())
    monkeypatch.setattr(_raster_io, "MAX_CELLS", 20)
    with pytest.raises(ValueError, match="925 Zellen"):  # 25 x 37 at 100 m
        files.load(
            FileLayer(id="pop", path=str(path), data_type="raster", format="tif"),
            LoadContext(),
        )
    assert files.load(_layer(path), LoadContext()).data.size == 12  # 3 x 4 at 1 km
    monkeypatch.setattr(_raster_io, "MAX_CELLS", 11)
    with pytest.raises(
        ValueError, match=r"Zielraster.*12 Zellen.*resolution_m vergrößern"
    ):
        files.load(_layer(path), LoadContext())


def test_fa77_strips_give_the_same_result_as_one_read(tmp_path, monkeypatch):
    data = _population(rows=95, cols=63, seed=3)
    path = _write(tmp_path / "pop.tif", data)
    whole = files.load(_layer(path), LoadContext()).data
    monkeypatch.setattr(
        raster_format, "STRIP_SOURCE_CELLS", 1
    )  # one target row per strip
    stripped = files.load(_layer(path), LoadContext()).data
    np.testing.assert_array_equal(whole, stripped)


# =====================================================================
# Preconditions
# =====================================================================


def test_fa77_resolution_must_be_a_whole_multiple_of_the_source_cell(tmp_path):
    path = _write(tmp_path / "pop.tif", _population())
    with pytest.raises(ValueError, match="kein ganzzahliges Vielfaches.*100 m"):
        files.load(_layer(path, resolution_m=250), LoadContext())
    with pytest.raises(ValueError, match="kein ganzzahliges Vielfaches"):
        files.load(_layer(path, resolution_m=50), LoadContext())


def test_fa77_geographic_source_crs_is_an_error(tmp_path):
    path = _write(
        tmp_path / "geo.tif",
        np.ones((10, 10)),
        crs="EPSG:4326",
        cell=0.01,
        origin=(13.0, 51.0),
    )
    with pytest.raises(ValueError, match="projiziertes Quell-CRS in Metern"):
        files.load(_layer(path), LoadContext())


# =====================================================================
# Review findings 03.10.2026: region outside, nodata edge cases
# =====================================================================


def _region_3035(x0, y0, x1, y1) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(geometry=[box(x0, y0, x1, y1)], crs="EPSG:3035").to_crs(
        "EPSG:4326"
    )


@pytest.mark.parametrize(
    "bounds",
    [
        (3_900_000, 2_996_000, 3_950_000, 2_999_000),  # west of the raster
        (4_100_000, 2_996_000, 4_150_000, 2_999_000),  # east
        (4_001_000, 3_100_000, 4_004_000, 3_150_000),  # north
        (4_001_000, 2_850_000, 4_004_000, 2_900_000),  # south
    ],
)
def test_fa77_region_without_overlap_gives_an_empty_raster_like_the_plain_read(
    tmp_path, bounds
):
    # raster: x 4.0e6..4.005e6, y 2.995e6..3.0e6
    path = _write(tmp_path / "pop.tif", _population(rows=50, cols=50))
    ctx = LoadContext(region=_region_3035(*bounds))
    plain = files.load(
        FileLayer(id="pop", path=str(path), data_type="raster", format="tif"), ctx
    )
    aggregated = files.load(_layer(path), ctx)
    assert plain.data.size == 0
    assert aggregated.data.size == 0 and aggregated.data.ndim == 2


def test_fa77_block_sum_equal_to_the_file_nodata_switches_the_output_nodata_to_nan(
    tmp_path,
):
    data = np.ones((20, 20))
    data[0:10, 0:10] = -2.0  # block (0, 0): 100 x -2 = -200 = file nodata
    data[10:20, 10:20] = NODATA  # block (1, 1): no valid cell
    result = files.load(_layer(_write(tmp_path / "pop.tif", data)), LoadContext())
    assert np.isnan(result.nodata)
    assert result.data[0, 0] == pytest.approx(-200.0)  # a real value, not nodata
    assert np.isnan(result.data[1, 1])  # the empty block is nodata
    assert np.nansum(result.data) == pytest.approx(-200.0 + 200.0)


def test_fa77_source_without_nodata_marks_an_all_nan_block_as_nan(tmp_path):
    data = np.ones((20, 20))
    data[10:20, 0:10] = np.nan
    result = files.load(
        _layer(_write(tmp_path / "pop.tif", data, nodata=None)), LoadContext()
    )
    assert np.isnan(result.nodata)
    assert np.isnan(result.data[1, 0])
    assert result.data[0, 0] == 100 and np.nansum(result.data) == 300
    # without any empty block there is no nodata at all
    full = files.load(
        _layer(_write(tmp_path / "full.tif", np.ones((20, 20)), nodata=None)),
        LoadContext(),
    )
    assert full.nodata is None


def test_fa77_llm_prompt_lists_every_raster_option():
    """The web demo's prompt lists the tif options as complete; every field of
    RasterOptions (including resolution_m/aggregation) must be in it. Since
    FA79 the list comes from the registry (fields of source 'file'), no longer
    from a hand-written block."""
    pytest.importorskip("fastapi")
    from geofact_web.llm import build_system_prompt

    prompt = build_system_prompt([], [])
    section = prompt.split("\n- file: ", 1)[1].split("\n- ", 1)[0]
    for name in raster_format.RasterOptions.model_fields:
        assert f"    {name}?: " in section, name
        assert "Formatfeld (tif)" in section


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"resolution_m": 1000}, "gehören zusammen"),
        ({"aggregation": "sum", "resampling": "sum"}, "gehören zusammen"),
        (
            {"resolution_m": 1000, "aggregation": "sum", "resampling": "nearest"},
            "resampling: sum",
        ),
        (
            {"resolution_m": 1000, "aggregation": "mean", "resampling": "sum"},
            "summiert Mittelwerte",
        ),
        (
            {"resolution_m": 0, "aggregation": "sum", "resampling": "sum"},
            "resolution_m",
        ),
        (
            {"resolution_m": 1000, "aggregation": "median", "resampling": "sum"},
            "aggregation",
        ),
    ],
)
def test_fa77_options_are_validated(fields, message):
    with pytest.raises(ValidationError, match=message):
        FileLayer(id="pop", path="pop.tif", data_type="raster", format="tif", **fields)
