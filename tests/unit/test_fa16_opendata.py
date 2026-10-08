"""Implements: FA16 (Open-Data-Konnektor: WFS + CKAN).

Contract-Tests für src/geofact/support/http.py, builtin/sources/wfs.py, ckan.py,
den generalisierten Snapshot-Mechanismus (snapshot.fetch_with_snapshot_key)
und die Schema-/Executor-Anbindung (WfsLayer, CkanLayer, source_types()).
Kein Netzzugriff: requests.get wird durch einen Fake ersetzt, analog zu
tests/unit/test_fa03_region.py."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
import requests
from shapely.geometry import Point, box

from geofact.core.scenario import Scenario
from geofact.plugin_api import LoadContext
from geofact.builtin.sources import ckan as ckan_module
from geofact.builtin.sources import osm
from geofact.support import http as http_module
from geofact.support import snapshot as snapshot_module
from geofact.builtin.sources import wfs as wfs_module
from geofact.builtin.sources.ckan import CkanLayer
from geofact.builtin.sources.wfs import WfsLayer

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _read_fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _leipzig_region() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["Leipzig"]},
        geometry=[box(12.30, 51.30, 12.40, 51.36)],
        crs="EPSG:4326",
    )


class _FakeStreamResponse:
    """Minimaler Stand-in für requests.Response im Streaming-Modus
    (Context-Manager + iter_content), wie ihn http.download_limited
    braucht."""

    def __init__(
        self, status_code: int = 200, content: bytes = b"", headers: dict | None = None
    ):
        self.status_code = status_code
        self._content = content
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


class _FakeJsonResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))


# =====================================================================
# http.py: Retry/Backoff, Timeout, Größenlimit
# =====================================================================


def test_fa16_http_get_json_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeJsonResponse(status_code=503)
        return _FakeJsonResponse(status_code=200, payload={"ok": True})

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    result = http_module.get_json("https://example.org/api", context="Test")
    assert calls["n"] == 3
    assert result == {"ok": True}


def test_fa16_http_get_json_persistent_failure_raises_actionable_error(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeJsonResponse(status_code=500)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    with pytest.raises(RuntimeError, match="https://example.org/api"):
        http_module.get_json("https://example.org/api", context="Test")


def test_fa16_http_download_limited_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        calls["n"] += 1
        if calls["n"] < 2:
            return _FakeStreamResponse(status_code=503)
        return _FakeStreamResponse(status_code=200, content=b"hello world")

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    result = http_module.download_limited("https://example.org/file", context="Test")
    assert result == b"hello world"
    assert calls["n"] == 2


def test_fa16_http_download_limited_aborts_on_content_length_over_limit(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(
            status_code=200,
            content=b"x" * 10,
            headers={"Content-Length": str(500 * 1024 * 1024)},
        )

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    monkeypatch.setenv("GEOFACT_OPENDATA_MAX_MB", "1")
    with pytest.raises(http_module.DownloadTooLargeError, match="500"):
        http_module.download_limited("https://example.org/big", context="Test")


def test_fa16_http_download_limited_aborts_mid_stream_without_content_length(
    monkeypatch,
):
    big_content = b"x" * (2 * 1024 * 1024)

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=big_content, headers={})

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    monkeypatch.setenv("GEOFACT_OPENDATA_MAX_MB", "1")
    with pytest.raises(http_module.DownloadTooLargeError):
        http_module.download_limited("https://example.org/big", context="Test")


# =====================================================================
# wfs.py
# =====================================================================


def test_fa16_wfs_getfeature_params_bbox_axis_order():
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    params = wfs_module.build_getfeature_params(layer, _leipzig_region())
    assert params["service"] == "WFS"
    assert params["typeNames"] == "ns:spielplaetze"
    assert params["outputFormat"] == "application/json"
    # urn-Form: lat,lon,lat,lon für EPSG:4326, nicht lon,lat,lon,lat
    min_lat, min_lon, max_lat, max_lon, crs_urn = params["bbox"].split(",")
    assert float(min_lat) == pytest.approx(51.30)
    assert float(min_lon) == pytest.approx(12.30)
    assert crs_urn == "urn:ogc:def:crs:EPSG::4326"


def test_fa16_wfs_fetch_parses_geojson(monkeypatch):
    content = _read_fixture_bytes("wfs_getfeature_response.json")

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    result = wfs_module.fetch(layer, _leipzig_region())
    assert len(result) == 3
    assert result.crs.to_epsg() == 4326


def test_fa16_wfs_fetch_refines_to_region(monkeypatch):
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": "in"},
                "geometry": {"type": "Point", "coordinates": [12.35, 51.33]},
            },
            {
                "type": "Feature",
                "properties": {"id": "out"},
                "geometry": {"type": "Point", "coordinates": [50.0, 50.0]},
            },
        ],
    }
    content = json.dumps(payload).encode("utf-8")

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    result = wfs_module.fetch(layer, _leipzig_region())
    assert list(result["id"]) == ["in"]


def test_fa16_wfs_empty_result_warns(monkeypatch):
    content = json.dumps({"type": "FeatureCollection", "features": []}).encode("utf-8")

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    with pytest.warns(wfs_module.EmptyWfsResultWarning):
        result = wfs_module.fetch(layer, _leipzig_region())
    assert len(result) == 0
    assert result.crs is not None


def test_fa16_wfs_error_message_contains_url_and_hint(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=500)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    with pytest.raises(RuntimeError, match="https://example.org/wfs"):
        wfs_module.load(layer, LoadContext(region=_leipzig_region()))


def test_fa16_wfs_layer_schema_requires_typename():
    with pytest.raises(ValueError):
        WfsLayer(id="w1", url="https://example.org/wfs")


# =====================================================================
# wfs.py: Paging (count/startIndex, Bugfix stille Antwort-Kappung)
# =====================================================================


def _geojson_bytes(n: int, offset: int = 0) -> bytes:
    """n Punkt-Features mit fortlaufender id (offset+0 .. offset+n-1)."""
    features = [
        {
            "type": "Feature",
            "properties": {"id": offset + i},
            "geometry": {"type": "Point", "coordinates": [12.35, 51.33]},
        }
        for i in range(n)
    ]
    return json.dumps({"type": "FeatureCollection", "features": features}).encode(
        "utf-8"
    )


def test_fa16_wfs_single_page_sends_one_request(monkeypatch):
    content = _geojson_bytes(3)
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        calls["n"] += 1
        assert params["startIndex"] == "0"
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    result = wfs_module.fetch(layer, _leipzig_region())
    assert len(result) == 3
    assert calls["n"] == 1


def test_fa16_wfs_paging_fetches_all_pages(monkeypatch):
    monkeypatch.setenv("GEOFACT_WFS_PAGE_SIZE", "10")
    # 2.5 Seiten: 10 + 10 + 5 = 25 Features insgesamt.
    pages = [
        _geojson_bytes(10, offset=0),
        _geojson_bytes(10, offset=10),
        _geojson_bytes(5, offset=20),
    ]
    captured_start_indexes = []

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        captured_start_indexes.append(params["startIndex"])
        assert params["count"] == "10"
        return _FakeStreamResponse(
            status_code=200, content=pages[len(captured_start_indexes) - 1]
        )

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    result = wfs_module.fetch(layer, _leipzig_region())
    assert len(result) == 25
    assert sorted(result["id"]) == list(range(25))
    assert captured_start_indexes == ["0", "10", "20"]


def test_fa16_wfs_paging_stop_on_numbermatched(monkeypatch):
    monkeypatch.setenv("GEOFACT_WFS_PAGE_SIZE", "10")
    # Erste Seite meldet numberMatched=10 - obwohl sie exakt PAGE_SIZE
    # Features lang ist (also für sich genommen eine weitere Seite
    # vermuten ließe), darf wegen numberMatched keine zweite Anfrage
    # erfolgen.
    payload = json.loads(_geojson_bytes(10))
    payload["numberMatched"] = 10
    content = json.dumps(payload).encode("utf-8")
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        calls["n"] += 1
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    result = wfs_module.fetch(layer, _leipzig_region())
    assert len(result) == 10
    assert calls["n"] == 1


def test_fa16_wfs_repeated_page_raises(monkeypatch):
    monkeypatch.setenv("GEOFACT_WFS_PAGE_SIZE", "10")
    # Server ignoriert startIndex und liefert bei jeder Anfrage dieselbe
    # volle Seite - muss als Fehler erkannt werden statt endlos zu loopen.
    content = _geojson_bytes(10)

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    with pytest.raises(RuntimeError, match="startIndex"):
        wfs_module.fetch(layer, _leipzig_region())


def test_fa16_wfs_paging_hard_cap_raises(monkeypatch):
    # Jede Seite ist voll (PAGE_SIZE Features) und trägt eine je Seite
    # unterschiedliche Geometrie, damit die Wiederholungsprüfung nicht
    # vorher greift - MAX_PAGES muss trotzdem terminieren.
    monkeypatch.setenv("GEOFACT_WFS_PAGE_SIZE", "1")
    monkeypatch.setattr(wfs_module, "MAX_PAGES", 3)
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        calls["n"] += 1
        feature = {
            "type": "Feature",
            "properties": {"id": calls["n"]},
            "geometry": {
                "type": "Point",
                "coordinates": [12.30 + calls["n"] * 0.001, 51.33],
            },
        }
        content = json.dumps(
            {"type": "FeatureCollection", "features": [feature]}
        ).encode("utf-8")
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    with pytest.raises(RuntimeError, match="MAX_PAGES"):
        wfs_module.fetch(layer, _leipzig_region())


def test_fa16_wfs_unparseable_payload_raises_with_context(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=b"not json and not xml")

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    with pytest.raises(RuntimeError, match="https://example.org/wfs"):
        wfs_module.fetch(layer, _leipzig_region())


# =====================================================================
# wfs.py: GML-Fallback (Server ohne JSON-outputFormat)
# =====================================================================

_EXCEPTION_REPORT = b"""<?xml version="1.0" encoding="UTF-8"?>
<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows/1.1" version="2.0.0">
  <ows:Exception exceptionCode="InvalidParameterValue" locator="outputFormat">
    <ows:ExceptionText>Invalid value for outputFormat: application/json</ows:ExceptionText>
  </ows:Exception>
