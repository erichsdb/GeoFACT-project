"""Implements: FA65 (Web-Teil: Lizenzen in Validierung, Laufstatus, Knoten-Ergebnis und Download).

Contract-Tests der Web-Schicht: ``ValidateResponse.attribution`` nennt ohne
Lauf die Lizenzen der Ausgabequellen; ``RunStatus.attribution`` und das
Knoten-Ergebnis (``attribution``) tragen sie im Lauf; der ZIP-Download enthält
``ATTRIBUTION.txt``. Ohne jede Lizenz bleibt alles wie zuvor.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"
SUBSTATIONS = (FIXTURES_DIR / "substations.geojson").as_posix()


def _yaml(license_block: str, second_layer: str = "") -> str:
    return f"""\
scenario:
  name: Web-Lizenzen
  region: "{BBOX_REGION}"
layers:
  - id: substations
    source: file
    data_type: vector
    path: "{SUBSTATIONS}"
{license_block}{second_layer}
steps:
  - id: catchment
    op: buffer
    inputs: {{geometry: substations}}
    params: {{radius_km: 1}}
output:
  - {{type: geojson, source: catchment, path: catchment.geojson}}
"""


LICENSED = _yaml(
    '    license: {name: CC0-1.0, attribution: "Eigene Erhebung", url: "https://example.org/cc0"}\n'
)
UNLICENSED = _yaml("")


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "web"))
    monkeypatch.setenv("GEOFACT_WEB_UPLOADS_DIR", str(tmp_path / "web" / "uploads"))
    settings_module.get_settings.cache_clear()
    return TestClient(create_app())


def _run(client: TestClient, text: str) -> tuple[str, dict]:
    created = client.post("/api/runs", json={"config_yaml": text})
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    deadline = time.monotonic() + 30
    while True:
        status = client.get(f"/api/runs/{run_id}").json()
        if status["status"] in ("done", "error", "cancelled"):
            break
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert status["status"] == "done", status.get("error")
    return run_id, status


def test_fa65_web_validate_response_carries_attribution(client):
    body = client.post("/api/config/validate", json={"config_yaml": LICENSED}).json()
    assert body["valid"] is True, body["issues"]
    attribution = body["attribution"]
    assert attribution["licenses"] == [
        {
            "name": "CC0-1.0",
            "attribution": "Eigene Erhebung",
            "url": "https://example.org/cc0",
        }
    ]
    assert attribution["undeclared"] == []
    assert (
        len(attribution["lines"]) == 1 and "Eigene Erhebung" in attribution["lines"][0]
    )


def test_fa65_web_validate_names_layers_without_licence(client):
    body = client.post("/api/config/validate", json={"config_yaml": UNLICENSED}).json()
    assert body["valid"] is True
    assert body["attribution"] == {
        "licenses": [],
        "undeclared": ["substations"],
        "lines": [],
    }


def test_fa65_web_run_status_and_node_result_carry_the_attribution(client):
    run_id, status = _run(client, LICENSED)
    assert status["attribution"]["licenses"][0]["name"] == "CC0-1.0"
    for node in ("substations", "catchment"):
        payload = client.get(f"/api/runs/{run_id}/nodes/{node}").json()
        assert payload["attribution"]["licenses"][0]["name"] == "CC0-1.0", node


def test_fa65_web_download_zip_contains_attribution_txt(client):
    run_id, _status = _run(client, LICENSED)
    archive = zipfile.ZipFile(
        io.BytesIO(client.get(f"/api/runs/{run_id}/download").content)
    )
    assert "ATTRIBUTION.txt" in archive.namelist()
    assert "Eigene Erhebung" in archive.read("ATTRIBUTION.txt").decode("utf-8")


def test_fa65_web_without_licence_download_is_unchanged(client):
    run_id, status = _run(client, UNLICENSED)
    archive = zipfile.ZipFile(
        io.BytesIO(client.get(f"/api/runs/{run_id}/download").content)
    )
    assert set(archive.namelist()) == {"catchment.geojson", "scenario.yaml"}
    assert status["attribution"]["licenses"] == []
