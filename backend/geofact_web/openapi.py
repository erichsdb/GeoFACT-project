"""OpenAPI-Spezifikation der Web-API: was FastAPI nicht selbst ableiten kann.

Implements: FA85

FastAPI erzeugt die Spezifikation aus den Routen; drei Dinge stehen nicht in deren
Signaturen und kommen hier dazu:

- Anmeldung: Das Gate ist eine Middleware, keine Dependency je Route. ``complete``
  trägt für jeden geschützten Pfad Security-Anforderung und Antwort 401 ein, nach
  derselben Regel wie die Middleware (``auth.requires_token``, ``auth.accepts_query_token``).
- Medientypen: Ereignisstrom, Rasterbild, ZIP und einzelne Datei (FA88) sind kein JSON;
  die Antwortklassen unten tragen den Medientyp.
- Fehlerantworten: ``errors`` beschreibt die Statuscodes einer Route.

``spec_text`` ist die eingecheckte Fassung (``docs/openapi.json``, geschrieben von
``scripts/export_openapi.py``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, Response, StreamingResponse

from . import auth
from .models import ErrorResponse
from .settings import REPO_ROOT

SPEC_PATH: Path = REPO_ROOT / "docs" / "openapi.json"

# Name, unter dem fastapi.security.HTTPBearer sein Schema einträgt.
BEARER_SCHEME = "HTTPBearer"
QUERY_TOKEN_SCHEME = "QueryToken"

_AUTH_NOTE = (
    "Nur wirksam, wenn GEOFACT_WEB_USERS gesetzt ist (FA24); ohne konfigurierte "
    "Nutzer wird kein Token geprüft."
)

_SECURITY_SCHEMES: dict[str, dict[str, Any]] = {
    BEARER_SCHEME: {
        "type": "http",
        "scheme": "bearer",
        "description": "Token aus POST /api/auth/login als 'Authorization: Bearer <token>'. "
        + _AUTH_NOTE,
    },
    QUERY_TOKEN_SCHEME: {
        "type": "apiKey",
        "in": "query",
        "name": "token",
        "description": "Dasselbe Token als Query-Parameter - nur für Ereignisstrom, Rasterbild "
        "und Download, die der Browser ohne Authorization-Header abruft. " + _AUTH_NOTE,
    },
}

TAGS: list[dict[str, str]] = [
    {
        "name": "auth",
        "description": "Anmeldung (FA24): Nutzername und Passwort gegen ein Token.",
    },
    {
        "name": "meta",
        "description": "Betriebszustand, Operationskatalog und Szenario-Schema.",
    },
    {
        "name": "catalog",
        "description": "Datenquellen: OSM-Presets, lokale Raster, eigene Uploads.",
    },
    {
        "name": "apis",
        "description": "Kuratierter Katalog offener Schnittstellen (FA36).",
    },
    {
        "name": "uploads",
        "description": "Eigene Dateien als Layer-Quelle hochladen und löschen (FA28).",
    },
    {
        "name": "config",
        "description": "Konfiguration prüfen, Lizenzen prüfen, per LLM erzeugen.",
    },
    {
        "name": "runs",
        "description": "Lauf starten, verfolgen, auflisten; Ergebnisse je Knoten, als einzelne Datei und als ZIP abrufen.",
    },
    {
        "name": "scenarios",
        "description": "Gespeicherte Szenarien und mitgelieferte Beispiele.",
    },
]


class EventStreamResponse(StreamingResponse):
    """Server-Sent Events."""

    media_type = "text/event-stream"


class PngResponse(Response):
    media_type = "image/png"


class ZipResponse(FileResponse):
    media_type = "application/zip"


class FileDownloadResponse(Response):
    """Eine einzelne Datei eines Laufs als Anhang (FA88)."""

    media_type = "application/octet-stream"


def errors(descriptions: dict[int, str]) -> dict[int | str, dict[str, Any]]:
    """``responses``-Einträge für die Statuscodes einer Route: Statuscode -> wann er auftritt."""
    return {
        code: {"model": ErrorResponse, "description": description}
        for code, description in descriptions.items()
    }


def complete(spec: dict[str, Any]) -> dict[str, Any]:
    """Trägt Security-Schemata, Anforderung und Antwort 401 je geschütztem Pfad ein. Ändert ``spec``."""
    components = spec.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update(_SECURITY_SCHEMES)
    components.setdefault("schemas", {}).setdefault(
        "ErrorResponse", ErrorResponse.model_json_schema()
    )
    unauthorized = {
        "description": "Token fehlt, ist ungültig oder abgelaufen. " + _AUTH_NOTE,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ErrorResponse"}
            }
        },
    }
    for path, item in spec.get("paths", {}).items():
        if not auth.requires_token(path):
            continue
        security: list[dict[str, list[str]]] = [{BEARER_SCHEME: []}]
        if auth.accepts_query_token(path):
            security.append({QUERY_TOKEN_SCHEME: []})
        for operation in item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            operation["security"] = security
            operation["responses"].setdefault("401", unauthorized)
    return spec


def install(app: FastAPI) -> None:
    """Hängt ``complete`` an die Spezifikation der App; ``/openapi.json``, ``/docs``,
    ``/redoc`` und ``spec_text`` liefern dieselbe Fassung."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = complete(FastAPI.openapi(app))
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]


def spec_text(app: FastAPI) -> str:
    """Die Spezifikation als Text von ``docs/openapi.json`` (sortierte Schlüssel, reproduzierbar)."""
    return (
        json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    )