</ows:ExceptionReport>"""

_GML_FEATURE_COLLECTION = b"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection
    xmlns:wfs="http://www.opengis.net/wfs/2.0"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:ns="http://example.org/ns"
    numberMatched="2" numberReturned="2">
  <wfs:member>
    <ns:spielplaetze gml:id="spielplaetze.1">
      <ns:name>Spielplatz Connewitz</ns:name>
      <ns:geometry>
        <gml:Point srsName="urn:ogc:def:crs:EPSG::4326" gml:id="p1">
          <gml:pos>51.3305 12.365</gml:pos>
        </gml:Point>
      </ns:geometry>
    </ns:spielplaetze>
  </wfs:member>
  <wfs:member>
    <ns:spielplaetze gml:id="spielplaetze.2">
      <ns:name>Spielplatz Markt</ns:name>
      <ns:geometry>
        <gml:Point srsName="urn:ogc:def:crs:EPSG::4326" gml:id="p2">
          <gml:pos>51.3405 12.3765</gml:pos>
        </gml:Point>
      </ns:geometry>
    </ns:spielplaetze>
  </wfs:member>
</wfs:FeatureCollection>"""


def _gml_driver_available() -> bool:
    try:
        gpd.read_file  # noqa: B018 - nur Import-/Attributpruefung
    except Exception:
        return False
    import tempfile as _tempfile

    tmp = _tempfile.NamedTemporaryFile(suffix=".gml", delete=False)
    try:
        tmp.write(_GML_FEATURE_COLLECTION)
        tmp.close()
        gpd.read_file(tmp.name)
        return True
    except Exception:
        return False
    finally:
        Path(tmp.name).unlink(missing_ok=True)


