"""Implements: FA56 (Regionsplausibilität: Abdeckung der Region je geladenem Layer).

Contract-Tests für engine/loading.py: ein Vektor-Layer ohne Überdeckung der
Region erzeugt EINE Warnung mit beiden Ausdehnungen, ein leeres Rasterfenster
ist ein Fehler mit beiden Ausdehnungen, Quellarten mit strengerer Prüfung
behalten sie."""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import Point

from _loading_helpers import (
    DRESDEN_BBOX,
    FAR_AWAY_BBOX,
    FIXTURES_DIR,
    context,
    layer_from,
)
from geofact.core.errors import LayerLoadError
from geofact.core.registry import default_registry
from geofact.engine import events as _ev
from geofact.engine.loading import load_layer

SUBSTATIONS = {
    "id": "umspannwerke",
    "source": "file",
    "data_type": "vector",
    "path": str(FIXTURES_DIR / "substations.geojson"),
}


def _region_warnings(events: list) -> list:
    return [
        e
        for e in events
        if e.type == _ev.LAYER_WARNING and e.detail.get("category") == "RegionWarning"
    ]


def test_fa56_file_outside_region_warns_with_both_extents():
    events: list = []

    data = load_layer(
        layer_from(SUBSTATIONS),
        context(FAR_AWAY_BBOX),
        default_registry(),
        events.append,
    )

    warnings = _region_warnings(events)
    assert len(warnings) == 1
    message = warnings[0].message
    assert "umspannwerke" in message
    assert "13.72" in message and "51.03" in message  # Ausdehnung der Daten in Grad
    assert "10.0000,48.0000" in message  # Ausdehnung der Region in Grad
    assert warnings[0].detail["severity"] == "warning"
    assert len(data) > 0  # geladen wird trotzdem (Warnung, kein Fehler)


def test_fa56_layer_inside_region_does_not_warn():
    events: list = []

    load_layer(
        layer_from(SUBSTATIONS),
        context(DRESDEN_BBOX),
        default_registry(),
        events.append,
    )

    assert _region_warnings(events) == []


def test_fa56_empty_layer_passes_the_coverage_check():
    empty = gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:4326")
    events: list = []

    load_layer(
        layer_from(SUBSTATIONS),
        context(FAR_AWAY_BBOX),
        default_registry(),
        events.append,
        override=lambda _l: empty,
    )

    assert _region_warnings(events) == []


def test_fa56_raster_window_outside_dataset_is_an_error():
    layer = layer_from(
        {
            "id": "bev",
            "source": "file",
            "data_type": "raster",
            "path": str(FIXTURES_DIR / "population.tif"),
        }
    )

    with pytest.raises(LayerLoadError) as raised:
        load_layer(layer, context(FAR_AWAY_BBOX), default_registry())

    message = str(raised.value)
    assert "Rasterfenster ist leer" in message
    assert "13.7000,51.0300,13.7800,51.0700" in message  # Datei
    assert "10.0000,48.0000,10.2000,48.1000" in message  # Region


def test_fa56_points_keep_their_stricter_check():
    layer = layer_from(
        {
            "id": "orte",
            "source": "points",
            "features": [{"name": "Dresden", "lon": 13.74, "lat": 51.05}],
        }
    )

    with pytest.raises(LayerLoadError, match="außerhalb der Szenario-Region"):
        load_layer(layer, context(FAR_AWAY_BBOX), default_registry())


def test_fa56_projected_layer_outside_region_reports_extent_in_degrees():
    utm_points = gpd.GeoDataFrame(
        {"n": [1]}, geometry=[Point(411_000, 5_655_000)], crs="EPSG:32633"
    )
    events: list = []

    load_layer(
        layer_from(SUBSTATIONS),
        context(FAR_AWAY_BBOX),
        default_registry(),
        events.append,
        override=lambda _l: utm_points,
    )

    [warning] = _region_warnings(events)
    assert "13.7" in warning.message and "51.0" in warning.message
