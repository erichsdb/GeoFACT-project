"""Tests für das Web-Backend (FastAPI) via TestClient.

Deckt Meta/Katalog/Validierung/Ausfuehrung/Download ab, ohne Netz:
Szenarien nutzen lokale Fixture-Dateien und ein BBox-Region-Literal.
LLM-Generierung wird nicht getestet (braucht externen Anbieter).
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact_web.main import create_app  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _vector_config() -> dict:
    return {
        "scenario": {"name": "Backend-Test", "region": BBOX_REGION},
        "layers": [
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
        ],
        "steps": [
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 1},
            },
        ],
        "output": [
            {"type": "geojson", "source": "catchment", "path": "catchment.geojson"}
        ],
    }


def test_health_and_meta(client: TestClient, monkeypatch):
    # Explizit auf overpass setzen statt auf die tatsächliche Laufzeitumgebung
    # zu vertrauen - eine lokale .env mit GEOFACT_OSM_BACKEND=postgis (für
    # den Entwicklungsbetrieb durchaus sinnvoll) darf diesen Test nicht
    # verfälschen (der postgis-Zweig hat eigene Tests weiter unten).
    from geofact_web import settings as settings_module

    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "overpass")
    settings_module.get_settings.cache_clear()
    try:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert set(health["checks"]) == {"postgis", "copernicus", "llm"}
        # PostGIS-Check ist bei overpass-Backend bewusst deaktiviert (keine
        # tote DB als Fehlerzustand).
        assert health["checks"]["postgis"]["enabled"] is False
        assert "raster_count" in health["checks"]["copernicus"]
        assert "configured" in health["checks"]["llm"]

        meta = client.get("/api/meta").json()
        assert "osm_backend" in meta and "llm_configured" in meta
    finally:
        settings_module.get_settings.cache_clear()


def test_health_postgis_reachable_when_configured(client: TestClient, monkeypatch):
    """postgis-Zweig des Health-Checks: aktiviert per GEOFACT_OSM_BACKEND,
    Verbindung wird mit einem Fake-Engine gemockt (keine echte DB nötig)."""
    from geofact_web import settings as settings_module

    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "postgis")
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    settings_module.get_settings.cache_clear()

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, _stmt):
            return None

    class _FakeEngine:
        def connect(self):
            return _FakeConn()

        def dispose(self):
            pass

    monkeypatch.setattr("sqlalchemy.create_engine", lambda *a, **kw: _FakeEngine())

    try:
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["checks"]["postgis"] == {
            "enabled": True,
            "reachable": True,
            "detail": None,
        }
    finally:
        settings_module.get_settings.cache_clear()


def test_health_postgis_unreachable_reports_degraded(client: TestClient, monkeypatch):
    from geofact_web import settings as settings_module

    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "postgis")
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    settings_module.get_settings.cache_clear()

    def _raise(*_a, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr("sqlalchemy.create_engine", _raise)

    try:
        health = client.get("/api/health").json()
        assert health["status"] == "degraded"
        assert health["checks"]["postgis"]["enabled"] is True
        assert health["checks"]["postgis"]["reachable"] is False
        assert "connection refused" in health["checks"]["postgis"]["detail"]
    finally:
        settings_module.get_settings.cache_clear()


def test_health_postgis_missing_dsn_reports_unreachable(
    client: TestClient, monkeypatch
):
    from geofact_web import settings as settings_module

    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "postgis")
    monkeypatch.delenv("GEOFACT_PG_DSN", raising=False)
    settings_module.get_settings.cache_clear()

    try:
        health = client.get("/api/health").json()
        assert health["status"] == "degraded"
        assert health["checks"]["postgis"]["reachable"] is False
        assert "GEOFACT_PG_DSN" in health["checks"]["postgis"]["detail"]
    finally:
        settings_module.get_settings.cache_clear()


def test_operations_and_schema(client: TestClient):
    ops = client.get("/api/operations").json()
    assert any(o["op"] == "buffer" for o in ops)
    schema = client.get("/api/schema").json()
    assert schema["title"] == "Scenario"


def test_catalog_has_osm_presets(client: TestClient):
    catalog = client.get("/api/catalog").json()
    assert catalog["osm_presets"]
    preset = catalog["osm_presets"][0]
    assert preset["layer_template"]["source"] == "osm"


def test_validate_reports_errors(client: TestClient):
    bad = {
        "config": {
            "scenario": {"name": "x", "region": "0,0,1,1"},
            "layers": [],
            "steps": [],
            "output": [],
        }
    }
    resp = client.post("/api/config/validate", json=bad).json()
    assert resp["valid"] is False
    assert resp["issues"]


def test_validate_ok(client: TestClient):
    resp = client.post("/api/config/validate", json={"config": _vector_config()}).json()
    assert resp["valid"] is True
    assert resp["execution_order"] == ["catchment"]


def _wait_for_done(client: TestClient, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/runs/{run_id}").json()
        if status["status"] in ("done", "error"):
            return status
        time.sleep(0.1)
    raise AssertionError("Lauf wurde nicht rechtzeitig fertig")


def test_full_run_and_node_result_and_download(client: TestClient):
    created = client.post("/api/runs", json={"config": _vector_config()}).json()
    run_id = created["run_id"]

    status = _wait_for_done(client, run_id)
    assert status["status"] == "done", status.get("error")
    assert status["steps"]["catchment"]["status"] == "done"
    # layer_load_ms getrennt von duration_ms (reine Operationslaufzeit) und
    # auf Run-Ebene je LAYER geführt (nicht je Schritt - die Ladezeit
    # gehört zur Quelle, siehe models.RunStatus.layer_load_ms).
    assert status["layer_load_ms"]["substations"] >= 0

    # Per-Node-Ergebnis (Vektor -> GeoJSON)
    node = client.get(f"/api/runs/{run_id}/nodes/catchment").json()
    assert node["describe"]["kind"] == "vector"
    assert node["geojson"]["type"] == "FeatureCollection"
    assert node["geojson"]["features"]

    # Download-ZIP enthält die deklarierte Ausgabe + scenario.yaml
    resp = client.get(f"/api/runs/{run_id}/download")
    assert resp.status_code == 200
    with zipfile.ZipFile(io.BytesIO(resp.content)) as archive:
        names = set(archive.namelist())
    assert "scenario.yaml" in names
    assert "catchment.geojson" in names


def test_sse_stream_emits_progress(client: TestClient):
    created = client.post("/api/runs", json={"config": _vector_config()}).json()
    run_id = created["run_id"]
    body = client.get(f"/api/runs/{run_id}/events").text
    assert "event: progress" in body
    assert "event: end" in body
    assert "step_done" in body


def test_run_rejects_invalid_config(client: TestClient):
    bad = {
        "config": {
            "scenario": {"name": "x", "region": "0,0,1,1"},
            "layers": [],
            "steps": [],
            "output": [],
        }
    }
    resp = client.post("/api/runs", json=bad)
    assert resp.status_code == 422


def test_operations_expose_params(client: TestClient):
    ops = {o["op"]: o for o in client.get("/api/operations").json()}
    assert ops["buffer"]["params"]["radius_km"]["required"] is True
    assert ops["spatial_join"]["params"]["predicate"]["choices"]


def _network_config() -> dict:
    return {
        "scenario": {"name": "Netz", "region": BBOX_REGION},
        "layers": [
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
            {
                "id": "power_lines",
                "source": "file",
                "path": str(FIXTURES_DIR / "power_lines.geojson"),
                "data_type": "vector",
            },
        ],
        "steps": [
            {
                "id": "grid",
                "op": "build_network",
                "inputs": {"lines": "power_lines", "nodes": "substations"},
                "params": {"tolerance_m": 100},
            },
            {"id": "nodes", "op": "graph_to_vector", "inputs": {"graph": "grid"}},
        ],
        "output": [{"type": "geojson", "source": "nodes"}],
    }


def test_warnings_captured_on_step(client: TestClient):
    created = client.post("/api/runs", json={"config": _network_config()}).json()
    status = _wait_for_done(client, created["run_id"])
    assert status["status"] == "done"
    # Getrennte Netzkomponenten -> Warnung am build_network-Schritt.
    assert status["steps"]["grid"]["warnings"]


def test_cancel_unknown_and_finished(client: TestClient):
    assert client.post("/api/runs/nope/cancel").status_code == 404
    created = client.post("/api/runs", json={"config": _vector_config()}).json()
    _wait_for_done(client, created["run_id"])
    # Bereits fertig -> nichts mehr abzubrechen.
    assert (
        client.post(f"/api/runs/{created['run_id']}/cancel").json()["cancelling"]
        is False
    )


def test_scenario_save_list_load_delete(client: TestClient):
    import yaml

    body = {"name": "demo-test", "config_yaml": yaml.safe_dump(_vector_config())}
    assert client.post("/api/scenarios", json=body).json()["name"] == "demo-test"
    names = [s["name"] for s in client.get("/api/scenarios").json()]
    assert "demo-test" in names
    loaded = client.get("/api/scenarios/demo-test").json()
    assert "scenario" in loaded["config_yaml"]
    assert client.delete("/api/scenarios/demo-test").json()["deleted"] is True


def test_superseded_example_hidden_from_scenario_list(client: TestClient):
    # szenario2_gruenflaechen.yaml bleibt für Tests/run_demo.py auf der
    # Platte, wurde aber inhaltlich von szenario3 abgelöst und soll in der
    # Web-Demo-Auswahl nicht mehr auftauchen.
    ids = [s["id"] for s in client.get("/api/scenarios").json()]
    assert "szenario2_gruenflaechen" not in ids


NEW_SHOWCASES = (
    "chemnitz_gruenanteil_osm",
    "chemnitz_datenquellen_lizenzen",
    "crs_showcase/drei_crs_eine_stadt",
    "deutschland_gruenste_grossstadt_osm",
)


def test_examples_listing_includes_new_showcases(client: TestClient):
    # W4-05: the four showcase examples (one of them in a subfolder) appear in the
    # web selection and load with their origin header.
    listed = {s["id"]: s for s in client.get("/api/scenarios").json()}
    for example_id in NEW_SHOWCASES:
        assert example_id in listed, example_id
        assert listed[example_id]["source"] == "example"
        loaded = client.get(f"/api/scenarios/examples/{example_id}")
        assert loaded.status_code == 200, loaded.text
        assert f"# geofact-origin: {example_id}" in loaded.json()["config_yaml"]


def test_fa92_examples_are_listed_alphabetically_by_name(client: TestClient):
    # Sorted by the displayed name, not by the file path: an example in a subfolder
    # stands between the others, and upper/lower case does not matter.
    examples = [
        s for s in client.get("/api/scenarios").json() if s["source"] == "example"
    ]
    names = [s["name"] for s in examples]
    assert len(names) > 10
    assert names == sorted(names, key=str.casefold)
    assert [s["id"] for s in examples] != sorted(s["id"] for s in examples)


def test_fa92_saved_scenarios_come_first_and_are_sorted_too(client: TestClient):
    for name in ("zeta", "Alpha", "mitte"):
        saved = client.post(
            "/api/scenarios", json={"name": name, "config_yaml": "scenario: {}\n"}
        )
        assert saved.status_code == 200, saved.text
    listed = client.get("/api/scenarios").json()
    own = [s["name"] for s in listed if s["source"] == "user"]
    assert own == ["Alpha", "mitte", "zeta"]
    assert [s["source"] for s in listed[: len(own)]] == ["user"] * len(own)


# ---------------------------------------------------------------------------
# Nachladbare Objektgrenze der Knotenansicht (FA12)
#
# Der Erstaufruf ist bewusst klein (schnelle Ansicht); die Oberfläche
# fordert per limit gezielt mehr an. Der Payload muss melden, welche
# Grenze galt und wie weit sie sich anheben lässt - sonst müsste der
# Client raten.
# ---------------------------------------------------------------------------


def _run_to_done(client: TestClient) -> str:
    created = client.post("/api/runs", json={"config": _vector_config()}).json()
    run_id = created["run_id"]
    _wait_for_done(client, run_id)
    return run_id


def test_fa12_node_result_reports_applied_and_max_limit(client: TestClient):
    run_id = _run_to_done(client)

    node = client.get(f"/api/runs/{run_id}/nodes/catchment").json()

    from geofact_web.routers.runs import _DEFAULT_NODE_FEATURES, _MAX_NODE_FEATURES

    assert node["feature_limit"] == _DEFAULT_NODE_FEATURES
    assert node["max_feature_limit"] == _MAX_NODE_FEATURES


def test_fa12_node_result_honours_explicit_limit(client: TestClient):
    run_id = _run_to_done(client)

    node = client.get(f"/api/runs/{run_id}/nodes/catchment?limit=1").json()

    assert node["feature_limit"] == 1
    assert len(node["geojson"]["features"]) == 1
    # describe() meldet weiterhin die echte Gesamtzahl - Grundlage für
    # "N von M geladen".
    assert node["describe"]["feature_count"] >= 1


def test_fa12_node_result_limit_is_capped_at_server_maximum(client: TestClient):
    run_id = _run_to_done(client)

    from geofact_web.routers.runs import _MAX_NODE_FEATURES

    node = client.get(f"/api/runs/{run_id}/nodes/catchment?limit=999999999").json()

    assert node["feature_limit"] == _MAX_NODE_FEATURES


def test_fa12_node_result_rejects_invalid_limit(client: TestClient):
    """Keine stillen Fehler: ein unsinniges limit wird abgelehnt, nicht
    stillschweigend korrigiert (goldene Regel 7)."""
    run_id = _run_to_done(client)

    resp = client.get(f"/api/runs/{run_id}/nodes/catchment?limit=0")

    assert resp.status_code == 422


def test_raster_png_url_quotes_the_node_id(client: TestClient):
    """Nachtreview 02.10. (Runde 2): ``raster_png_url`` setzte die id roh in
    den Pfad (die Karte nutzt die URL unverändert); eine id mit Leerzeichen
    oder Umlaut wird jetzt kodiert und trifft den PNG-Endpunkt."""
    from urllib.parse import quote

    raster_id = "bevölkerung 2024"
    config = {
        "scenario": {"name": "Raster-URL", "region": "12.0,50.5,14.5,51.5"},
        "layers": [
            {
                "id": "zone",
                "source": "file",
                "path": str(FIXTURES_DIR / "sachsen_region.geojson"),
                "data_type": "vector",
            },
            {
                "id": raster_id,
                "source": "file",
                "data_type": "raster",
                "format": "tif",
                "path": str(FIXTURES_DIR / "population.tif"),
            },
        ],
        "steps": [
            {
                "id": "summe",
                "op": "zonal_stats",
                "inputs": {"zones": "zone", "values": raster_id},
                "params": {"stat": "sum"},
            }
        ],
        "output": [{"type": "geojson", "source": "summe"}],
    }
    run_id = client.post("/api/runs", json={"config": config}).json()["run_id"]
    assert _wait_for_done(client, run_id)["status"] == "done"

    node = client.get(f"/api/runs/{run_id}/nodes/{quote(raster_id, safe='')}").json()
    url = node["raster_png_url"]
    assert url == f"/api/runs/{run_id}/nodes/{quote(raster_id, safe='')}/raster.png"
    assert client.get(url).status_code == 200


@pytest.mark.parametrize("node_id", ["a/b", "..", "dgm#1", "pop?2", "ndvi%a"])
def test_validate_rejects_node_ids_that_break_urls(client: TestClient, node_id):
    """Nachtreview 02.10. (Runde 2): solche ids bestanden die Validierung, ihr
    Knoten-Endpunkt antwortete aber 404 oder mit dem Laufstatus."""
    config = _vector_config()
    config["steps"][0]["id"] = node_id
    config["output"][0]["source"] = node_id
    result = client.post("/api/config/validate", json={"config": config}).json()
    assert result["valid"] is False
    assert any("steps -> 0 -> id" in str(issue) for issue in result["issues"])


def _sse_events(body: str) -> list[tuple[str, dict]]:
    import json as _json

    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in lines:
            events.append((lines["event"], _json.loads(lines["data"])))
    return events


def test_fa09_sse_initial_status_plus_replay_does_not_double_warnings(
    client: TestClient,
):
    """Nachtreview 02.10. (Runde 2): der erste ``status`` enthielt schon alle
    Ereignisse, das Replay wendete sie ein zweites Mal an - Lauf- und
    Schrittwarnungen erschienen doppelt, fertige Schritte kurz als 'running'.
    Der erste Status ist jetzt der Stand vor dem ersten Ereignis."""
    config = _vector_config()
    config["scenario"]["region"] = "6.0,50.0,13.94,51.15"  # breiter als eine UTM-Zone
    run_id = client.post("/api/runs", json={"config": config}).json()["run_id"]
    final = _wait_for_done(client, run_id)
    assert final["status"] == "done" and len(final["warnings"]) == 1

    events = _sse_events(client.get(f"/api/runs/{run_id}/events").text)
    kind, initial = events[0]
    assert kind == "status"
    replayed = [data for kind, data in events[1:] if kind == "progress"]
    region_warnings = [e for e in replayed if e["type"] == "region_warning"]
    assert len(initial["warnings"]) + len(region_warnings) == len(final["warnings"])
    assert all(step["status"] == "pending" for step in initial["steps"].values())


def test_fa65_run_status_carries_the_node_attribution(client: TestClient):
    """Nachtreview 02.10. (Runde 2): ``node_attribution`` stand nur im
    plan-Ereignis; der letzte SSE-Status und GET /api/runs/{id} (nach einem
    Neuladen) ließen es weg, der Fallback im Inspector war danach leer."""
    run_id = client.post("/api/runs", json={"config": _vector_config()}).json()[
        "run_id"
    ]
    final = _wait_for_done(client, run_id)
    assert set(final["node_attribution"]) >= {"substations", "catchment"}

    events = _sse_events(client.get(f"/api/runs/{run_id}/events").text)
    last_status = [data for kind, data in events if kind == "status"][-1]
    assert last_status["node_attribution"] == final["node_attribution"]
