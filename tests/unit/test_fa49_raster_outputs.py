"""Implements: FA49 (Raster-Ausgaben: type geotiff, Raster auf der Karte), FA12 (map: normalize_by_area).

Contract tests of the output `geotiff` (builtin/outputs/geotiff.py) and of the raster
branch of the output `map` (builtin/outputs/map.py).
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import CRS
from rasterio.transform import array_bounds
from rasterio.warp import transform_bounds
from shapely.geometry import box

from geofact import api
from geofact.builtin.outputs import map as map_output
from geofact.builtin.outputs.geotiff import write_geotiff
from geofact.builtin.outputs.map import MapOptions, render_raster_map
from geofact.core.scenario import Scenario
from geofact.core.store import LayerStore
from geofact.core.types import RasterLayer
from geofact.engine.executor import ExecutionResult
from geofact.engine.outputs import write_outputs

TRANSFORM = Affine(10, 0, 352000, 0, -10, 5636000)
CRS33 = CRS.from_epsg(32633)


def _raster(data, **fields) -> RasterLayer:
    return RasterLayer(data=np.asarray(data), transform=TRANSFORM, crs=CRS33, **fields)


def _ndvi_like(rows: int = 30, cols: int = 40) -> RasterLayer:
    data = np.linspace(-0.2, 0.9, rows * cols, dtype="float32").reshape(rows, cols)
    data[:3, :3] = np.nan
    return _raster(data, nodata=float("nan"), band_names=["ndvi"])


RASTER_LAYER = {
    "id": "r",
    "source": "file",
    "path": "x.tif",
    "data_type": "raster",
    "format": "tif",
}
VECTOR_LAYER = {"id": "v", "source": "file", "path": "x.geojson", "data_type": "vector"}


def _config(outputs: list[dict]) -> dict:
    """A scenario whose outputs read the raster layer `r` directly."""
    return {
        "scenario": {"name": "t", "region": "13.0,50.9,13.1,51.0"},
        "layers": [RASTER_LAYER, VECTOR_LAYER],
        "steps": [
            {
                "id": "b",
                "op": "buffer",
                "inputs": {"geometry": "v"},
                "params": {"radius_km": 1},
            }
        ],
        "output": outputs,
    }


def _outcome(tmp_path, raster, outputs):
    scenario = Scenario.model_validate(_config(outputs))
    store = LayerStore()
    store["r"] = raster
    return write_outputs(scenario, ExecutionResult(store=store, order=[]), tmp_path)


# =====================================================================
# geotiff
# =====================================================================


def test_fa49_geotiff_round_trip_of_a_float_raster_with_nan_nodata(tmp_path):
    raster = _ndvi_like()
    target = tmp_path / "ndvi.tif"
    write_geotiff(raster, target)
    with rasterio.open(target) as dataset:
        assert dataset.count == 1 and dataset.dtypes[0] == "float32"
        assert dataset.crs.to_epsg() == 32633 and dataset.transform == TRANSFORM
        assert np.isnan(dataset.nodata) and dataset.descriptions == ("ndvi",)
        assert np.array_equal(dataset.read(1), raster.data, equal_nan=True)


def test_fa49_geotiff_keeps_every_band_with_its_name_and_dtype(tmp_path):
    data = np.stack([np.full((6, 7), 100, "uint16"), np.full((6, 7), 200, "uint16")])
    raster = _raster(data, nodata=0, band_names=["red", "nir"])
    write_geotiff(raster, tmp_path / "szene.tif")
    with rasterio.open(tmp_path / "szene.tif") as dataset:
        assert dataset.count == 2 and dataset.dtypes == ("uint16", "uint16")
        assert dataset.descriptions == ("red", "nir") and dataset.nodata == 0
        assert dataset.read()[1, 0, 0] == 200


def test_fa49_geotiff_uint8_mask_with_nodata_255(tmp_path):
    raster = _raster(
        np.array([[0, 1], [255, 1]], dtype="uint8"), nodata=255, band_names=["gruen"]
    )
    write_geotiff(raster, tmp_path / "m.tif")
    with rasterio.open(tmp_path / "m.tif") as dataset:
        assert dataset.nodata == 255 and dataset.dtypes[0] == "uint8"
        assert dataset.read(1).tolist() == [[0, 1], [255, 1]]


def test_fa49_geotiff_without_names_or_nodata(tmp_path):
    write_geotiff(_raster(np.ones((3, 3), "int16")), tmp_path / "plain.tif")
    with rasterio.open(tmp_path / "plain.tif") as dataset:
        assert dataset.nodata is None and dataset.descriptions == (None,)


def test_fa49_geotiff_creates_the_directory(tmp_path):
    write_geotiff(_raster(np.ones((2, 2), "uint8")), tmp_path / "a" / "b" / "c.tif")
    assert (tmp_path / "a" / "b" / "c.tif").is_file()


def test_fa49_geotiff_refuses_nan_nodata_for_an_integer_raster(tmp_path):
    with pytest.raises(ValueError, match="NaN"):
        write_geotiff(
            _raster(np.ones((2, 2), "uint8"), nodata=float("nan")), tmp_path / "x.tif"
        )


def test_fa49_geotiff_refuses_a_raster_without_crs(tmp_path):
    raster = RasterLayer(data=np.ones((2, 2), "uint8"), transform=TRANSFORM, crs=None)
    with pytest.raises(ValueError, match="CRS"):
        write_geotiff(raster, tmp_path / "x.tif")


def test_fa49_geotiff_output_is_written_through_the_scenario(tmp_path):
    outcome = _outcome(
        tmp_path, _ndvi_like(), [{"type": "geotiff", "source": "r", "path": "ndvi.tif"}]
    )
    assert [p.name for p in outcome.written] == ["ndvi.tif"] and outcome.skipped == []
    with rasterio.open(tmp_path / "ndvi.tif") as dataset:
        assert dataset.shape == (30, 40)


def test_fa49_geotiff_default_file_name_uses_the_tif_extension(tmp_path):
    outcome = _outcome(tmp_path, _ndvi_like(), [{"type": "geotiff", "source": "r"}])
    assert [p.name for p in outcome.written] == ["00_r.tif"]


def test_fa49_geotiff_failure_is_a_skipped_output_with_a_reason_not_a_crash(tmp_path):
    outcome = _outcome(
        tmp_path,
        _raster(np.ones((2, 2), "uint8"), nodata=float("nan")),
        [{"type": "geotiff", "source": "r", "path": "bad.tif"}],
    )
    assert outcome.written == [] and len(outcome.skipped) == 1
    assert "NaN" in outcome.skipped[0].reason
    assert not (tmp_path / "bad.tif").exists()


def test_fa49_geotiff_empty_raster_is_a_skipped_output_not_a_crash(tmp_path):
    """Edge case 'empty layer'."""
    outcome = _outcome(
        tmp_path,
        _raster(np.zeros((0, 0), "uint8")),
        [{"type": "geotiff", "source": "r", "path": "empty.tif"}],
    )
    assert outcome.written == [] and len(outcome.skipped) == 1
    assert not (tmp_path / "empty.tif").exists()


def test_fa49_geotiff_accepts_only_rasters_checked_before_the_run():
    config = {
        "scenario": {"name": "t", "region": "13.0,50.9,13.1,51.0"},
        "layers": [
            {"id": "v", "source": "file", "path": "x.geojson", "data_type": "vector"}
        ],
        "steps": [
            {
                "id": "b",
                "op": "buffer",
                "inputs": {"geometry": "v"},
                "params": {"radius_km": 1},
            }
        ],
        "output": [{"type": "geotiff", "source": "b"}],
    }
    issues = api.validate(config).issues
    assert issues == [
        (
            "output -> 0 -> source",
            "Ausgabeformat 'geotiff' schreibt nur Raster, die Quelle 'b' liefert aber Vektor",
        )
    ]


def test_fa49_geotiff_is_catalogued_as_a_raster_output():
    entries = {e["name"]: e for e in api.extension_catalog()["outputs"]}
    assert (
        entries["geotiff"]["accepts"] == ["raster"]
        and entries["geotiff"]["extension"] == "tif"
    )
    assert entries["map"]["accepts"] == ["graph", "raster", "vector"]  # graph: FA53


# =====================================================================
# map: raster overlay
# =====================================================================


def _overlays(fmap):
    return [
        child
        for child in fmap._children.values()
        if type(child).__name__ == "ImageOverlay"
    ]


def test_fa49_map_draws_a_raster_as_a_png_overlay_with_legend(tmp_path):
    outcome = _outcome(
        tmp_path,
        _ndvi_like(),
        [
            {
                "type": "map",
                "source": "r",
                "path": "karte.html",
                "colormap": "rdylgn",
                "vmin": 0,
                "vmax": 0.9,
            }
        ],
    )
    assert outcome.skipped == [] and [p.name for p in outcome.written] == ["karte.html"]
    html = (tmp_path / "karte.html").read_text(encoding="utf-8")
    assert re.search(r"data:image/png;base64,[A-Za-z0-9+/=]{100,}", html)
    assert "ndvi" in html  # legend caption = band name


def test_fa49_map_overlay_bounds_are_the_wgs84_bounds_of_the_raster():
    raster = _ndvi_like()
    (overlay,) = _overlays(render_raster_map(raster))
    (south, west), (north, east) = overlay.bounds
    expected = transform_bounds(
        "EPSG:32633", "EPSG:4326", *array_bounds(30, 40, TRANSFORM)
    )
    # the Web-Mercator grid of the overlay only extends by a fraction of a cell
    assert (west, south, east, north) == pytest.approx(expected, abs=2e-3)


def test_fa49_map_overlay_pixels_are_transparent_for_nodata_and_coloured_otherwise():
    colors = map_output._colorize(
        np.array([[np.nan, 0.0, 1.0]], dtype="float32"), "gray", 0.0, 1.0
    )
    assert colors.shape == (1, 3, 4)
    assert colors[0, 0, 3] == 0  # nodata: transparent
    assert colors[0, 1].tolist() == [0, 0, 0, 255] and colors[0, 2].tolist() == [
        255,
        255,
        255,
        255,
    ]


def test_fa49_map_colormaps_interpolate_between_their_stops():
    low, mid, high = map_output._colorize(
        np.array([[0.0, 0.5, 1.0]], "float32"), "viridis", 0, 1
    )[0]
    assert low[:3].tolist() == [0x44, 0x01, 0x54] and high[:3].tolist() == [
        0xFD,
        0xE7,
        0x25,
    ]
    assert not np.array_equal(mid[:3], low[:3]) and not np.array_equal(
        mid[:3], high[:3]
    )


def test_fa49_map_overlay_is_downsampled_to_the_pixel_budget(monkeypatch):
    monkeypatch.setattr(map_output, "MAX_OVERLAY_PIXELS", 400)
    raster = _ndvi_like(rows=100, cols=120)
    grid, _ = map_output._overlay_grid(raster, raster.data)
    assert grid.size <= 400 * 1.3  # budget, plus rounding up of the shape
    assert grid.shape[0] < 100 and grid.shape[1] < 120


def test_fa49_map_categorical_raster_is_not_blended():
    """Class rasters (integer dtype) are resampled with nearest: no value outside the classes."""
    data = np.tile(np.array([0, 1, 2, 2], dtype="uint8"), (40, 25))
    raster = _raster(data)
    grid, _ = map_output._overlay_grid(raster, data.astype("float32"))
    values = set(np.unique(grid[np.isfinite(grid)]))
    assert values <= {0.0, 1.0, 2.0}


def test_fa49_map_needs_a_band_for_a_multi_band_raster_and_accepts_one(tmp_path):
    data = np.stack(
        [
            np.full((10, 10), 1.0, "float32"),
            np.arange(100, dtype="float32").reshape(10, 10),
        ]
    )
    raster = _raster(data, band_names=["a", "b"])
    skipped = _outcome(
        tmp_path, raster, [{"type": "map", "source": "r", "path": "m1.html"}]
    )
    assert len(skipped.skipped) == 1 and "2 Bänder" in skipped.skipped[0].reason
    written = _outcome(
        tmp_path,
        raster,
        [{"type": "map", "source": "r", "path": "m2.html", "band": "b"}],
    )
    assert written.skipped == [] and (tmp_path / "m2.html").is_file()


def test_fa49_map_raster_without_valid_cells_is_a_skipped_output(tmp_path):
    """Edge case 'empty layer': nothing to draw -> skipped with a reason."""
    raster = _raster(np.full((5, 5), np.nan, "float32"), nodata=float("nan"))
    outcome = _outcome(
        tmp_path, raster, [{"type": "map", "source": "r", "path": "leer.html"}]
    )
    assert (
        len(outcome.skipped) == 1
        and "keine gültigen Zellen" in outcome.skipped[0].reason
    )


def test_fa49_map_constant_raster_still_renders(tmp_path):
    outcome = _outcome(
        tmp_path,
        _raster(np.full((5, 5), 3.0, "float32")),
        [{"type": "map", "source": "r", "path": "konst.html"}],
    )
    assert outcome.skipped == [] and (tmp_path / "konst.html").is_file()


def test_fa49_map_options_are_validated_with_position():
    config = _config(
        [{"type": "map", "source": "r", "colormap": "rainbow", "vmin": "viel"}]
    )
    locations = [location for location, _ in api.validate(config).issues]
    assert "output -> 0 -> colormap" in locations and "output -> 0 -> vmin" in locations


def test_fa49_map_vmax_below_vmin_is_an_error():
    with pytest.raises(ValueError, match="vmax"):
        render_raster_map(_ndvi_like(), vmin=1.0, vmax=0.0)


def test_fa49_map_raster_without_crs_is_an_error():
    raster = RasterLayer(data=np.ones((3, 3), "float32"), transform=TRANSFORM, crs=None)
    with pytest.raises(ValueError, match="CRS"):
        render_raster_map(raster)


# =====================================================================
# map: vector colour field is a share, not a density (normalize_by_area)
# =====================================================================


def _districts() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"gruenanteil": [0.2, 0.8]},
        geometry=[box(13.0, 50.8, 13.01, 50.81), box(13.01, 50.8, 13.05, 50.84)],
        crs="EPSG:4326",
    )


def test_fa12_map_colours_polygons_by_value_per_area_by_default(tmp_path):
    spec = SimpleNamespace(options=MapOptions(color_field="gruenanteil"))
    map_output._write_output(_districts(), tmp_path / "a.html", spec)
    assert "pro km" in (tmp_path / "a.html").read_text(encoding="utf-8")


def test_fa12_map_normalize_by_area_false_colours_by_the_value_itself(tmp_path):
    spec = SimpleNamespace(
        options=MapOptions(color_field="gruenanteil", normalize_by_area=False)
    )
    map_output._write_output(_districts(), tmp_path / "b.html", spec)
    html = (tmp_path / "b.html").read_text(encoding="utf-8")
    assert "pro km" not in html and "gruenanteil" in html


def test_fa12_map_options_defaults_keep_the_old_behaviour():
    options = MapOptions()
    assert options.color_field is None and options.normalize_by_area is True
    assert options.band is None and options.colormap == "viridis"
    assert options.vmin is None and options.vmax is None


def test_fa12_map_still_draws_a_vector_result(tmp_path):
    config = {
        "scenario": {"name": "t", "region": "13.0,50.9,13.1,51.0"},
        "layers": [
            {"id": "v", "source": "file", "path": "x.geojson", "data_type": "vector"}
        ],
        "steps": [
            {
                "id": "b",
                "op": "buffer",
                "inputs": {"geometry": "v"},
                "params": {"radius_km": 1},
            }
        ],
        "output": [
            {
                "type": "map",
                "source": "b",
                "path": "v.html",
                "color_field": "gruenanteil",
                "normalize_by_area": False,
            }
        ],
    }
    scenario = Scenario.model_validate(config)
    store = LayerStore()
    store["b"] = _districts()
    outcome = write_outputs(
        scenario, ExecutionResult(store=store, order=["b"]), tmp_path
    )
    assert outcome.skipped == [] and (tmp_path / "v.html").is_file()
