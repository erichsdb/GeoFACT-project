"""Implements: FA88 (Ausgaben eines Laufs als Dateiliste, einzeln abrufbar) - Backend-Teil.

Contract-Tests der beiden Endpunkte ``GET /api/runs/{id}/outputs`` und
``GET /api/runs/{id}/outputs/{name}``: die Liste ist dieselbe Aufzählung wie
das ZIP, jede Datei kommt einzeln mit denselben Bytes, übersprungene Ausgaben
stehen mit Grund in der Liste. Typfehler-Fall: unbekannter Name und Name als
Pfad. Randfall: ein Lauf ohne geschriebene Ausgabe (nicht gestartet).

Der Frontend-Teil steht in test_fa88_frontend_run_files.py.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import auth as auth_module  # noqa: E402
from geofact_web import scenario_service  # noqa: E402
from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.run_manager import manager  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"

YAML_TEXT = f"""\
# Kommentar, der im Einzelabruf erhalten bleibt
scenario:
  name: Dateiliste
  region: "{BBOX_REGION}"
layers:
  - id: substations
    source: file
    data_type: vector
    path: "{(FIXTURES_DIR / "substations.geojson").as_posix()}"
steps:
  - id: catchment
    op: buffer
    inputs: {{geometry: substations}}
    params: {{radius_km: 1}}
output:
  - {{type: geojson, source: catchment, path: catchment.geojson}}
  - {{type: csv, source: substations, path: stationen.csv}}
