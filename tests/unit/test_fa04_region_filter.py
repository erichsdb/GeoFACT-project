"""Implements: FA4 (Zusatz region_filter, Changelog 83).

Contract-Tests für FileLayer.region_filter (builtin/sources/file.py,
builtin/_vector_io.py): ``bbox`` liest per pyogrio-Pushdown nur Objekte im
Rechteck der Region, ``intersects`` verfeinert danach auf die Regionsgeometrie,
``none`` (Default) lädt alles und meldet Objekte außerhalb EINMAL; ein
Raster-Layer mit region_filter ist ein Konfigurationsfehler.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Polygon, box

from geofact.builtin import _vector_io
from geofact.builtin.sources import file as files
from geofact.builtin.sources.file import FileLayer
from geofact.plugin_api import LoadContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SUBSTATIONS = str(FIXTURES / "substations.geojson")

# Dreieck: sein Rechteck enthält die Punkte (13.72, 51.04) und (13.74, 51.045),
# die Fläche selbst nur den ersten. Die übrigen drei Punkte liegen außerhalb.
TRIANGLE = Polygon([(13.71, 51.035), (13.745, 51.035), (13.71, 51.05)])


def _ctx(geometry=TRIANGLE) -> LoadContext:
    return LoadContext(region=gpd.GeoDataFrame(geometry=[geometry], crs="EPSG:4326"))


def _layer(**extra) -> FileLayer:
    return FileLayer(id="umspannwerke", path=SUBSTATIONS, data_type="vector", **extra)


def test_fa04_region_filter_bbox_pushes_the_region_rectangle_into_the_reader(
    monkeypatch,
):
    seen: dict = {}
    original = gpd.read_file

    def spy(source, **kwargs):
        seen.update(kwargs)
        return original(source, **kwargs)

    monkeypatch.setattr(_vector_io.gpd, "read_file", spy)
    result = files.load(_layer(region_filter="bbox"), _ctx())
    assert "bbox" in seen  # Pushdown: der Treiber filtert, nicht erst GeoPandas
    assert len(result) == 2
    assert sorted(round(p.x, 3) for p in result.geometry) == [13.72, 13.74]


def test_fa04_region_filter_intersects_refines_to_the_region_geometry():
    result = files.load(_layer(region_filter="intersects"), _ctx())
    assert len(result) == 1
    assert round(result.geometry.iloc[0].x, 3) == 13.72


def test_fa04_region_filter_none_keeps_everything_and_warns_once():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = files.load(_layer(), _ctx())
    assert len(result) == 5
    outside = [
        w for w in caught if issubclass(w.category, _vector_io.OutsideRegionWarning)
    ]
    assert len(outside) == 1
    message = str(outside[0].message)
    assert (
        "umspannwerke" in message
        and "3 von 5" in message
        and "region_filter" in message
    )


def test_fa04_region_filter_none_inside_region_stays_silent():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        files.load(_layer(), _ctx(box(13.6, 51.0, 13.9, 51.1)))
    assert not [
        w for w in caught if issubclass(w.category, _vector_io.OutsideRegionWarning)
    ]


def test_fa04_region_filter_on_raster_is_a_config_error():
    with pytest.raises(ValidationError) as caught:
        FileLayer(
            id="pop",
            path=str(FIXTURES / "population.tif"),
            data_type="raster",
            region_filter="bbox",
        )
    assert "region_filter" in str(caught.value)
    assert "Raster" in str(caught.value)


def test_fa04_region_filter_rejects_unknown_mode():
    with pytest.raises(ValidationError):
        _layer(region_filter="within")


def test_fa04_region_filter_empty_result_is_an_empty_layer():
    result = files.load(
        _layer(region_filter="intersects"), _ctx(box(10.0, 50.0, 10.1, 50.1))
    )
    assert len(result) == 0
    assert isinstance(result, gpd.GeoDataFrame)


def test_fa04_region_filter_without_region_reads_everything():
    result = files.load(_layer(region_filter="intersects"), LoadContext())
    assert len(result) == 5


def test_fa04_count_outside_region_uses_bounds_of_each_object():
    gdf = gpd.read_file(SUBSTATIONS)
    assert _vector_io.count_outside_region(gdf, _ctx()) == 3
    assert _vector_io.count_outside_region(gdf.iloc[0:0], _ctx()) == 0
