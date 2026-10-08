"""Implements: FA55 (target CRS per output: geojson, csv, geotiff).

Contract tests of the output option ``crs`` (builtin/outputs/export.py,
builtin/outputs/geotiff.py): geojson defaults to EPSG:4326, csv and geotiff to
the working CRS; an unknown CRS is rejected at validation with position
``output -> i -> crs``; ``map`` has no ``crs`` option; ``geotiff`` warps over
``_raster_io.warp_raster`` with ``resampling``.

The writers are called through the registry (``plugin.write(data, target, spec)``)
so the tests do not depend on how ``engine/outputs.py`` binds run data."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import rasterio
from affine import Affine
from pyproj import CRS
from shapely import wkt as shapely_wkt
from shapely.geometry import Point

from geofact import api
from geofact.core.errors import ConfigError
from geofact.core.types import RasterLayer

HEAD = """
scenario:
  name: Ziel-CRS
  region: "13.0,50.9,13.1,51.0"
layers:
  - {id: v, source: file, path: data/v.geojson, data_type: vector}
  - {id: r, source: file, path: data/r.tif, data_type: raster, format: tif}
steps:
  - {id: b, op: buffer, inputs: {geometry: v}, params: {radius_km: 1}}
output:
"""

UTM33 = CRS.from_epsg(32633)


def _spec(output: str):
    return api.load_scenario(HEAD + output).scenario.output[0]


def _write(spec, data, target):
    api.registry().output(spec.type).write(data, target, spec)


def _points_utm() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[Point(13.05, 50.95), Point(13.08, 50.97)],
        crs="EPSG:4326",
    ).to_crs(UTM33)


def _raster() -> RasterLayer:
    data = np.arange(400, dtype="float32").reshape(20, 20)
    return RasterLayer(
        data=data,
        transform=Affine(100, 0, 352000, 0, -100, 5646000),
        crs=UTM33,
        nodata=float("nan"),
        band_names=["wert"],
    )


def test_fa55_geojson_defaults_to_wgs84(tmp_path):
    target = tmp_path / "o.geojson"
    _write(_spec("  - {type: geojson, source: v}\n"), _points_utm(), target)
    back = gpd.read_file(target)
    assert back.crs.to_epsg() == 4326
    assert back.geometry.iloc[0].x == pytest.approx(13.05, abs=1e-6)


def test_fa55_geojson_writes_the_declared_target_crs(tmp_path):
    target = tmp_path / "o.geojson"
    _write(
        _spec("  - {type: geojson, source: v, crs: 'EPSG:25833'}\n"),
        _points_utm(),
        target,
    )
    back = gpd.read_file(target)
    assert back.crs.to_epsg() == 25833
    expected = _points_utm().to_crs("EPSG:25833").geometry.iloc[0]
    assert back.geometry.iloc[0].x == pytest.approx(expected.x, abs=1e-3)


def test_fa55_csv_defaults_to_the_working_crs(tmp_path):
    target = tmp_path / "o.csv"
    data = _points_utm()
    _write(_spec("  - {type: csv, source: v}\n"), data, target)
    first = shapely_wkt.loads(pd.read_csv(target)["wkt"].iloc[0])
    assert first.x == pytest.approx(data.geometry.iloc[0].x)


def test_fa55_csv_writes_the_declared_target_crs(tmp_path):
    target = tmp_path / "o.csv"
    _write(
        _spec("  - {type: csv, source: v, crs: 'EPSG:4326'}\n"), _points_utm(), target
    )
    first = shapely_wkt.loads(pd.read_csv(target)["wkt"].iloc[0])
    assert (first.x, first.y) == (
        pytest.approx(13.05, abs=1e-6),
        pytest.approx(50.95, abs=1e-6),
    )


def test_fa55_unknown_output_crs_is_rejected_with_position():
    with pytest.raises(ConfigError) as exc:
        _spec("  - {type: geojson, source: v, crs: 'EPSG:9999'}\n")
    [(position, message)] = exc.value.issues
    assert position == "output -> 0 -> crs"
    assert message.startswith("unbekanntes CRS 'EPSG:9999' (pyproj:")


def test_fa55_map_has_no_crs_option():
    with pytest.raises(ConfigError) as exc:
        _spec("  - {type: map, source: v, crs: 'EPSG:4326'}\n")
    [(position, message)] = exc.value.issues
    assert position == "output -> 0 -> crs"
    assert "unbekanntes Feld für Ausgabeformat 'map'" in message


def test_fa55_geotiff_defaults_to_the_working_crs(tmp_path):
    target = tmp_path / "r.tif"
    raster = _raster()
    _write(_spec("  - {type: geotiff, source: r}\n"), raster, target)
    with rasterio.open(target) as dataset:
        assert dataset.crs.to_epsg() == 32633 and dataset.transform == raster.transform
        assert np.array_equal(dataset.read(1), raster.data)


def test_fa55_geotiff_warps_to_the_declared_crs_with_resampling(tmp_path):
    target = tmp_path / "r.tif"
    spec = _spec(
        "  - {type: geotiff, source: r, crs: 'EPSG:4326', resampling: bilinear}\n"
    )
    assert spec.options.resampling == "bilinear"
    _write(spec, _raster(), target)
    with rasterio.open(target) as dataset:
        assert dataset.crs.to_epsg() == 4326
        assert dataset.descriptions == ("wert",)
        west, south, east, north = dataset.bounds
        assert 12.8 < west < east < 13.4 and 50.7 < south < north < 51.1


def test_fa55_geotiff_resampling_without_crs_is_rejected():
    with pytest.raises(ConfigError) as exc:
        _spec("  - {type: geotiff, source: r, resampling: average}\n")
    assert "resampling" in exc.value.issues[0][1] and "crs" in exc.value.issues[0][1]


def test_fa55_output_crs_on_a_layer_without_crs_is_an_error(tmp_path):
    data = gpd.GeoDataFrame({"name": ["a"]}, geometry=[Point(1, 2)], crs=None)
    with pytest.raises(ValueError, match="ohne CRS"):
        _write(
            _spec("  - {type: csv, source: v, crs: 'EPSG:4326'}\n"),
            data,
            tmp_path / "o.csv",
        )
