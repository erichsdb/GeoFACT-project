"""Seiten der API-Dokumentation (Swagger UI, ReDoc) mit fest benannten Skripten.

Implements: FA95

Die Seiten liegen im selben Ursprung wie die Oberfläche, die das Token im Browser
hält. FastAPI lädt Swagger UI und ReDoc in der Voreinstellung als gleitende Fassung
(``@5``, ``@2``) von einem CDN; ein dort ausgetauschtes Skript liefe mit den Rechten
der Oberfläche. Deshalb nennt GeoFACT genau eine Fassung je Datei und ihre Prüfsumme
(Subresource Integrity): der Browser führt nur die Datei mit dieser Prüfsumme aus.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse

from .auth import DOCS_URL, OAUTH2_REDIRECT_URL, OPENAPI_URL, REDOC_URL

_CDN = "https://cdn.jsdelivr.net/npm/"


@dataclass(frozen=True)
class PinnedAsset:
    """Eine Datei vom CDN in genau einer Fassung, mit ihrer SHA-384-Prüfsumme."""

    url: str
    integrity: str


# Neue Fassung: URL ändern und die Prüfsumme der Datei neu bilden
# (openssl dgst -sha384 -binary DATEI | openssl base64 -A).
SWAGGER_JS = PinnedAsset(
    _CDN + "swagger-ui-dist@5.17.14/swagger-ui-bundle.js",
    "sha384-wmyclcVGX/WhUkdkATwhaK1X1JtiNrr2EoYJ+diV3vj4v6OC5yCeSu+yW13SYJep",
)
SWAGGER_CSS = PinnedAsset(
    _CDN + "swagger-ui-dist@5.17.14/swagger-ui.css",
    "sha384-wxLW6kwyHktdDGr6Pv1zgm/VGJh99lfUbzSn6HNHBENZlCN7W602k9VkGdxuFvPn",
)
REDOC_JS = PinnedAsset(
    _CDN + "redoc@2.1.5/bundles/redoc.standalone.js",
    "sha384-0GrsyTQc9Oqd8h+b2dbc4XdR2T/DYpy0tLNNstyx+LBMUyiBbcWPbEs9aRmUcaxD",
)


def with_integrity(html: str, asset: PinnedAsset) -> str:
    """Trägt Prüfsumme und ``crossorigin`` an der einen Stelle ein, die ``asset`` lädt.

    FastAPIs Seitenvorlagen kennen kein ``integrity``-Attribut. Steht die URL nicht
    genau einmal in der Seite (Vorlage geändert), scheitert der Start, statt die Seite
    ungeprüft auszuliefern.
    """
    quoted = f'"{asset.url}"'
    if html.count(quoted) != 1:
        raise RuntimeError(
            f"API-Dokumentation: '{asset.url}' steht {html.count(quoted)}-mal in der "
            "Seitenvorlage von FastAPI, erwartet ist genau einmal - die Prüfsumme "
            "(integrity) lässt sich nicht eintragen."
        )
    return html.replace(
        quoted, f'{quoted} integrity="{asset.integrity}" crossorigin="anonymous"'
    )


def swagger_html(title: str) -> str:
    html = get_swagger_ui_html(
        openapi_url=OPENAPI_URL,
        title=f"{title} - Swagger UI",
        swagger_js_url=SWAGGER_JS.url,
        swagger_css_url=SWAGGER_CSS.url,
        oauth2_redirect_url=OAUTH2_REDIRECT_URL,
    ).body.decode("utf-8")
    return with_integrity(with_integrity(html, SWAGGER_JS), SWAGGER_CSS)


def redoc_html(title: str) -> str:
    # Ohne Google Fonts: ReDoc bleibt lesbar, und die Seite lädt nur die eine Datei.
    html = get_redoc_html(
        openapi_url=OPENAPI_URL,
        title=f"{title} - ReDoc",
        redoc_js_url=REDOC_JS.url,
        with_google_fonts=False,
    ).body.decode("utf-8")
    return with_integrity(html, REDOC_JS)


def install(app: FastAPI) -> None:
    """Hängt die beiden Seiten und den Rücksprungpfad von Swagger UI ein.

    Die Seiten entstehen einmal beim Aufbau der App; sie stehen nicht in der
    Spezifikation (einfache Starlette-Routen).
    """
    swagger = swagger_html(app.title)
    redoc = redoc_html(app.title)

    def swagger_page(_request: Request) -> HTMLResponse:
        return HTMLResponse(swagger)

    def redoc_page(_request: Request) -> HTMLResponse:
        return HTMLResponse(redoc)

    def oauth2_redirect(_request: Request) -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    for path, page in (
        (DOCS_URL, swagger_page),
        (REDOC_URL, redoc_page),
        (OAUTH2_REDIRECT_URL, oauth2_redirect),
    ):
        app.add_route(path, page, methods=["GET"], include_in_schema=False)
