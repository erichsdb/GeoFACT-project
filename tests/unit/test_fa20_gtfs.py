"""Implements: FA20 (GTFS-Fahrplandaten in ein Liniennetz überführen).

Contract-Tests für src/geofact/builtin/sources/gtfs.py und die Schema-/
Executor-Anbindung (GtfsLayer, source_types()). Kein Netzzugriff für
den url-Pfad: requests.get wird durch einen Fake ersetzt, analog zu
tests/unit/test_fa16_opendata.py. Der path-Pfad nutzt direkt die
eingecheckte Mini-Fixture tests/fixtures/gtfs_mini.zip (siehe
tests/fixtures/make_gtfs_fixture.py für den Aufbau: 8 Haltestellen,
3 Routen - 1x Tram route_type=0, 2x Bus route_type=3 -, 5 Fahrten,
davon 3 Bus-Fahrten auf derselben Linie mit überlappenden Halte-
stellenfolgen für den Dedup-Test)."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

from geofact.core.scenario import Scenario
from geofact.plugin_api import LoadContext
from geofact.builtin.sources import gtfs as gtfs_module
from geofact.support import http as http_module
from geofact.support import snapshot as snapshot_module
from geofact.builtin.sources.gtfs import GtfsLayer

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
MINI_ZIP = FIXTURES_DIR / "gtfs_mini.zip"
MISSING_STOP_TIMES_ZIP = FIXTURES_DIR / "gtfs_mini_missing_stop_times.zip"


class _FakeStreamResponse:
    """Minimaler Stand-in für requests.Response im Streaming-Modus
    (Context-Manager + iter_content), wie ihn http.download_limited
    braucht - identisch zum Fake in test_fa16_opendata.py."""

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


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))


def _fake_get_for(zip_path: Path):
    content = zip_path.read_bytes()

    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=200, content=content)

    return fake_get


# =====================================================================
# (a) Happy Path: Bus-only (route_types=[3])
# =====================================================================


def test_fa20_happy_path_bus_only():
    layer = GtfsLayer(id="g1", path=str(MINI_ZIP), route_types=[3])
    result = gtfs_module.fetch(layer)

    assert result.crs.to_epsg() == 4326
    # BUS1 hat 3 eindeutige Kanten (S1-S2, S2-S3, S3-S8) trotz 3 Fahrten;
    # BUS2 hat 2 Kanten (S5-S6, S6-S7). Insgesamt 5 Kanten für Bus.
    assert len(result) == 5

    bus_stops = {"S1", "S2", "S3", "S5", "S6", "S7", "S8"}
    seen_stops = set(result["from_stop_id"]) | set(result["to_stop_id"])
    assert seen_stops <= bus_stops
    # Die Tram-only-Haltestelle S4 darf in einem bus-only-Ergebnis
    # nicht auftauchen.
    assert "S4" not in seen_stops


# =====================================================================
# (b) route_type-Filterung: Tram-only ergibt ein anderes Kantenset
# =====================================================================


def test_fa20_route_type_filter_tram_differs_from_bus():
    bus_layer = GtfsLayer(id="g1", path=str(MINI_ZIP), route_types=[3])
    tram_layer = GtfsLayer(id="g2", path=str(MINI_ZIP), route_types=[0])

    bus_result = gtfs_module.fetch(bus_layer)
    tram_result = gtfs_module.fetch(tram_layer)

    # Tram: 1 Fahrt S1->S2->S3->S4 = 3 eindeutige Kanten.
    assert len(tram_result) == 3
    tram_stops = set(tram_result["from_stop_id"]) | set(tram_result["to_stop_id"])
    assert tram_stops == {"S1", "S2", "S3", "S4"}

    assert len(bus_result) != len(tram_result)
    bus_pairs = set(zip(bus_result["from_stop_id"], bus_result["to_stop_id"]))
    tram_pairs = set(zip(tram_result["from_stop_id"], tram_result["to_stop_id"]))
    assert bus_pairs != tram_pairs


# =====================================================================
# (c) Dedup: mehrere Fahrten auf derselben Linie liefern jede Kante nur 1x
# =====================================================================


def test_fa20_dedup_across_trips_on_same_route():
    layer = GtfsLayer(id="g1", path=str(MINI_ZIP), route_types=[3])
    result = gtfs_module.fetch(layer)

    pairs = list(zip(result["from_stop_id"], result["to_stop_id"]))
    # Keine doppelten Paare, obwohl bus_trip_1/2/3 gemeinsam 6
    # Kanten-Vorkommen erzeugen (S1-S2 x2, S2-S3 x3, S3-S8 x1).
    assert len(pairs) == len(set(pairs))
    assert ("S1", "S2") in pairs
    assert ("S2", "S3") in pairs
    assert ("S3", "S8") in pairs
    # Insgesamt nur 5 eindeutige Kanten (3 BUS1 + 2 BUS2), nicht die
    # Summe aller Fahrten-Kanten-Vorkommen (2+2+2+2 = 8 für BUS1 allein
    # über 3 Fahrten wäre bei fehlendem Dedup zu erwarten).
    assert len(result) == 5


# =====================================================================
# (d) route_types fehlt / leer -> Schema-Validierungsfehler
# =====================================================================


def test_fa20_missing_route_types_raises():
    with pytest.raises(ValueError):
        GtfsLayer(id="g1", path=str(MINI_ZIP))


def test_fa20_empty_route_types_raises():
    with pytest.raises(ValueError):
        GtfsLayer(id="g1", path=str(MINI_ZIP), route_types=[])


def test_fa20_url_and_path_both_given_raises():
    with pytest.raises(ValueError):
        GtfsLayer(
            id="g1", path=str(MINI_ZIP), url="https://x/gtfs.zip", route_types=[3]
        )


def test_fa20_neither_url_nor_path_raises():
    with pytest.raises(ValueError):
        GtfsLayer(id="g1", route_types=[3])


# =====================================================================
# (e) route_types matcht keine Fahrt -> klarer Fehler, keine stille Leere
# =====================================================================


def test_fa20_route_types_no_match_raises_actionable_error():
    layer = GtfsLayer(
        id="g1", path=str(MINI_ZIP), route_types=[2]
    )  # kein route_type=2 im Feed
    with pytest.raises(RuntimeError, match=r"\[2\]"):
        gtfs_module.fetch(layer)

    # Fehlermeldung muss die tatsächlich vorhandenen route_types nennen,
    # damit ein Tippfehler im Filter auffindbar ist.
    with pytest.raises(RuntimeError, match=r"0.*3|3.*0"):
        gtfs_module.fetch(layer)


# =====================================================================
# (f) fehlende Pflichtdatei in der Zip -> klarer Fehler
# =====================================================================


def test_fa20_missing_required_file_raises():
    layer = GtfsLayer(id="g1", path=str(MISSING_STOP_TIMES_ZIP), route_types=[3])
    with pytest.raises(RuntimeError, match="stop_times.txt"):
        gtfs_module.fetch(layer)


def test_fa20_missing_zip_path_raises():
    layer = GtfsLayer(
        id="g1", path=str(FIXTURES_DIR / "does_not_exist.zip"), route_types=[3]
    )
    with pytest.raises(RuntimeError, match="does_not_exist.zip"):
        gtfs_module.fetch(layer)


# =====================================================================
# url-Pfad (Download über http.download_limited, kein Netzzugriff)
# =====================================================================


def test_fa20_fetch_via_url(monkeypatch):
    monkeypatch.setattr(http_module.requests, "get", _fake_get_for(MINI_ZIP))
    layer = GtfsLayer(id="g1", url="https://example.org/gtfs.zip", route_types=[3])
    result = gtfs_module.fetch(layer)
    assert len(result) == 5
    assert result.crs.to_epsg() == 4326


def test_fa20_url_error_message_contains_url(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=None, stream=False):
        return _FakeStreamResponse(status_code=500)

    monkeypatch.setattr(http_module.requests, "get", fake_get)
    layer = GtfsLayer(id="g1", url="https://example.org/gtfs.zip", route_types=[3])
    with pytest.raises(RuntimeError, match="https://example.org/gtfs.zip"):
        gtfs_module.load(layer, LoadContext(region=_germany_region()))


def _germany_region():
    import geopandas as gpd
    from shapely.geometry import box

    return gpd.GeoDataFrame(
        {"name": ["region"]},
        geometry=[box(12.30, 51.30, 12.40, 51.36)],
        crs="EPSG:4326",
    )


# =====================================================================
# Snapshot-Regression: zweiter load() liest keinen zweiten Download/Parse
# =====================================================================


def test_fa20_snapshot_second_load_skips_refetch(monkeypatch):
    calls = {"n": 0}
    real_fetch = gtfs_module.fetch

    def counting_fetch(layer):
        calls["n"] += 1
        return real_fetch(layer)

    monkeypatch.setattr(gtfs_module, "fetch", counting_fetch)

    layer = GtfsLayer(id="g1", path=str(MINI_ZIP), route_types=[3])
    region = _germany_region()

    first = gtfs_module.load(layer, LoadContext(region=region))
    second = gtfs_module.load(layer, LoadContext(region=region))

    assert calls["n"] == 1
    assert len(first) == len(second)


def test_fa20_snapshot_key_differs_by_route_types():
    """Unterschiedliche route_types-Filter auf demselben Feed dürfen
    NICHT denselben Snapshot treffen (sonst würde load() fälschlich
    das falsch gefilterte Ergebnis wiederverwenden)."""
    bus_layer = GtfsLayer(id="g1", path=str(MINI_ZIP), route_types=[3])
    tram_layer = GtfsLayer(id="g1", path=str(MINI_ZIP), route_types=[0])

    bus_key = snapshot_module.payload_key(
        {
            "kind": "gtfs",
            "url": bus_layer.url,
            "path": bus_layer.path,
            "route_types": sorted(bus_layer.route_types),
        }
    )
    tram_key = snapshot_module.payload_key(
        {
            "kind": "gtfs",
            "url": tram_layer.url,
            "path": tram_layer.path,
            "route_types": sorted(tram_layer.route_types),
        }
    )
    assert bus_key != tram_key


# =====================================================================
# Schema-Union-Dispatch + Katalog
# =====================================================================


def test_fa20_layer_union_dispatch_gtfs():
    minimal = {
        "scenario": {"name": "t", "region": "12.0,51.0,12.1,51.1"},
        "layers": [
            {
                "id": "g1",
                "source": "gtfs",
                "path": str(MINI_ZIP),
                "route_types": [3],
            }
        ],
        "steps": [{"id": "s", "op": "network_nodes", "inputs": {"lines": "g1"}}],
        "output": [{"type": "geojson", "source": "s", "path": "out.geojson"}],
    }
    scenario = Scenario(**minimal)
    assert isinstance(scenario.layers[0], GtfsLayer)
    assert scenario.layers[0].source == "gtfs"


def test_fa20_catalog_lists_gtfs_source_type():
    from geofact.engine import catalog

    sources = {s["source"] for s in catalog.source_types()}
    assert "gtfs" in sources


# =====================================================================
# (h) Downstream-Regression: network_nodes + build_network kompatibel
# =====================================================================


def test_fa20_regression_bus_edges_build_network():
    """Spiegelt den motivierenden Fall der Anforderung FA20:
    ein aus GTFS abgeleiteter Bus-Kanten-Layer muss ohne Änderungen an
    network_nodes (FA17) / build_network (FA10) funktionieren - exakt
    dieselbe Kompatibilitätsprüfung wie FA19s Regressionstest für
    Tram+S-Bahn."""
    from geofact.builtin.operations import graph as graph_module
    from geofact.builtin.operations import network_nodes

    layer = GtfsLayer(id="g1", path=str(MINI_ZIP), route_types=[3])
    lines = gtfs_module.fetch(layer)
    assert len(lines) == 5

    nodes = network_nodes.run_network_nodes({"lines": lines}, {"tolerance_m": 0})
    result = graph_module.run_build_network(
        {"lines": lines, "nodes": nodes}, {"tolerance_m": 0}
    )

    assert result.nx_graph.number_of_edges() == 5
    assert result.nx_graph.number_of_nodes() > 0
