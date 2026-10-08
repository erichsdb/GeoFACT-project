"""Implements: FA73 (Web-Teil: Ausgabelizenz in der Validierung, Lizenzpruefung per Endpunkt).

Contract tests of the web layer: ``ValidateResponse.output_licenses`` names the
user's output licences without any network access; ``POST /api/config/licenses``
runs the DALICC check explicitly (HTTP mocked) and returns the status per output;
an invalid configuration is reported with its issues and checks nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact.support import http  # noqa: E402
from geofact.support.dalicc import LIBRARY_PREFIX  # noqa: E402
from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
SUBSTATIONS = (FIXTURES_DIR / "substations.geojson").as_posix()
ODBL, CC0 = LIBRARY_PREFIX + "OdcOpenDatabaseLicense", LIBRARY_PREFIX + "Cc010Universal"

CONFIG = f"""\
scenario:
  name: Web-Lizenzprüfung
  region: "13.60,51.01,13.94,51.15"
  output_license: {{name: ODbL-1.0, attribution: "Eigene Auswertung"}}
layers:
  - id: substations
    source: file
    data_type: vector
    path: "{SUBSTATIONS}"
    license: {{name: CC0-1.0, attribution: "Eigene Erhebung"}}
steps:
  - id: catchment
    op: buffer
    inputs: {{geometry: substations}}
    params: {{radius_km: 1}}
output:
  - {{type: geojson, source: catchment, path: catchment.geojson}}
  - {{type: csv, source: catchment, path: catchment.csv, license: {{name: CC0-1.0}}}}
"""

CONFLICT = {
    "conflicting_statements": {
        "direct": {
            "0": {
                "statement_1": [
                    CC0,
                    "http://www.w3.org/ns/odrl/2/permission",
                    "https://dalicc.net/ns#ChangeLicense",
                ],
                "statement_2": [
                    ODBL,
                    "http://www.w3.org/ns/odrl/2/prohibition",
                    "https://dalicc.net/ns#ChangeLicense",
                ],
                "reason": "Direct permission-prohibition conflict.",
            }
        },
        "derived": {},
    }
}


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "web"))
    monkeypatch.setenv("GEOFACT_WEB_UPLOADS_DIR", str(tmp_path / "web" / "uploads"))
    settings_module.get_settings.cache_clear()
    return TestClient(create_app())


@pytest.fixture
def dalicc_calls(monkeypatch) -> list[str]:
    calls: list[str] = []

    def get(url, *, params=None, context, max_retries=3):
        calls.append("list")
        return {
            "results": {"bindings": [{"id": {"value": ODBL}}, {"id": {"value": CC0}}]}
        }

    def post(url, *, body, context, max_retries=3):
        calls.append("check")
        return CONFLICT

    monkeypatch.setattr(http, "get_json", get)
    monkeypatch.setattr(http, "post_json", post)
    return calls


def test_fa73_web_validate_names_output_licenses_without_network(client, dalicc_calls):
    body = client.post("/api/config/validate", json={"config_yaml": CONFIG}).json()
    assert body["valid"], body
    licenses = body["output_licenses"]
    assert [
        (item["index"], item["license"]["name"], item["declared_at"])
        for item in licenses
    ] == [
        (0, "ODbL-1.0", "scenario"),
        (1, "CC0-1.0", "output"),
    ]
    assert licenses[0]["line"].endswith("(vom Nutzer festgelegt, ungeprüft)")
    assert dalicc_calls == []


def test_fa73_web_license_endpoint_reports_status_per_output(client, dalicc_calls):
    response = client.post("/api/config/licenses", json={"config_yaml": CONFIG})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] and body["status"] == "conflicts"
    first, second = body["checks"]
    assert first["status"] == "conflicts"
    assert first["conflicts"][0]["message"].startswith(
        "Direkter Konflikt: CC0-1.0 erlaubt"
    )
    assert second["status"] == "compatible"  # CC0 source + CC0 output: one licence
    assert dalicc_calls.count("check") == 1


def test_fa73_web_license_endpoint_with_invalid_config_checks_nothing(
    client, dalicc_calls
):
    body = client.post(
        "/api/config/licenses", json={"config_yaml": "scenario: {}"}
    ).json()
    assert body["valid"] is False and body["issues"]
    assert body["checks"] == [] and dalicc_calls == []


def test_fa73_web_license_endpoint_reports_an_unreachable_service(client, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("DALICC nicht erreichbar")

    monkeypatch.setattr(http, "get_json", down)
    monkeypatch.setattr(http, "post_json", down)
    body = client.post("/api/config/licenses", json={"config_yaml": CONFIG}).json()
    assert body["status"] == "unreachable"
    assert all(
        check["status"] in ("unreachable", "compatible") for check in body["checks"]
    )


def test_fa73_web_license_endpoint_requires_a_logged_in_user(
    client, dalicc_calls, monkeypatch
):
    from geofact_web import auth as auth_module

    monkeypatch.setenv(
        "GEOFACT_WEB_USERS", f"alice:{auth_module.hash_password('geheim')}"
    )
    settings_module.get_settings.cache_clear()
    try:
        anonymous = client.post("/api/config/licenses", json={"config_yaml": CONFIG})
        assert anonymous.status_code == 401
        garbage = client.post(
            "/api/config/licenses",
            json={"config_yaml": CONFIG},
            headers={"Authorization": "Bearer kein-token"},
        )
        assert garbage.status_code == 401
        assert dalicc_calls == []  # rejected before any call to the external service

        login = client.post(
            "/api/auth/login", json={"username": "alice", "password": "geheim"}
        )
        token = login.json()["token"]
        allowed = client.post(
            "/api/config/licenses",
            json={"config_yaml": CONFIG},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert allowed.status_code == 200, allowed.text
        assert "check" in dalicc_calls
    finally:
        settings_module.get_settings.cache_clear()


def test_fa73_frontend_license_panel_is_reset_when_parameters_change():
    """Source contract (no frontend test runner in the repo): the panel's key
    covers the YAML and the parameter values, both of which the check sends."""
    frontend = Path(__file__).resolve().parents[2] / "frontend" / "components"
    shell = (frontend / "app-shell.tsx").read_text(encoding="utf-8")
    panel = (frontend / "license-check.tsx").read_text(encoding="utf-8")
    usage = shell[shell.index("<LicenseCheckPanel") :]
    usage = usage[: usage.index("/>")]
    assert "key={licenseCheckKey(configYaml, parameterValues)}" in usage
    assert "parameters={parameterValues}" in usage
    helper = panel[panel.index("export function licenseCheckKey(") :]
    helper = helper[: helper.index("\n}\n")]
    assert "configYaml" in helper and "parameters" in helper and ".sort()" in helper
