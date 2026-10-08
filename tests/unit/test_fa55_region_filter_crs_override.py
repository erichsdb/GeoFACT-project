"""Implements: FA55 (crs_override), FA4 (region_filter) - Zusammenspiel.

Mit ``crs_override: true`` gilt das konfigurierte ``crs`` auch für den
Regionsfilter der Dateiquelle (builtin/_vector_io.py): der bbox-Pushdown, die
Verfeinerung und die Zählung außerhalb der Region rechnen im erzwungenen CRS,
nicht im (falschen) CRS der Datei.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, box

from geofact.builtin import _vector_io
from geofact.builtin.sources import file as files
from geofact.builtin.sources.file import FileLayer
from geofact.plugin_api import LoadContext

REGION = box(13.6, 51.0, 13.9, 51.1)  # Dresden


def _mislabelled_file(tmp_path: Path) -> str:
    """Punkte in Dresden mit UTM-33-Koordinaten (Meter), die Datei behauptet
    aber EPSG:4326 - der typische Fall für crs_override."""
    inside = gpd.GeoSeries([Point(13.72, 51.04), Point(13.75, 51.05)], crs="EPSG:4326")
    utm = inside.to_crs("EPSG:32633")
    gdf = gpd.GeoDataFrame({"name": ["a", "b"]}, geometry=list(utm), crs="EPSG:4326")
    path = tmp_path / "falsch_deklariert.gpkg"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        gdf.to_file(path, driver="GPKG")
    return str(path)


def _ctx() -> LoadContext:
    return LoadContext(region=gpd.GeoDataFrame(geometry=[REGION], crs="EPSG:4326"))


def _layer(path: str, **extra) -> FileLayer:
    return FileLayer(
        id="punkte",
        path=path,
        data_type="vector",
        crs="EPSG:32633",
        crs_override=True,
        **extra,
    )


def test_fa55_region_filter_bbox_uses_the_forced_crs(tmp_path):
    result = files.load(
        _layer(_mislabelled_file(tmp_path), region_filter="bbox"), _ctx()
    )
    assert len(result) == 2


def test_fa55_region_filter_intersects_uses_the_forced_crs(tmp_path):
    result = files.load(
        _layer(_mislabelled_file(tmp_path), region_filter="intersects"), _ctx()
    )
    assert len(result) == 2


def test_fa55_outside_region_count_uses_the_forced_crs(tmp_path):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = files.load(_layer(_mislabelled_file(tmp_path)), _ctx())
    assert len(result) == 2
    assert not [
        w for w in caught if issubclass(w.category, _vector_io.OutsideRegionWarning)
    ]


def test_fa55_without_override_the_file_crs_still_wins_for_the_filter(tmp_path):
    layer = FileLayer(
        id="punkte",
        path=_mislabelled_file(tmp_path),
        data_type="vector",
        crs="EPSG:32633",
        region_filter="bbox",
    )
    result = files.load(layer, _ctx())
    assert len(result) == 0