_GML_AVAILABLE = _gml_driver_available()


@pytest.mark.skipif(
    not _GML_AVAILABLE, reason="GDAL-GML-Treiber in dieser Umgebung nicht verfügbar"
)
def test_fa16_wfs_gml_fallback_parses_features(monkeypatch):
    responses = [_EXCEPTION_REPORT, _GML_FEATURE_COLLECTION]
    captured_params = []

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        captured_params.append(dict(params))
        content = responses[len(captured_params) - 1]
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    result = wfs_module.fetch(layer, _leipzig_region())
    assert len(result) == 2
    assert result.crs.to_epsg() == 4326
    # zweite Anfrage (GML-Fallback) darf outputFormat nicht mehr setzen
    assert "outputFormat" not in captured_params[1]
    assert "outputFormat" in captured_params[0]


@pytest.mark.skipif(
    not _GML_AVAILABLE, reason="GDAL-GML-Treiber in dieser Umgebung nicht verfügbar"
)
def test_fa16_wfs_both_json_and_gml_fail_raises_with_context(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=_EXCEPTION_REPORT)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(id="w1", url="https://example.org/wfs", typename="ns:spielplaetze")
    with pytest.raises(RuntimeError, match="outputFormat"):
        wfs_module.fetch(layer, _leipzig_region())


