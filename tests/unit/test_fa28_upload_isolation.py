"""Tests für FA28 (Upload-Eigentümerschaft + Löschung).

Deckt: Happy Path (eigener Upload im Katalog sichtbar, löschbar),
Typfehler-Fall (fremder Upload weder im Katalog sichtbar noch löschbar -
404, nicht 403), Randfall (Löschen einer unbekannten/bereits gelöschten
source_id, unverändertes Single-User-Verhalten ohne konfigurierte
Nutzer).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import auth as auth_module  # noqa: E402
from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

ALICE_PASSWORD = "alice-secret"
BOB_PASSWORD = "bob-secret"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def two_users(monkeypatch, tmp_path):
    """Wie test_fa24_auth.py::two_users, zusätzlich mit isoliertem
    Upload-Verzeichnis je Test (sonst würden Uploads verschiedener
    Testläufe sich im eingecheckten Default-Verzeichnis ansammeln)."""
    alice_hash = auth_module.hash_password(ALICE_PASSWORD)
    bob_hash = auth_module.hash_password(BOB_PASSWORD)
    monkeypatch.setenv("GEOFACT_WEB_USERS", f"alice:{alice_hash},bob:{bob_hash}")
    monkeypatch.setenv("GEOFACT_WEB_UPLOADS_DIR", str(tmp_path / "uploads"))
    settings_module.get_settings.cache_clear()
    try:
        yield
    finally:
        settings_module.get_settings.cache_clear()


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _upload(
    client: TestClient, token: str, filename: str = "substations.geojson"
) -> str:
    with (FIXTURES_DIR / filename).open("rb") as fh:
        response = client.post(
            "/api/uploads",
            files={"file": (filename, fh, "application/geo+json")},
            headers=_auth_headers(token),
        )
    assert response.status_code == 200, response.text
    return response.json()["source"]["id"]


# --- Happy Path ----------------------------------------------------------


def test_fa28_own_upload_appears_in_own_catalog(client: TestClient, two_users):
    token = _login(client, "alice", ALICE_PASSWORD)
    source_id = _upload(client, token)

    catalog = client.get("/api/catalog", headers=_auth_headers(token)).json()
    assert any(u["id"] == source_id for u in catalog["uploads"])


def test_fa28_owner_can_delete_own_upload(client: TestClient, two_users):
    token = _login(client, "alice", ALICE_PASSWORD)
    source_id = _upload(client, token)

    response = client.delete(f"/api/uploads/{source_id}", headers=_auth_headers(token))
    assert response.status_code == 204

    catalog = client.get("/api/catalog", headers=_auth_headers(token)).json()
    assert all(u["id"] != source_id for u in catalog["uploads"])


# --- Typfehler-Fall: fremder Zugriff -------------------------------------


def test_fa28_upload_is_invisible_to_other_user(client: TestClient, two_users):
    alice_token = _login(client, "alice", ALICE_PASSWORD)
    bob_token = _login(client, "bob", BOB_PASSWORD)
    source_id = _upload(client, alice_token)

    bob_catalog = client.get("/api/catalog", headers=_auth_headers(bob_token)).json()
    assert all(u["id"] != source_id for u in bob_catalog["uploads"])


def test_fa28_other_user_cannot_delete_upload(client: TestClient, two_users):
    alice_token = _login(client, "alice", ALICE_PASSWORD)
    bob_token = _login(client, "bob", BOB_PASSWORD)
    source_id = _upload(client, alice_token)

    # Bob bekommt 404 - nicht 403 (keine Bestätigung, dass die source_id
    # überhaupt existiert), analog zu FA24s run_id-Isolation.
    response = client.delete(
        f"/api/uploads/{source_id}", headers=_auth_headers(bob_token)
    )
    assert response.status_code == 404

    # Alice sieht ihren Upload weiterhin - Bobs Versuch hat nichts gelöscht.
    alice_catalog = client.get(
        "/api/catalog", headers=_auth_headers(alice_token)
    ).json()
    assert any(u["id"] == source_id for u in alice_catalog["uploads"])


# --- Randfall --------------------------------------------------------------


def test_fa28_delete_unknown_upload_returns_404(client: TestClient, two_users):
    token = _login(client, "alice", ALICE_PASSWORD)
    response = client.delete(
        "/api/uploads/upload.does-not-exist.geojson", headers=_auth_headers(token)
    )
    assert response.status_code == 404


def test_fa28_delete_already_deleted_upload_returns_404(client: TestClient, two_users):
    token = _login(client, "alice", ALICE_PASSWORD)
    source_id = _upload(client, token)

    first = client.delete(f"/api/uploads/{source_id}", headers=_auth_headers(token))
    assert first.status_code == 204

    second = client.delete(f"/api/uploads/{source_id}", headers=_auth_headers(token))
    assert second.status_code == 404


def test_fa28_no_configured_users_keeps_uploads_visible_to_anonymous(
    client: TestClient, monkeypatch, tmp_path
):
    """Ohne GEOFACT_WEB_USERS bleibt das Verhalten unverändert: ein
    einzelner impliziter Pseudo-Nutzer 'anonymous' lädt hoch und sieht
    seinen eigenen Upload weiterhin (kein Bruch der Single-User-Demo)."""
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    monkeypatch.setenv("GEOFACT_WEB_UPLOADS_DIR", str(tmp_path / "uploads"))
    settings_module.get_settings.cache_clear()
    try:
        with (FIXTURES_DIR / "substations.geojson").open("rb") as fh:
            uploaded = client.post(
                "/api/uploads",
                files={"file": ("substations.geojson", fh, "application/geo+json")},
            )
        assert uploaded.status_code == 200, uploaded.text
        source_id = uploaded.json()["source"]["id"]

        catalog = client.get("/api/catalog").json()
        assert any(u["id"] == source_id for u in catalog["uploads"])

        deleted = client.delete(f"/api/uploads/{source_id}")
        assert deleted.status_code == 204
    finally:
        settings_module.get_settings.cache_clear()
