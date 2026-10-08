"""Implements: FA3 (Region-Auflösung), FA6 (spatial_check-Referenz).

Contract-Tests für src/geofact/engine/region.py. Kein Netzzugriff:
Namensauflösung nutzt ausschließlich eine eingecheckte Fixture-Antwort.
"""

import json
from pathlib import Path

import pytest
import requests

from geofact.engine import region as region_module

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def fixture_fetcher(name: str) -> list[dict]:
    raw = json.loads(
        (FIXTURES_DIR / "nominatim_dresden.json").read_text(encoding="utf-8")
    )
    return raw if name == "Dresden, Deutschland" else []


def empty_fetcher(name: str) -> list[dict]:
    return []


def test_fa03_region_bbox_literal_parsed():
    gdf = region_module.resolve_region("13.60,51.01,13.94,51.15")
    assert len(gdf) == 1
    bounds = gdf.geometry.iloc[0].bounds
    assert bounds == pytest.approx((13.60, 51.01, 13.94, 51.15))
    assert gdf.crs.to_epsg() == 4326


def test_fa03_region_name_resolved_from_cached_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    gdf = region_module.resolve_region("Dresden, Deutschland", fetcher=fixture_fetcher)
    assert len(gdf) == 1
    assert gdf.geometry.iloc[0].bounds == pytest.approx((13.60, 51.01, 13.94, 51.15))

    # zweiter Aufruf liest aus dem Cache, ruft den Fetcher nicht mehr auf
    calls = []

    def counting_fetcher(name: str) -> list[dict]:
        calls.append(name)
        return fixture_fetcher(name)

    gdf_cached = region_module.resolve_region(
        "Dresden, Deutschland", fetcher=counting_fetcher
    )
    assert len(calls) == 0
    assert gdf_cached.geometry.iloc[0].bounds == pytest.approx(
        (13.60, 51.01, 13.94, 51.15)
    )


def test_fa03_region_unresolvable_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="Nirgendwo, Nichtland"):
        region_module.resolve_region("Nirgendwo, Nichtland", fetcher=empty_fetcher)


# =====================================================================
# Nominatim-Härtung: Retry/Backoff + Rate-Limit + Fehler-Wrapping
# (_default_fetcher, kein Netzzugriff - requests.get wird durch einen
# Fake ersetzt, time.sleep wird abgeschnitten, damit die Tests schnell
# bleiben und keinen echten Backoff/Rate-Limit-Takt abwarten).
# =====================================================================


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: list[dict] | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def json(self) -> list[dict]:
        return self._payload


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Verhindert, dass Backoff- und Rate-Limit-Sleeps die Tests verlangsamen."""
    monkeypatch.setattr(region_module.time, "sleep", lambda _seconds: None)
    # Rate-Limiter-Zustand zwischen Tests zurücksetzen (Modul-globaler Timer).
    monkeypatch.setattr(region_module, "_last_request_at", 0.0)


def test_fa03_default_fetcher_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return _FakeResponse(status_code=503)
        return _FakeResponse(
            status_code=200,
            payload=[{"geojson": {"type": "Point", "coordinates": [0, 0]}}],
        )

    monkeypatch.setattr(region_module.requests, "get", fake_get)

    result = region_module._default_fetcher("Testregion")

    assert calls["n"] == 3
    assert result[0]["geojson"]["type"] == "Point"


def test_fa03_default_fetcher_persistent_failure_raises_clear_error(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, headers=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse(status_code=429)

    monkeypatch.setattr(region_module.requests, "get", fake_get)

    with pytest.raises(RuntimeError, match="Testregion"):
        region_module._default_fetcher("Testregion")

    assert calls["n"] == region_module.NOMINATIM_MAX_RETRIES


def test_fa03_default_fetcher_rate_limits_between_calls(monkeypatch):
    """Zwischen zwei tatsächlichen Requests muss gewartet werden, wenn der
    letzte Request-Zeitstempel jünger als NOMINATIM_MIN_INTERVAL_S ist."""
    sleep_calls = []
    monkeypatch.setattr(
        region_module.time, "sleep", lambda seconds: sleep_calls.append(seconds)
    )

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(
            status_code=200,
            payload=[{"geojson": {"type": "Point", "coordinates": [0, 0]}}],
        )

    monkeypatch.setattr(region_module.requests, "get", fake_get)

    # Erster Request: kein Zeitstempel -> kein Rate-Limit-Sleep nötig.
    region_module._default_fetcher("Erste Region")
    assert sleep_calls == []

    # Zweiter Request unmittelbar danach -> muss auf das Intervall warten.
    region_module._default_fetcher("Zweite Region")
    assert len(sleep_calls) == 1
    assert sleep_calls[0] > 0
