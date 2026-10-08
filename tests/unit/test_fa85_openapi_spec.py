"""Tests für FA85 (aufrufbare OpenAPI-Spezifikation der Web-API).

Deckt die Nachbedingungen: Security und 401 decken sich mit der Middleware,
Query-Token nur an den vier Pfaden, jede Erfolgsantwort hat ein Schema bzw.
den richtigen Medientyp, gemeldete Fehlercodes sind beschrieben, die
typisierten Antworten verlieren nichts, die eingecheckte Datei ist aktuell.
Randfälle: leere Spezifikation, Erreichbarkeit ohne Token.
"""

from __future__ import annotations

import inspect
import re
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import auth as auth_module  # noqa: E402
from geofact_web import models, openapi  # noqa: E402
from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.routers import meta as meta_router  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"

JSON = "application/json"
# Pfade, deren Erfolgsantwort kein JSON ist, mit ihrem Medientyp.
NON_JSON = {
    ("post", "/api/config/generate/stream"): "text/event-stream",
    ("get", "/api/runs/{run_id}/events"): "text/event-stream",
    ("get", "/api/runs/{run_id}/nodes/{node_id}/raster.png"): "image/png",
    ("get", "/api/runs/{run_id}/download"): "application/zip",
    ("get", "/api/runs/{run_id}/outputs/{name}"): "application/octet-stream",  # FA88
}
# Die Antwort ist selbst ein JSON-Schema-Dokument - bewusst frei.
FREE_FORM = {("get", "/api/schema")}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "daten"))
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    settings_module.get_settings.cache_clear()
    try:
        yield create_app()
    finally:
        settings_module.get_settings.cache_clear()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def spec(app) -> dict:
    return app.openapi()


@pytest.fixture
def one_user(monkeypatch):
    monkeypatch.setenv(
        "GEOFACT_WEB_USERS", f"alice:{auth_module.hash_password('geheim')}"
    )
    settings_module.get_settings.cache_clear()


def _operations(spec: dict):
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            yield method, path, operation


def _success(operation: dict) -> tuple[str, dict]:
    code = next(code for code in operation["responses"] if code.startswith("2"))
    return code, operation["responses"][code]


