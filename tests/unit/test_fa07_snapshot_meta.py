"""Implements: FA7 (Snapshot-Metadaten: <key>.meta.json, SnapshotNotice, Backend im Schlüssel).

Contract-Tests für den FA7-Zusatz in src/geofact/support/snapshot.py und den
OSM-Schlüssel in src/geofact/builtin/sources/osm.py.
"""

from __future__ import annotations

import json
import re
import warnings

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from geofact.builtin.sources import osm
from geofact.support import snapshot


def _frame() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["a"]}, geometry=[Point(13.7, 51.05)], crs="EPSG:4326"
    )


def _region() -> gpd.GeoDataFrame:
    polygon = Polygon([(13.6, 51.0), (13.9, 51.0), (13.9, 51.2), (13.6, 51.2)])
    return gpd.GeoDataFrame(geometry=[polygon], crs="EPSG:4326")


@pytest.fixture
def snapdir(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    return tmp_path


def test_fa07_fresh_fetch_writes_meta_with_fetched_at_backend_osm_base(snapdir):
    key = snapshot.payload_key({"kind": "t", "n": 1})
    snapshot.fetch_with_snapshot_key(
        key, _frame, meta={"backend": "overpass", "osm_base": "2026-10-01T12:00:00Z"}
    )
    meta = json.loads((snapdir / f"{key}.meta.json").read_text(encoding="utf-8"))
    assert meta["backend"] == "overpass"
    assert meta["osm_base"] == "2026-10-01T12:00:00Z"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", meta["fetched_at"])


def test_fa07_meta_without_source_info_still_records_fetched_at(snapdir):
    key = snapshot.payload_key({"kind": "t", "n": 2})
    snapshot.fetch_with_snapshot_key(key, _frame)
    meta = snapshot.snapshot_meta(key)
    assert meta is not None and meta["fetched_at"]
    assert meta["backend"] is None and meta["osm_base"] is None


def test_fa07_callable_meta_is_evaluated_after_the_fetch(snapdir):
    state: dict = {}

    def fetch():
        state["osm_base"] = "2026-09-30T00:00:00Z"
        return _frame()

    key = snapshot.payload_key({"kind": "t", "n": 3})
    snapshot.fetch_with_snapshot_key(key, fetch, meta=lambda: {"backend": "x", **state})
    assert snapshot.snapshot_meta(key)["osm_base"] == "2026-09-30T00:00:00Z"


def test_fa07_snapshot_meta_missing_returns_none(snapdir):
    assert snapshot.snapshot_meta("0" * 64) is None


def test_fa07_snapshot_hit_emits_notice_with_date_and_backend(snapdir):
    key = snapshot.payload_key({"kind": "t", "n": 4})
    snapshot.fetch_with_snapshot_key(key, _frame, meta={"backend": "postgis"})
    fetched_at = snapshot.snapshot_meta(key)["fetched_at"]
    with pytest.warns(snapshot.SnapshotNotice) as record:
        data = snapshot.fetch_with_snapshot_key(key, _frame)
    message = str(record[0].message)
    assert fetched_at in message and "postgis" in message and key[:12] in message
    assert len(data) == 1


def test_fa07_fresh_fetch_emits_no_notice(snapdir):
    key = snapshot.payload_key({"kind": "t", "n": 5})
    with warnings.catch_warnings():
        warnings.simplefilter("error", snapshot.SnapshotNotice)
        snapshot.fetch_with_snapshot_key(key, _frame)
        snapshot.fetch_with_snapshot_key(key, _frame, refresh=True)


def test_fa07_legacy_snapshot_without_meta_is_reported_not_rejected(snapdir):
    key = snapshot.payload_key({"kind": "t", "n": 6})
    snapshot.fetch_with_snapshot_key(key, _frame)
    (snapdir / f"{key}.meta.json").unlink()
    with pytest.warns(snapshot.SnapshotNotice, match="ohne Metadaten"):
        data = snapshot.fetch_with_snapshot_key(
            key, lambda: pytest.fail("kein Neubezug")
        )
    assert data["name"].tolist() == ["a"]


def test_fa07_unreadable_meta_is_reported(snapdir):
    key = snapshot.payload_key({"kind": "t", "n": 7})
    snapshot.fetch_with_snapshot_key(key, _frame)
    (snapdir / f"{key}.meta.json").write_text("{kaputt", encoding="utf-8")
    assert snapshot.snapshot_meta(key) is None
    with pytest.warns(snapshot.SnapshotNotice, match="unlesbar"):
        snapshot.fetch_with_snapshot_key(key, _frame)


def test_fa07_refresh_rewrites_meta(snapdir):
    key = snapshot.payload_key({"kind": "t", "n": 8})
    snapshot.fetch_with_snapshot_key(key, _frame, meta={"backend": "a"})
    snapshot.fetch_with_snapshot_key(key, _frame, refresh=True, meta={"backend": "b"})
    assert snapshot.snapshot_meta(key)["backend"] == "b"


def test_fa07_failed_fetch_writes_neither_snapshot_nor_meta(snapdir):
    key = snapshot.payload_key({"kind": "t", "n": 9})

    def broken():
        raise RuntimeError("Netz weg")

    with pytest.raises(RuntimeError):
        snapshot.fetch_with_snapshot_key(key, broken, meta={"backend": "x"})
    assert list(snapdir.iterdir()) == []


def test_fa07_osm_snapshot_key_contains_backend_only_if_not_overpass(snapdir):
    region = _region()

    class Backend:
        def __init__(self, name):
            self.name = name
            self.last_meta = {"backend": name, "osm_base": None}

        def fetch(self, tags, region):
            return _frame()

    osm.fetch_with_snapshot({"a": "b"}, region, Backend("overpass"))
    osm.fetch_with_snapshot({"a": "b"}, region, Backend("postgis"))
    overpass_key = osm.snapshot_key({"a": "b"}, region)
    postgis_key = osm.snapshot_key({"a": "b"}, region, backend="postgis")
    assert snapshot.snapshot_meta(overpass_key)["backend"] == "overpass"
    assert snapshot.snapshot_meta(postgis_key)["backend"] == "postgis"
    assert overpass_key != postgis_key


def test_fa07_osm_backend_without_name_keeps_the_legacy_key(snapdir):
    """Fake-Backends ohne ``name`` (und alle bestehenden Overpass-Snapshots)
    treffen weiterhin den Schlüssel von vor dem Zusatz."""
    region = _region()

    class Backend:
        def fetch(self, tags, region):
            return _frame()

    osm.fetch_with_snapshot({"a": "b"}, region, Backend())
    assert (snapdir / f"{osm.snapshot_key({'a': 'b'}, region)}.gpkg").is_file()
