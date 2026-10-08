"""Implements: FA7 (SnapshotNotice als Hinweis), FA4 (OutsideRegionWarning als Warnung), FA9 (LAYER_WARNING mit Schweregrad).

Eine Regel für die Darstellung: Warnungsklassen, deren Name auf ``Notice``
endet, sind Hinweise (``severity: info``), alle anderen Warnungen
(``severity: warning``)."""

from __future__ import annotations

import warnings

import geopandas as gpd
from shapely.geometry import Point

from _loading_helpers import FIXTURES_DIR, context, layer_from
from geofact.core.registry import default_registry
from geofact.engine import events as _ev
from geofact.engine.loading import load_layer, warning_severity
from geofact.support.snapshot import SnapshotNotice

SUBSTATIONS = {
    "id": "umspannwerke",
    "source": "file",
    "data_type": "vector",
    "path": str(FIXTURES_DIR / "substations.geojson"),
}


def _warnings(events):
    return {
        e.detail["category"]: e.detail["severity"]
        for e in events
        if e.type == _ev.LAYER_WARNING
    }


def test_fa07_warning_severity_rule():
    assert warning_severity("SnapshotNotice") == "info"
    assert warning_severity("StacSceneNotice") == "info"
    assert warning_severity("OutsideRegionWarning") == "warning"
    assert warning_severity("UserWarning") == "warning"


def test_fa07_snapshot_notice_is_presented_as_info():
    def from_snapshot(_layer):
        warnings.warn(
            "Snapshot abc verwendet statt eines frischen Bezugs", SnapshotNotice
        )
        return gpd.GeoDataFrame(
            {"n": [1]}, geometry=[Point(13.74, 51.05)], crs="EPSG:4326"
        )

    events: list = []
    load_layer(
        layer_from(SUBSTATIONS),
        context(),
        default_registry(),
        events.append,
        override=from_snapshot,
    )

    assert _warnings(events) == {"SnapshotNotice": "info"}


def test_fa04_outside_region_warning_is_presented_as_warning():
    events: list = []
    # Region schneidet nur einen Teil der Umspannwerke -> OutsideRegionWarning
    load_layer(
        layer_from(SUBSTATIONS),
        context("13.70,51.03,13.74,51.05"),
        default_registry(),
        events.append,
    )

    assert _warnings(events).get("OutsideRegionWarning") == "warning"