# =====================================================================
# ckan.py
# =====================================================================


def test_fa16_ckan_resource_show_resolution(monkeypatch):
    payload = json.loads(_read_fixture_bytes("ckan_resource_show.json"))

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "resource_show" in url
        assert params["id"] == "res-spielplaetze-001"
        return _FakeJsonResponse(status_code=200, payload=payload)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = CkanLayer(
        id="c1",
        base_url="https://opendata.leipzig.de",
        resource_id="res-spielplaetze-001",
    )
    resolved = ckan_module.resolve_resource(layer)
    assert resolved["format"] == "geojson"
    assert resolved["url"].endswith("spielplaetze.geojson")


def test_fa16_ckan_package_show_picks_geojson_by_format_hint(monkeypatch):
    payload = json.loads(_read_fixture_bytes("ckan_package_show.json"))

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "package_show" in url
        return _FakeJsonResponse(status_code=200, payload=payload)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = CkanLayer(
        id="c1",
        base_url="https://opendata.leipzig.de",
        dataset="spielplaetze",
        format="geojson",
    )
    resolved = ckan_module.resolve_resource(layer)
    assert resolved["format"] == "geojson"
    assert "res-spielplaetze-001" in resolved["url"]


def test_fa16_ckan_package_show_without_hint_prefers_geojson_over_csv(monkeypatch):
    payload = json.loads(_read_fixture_bytes("ckan_package_show.json"))

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeJsonResponse(status_code=200, payload=payload)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = CkanLayer(
        id="c1", base_url="https://opendata.leipzig.de", dataset="spielplaetze"
    )
    resolved = ckan_module.resolve_resource(layer)
    assert resolved["format"] == "geojson"