"""


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "web"))
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    settings_module.get_settings.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        settings_module.get_settings.cache_clear()


def _run(client: TestClient, headers: dict | None = None, **body) -> str:
    created = client.post("/api/runs", json=body, headers=headers)
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    deadline = time.monotonic() + 30
    while True:
        status = client.get(f"/api/runs/{run_id}", headers=headers).json()
        if status["status"] in ("done", "error", "cancelled"):
            break
        assert time.monotonic() < deadline, "web run did not finish"
        time.sleep(0.05)
    assert status["status"] == "done", status.get("error")
    return run_id


def _archive(client: TestClient, run_id: str) -> zipfile.ZipFile:
    response = client.get(f"/api/runs/{run_id}/download")
    assert response.status_code == 200
    return zipfile.ZipFile(io.BytesIO(response.content))


def _config_with_a_skipped_output_in_the_middle() -> dict:
    config = yaml.safe_load(YAML_TEXT)
    # gültig, aber nicht schreibbar: RDF verlangt ein CRS, eine Tabelle hat keins
    config["layers"].append(
        {
            "id": "bevoelkerung",
            "source": "table",
            "delimiter": ";",
            "path": (FIXTURES_DIR / "bip_mini.csv").as_posix(),
        }
    )
    config["output"].insert(
        1, {"type": "rdf", "source": "bevoelkerung", "path": "statistik.ttl"}
    )
    return config


# =====================================================================
# Liste
# =====================================================================


def test_fa88_list_names_the_written_outputs_with_type_and_source(client):
    run_id = _run(client, config_yaml=YAML_TEXT)
    body = client.get(f"/api/runs/{run_id}/outputs").json()

    assert body["run_id"] == run_id and body["status"] == "done"
    assert [(f["name"], f["role"], f["type"], f["source"]) for f in body["files"]] == [
        ("catchment.geojson", "output", "geojson", "catchment"),
        ("stationen.csv", "output", "csv", "substations"),
        ("scenario.yaml", "companion", None, None),
    ]
    assert all(f["size_bytes"] > 0 for f in body["files"])
    assert body["skipped"] == []


def test_fa88_list_is_the_zip_without_the_skipped_note_in_the_same_order(client):
    with pytest.warns(UserWarning, match="nicht alle deklarierten Ausgaben"):
        run_id = _run(client, config=_config_with_a_skipped_output_in_the_middle())
    names = [
        f["name"] for f in client.get(f"/api/runs/{run_id}/outputs").json()["files"]
    ]
    zipped = _archive(client, run_id).namelist()

    assert "SKIPPED_OUTPUTS.txt" in zipped
    assert names == [name for name in zipped if name != "SKIPPED_OUTPUTS.txt"]


def test_fa88_skipped_output_is_listed_with_its_reason_and_does_not_shift_the_others(
    client,
):
    with pytest.warns(UserWarning, match="nicht alle deklarierten Ausgaben"):
        run_id = _run(client, config=_config_with_a_skipped_output_in_the_middle())
    body = client.get(f"/api/runs/{run_id}/outputs").json()

    assert len(body["skipped"]) == 1
    skipped = body["skipped"][0]
    assert (skipped["index"], skipped["type"], skipped["source"]) == (
        1,
        "rdf",
        "bevoelkerung",
    )
    assert "CRS" in skipped["reason"]
    # die Ausgabe NACH der übersprungenen trägt weiter ihre eigene Deklaration
    outputs = [
        (f["name"], f["type"], f["source"])
        for f in body["files"]
        if f["role"] == "output"
    ]
    assert outputs == [
        ("catchment.geojson", "geojson", "catchment"),
        ("stationen.csv", "csv", "substations"),
    ]


def test_fa88_type_and_source_stay_empty_when_files_and_declarations_do_not_line_up(
    client,
):
    """Mehr Dateien als nicht übersprungene Deklarationen: nichts raten."""
    run_id = _run(client, config_yaml=YAML_TEXT)
    state = manager.get(run_id)
    extra = state.run_dir / "zusatz.txt"
    extra.write_text("x", encoding="utf-8")
    state.report.outputs.written.append(extra)

    files = client.get(f"/api/runs/{run_id}/outputs").json()["files"]
    outputs = [f for f in files if f["role"] == "output"]
    assert [f["name"] for f in outputs] == [
        "catchment.geojson",
        "stationen.csv",
        "zusatz.txt",
    ]
    assert all(f["type"] is None and f["source"] is None for f in outputs)


def test_fa88_parameter_overrides_and_attribution_are_companion_files(client):
    run_id = _run(client, config_yaml=YAML_TEXT)
    state = manager.get(run_id)
    state.parameters = {"radius": 2}
    notice = state.run_dir / "ATTRIBUTION.txt"
    notice.write_text("Quelle: Test\n", encoding="utf-8")
    state.report.attribution_file = notice

    files = client.get(f"/api/runs/{run_id}/outputs").json()["files"]
    companions = [f["name"] for f in files if f["role"] == "companion"]
    assert companions == ["scenario.yaml", "ATTRIBUTION.txt", "parameters.yaml"]
    assert [f["name"] for f in files] == _archive(client, run_id).namelist()


# =====================================================================
# Einzelabruf
# =====================================================================


def test_fa88_single_file_has_the_bytes_of_the_zip_entry(client):
    run_id = _run(client, config_yaml=YAML_TEXT)
    archive = _archive(client, run_id)

    for name in ("catchment.geojson", "stationen.csv", "scenario.yaml"):
        response = client.get(f"/api/runs/{run_id}/outputs/{name}")
        assert response.status_code == 200, name
        assert response.content == archive.read(name), name
        assert response.headers["content-type"].startswith("application/octet-stream")
        assert f'filename="{name}"' in response.headers["content-disposition"]
    # der ausgeführte Text, nicht ein Dump des Modells
    assert client.get(f"/api/runs/{run_id}/outputs/scenario.yaml").text == YAML_TEXT


def test_fa88_unknown_name_is_not_found(client):
    run_id = _run(client, config_yaml=YAML_TEXT)
    response = client.get(f"/api/runs/{run_id}/outputs/gibt_es_nicht.geojson")
    assert response.status_code == 404
    assert "gibt_es_nicht.geojson" in response.json()["detail"]
    # SKIPPED_OUTPUTS.txt ist keine Datei der Liste
    assert (
        client.get(f"/api/runs/{run_id}/outputs/SKIPPED_OUTPUTS.txt").status_code == 404
    )


def test_fa88_name_is_never_used_as_a_path(client, tmp_path):
    run_id = _run(client, config_yaml=YAML_TEXT)
    state = manager.get(run_id)
    secret = state.run_dir.parent / "geheim.txt"
    secret.write_text("nicht ausliefern", encoding="utf-8")
    inside = state.run_dir / "nicht_deklariert.txt"
    inside.write_text(
        "liegt im Laufverzeichnis, steht aber nicht in der Liste", encoding="utf-8"
    )

    for name in ("..%2Fgeheim.txt", "%2E%2E%2Fgeheim.txt", "nicht_deklariert.txt"):
        response = client.get(f"/api/runs/{run_id}/outputs/{name}")
        assert response.status_code == 404, name
        assert b"nicht ausliefern" not in response.content


def test_fa88_unknown_run_is_not_found(client):
    assert client.get("/api/runs/unbekannt/outputs").status_code == 404
    assert client.get("/api/runs/unbekannt/outputs/scenario.yaml").status_code == 404


# =====================================================================
# Randfälle: nicht abgeschlossen, nichts neu geschrieben
# =====================================================================


def test_fa88_before_the_run_is_done_only_the_companion_files_exist(client):
    document, _ = scenario_service.validate_config(YAML_TEXT, None)
    state = manager.create(document, refresh_snapshots=False)
    try:
        body = client.get(f"/api/runs/{state.run_id}/outputs").json()
        assert body["status"] == "pending"
        assert [(f["name"], f["role"]) for f in body["files"]] == [
            ("scenario.yaml", "companion")
        ]
        assert body["skipped"] == []
        assert (
            client.get(f"/api/runs/{state.run_id}/outputs/scenario.yaml").text
            == YAML_TEXT
        )
        assert (
            client.get(
                f"/api/runs/{state.run_id}/outputs/catchment.geojson"
            ).status_code
            == 404
        )
    finally:
        # Der Manager ist prozessweit: ein nie gestarteter Lauf zählte sonst für
        # alle folgenden Tests gegen das Limit gleichzeitiger Läufe (FA24).
        state.status = "cancelled"


def test_fa88_list_and_single_file_write_nothing(client):
    run_id = _run(client, config_yaml=YAML_TEXT)
    state = manager.get(run_id)
    before = {p.name: p.stat().st_mtime_ns for p in state.run_dir.iterdir()}

    client.get(f"/api/runs/{run_id}/outputs")
    client.get(f"/api/runs/{run_id}/outputs/catchment.geojson")
    client.get(f"/api/runs/{run_id}/outputs/scenario.yaml")
    assert {p.name: p.stat().st_mtime_ns for p in state.run_dir.iterdir()} == before


def test_fa88_a_vanished_run_dir_is_reported_as_a_lost_run(client):
    run_id = _run(client, config_yaml=YAML_TEXT)
    state = manager.get(run_id)
    for path in state.report.outputs.written:
        path.unlink()

    assert client.get(f"/api/runs/{run_id}/outputs").status_code == 404
    assert (
        client.get(f"/api/runs/{run_id}/outputs/catchment.geojson").status_code == 404
    )


# =====================================================================
# Anmeldung (FA24): fremde Läufe, Token als ?token=
# =====================================================================


@pytest.fixture
def two_users(monkeypatch):
    users = ",".join(
        f"{name}:{auth_module.hash_password(name + '-geheim')}"
        for name in ("alice", "bob")
    )
    monkeypatch.setenv("GEOFACT_WEB_USERS", users)
    settings_module.get_settings.cache_clear()


def _token(client: TestClient, name: str) -> str:
    response = client.post(
        "/api/auth/login", json={"username": name, "password": name + "-geheim"}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def test_fa88_files_of_another_user_are_not_found(client, two_users):
    alice = {"Authorization": f"Bearer {_token(client, 'alice')}"}
    bob = {"Authorization": f"Bearer {_token(client, 'bob')}"}
    run_id = _run(client, headers=alice, config_yaml=YAML_TEXT)

    assert client.get(f"/api/runs/{run_id}/outputs", headers=alice).status_code == 200
    assert client.get(f"/api/runs/{run_id}/outputs", headers=bob).status_code == 404
    assert (
        client.get(
            f"/api/runs/{run_id}/outputs/catchment.geojson", headers=bob
        ).status_code
        == 404
    )


def test_fa88_query_token_is_accepted_for_a_single_file_but_not_for_the_list(
    client, two_users
):
    token = _token(client, "alice")
    run_id = _run(
        client, headers={"Authorization": f"Bearer {token}"}, config_yaml=YAML_TEXT
    )

    assert (
        client.get(
            f"/api/runs/{run_id}/outputs/catchment.geojson?token={token}"
        ).status_code
        == 200
    )
    assert (
        client.get(f"/api/runs/{run_id}/outputs/catchment.geojson").status_code == 401
    )
    # die Liste ist JSON und wird mit Authorization-Header abgerufen
    assert client.get(f"/api/runs/{run_id}/outputs?token={token}").status_code == 401
    assert auth_module.accepts_query_token(
        f"/api/runs/{run_id}/outputs/catchment.geojson"
    )
    assert not auth_module.accepts_query_token(f"/api/runs/{run_id}/outputs")
