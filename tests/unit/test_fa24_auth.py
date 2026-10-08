"""Tests für FA24 (Web-Zugriffskontrolle: Nutzerliste, Token, Run-Isolation).

Deckt: Login-Happy-Path, Typfehler-Fälle (fehlendes/ungueltiges/falsches
Token, falsches Passwort), Randfall (Concurrency-Limit je Nutzer,
Owner-Isolation fremder Läufe, unverändertes Verhalten ohne
konfigurierte Nutzer - Rückwärtskompatibilität zur Single-User-Demo).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import auth as auth_module  # noqa: E402
from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"

ALICE_PASSWORD = "alice-secret"
BOB_PASSWORD = "bob-secret"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def two_users(monkeypatch):
    """Setzt GEOFACT_WEB_USERS auf alice/bob und räumt den Settings-Cache
    davor UND danach auf (Cache ist @lru_cache, siehe test_web_backend.py)."""
    alice_hash = auth_module.hash_password(ALICE_PASSWORD)
    bob_hash = auth_module.hash_password(BOB_PASSWORD)
    monkeypatch.setenv("GEOFACT_WEB_USERS", f"alice:{alice_hash},bob:{bob_hash}")
    monkeypatch.setenv("GEOFACT_WEB_MAX_CONCURRENT_RUNS_PER_USER", "1")
    settings_module.get_settings.cache_clear()
    try:
        yield
    finally:
        settings_module.get_settings.cache_clear()


def _vector_config() -> dict:
    return {
        "scenario": {"name": "Auth-Test", "region": BBOX_REGION},
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


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- Happy Path -------------------------------------------------------


def test_fa24_login_succeeds_with_correct_password(client: TestClient, two_users):
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": ALICE_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "alice"
    assert body["token"]
    assert body["expires_in_s"] > 0


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer abgelaufen-oder-kaputt"}]
)
def test_fa24_rejection_is_readable_by_the_browser(
    client: TestClient, two_users, headers
):
    """Die 401-Antwort des Gates trägt die CORS-Header. Ohne sie verwirft der
    Browser die Antwort als Netzwerkfehler: die Oberfläche sah bei einem
    abgelaufenen Token keinen Status 401 und zeigte keine Anmeldemaske
    (Nutzerbericht 08.10.2026)."""
    origin = "http://localhost:3000"
    response = client.get("/api/meta", headers={"Origin": origin, **headers})
    assert response.status_code == 401
    assert response.headers.get("access-control-allow-origin") == origin


def test_fa24_preflight_still_passes_without_a_token(client: TestClient, two_users):
    response = client.options(
        "/api/meta",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code == 200
    assert (
        response.headers.get("access-control-allow-origin") == "http://localhost:3000"
    )


def test_fa24_token_grants_access_to_protected_endpoint(client: TestClient, two_users):
    token = _login(client, "alice", ALICE_PASSWORD)
    response = client.get("/api/operations", headers=_auth_headers(token))
    assert response.status_code == 200


def test_fa24_query_token_accepted_for_download_but_not_regular_endpoints(
    client: TestClient, two_users
):
    """EventSource/<img>/<a> können keinen Authorization-Header setzen -
    genau die betroffenen Routen (events/raster.png/download, seit FA88 auch
    outputs/{name}; dazu test_fa88_run_outputs.py) dürfen
    das Token als ?token= tragen, alle anderen Endpunkte NICHT (sonst
    landen Tokens unnötig in Server-/Proxy-Logs)."""
    token = _login(client, "alice", ALICE_PASSWORD)
    run_id = _run_to_done(client, token)

    ok = client.get(f"/api/runs/{run_id}/download?token={token}")
    assert ok.status_code == 200

    # Regulärer JSON-Endpunkt akzeptiert das Token NICHT über die Query.
    rejected = client.get(f"/api/runs/{run_id}?token={token}")
    assert rejected.status_code == 401


def test_fa24_health_and_login_remain_public_without_token(
    client: TestClient, two_users
):
    assert client.get("/api/health").status_code == 200
    # Falsches Passwort soll 401 liefern, nicht durch das Auth-Gate selbst
    # blockiert werden (der Login-Endpunkt ist von der Gate-Prüfung
    # ausgenommen, damit man sich überhaupt anmelden kann).
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong"}
    )
    assert response.status_code == 401


def test_fa24_cors_preflight_is_never_blocked_by_auth_gate(
    client: TestClient, two_users
):
    """Regression: ein Browser-CORS-Preflight (OPTIONS) trägt nie einen
    Authorization-Header (der Browser sendet ihn erst bei der eigentlichen
    Anfrage) - die Middleware darf einen Preflight deshalb nicht mit 401
    ablehnen, sonst bricht der Browser die eigentliche (authentifizierte)
    Anfrage ab, bevor sie je gesendet wird ('Load failed' ohne erkennbaren
    Statuscode, kein sichtbarer 401 im Netzwerk-Tab)."""
    response = client.options(
        "/api/meta",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert response.status_code != 401


# --- Typfehler-Fälle ---------------------------------------------------


def test_fa24_login_rejects_wrong_password(client: TestClient, two_users):
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": "wrong"}
    )
    assert response.status_code == 401


def test_fa24_login_rejects_unknown_user(client: TestClient, two_users):
    response = client.post(
        "/api/auth/login", json={"username": "carol", "password": "whatever"}
    )
    assert response.status_code == 401


def test_fa24_protected_endpoint_without_token_is_rejected(
    client: TestClient, two_users
):
    response = client.get("/api/operations")
    assert response.status_code == 401


def test_fa24_protected_endpoint_with_garbage_token_is_rejected(
    client: TestClient, two_users
):
    response = client.get("/api/operations", headers=_auth_headers("not-a-real-token"))
    assert response.status_code == 401


def test_fa24_expired_token_is_rejected(client: TestClient, two_users, monkeypatch):
    # Token mit TTL=0 erzeugen (sofort "abgelaufen"). Settings ist frozen -
    # über die Env-Variable + Cache-Clear setzen statt zu mutieren.
    monkeypatch.setenv("GEOFACT_WEB_TOKEN_TTL_S", "0")
    settings_module.get_settings.cache_clear()
    settings = settings_module.get_settings()
    token = auth_module.create_token("alice", settings)
    time.sleep(0.01)
    response = client.get("/api/operations", headers=_auth_headers(token))
    assert response.status_code == 401


# --- Randfall: Run-Isolation + Concurrency-Limit ------------------------


def _run_to_done(client: TestClient, token: str, timeout: float = 30.0) -> str:
    created = client.post(
        "/api/runs", json={"config": _vector_config()}, headers=_auth_headers(token)
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/api/runs/{run_id}", headers=_auth_headers(token)).json()
        if status["status"] in ("done", "error", "cancelled"):
            assert status["status"] == "done", status
            return run_id
        time.sleep(0.05)
    raise AssertionError("Lauf wurde nicht rechtzeitig fertig.")


def test_fa24_run_is_invisible_to_other_user(client: TestClient, two_users):
    alice_token = _login(client, "alice", ALICE_PASSWORD)
    bob_token = _login(client, "bob", BOB_PASSWORD)

    run_id = _run_to_done(client, alice_token)

    # Bob bekommt für Alice's run_id ein 404 - nicht 403 (keine
    # Bestätigung, dass die run_id überhaupt existiert).
    response = client.get(f"/api/runs/{run_id}", headers=_auth_headers(bob_token))
    assert response.status_code == 404

    # Alice selbst sieht ihn weiterhin.
    own = client.get(f"/api/runs/{run_id}", headers=_auth_headers(alice_token))
    assert own.status_code == 200


def test_fa24_concurrent_run_limit_rejects_extra_run(two_users, monkeypatch):
    """GEOFACT_WEB_MAX_CONCURRENT_RUNS_PER_USER=1 (two_users-Fixture): ein
    zweiter Lauf desselben Nutzers wird abgelehnt (429-Mapping), solange der
    erste noch nicht abgeschlossen ist - Bob ist von Alice's Limit
    unbeeinflusst (Limit ist pro Nutzer).

    Direkt gegen RunManager statt über HTTP-Timing: der reale Lauf einer
    winzigen Fixture kann schneller fertig sein als der zweite Request
    ankommt (dann zählt er nicht mehr als "laufend") - der eigentliche
    Vertrag (create() wirft RunLimitExceeded ab dem konfigurierten Limit)
    ist unabhängig von Ausführungsgeschwindigkeit prüfbar, indem der
    erste Lauf absichtlich nicht gestartet wird (bleibt "pending", zählt
    laut _running_count_for_owner_locked ebenfalls als laufend)."""
    from geofact_web.run_manager import RunLimitExceeded, RunManager
    from geofact_web.scenario_service import validate_config

    manager = RunManager()
    document, validation = validate_config(None, _vector_config())
    assert document is not None, validation

    manager.create(
        document, refresh_snapshots=False, owner="alice"
    )  # nicht gestartet, bleibt pending
    with pytest.raises(RunLimitExceeded):
        manager.create(document, refresh_snapshots=False, owner="alice")

    # Bob ist von Alice's Limit unbeeinflusst.
    bob_state = manager.create(document, refresh_snapshots=False, owner="bob")
    assert bob_state.owner == "bob"


# --- Rückwärtskompatibilität ------------------------------------------


def test_fa24_no_configured_users_means_unauthenticated_access_unchanged(
    client: TestClient, monkeypatch
):
    """Ohne GEOFACT_WEB_USERS bleibt das bisherige Single-User-Demo-
    Verhalten unverändert - kein Token nötig, wie vor FA24."""
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    settings_module.get_settings.cache_clear()
    try:
        response = client.get("/api/operations")
        assert response.status_code == 200
    finally:
        settings_module.get_settings.cache_clear()
