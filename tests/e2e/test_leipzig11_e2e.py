"""Implements: FA16 (Open-Data-Konnektor), Phase-2-DoD-Analogon.

End-to-End-Test: Leipzig-Szenario 11 (Baeume amtlich vs. OSM) läuft
komplett aus der YAML bis vor die Ausgabeschicht. Anders als die
übrigen E2E-Tests wird der WFS-Layer NICHT per connector_override
ersetzt, sondern requests.get auf HTTP-Ebene gefaked (siehe
test_fa16_opendata.py) - so durchläuft der Test den echten
wfs.py/http.py/snapshot.py-Codepfad, nur ohne Netzzugriff. Der
zweite Lauf beweist zusätzlich, dass der FA7-Snapshot-Mechanismus
(generalisiert für FA16) einen wiederholten Download vermeidet.

Szenario wechselte von Spielplätze (CKAN) auf Baeume (WFS), siehe
Kommentar in examples/leipzig/leipzig11_opendata_vergleich.yaml: das
CKAN-Dataset "spielplaetze" existiert auf opendata.leipzig.de nicht
mehr (package_show liefert 404 im Live-Betrieb)."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
import yaml

from geofact.core.scenario import Scenario
from geofact.support import http as http_module
from geofact.engine.executor import run_scenario

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.e2e


def _fixture_loader(fixture_name: str):
    def loader(layer):
        return gpd.read_file(FIXTURES_DIR / fixture_name)

    return loader


def _region_fixture_fetcher(name: str) -> list[dict]:
    raw = json.loads(
        (FIXTURES_DIR / "nominatim_leipzig.json").read_text(encoding="utf-8")
    )
    return raw if name == "Leipzig, Deutschland" else []


class _FakeStreamResponse:
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
        pass

    def iter_content(self, chunk_size: int):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


def test_e2e_leipzig11_runs_end_to_end_and_uses_snapshot_on_second_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))

    # wfs_getfeature_response.json: 3 Punkt-Features (siehe FA16-Fixture,
    # Attributnamen sind für diesen Zähl-Test irrelevant).
    wfs_geojson = (FIXTURES_DIR / "wfs_getfeature_response.json").read_bytes()
    calls = {"download": 0}

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        calls["download"] += 1
        return _FakeStreamResponse(status_code=200, content=wfs_geojson)

    monkeypatch.setattr(http_module.requests, "get", fake_get)

    raw = yaml.safe_load(
        (EXAMPLES_DIR / "leipzig" / "leipzig11_opendata_vergleich.yaml").read_text(
            encoding="utf-8"
        )
    )
    scenario = Scenario(**raw)

    result = run_scenario(
        scenario,
        connector_override={
            "baeume_osm": _fixture_loader("leipzig_playgrounds_osm.geojson"),
            "ortsteile": _fixture_loader("leipzig_ortsteile_polygon.geojson"),
        },
        region_fetcher=_region_fixture_fetcher,
    )

    assert set(result.order) == {
        "ortsteile_flaechen",
        "amtlich_je_ortsteil",
        "osm_je_ortsteil",
    }
    amtlich = result.store["amtlich_je_ortsteil"]
    osm = result.store["osm_je_ortsteil"]
    assert amtlich["count"].iloc[0] == 3  # wfs_getfeature_response.json: 3 Punkte
    assert osm["count"].iloc[0] == 4  # leipzig_playgrounds_osm.geojson: 4 Punkte
    assert calls["download"] == 1

    # Zweiter Lauf: Snapshot greift, kein erneuter WFS-Zugriff.
    result2 = run_scenario(
        scenario,
        connector_override={
            "baeume_osm": _fixture_loader("leipzig_playgrounds_osm.geojson"),
            "ortsteile": _fixture_loader("leipzig_ortsteile_polygon.geojson"),
        },
        region_fetcher=_region_fixture_fetcher,
    )
    assert calls["download"] == 1
    assert result2.store["amtlich_je_ortsteil"]["count"].iloc[0] == 3
