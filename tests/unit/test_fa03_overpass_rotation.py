"""Implements: FA3 (OSM-Konnektor: Overpass-Instanzen im Wechsel bei Ueberlastung).

The public Overpass instance answers 504/429 under load (seen on the live run of the
Sentinel-2 example, 01.10.2026). Without an explicit instance the attempts rotate
between the equivalent public instances; an explicit URL (parameter or
GEOFACT_OVERPASS_URL) is used alone, as before.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
import requests
from shapely.geometry import box

from geofact.builtin.sources import osm

REGION = gpd.GeoDataFrame(
    {"name": ["r"]}, geometry=[box(12.9, 50.8, 13.0, 50.9)], crs="EPSG:4326"
)


class _Response:
    def __init__(self, status: int):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def json(self):
        return {"elements": []}


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(osm.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _no_overpass_env(monkeypatch):
    monkeypatch.delenv("GEOFACT_OVERPASS_URL", raising=False)


def _record(monkeypatch, statuses: list[int]):
    seen: list[dict] = []

    def fake_post(url, data=None, headers=None, timeout=None):
        seen.append({"url": url, "headers": headers})
        return _Response(statuses[min(len(seen), len(statuses)) - 1])

    monkeypatch.setattr(osm.requests, "post", fake_post)
    return seen


def test_fa03_default_is_the_public_instance_first_then_its_siblings():
    backend = osm.OverpassBackend()
    assert backend.url == osm.OVERPASS_URL
    assert (
        backend.urls == [osm.OVERPASS_URL, *osm.OVERPASS_FALLBACK_URLS]
        and len(backend.urls) == 3
    )
    assert backend.max_retries == 6  # two rounds


@pytest.mark.parametrize("how", ["parameter", "environment"])
def test_fa03_explicit_instance_is_used_alone_with_the_old_three_attempts(
    monkeypatch, how
):
    if how == "environment":
        monkeypatch.setenv("GEOFACT_OVERPASS_URL", "https://eigene.example/api")
        backend = osm.OverpassBackend()
    else:
        backend = osm.OverpassBackend(url="https://eigene.example/api")
    assert backend.urls == ["https://eigene.example/api"] and backend.max_retries == 3
    seen = _record(monkeypatch, [504])
    with pytest.raises(RuntimeError):
        backend.fetch({"power": "substation"}, REGION)
    assert {call["url"] for call in seen} == {"https://eigene.example/api"} and len(
        seen
    ) == 3


def test_fa03_a_504_on_the_first_instance_is_answered_by_the_next(monkeypatch):
    seen = _record(monkeypatch, [504, 200])
    result = osm.OverpassBackend().fetch({"power": "substation"}, REGION)
    assert len(result) == 0
    assert [call["url"] for call in seen] == [
        osm.OVERPASS_URL,
        osm.OVERPASS_FALLBACK_URLS[0],
    ]


def test_fa03_attempts_rotate_through_all_instances_twice_before_giving_up(monkeypatch):
    seen = _record(monkeypatch, [429])
    with pytest.raises(RuntimeError) as excinfo:
        osm.OverpassBackend().fetch({"power": "substation"}, REGION)
    order = [osm.OVERPASS_URL, *osm.OVERPASS_FALLBACK_URLS]
    assert [call["url"] for call in seen] == order + order
    message = str(excinfo.value)
    assert "nach 6 Versuchen" in message and "429" in message
    for url in order:
        assert url in message  # the message names the instances


def test_fa03_every_request_carries_a_user_agent(monkeypatch):
    seen = _record(monkeypatch, [503, 503, 200])
    osm.OverpassBackend().fetch({"power": "substation"}, REGION)
    assert len(seen) == 3
    assert all("GeoFACT" in call["headers"]["User-Agent"] for call in seen)


def test_fa03_explicit_max_retries_still_wins(monkeypatch):
    seen = _record(monkeypatch, [500])
    with pytest.raises(RuntimeError, match="nach 2 Versuchen"):
        osm.OverpassBackend(max_retries=2).fetch({"power": "substation"}, REGION)
    assert len(seen) == 2