def test_fa16_ckan_api_error_flag_raises(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeJsonResponse(
            status_code=200,
            payload={"success": False, "error": {"message": "Not found"}},
        )

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = CkanLayer(
        id="c1", base_url="https://opendata.leipzig.de", resource_id="missing"
    )
    with pytest.raises(RuntimeError, match="Not found"):
        ckan_module.resolve_resource(layer)


def test_fa16_ckan_unsupported_format_actionable_error(monkeypatch):
    resource_show = {
        "success": True,
        "result": {"id": "r1", "format": "shapefile", "url": "https://x/r1.zip"},
    }

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        if "resource_show" in url:
            return _FakeJsonResponse(status_code=200, payload=resource_show)
        return _FakeStreamResponse(status_code=200, content=b"binary")

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = CkanLayer(id="c1", base_url="https://opendata.leipzig.de", resource_id="r1")
    with pytest.raises(RuntimeError, match="shapefile"):
        ckan_module.fetch(layer)


def test_fa16_ckan_fetch_geojson(monkeypatch):
    resource_show = json.loads(_read_fixture_bytes("ckan_resource_show.json"))
    geojson_bytes = _read_fixture_bytes("ckan_resource.geojson")

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        if "resource_show" in url:
            return _FakeJsonResponse(status_code=200, payload=resource_show)
        return _FakeStreamResponse(status_code=200, content=geojson_bytes)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = CkanLayer(
        id="c1",
        base_url="https://opendata.leipzig.de",
        resource_id="res-spielplaetze-001",
    )
    result = ckan_module.fetch(layer)
    assert len(result) == 2
    assert result.crs.to_epsg() == 4326


def test_fa16_ckan_layer_needs_resource_or_dataset():
    with pytest.raises(ValueError):
        CkanLayer(id="c1", base_url="https://opendata.leipzig.de")


# =====================================================================
# Snapshot-Generalisierung (FA7-Regression + neuer Payload-Pfad)
# =====================================================================


def test_fa16_snapshot_key_unchanged_for_osm_payload():
    region = _leipzig_region()
    tags = {"amenity": "doctors"}
    # snapshot_key() ruft intern payload_key() mit demselben Payload wie
    # vor der Generalisierung auf - der Hash darf sich NICHT ändern,
    # sonst invalidieren bestehende FA7-Snapshots.
    expected_payload = {
        "tags": osm._canonical_tags(tags),
        "region_wkt": region.union_all().wkt,
    }
    assert osm.snapshot_key(tags, region) == snapshot_module.payload_key(
        expected_payload
    )


def test_fa16_snapshot_key_matches_frozen_hash():
    """FA7-Regression gegen den ECHTEN Altbestand: die Schlüssel sind mit der
    Fassung vor dem Umbau der Paketstruktur (FA45; OSM-Schlüssel zog von
    support/snapshot.py nach builtin/sources/osm.py) berechnet und
    fest eingetragen. Ändert sich eine dieser Konstanten, treffen bestehende
    Snapshots unter GEOFACT_SNAPSHOT_DIR nicht mehr - dann ist der Umbau
    fehlerhaft, nicht der Test."""
    region = _leipzig_region()
    assert osm.snapshot_key({"amenity": "doctors"}, region) == (
        "7d51e99efd3a55aa7ca63217a834afe72dd158157efe604310cb9a913a162a05"
    )
    assert (
        osm.snapshot_key(
            [{"leisure": "park"}, {"landuse": "village_green"}, {"leisure": "garden"}],
            region,
        )
        == "f0ee0d457e822228ea2d6a5ed1e9511d50ced90b8bc464501789f460698fd44c"
    )
    assert osm.snapshot_key({"power": "substation", "voltage": "*"}, region) == (
        "cbcd936f10b3e601014d88250289d3b7146eb998d5b35a5a12c83a24de38f684"
    )


def test_fa16_snapshot_second_call_skips_fetch(tmp_path):
    calls = {"n": 0}

    def fetch_fn():
        calls["n"] += 1
        return gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")

    key = snapshot_module.payload_key({"kind": "test", "x": 1})
    first = snapshot_module.fetch_with_snapshot_key(key, fetch_fn)
    second = snapshot_module.fetch_with_snapshot_key(key, fetch_fn)
    assert calls["n"] == 1
    assert len(first) == len(second) == 1


# =====================================================================
# Schema-Union-Dispatch + Katalog
# =====================================================================


def test_fa16_layer_union_dispatch_wfs():
    minimal = {
        "scenario": {"name": "t", "region": "12.0,51.0,12.1,51.1"},
        "layers": [
            {"id": "w1", "source": "wfs", "url": "https://x/wfs", "typename": "ns:l"}
        ],
        "steps": [{"id": "s", "op": "to_points", "inputs": {"features": "w1"}}],
        "output": [{"type": "geojson", "source": "s", "path": "out.geojson"}],
    }
    scenario = Scenario(**minimal)
    assert isinstance(scenario.layers[0], WfsLayer)
    assert scenario.layers[0].source == "wfs"


def test_fa16_layer_union_dispatch_ckan():
    minimal = {
        "scenario": {"name": "t", "region": "12.0,51.0,12.1,51.1"},
        "layers": [
            {"id": "c1", "source": "ckan", "base_url": "https://x", "dataset": "d"}
        ],
        "steps": [{"id": "s", "op": "to_points", "inputs": {"features": "c1"}}],
        "output": [{"type": "geojson", "source": "s", "path": "out.geojson"}],
    }
    scenario = Scenario(**minimal)
    assert isinstance(scenario.layers[0], CkanLayer)
    assert scenario.layers[0].source == "ckan"


def test_fa16_catalog_lists_new_source_types():
    from geofact.engine import catalog

    sources = {s["source"] for s in catalog.source_types()}
    assert {"wfs", "ckan"} <= sources


# =====================================================================
# Nachtreview 02.10. (Runde 2): projizierte CRS bei CKAN und WFS
# =====================================================================


def test_fa16_ckan_resource_in_a_projected_crs_is_filtered_against_the_region(
    monkeypatch,
):
    """Eine CKAN-Ressource in EPSG:25833 brach den Regionsfilter (Regions-CRS
    EPSG:4326 != Daten-CRS); jetzt wird die Region in das Daten-CRS gebracht."""
    inside = gpd.GeoSeries([Point(12.35, 51.33)], crs="EPSG:4326").to_crs("EPSG:25833")[
        0
    ]
    outside = gpd.GeoSeries([Point(13.75, 51.05)], crs="EPSG:4326").to_crs(
        "EPSG:25833"
    )[0]
    data = gpd.GeoDataFrame(
        {"id": ["in", "out"]}, geometry=[inside, outside], crs="EPSG:25833"
    )
    monkeypatch.setattr(ckan_module, "fetch", lambda layer: data)
    layer = CkanLayer(id="c1", base_url="https://opendata.leipzig.de", resource_id="r1")

    result = ckan_module.load(layer, LoadContext(region=_leipzig_region()))

    assert list(result["id"]) == ["in"]


def _wfs_point_payload(srs: str, include_crs_member: bool) -> bytes:
    point = gpd.GeoSeries([Point(12.35, 51.33)], crs="EPSG:4326").to_crs(srs)[0]
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": "in"},
                "geometry": {"type": "Point", "coordinates": [point.x, point.y]},
            }
        ],
    }
    if include_crs_member:
        payload["crs"] = {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:EPSG::25833"},
        }
    return json.dumps(payload).encode("utf-8")


