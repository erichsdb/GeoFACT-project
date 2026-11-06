"""Tests für FA95 (API-Dokumentation unter /api erreichbar).

Implements: FA95

Deckt: die drei Dokumentationspfade ohne Token mit und ohne konfigurierte Anmeldung,
andere /api-Pfade bleiben geschützt (Gegenprobe), die alten Wurzelpfade leiten weiter,
Swagger UI lädt die Spezifikation von /api/openapi.json, die ausgelieferte Spezifikation
ist die eingecheckte Fassung; jeder Dokumentationspfad verträgt einen Schrägstrich am Ende,
und die Seiten laden ihre Skripte in fester Fassung mit Prüfsumme.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import auth as auth_module  # noqa: E402
from geofact_web import docs_pages  # noqa: E402
from geofact_web import openapi  # noqa: E402
from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402

DOC_PATHS = ("/api/docs", "/api/redoc", "/api/openapi.json")


@pytest.fixture
def open_client(tmp_path, monkeypatch) -> TestClient:
    """Ohne GEOFACT_WEB_USERS (lokale Demo)."""
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "daten"))
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    settings_module.get_settings.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        settings_module.get_settings.cache_clear()


@pytest.fixture
def guarded_client(tmp_path, monkeypatch) -> TestClient:
    """Mit konfigurierter Anmeldung (gehostete Instanz)."""
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "daten"))
    monkeypatch.setenv(
        "GEOFACT_WEB_USERS", f"alice:{auth_module.hash_password('geheim')}"
    )
    settings_module.get_settings.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        settings_module.get_settings.cache_clear()


@pytest.mark.parametrize("path", DOC_PATHS)
def test_fa95_docs_paths_answer_without_token_when_auth_configured(
    guarded_client, path
):
    assert guarded_client.get(path).status_code == 200


@pytest.mark.parametrize("path", DOC_PATHS)
def test_fa95_docs_paths_answer_without_auth_configured(open_client, path):
    assert open_client.get(path).status_code == 200


def test_fa95_other_api_path_still_requires_token(guarded_client):
    assert guarded_client.get("/api/catalog").status_code == 401
    assert guarded_client.get("/api/docs/anything-else").status_code == 401


def test_fa95_docs_paths_are_public_for_the_gate_and_the_spec():
    for path in (*DOC_PATHS, auth_module.OAUTH2_REDIRECT_URL):
        assert not auth_module.requires_token(path)
    assert auth_module.requires_token("/api/catalog")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/docs", "/api/docs"),
        ("/redoc", "/api/redoc"),
        ("/openapi.json", "/api/openapi.json"),
    ],
)
def test_fa95_old_root_paths_redirect_to_api(open_client, old, new):
    response = open_client.get(old, follow_redirects=False)
    assert response.status_code in (301, 302, 307, 308)
    assert response.headers["location"] == new
    assert open_client.get(old).status_code == 200  # Weiterleitung führt ans Ziel


@pytest.mark.parametrize("page", auth_module.DOCS_PATHS)
def test_fa95_trailing_slash_redirects_without_token(guarded_client, page):
    response = guarded_client.get(page + "/", follow_redirects=False)
    assert response.status_code in (301, 302, 307, 308)
    assert response.headers["location"] == page  # relativ: unabhängig vom Proxy-Schema
    assert guarded_client.get(page + "/").status_code == 200


@pytest.mark.parametrize("page", auth_module.DOCS_PATHS)
def test_fa95_trailing_slash_redirects_the_same_way_without_auth_configured(
    open_client, page
):
    """Ohne Anmeldung dieselbe relative Weiterleitung, nicht Starlettes absolute Adresse."""
    response = open_client.get(page + "/", follow_redirects=False)
    assert response.status_code in (301, 302, 307, 308)
    assert response.headers["location"] == page
    assert open_client.get(page + "/").status_code == 200


def test_fa95_docs_paths_are_the_four_documentation_paths():
    assert auth_module.DOCS_PATHS == (
        "/api/docs",
        "/api/redoc",
        "/api/openapi.json",
        "/api/docs/oauth2-redirect",
    )
    # Gegenprobe: der Schrägstrich öffnet nichts außer der Dokumentation
    assert auth_module.requires_token("/api/catalog/")
    assert auth_module.requires_token("/api/docs/anything-else/")


def test_fa95_redirects_are_not_part_of_the_schema(open_client):
    paths = open_client.get("/api/openapi.json").json()["paths"]
    assert not {"/docs", "/redoc", "/openapi.json", *DOC_PATHS} & set(paths)


def test_fa95_swagger_html_loads_spec_from_api_path(open_client):
    html = open_client.get("/api/docs").text
    assert "/api/openapi.json" in html
    assert "/api/docs/oauth2-redirect" in html


def _tags_loading(html: str, url: str) -> list[str]:
    return [tag for tag in re.findall(r"<(?:script|link)\b[^>]*>", html) if url in tag]


@pytest.mark.parametrize(
    ("page", "assets"),
    [
        ("/api/docs", (docs_pages.SWAGGER_JS, docs_pages.SWAGGER_CSS)),
        ("/api/redoc", (docs_pages.REDOC_JS,)),
    ],
)
def test_fa95_pages_load_pinned_assets_with_integrity(open_client, page, assets):
    html = open_client.get(page).text
    for asset in assets:
        tags = _tags_loading(html, asset.url)
        assert len(tags) == 1, asset.url
        assert f'integrity="{asset.integrity}"' in tags[0]
        assert 'crossorigin="anonymous"' in tags[0]
    # jedes fremde Skript und Stylesheet der Seite trägt eine Prüfsumme
    for tag in re.findall(r"<(?:script|link)\b[^>]*https?://[^>]*>", html):
        if 'rel="shortcut icon"' in tag:
            continue
        assert "integrity=" in tag, tag
    assert "fonts.googleapis.com" not in html


def test_fa95_pinned_assets_name_an_exact_version_and_a_sha384_checksum():
    for asset in (docs_pages.SWAGGER_JS, docs_pages.SWAGGER_CSS, docs_pages.REDOC_JS):
        # genau eine Fassung (x.y.z), keine gleitende wie @5 oder @latest
        assert re.search(r"@\d+\.\d+\.\d+/", asset.url), asset.url
        assert asset.url.startswith("https://")
        assert re.fullmatch(r"sha384-[A-Za-z0-9+/]{64}", asset.integrity), asset.url


def test_fa95_missing_asset_in_the_template_fails_instead_of_serving_unchecked():
    """Goldene Regel 7: ohne Stelle für die Prüfsumme kein stilles Ausliefern."""
    with pytest.raises(RuntimeError, match="integrity"):
        docs_pages.with_integrity("<html></html>", docs_pages.SWAGGER_JS)
    twice = f'<script src="{docs_pages.REDOC_JS.url}"></script>' * 2
    with pytest.raises(RuntimeError, match="2-mal"):
        docs_pages.with_integrity(twice, docs_pages.REDOC_JS)


def test_fa95_oauth2_redirect_answers_without_token(guarded_client):
    assert guarded_client.get("/api/docs/oauth2-redirect").status_code == 200


def test_fa95_served_spec_equals_spec_text(open_client):
    served = open_client.get("/api/openapi.json").json()
    assert served == open_client.app.openapi()
    expected = openapi.spec_text(open_client.app)
    assert expected.startswith("{")
    import json

    assert json.loads(expected) == served


def test_fa95_authorize_scheme_stays_in_the_served_spec(guarded_client):
    """Swagger UI bietet 'Authorize' nur an, wenn die Spezifikation das Schema trägt (FA85)."""
    spec = guarded_client.get("/api/openapi.json").json()
    assert openapi.BEARER_SCHEME in spec["components"]["securitySchemes"]
    assert spec["paths"]["/api/catalog"]["get"]["security"]