def _vector_config() -> dict:
    return {
        "scenario": {"name": "OpenAPI-Test", "region": BBOX_REGION},
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


def _run_to_done(client: TestClient, timeout: float = 30.0) -> str:
    created = client.post("/api/runs", json={"config": _vector_config()})
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/runs/{run_id}").json()
        if status["status"] in ("done", "error", "cancelled"):
            assert status["status"] == "done", status
            return run_id
        time.sleep(0.05)
    raise AssertionError("Lauf wurde nicht rechtzeitig fertig.")


# --- Anmeldung: Spezifikation = Middleware ------------------------------


def test_fa85_security_and_401_follow_the_middleware_rule(spec):
    for method, path, operation in _operations(spec):
        schemes = [name for entry in operation.get("security", []) for name in entry]
        if auth_module.requires_token(path):
            assert openapi.BEARER_SCHEME in schemes, f"{method} {path}"
            assert "401" in operation["responses"], f"{method} {path}"
        else:
            assert schemes == [], f"{method} {path}"


def test_fa85_login_and_health_are_the_only_public_operations(spec):
    public = {
        path
        for _method, path, operation in _operations(spec)
        if not operation.get("security")
    }
    assert public == {"/api/auth/login", "/api/health"}


def test_fa85_query_token_is_declared_exactly_where_the_middleware_accepts_it(spec):
    declared = {
        path
        for _method, path, operation in _operations(spec)
        if {openapi.QUERY_TOKEN_SCHEME: []} in operation.get("security", [])
    }
    assert declared == {
        "/api/runs/{run_id}/events",
        "/api/runs/{run_id}/nodes/{node_id}/raster.png",
        "/api/runs/{run_id}/download",
        "/api/runs/{run_id}/outputs/{name}",  # FA88
    }
    scheme = spec["components"]["securitySchemes"][openapi.QUERY_TOKEN_SCHEME]
    assert (scheme["in"], scheme["name"]) == ("query", "token")


def test_fa85_a_route_added_later_is_described_as_protected_without_further_work(app):
    """Die Middleware schützt auch eine Route ohne require_user - die
    Spezifikation muss das von selbst wissen."""
    app.add_api_route("/api/spaeter", lambda: {"ok": True}, methods=["GET"])
    app.openapi_schema = None
    operation = app.openapi()["paths"]["/api/spaeter"]["get"]
    assert operation["security"] == [{openapi.BEARER_SCHEME: []}]
    assert "401" in operation["responses"]


def test_fa85_documented_401_is_what_a_protected_route_answers(app, one_user):
    response = TestClient(app).get("/api/catalog")
    assert response.status_code == 401
    models.ErrorResponse.model_validate(response.json())


def test_fa85_spec_and_docs_stay_reachable_without_a_token(app, one_user):
    client = TestClient(app)
    assert client.get("/openapi.json").json() == app.openapi()
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


# --- Erfolgsantworten: Schema und Medientyp -----------------------------


def test_fa85_every_json_answer_has_a_named_schema(spec):
    for method, path, operation in _operations(spec):
        if (method, path) in NON_JSON or (method, path) in FREE_FORM:
            continue
        code, response = _success(operation)
        if code == "204":
            assert "content" not in response, f"{method} {path}"
            continue
        schema = response["content"][JSON]["schema"]
        ref = schema.get("$ref") or schema.get("items", {}).get("$ref")
        assert ref, f"{method} {path}: Antwort ohne Schema ({schema})"
        assert ref.rsplit("/", 1)[1] in spec["components"]["schemas"]


def test_fa85_streams_image_and_archive_declare_their_media_type(spec):
    for (method, path), media_type in NON_JSON.items():
        _code, response = _success(spec["paths"][path][method])
        assert list(response["content"]) == [media_type], f"{method} {path}"


def test_fa85_declared_media_types_are_what_a_run_answers(client, spec):
    run_id = _run_to_done(client)
    for suffix, template in (
        ("events", "/api/runs/{run_id}/events"),
        ("download", "/api/runs/{run_id}/download"),
    ):
        response = client.get(f"/api/runs/{run_id}/{suffix}")
        assert response.status_code == 200, response.text
        assert (
            response.headers["content-type"].split(";")[0]
            == NON_JSON[("get", template)]
        )


def test_fa85_node_result_matches_its_documented_model(client):
    run_id = _run_to_done(client)
    body = client.get(f"/api/runs/{run_id}/nodes/catchment").json()
    result = models.NodeResult.model_validate(body)
    assert result.describe.kind == "vector"
    assert result.geojson is not None and result.graph is None
    # Nur beschrieben, nicht gefiltert: kein Feld der Antwort fehlt im Modell.
    assert set(body) <= set(models.NodeResult.model_fields)


def test_fa85_typed_answers_drop_nothing(client):
    """response_model filtert unbekannte Felder still weg - die Modelle
    müssen deshalb alles tragen, was die Funktionen liefern."""
    assert client.get("/api/health").json() == meta_router.health()
    assert client.get("/api/meta").json() == meta_router.meta()
    for entry in client.get("/api/operations").json():
        assert set(entry) == set(models.OperationContract.model_fields)
        models.OperationContract.model_validate(entry)


def test_fa85_cancel_and_delete_answers_match_their_models(client):
    run_id = _run_to_done(client)
    cancelled = client.post(f"/api/runs/{run_id}/cancel").json()
    assert cancelled == {"run_id": run_id, "cancelling": False}
    deleted = client.delete("/api/scenarios/gibt-es-nicht").json()
    assert deleted == {"name": "gibt-es-nicht", "deleted": False}


# --- Fehlerantworten ----------------------------------------------------


def test_fa85_status_codes_raised_in_a_route_are_documented(app, spec):
    """Statisch: jeder Statuscode, den der Quelltext einer Route nennt, steht
    in ihrer Beschreibung (Hilfsfunktionen deckt der nächste Test ab)."""
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        raised = set(
            re.findall(r"status_code=(\d{3})", inspect.getsource(route.endpoint))
        )
        for method in route.methods:
            documented = set(
                spec["paths"][route.path_format][method.lower()]["responses"]
            )
            assert raised <= documented, (
                f"{method} {route.path_format}: {raised - documented}"
            )


@pytest.mark.parametrize(
    ("method", "path", "template", "status"),
    [
        ("get", "/api/runs/unbekannt", "/api/runs/{run_id}", 404),
        ("post", "/api/runs/unbekannt/cancel", "/api/runs/{run_id}/cancel", 404),
        ("get", "/api/runs/unbekannt/events", "/api/runs/{run_id}/events", 404),
        (
            "get",
            "/api/runs/unbekannt/nodes/x",
            "/api/runs/{run_id}/nodes/{node_id}",
            404,
        ),
        (
            "get",
            "/api/runs/unbekannt/nodes/x/raster.png",
            "/api/runs/{run_id}/nodes/{node_id}/raster.png",
            404,
        ),
        ("get", "/api/runs/unbekannt/download", "/api/runs/{run_id}/download", 404),
        ("get", "/api/scenarios/gibt-es-nicht", "/api/scenarios/{name}", 404),
        ("get", "/api/scenarios/%20", "/api/scenarios/{name}", 400),
        ("delete", "/api/scenarios/%20", "/api/scenarios/{name}", 400),
        (
            "get",
            "/api/scenarios/examples/gibt-es-nicht",
            "/api/scenarios/examples/{example_id}",
            404,
        ),
        ("get", "/api/apis/gibt-es-nicht/spec", "/api/apis/{api_id}/spec", 404),
        ("delete", "/api/uploads/gibt-es-nicht", "/api/uploads/{source_id}", 404),
    ],
)
def test_fa85_an_error_answer_is_documented_and_has_the_error_shape(
    client, spec, method, path, template, status
):
    response = client.request(method, path)
    assert response.status_code == status, response.text
    assert str(status) in spec["paths"][template][method]["responses"]
    models.ErrorResponse.model_validate(response.json())


def test_fa85_rejected_upload_is_documented(client, spec):
    response = client.post("/api/uploads", files={"file": ("daten.exe", b"x")})
    assert response.status_code == 400
    assert "400" in spec["paths"]["/api/uploads"]["post"]["responses"]


def test_fa85_invalid_config_and_malformed_request_share_the_documented_422(
    client, spec
):
    documented = spec["paths"]["/api/runs"]["post"]["responses"]["422"]
    assert documented["content"][JSON]["schema"]["$ref"].endswith(
        "/RejectedRequestResponse"
    )

    invalid = client.post("/api/runs", json={"config": {"scenario": {}}})
    assert invalid.status_code == 422
    detail = models.RejectedRequestResponse.model_validate(invalid.json()).detail
    assert isinstance(detail, models.InvalidConfigDetail) and detail.issues

    malformed = client.post("/api/runs", json={"refresh_snapshots": "vielleicht"})
    assert malformed.status_code == 422
    assert isinstance(
        models.RejectedRequestResponse.model_validate(malformed.json()).detail, list
    )


def test_fa85_download_before_the_end_is_the_documented_409(client, spec, monkeypatch):
    from geofact_web.run_manager import manager

    monkeypatch.setattr(manager, "start", lambda state: None)  # Lauf bleibt "pending"
    run_id = client.post("/api/runs", json={"config": _vector_config()}).json()[
        "run_id"
    ]
    try:
        response = client.get(f"/api/runs/{run_id}/download")
    finally:
        # Der Manager ist prozessweit: kein hängender Lauf für spätere Tests.
        manager.get(run_id).status = "cancelled"
    assert response.status_code == 409
    assert "409" in spec["paths"]["/api/runs/{run_id}/download"]["get"]["responses"]


# --- Lesbarkeit ---------------------------------------------------------


def test_fa85_every_operation_has_summary_description_and_a_described_tag(spec):
    described = {tag["name"] for tag in spec["tags"] if tag.get("description")}
    for method, path, operation in _operations(spec):
        assert operation.get("summary"), f"{method} {path}"
        assert operation.get("description"), f"{method} {path}"
        assert set(operation["tags"]) <= described, f"{method} {path}"
        for code, response in operation["responses"].items():
            assert response.get("description"), f"{method} {path} {code}"


# --- Eingecheckte Datei -------------------------------------------------


def test_fa85_committed_spec_matches_the_code(app):
    assert openapi.SPEC_PATH.is_file(), (
        "docs/openapi.json fehlt - scripts/export_openapi.py ausführen"
    )
    assert openapi.SPEC_PATH.read_text(encoding="utf-8") == openapi.spec_text(app), (
        "docs/openapi.json ist veraltet - uv run --extra web python scripts/export_openapi.py"
    )


def test_fa85_spec_text_is_the_same_for_every_app_and_configuration(app, one_user):
    """Die Datei hängt nicht davon ab, ob Nutzer konfiguriert sind."""
    assert openapi.spec_text(create_app()) == openapi.spec_text(app)


# --- Randfall -----------------------------------------------------------


def test_fa85_complete_on_an_empty_spec_only_adds_the_schemes():
    spec = openapi.complete({})
    assert set(spec["components"]["securitySchemes"]) == {
        openapi.BEARER_SCHEME,
        openapi.QUERY_TOKEN_SCHEME,
    }
    assert "paths" not in spec