@pytest.mark.parametrize("include_crs_member", [False, True])
def test_fa16_wfs_json_in_the_requested_srsname_is_reprojected(
    monkeypatch, include_crs_member
):
    """GeoJSON im angefragten srsName (Meter) wurde als EPSG:4326 gelesen und
    vom Regionsfilter still komplett verworfen."""
    content = _wfs_point_payload("EPSG:25833", include_crs_member)

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(
        id="w1", url="https://example.org/wfs", typename="ns:x", srsname="EPSG:25833"
    )
    result = wfs_module.fetch(layer, _leipzig_region())
    assert list(result["id"]) == ["in"]
    assert result.crs.to_epsg() == 4326
    assert result.geometry[0].x == pytest.approx(12.35, abs=1e-6)


def test_fa16_wfs_rfc7946_json_despite_projected_srsname_stays_wgs84(monkeypatch):
    """Randfall: ein Server, der srsName für JSON ignoriert (RFC 7946, immer
    WGS 84), bleibt unverändert."""
    content = _wfs_point_payload("EPSG:4326", False)

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=content)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = WfsLayer(
        id="w1", url="https://example.org/wfs", typename="ns:x", srsname="EPSG:25833"
    )
    result = wfs_module.fetch(layer, _leipzig_region())
    assert list(result["id"]) == ["in"]
