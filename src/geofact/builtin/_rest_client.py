"""Implements: Grundlage für FA41, FA42, FA43 (REST-Endpunkte in der Konfiguration).

Gemeinsame Bausteine der drei REST-Bausteine des Kerns: Quellart ``source: rest``
(FA42), Operation ``op: rest`` (FA41) und Ausgabeformat ``type: rest`` (FA43).
Der Endpunkt und sein Vertrag stehen in der Szenario-YAML.

Orientierung an Standards:
- GeoJSON (RFC 7946): Austausch in WGS84.
- OGC API - Features: Region als ``bbox=min_lon,min_lat,max_lon,max_lat``,
  Blättern über ``links`` mit ``rel: next`` (paging: next_link).
- OGC API - Processes (synchron): Anfrage-Body als Vorlage mit Platzhaltern
  ``"@<port>"`` für die Eingangsdaten.
- HTTP: Wiederholung nur für GET und nur bei 429/502/503/504, nie für POST.

Asynchrone Aufträge, OAuth-Abläufe und andere Protokolle sind ausgeschlossen
und Fall für ein Python-Plugin.

Sicherheit: ``${VAR}`` in URL, Query, Headern und Body wird aus der Umgebung
ersetzt, aber nur für Variablen mit dem Präfix GEOFACT_REST_. Eine Szenario-Datei
kann aus fremder Quelle stammen (Web-Demo, LLM) und sonst Server-Geheimnisse an
eine fremde URL senden.
"""

from __future__ import annotations

import json
import os
import re
import time
import warnings
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import geopandas as gpd
import requests
from geopandas import GeoDataFrame
from pydantic import BaseModel, Field, field_validator, model_validator
from shapely import wkt as shapely_wkt
from shapely.geometry import Point

from geofact.builtin._tabular import GeometryMapping
from geofact.support import http

REST_CRS = "EPSG:4326"
ENV_PREFIX = "GEOFACT_REST_"
RETRY_STATUS = {429, 502, 503, 504}

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
PLACEHOLDER_PREFIX = "@"


class InsecureEndpointWarning(UserWarning):
    """Endpunkt ohne HTTPS: Daten und Header gehen im Klartext über die
    Leitung. Wird gemeldet, nicht verhindert (lokale Testdienste)."""


# --- Deklaration in der Konfiguration ---


class ResponseMapping(BaseModel):
    """Wie die Antwort zu lesen ist.

    format geojson: eine FeatureCollection (Standardfall, OGC API Features).
    crs nur setzen, wenn der Dienst ausdrücklich ein anderes CRS liefert.
    format json: beliebiges JSON; items ist der Pfad zur Objektliste
    (z. B. "data.results", leer = die Antwort selbst ist die Liste),
    geometry bildet Felder auf Geometrie ab - dasselbe Mapping wie bei
    Tabellen (x_column/y_column oder wkt_column, dazu crs)."""

    format: Literal["geojson", "json"] = "geojson"
    items: Optional[str] = None
    geometry: Optional[GeometryMapping] = None
    crs: str = REST_CRS

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def json_needs_geometry(self) -> "ResponseMapping":
        if self.format == "json" and self.geometry is None:
            raise ValueError(
                "response.format json braucht response.geometry "
                "(x_column+y_column oder wkt_column)"
            )
        if self.format == "geojson" and (self.items or self.geometry):
            raise ValueError("response.items/geometry gelten nur für format: json")
        return self


class Paging(BaseModel):
    """Blättern durch große Ergebnismengen (nur GET).
    next_link: OGC-API-Features-Stil, Folgeseite aus links[rel=next].
    offset: limit_param/offset_param mit fester Seitengröße; Ende, sobald
    eine Seite weniger als page_size Objekte liefert."""

    type: Literal["next_link", "offset"]
    page_size: int = Field(1000, gt=0)
    limit_param: str = "limit"
    offset_param: str = "offset"
    max_pages: int = Field(100, gt=0)

    model_config = {"extra": "forbid"}


class EndpointSpec(BaseModel):
    """Gemeinsame Felder aller REST-Deklarationen."""

    model_config = {"extra": "forbid"}

    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: Optional[float] = Field(None, gt=0)

    @field_validator("url")
    @classmethod
    def http_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                f"url muss mit http:// oder https:// beginnen, nicht '{v}'"
            )
        return v


# --- Platzhalter und Umgebungsvariablen ---


def _expand_env(value: str, context: str) -> str:
    def replace(match: re.Match) -> str:
        var = match.group(1)
        if not var.startswith(ENV_PREFIX):
            raise RuntimeError(
                f"{context}: Umgebungsvariable '{var}' darf nicht verwendet werden - "
                f"nur Variablen mit Präfix {ENV_PREFIX} sind erlaubt (Schutz von "
                "Server-Geheimnissen vor fremden Szenario-Dateien)"
            )
        if var not in os.environ:
            raise RuntimeError(
                f"{context}: Umgebungsvariable '{var}' ist nicht gesetzt (verwendet als ${{{var}}})"
            )
        return os.environ[var]

    return _ENV_PATTERN.sub(replace, value)


def expand_env(value: Any, context: str) -> Any:
    """${VAR} rekursiv in Strings, Listen und Dicts ersetzen."""
    if isinstance(value, str):
        return _expand_env(value, context)
    if isinstance(value, list):
        return [expand_env(v, context) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v, context) for k, v in value.items()}
    return value


def env_vars_in(value: Any) -> set[str]:
    """Alle ${VAR}-Namen in einem Wert (für die Prüfung vor dem Lauf)."""
    if isinstance(value, str):
        return set(_ENV_PATTERN.findall(value))
    if isinstance(value, list):
        return set().union(*(env_vars_in(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return (
            set().union(*(env_vars_in(v) for v in value.values())) if value else set()
        )
    return set()


def check_env_prefix(value: Any, where: str) -> None:
    """Vor dem Lauf (FA2): nur GEOFACT_REST_-Variablen sind erlaubt."""
    forbidden = sorted(v for v in env_vars_in(value) if not v.startswith(ENV_PREFIX))
    if forbidden:
        raise ValueError(
            f"{where}: Umgebungsvariablen {forbidden} sind nicht erlaubt - nur "
            f"Variablen mit Präfix {ENV_PREFIX}"
        )


def placeholders_in(value: Any) -> set[str]:
    """Namen aller Platzhalter "@name" (ganzer String-Wert) in einer Vorlage."""
    if isinstance(value, str):
        if value.startswith(PLACEHOLDER_PREFIX) and len(value) > 1:
            return {value[1:]}
        return set()
    if isinstance(value, list):
        return set().union(*(placeholders_in(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return (
            set().union(*(placeholders_in(v) for v in value.values()))
            if value
            else set()
        )
    return set()


def fill_placeholders(template: Any, values: dict[str, Any]) -> Any:
    """Ersetzt jeden String-Wert "@name" durch values[name]."""
    if isinstance(template, str):
        if template.startswith(PLACEHOLDER_PREFIX) and template[1:] in values:
            return values[template[1:]]
        return template
    if isinstance(template, list):
        return [fill_placeholders(v, values) for v in template]
    if isinstance(template, dict):
        return {k: fill_placeholders(v, values) for k, v in template.items()}
    return template


# --- HTTP ---


def warn_if_insecure(url: str, context: str) -> None:
    if urlparse(url).scheme == "http":
        warnings.warn(
            f"{context}: Endpunkt {url} verwendet kein HTTPS",
            InsecureEndpointWarning,
            stacklevel=3,
        )


def request_limited(
    method: str,
    url: str,
    *,
    context: str,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    params: dict[str, Any] | None = None,
    timeout: float | None = None,
) -> bytes:
    """HTTP-Anfrage mit Timeout und hartem Größenlimit
    (GEOFACT_OPENDATA_MAX_MB, FA16) - die Pipeline terminiert immer (NFA2).
    GET wird bei 429/502/503/504 und Verbindungsfehlern bis zu dreimal mit
    wachsender Pause wiederholt, POST nie."""
    limit_bytes = http.max_mb() * 1024 * 1024
    attempts = http.DEFAULT_MAX_RETRIES if method == "GET" else 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with requests.request(
                method,
                url,
                json=json_body,
                params=params,
                headers={"User-Agent": http.USER_AGENT, **(headers or {})},
                timeout=timeout or http.timeout_s(),
                stream=True,
            ) as response:
                if response.status_code in RETRY_STATUS and attempt < attempts - 1:
                    last_error = RuntimeError(f"HTTP {response.status_code}")
                    time.sleep(2**attempt)
                    continue
                if response.status_code >= 400:
                    raise RuntimeError(
                        f"{context} ({url}): HTTP {response.status_code} "
                        f"({response.text[:200]!r})"
                    )
                chunks, total = [], 0
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    total += len(chunk)
                    if total > limit_bytes:
                        raise http.DownloadTooLargeError(
                            f"{context} ({url}): Antwort überschreitet "
                            f"{http.max_mb():.0f} MB (GEOFACT_OPENDATA_MAX_MB)"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except requests.RequestException as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(2**attempt)
    raise RuntimeError(
        f"{context} ({url}): nicht erreichbar: {last_error}"
    ) from last_error


# --- Antworten ---


def to_feature_collection(gdf: GeoDataFrame, context: str) -> dict:
    if gdf.crs is None:
        raise ValueError(
            f"{context}: Layer hat kein CRS - kann nicht als GeoJSON (WGS84) gesendet werden"
        )
    return json.loads(gdf.to_crs(REST_CRS).to_json(drop_id=True))


def parse_json(raw: bytes, context: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        snippet = raw[:200].decode("utf-8", errors="replace")
        raise RuntimeError(f"{context}: Antwort ist kein JSON ({snippet!r})") from exc


def _items_at(body: Any, path: Optional[str], context: str) -> list:
    node = body
    for part in path.split(".") if path else []:
        if not isinstance(node, dict) or part not in node:
            raise RuntimeError(
                f"{context}: Pfad '{path}' nicht in der Antwort gefunden ('{part}')"
            )
        node = node[part]
    if not isinstance(node, list):
        raise RuntimeError(
            f"{context}: unter '{path or '(Wurzel)'}' steht keine Liste, sondern "
            f"{type(node).__name__}"
        )
    return node


def _geometry_from_item(item: dict, mapping: GeometryMapping, context: str):
    try:
        if mapping.wkt_column is not None:
            value = item.get(mapping.wkt_column)
            return shapely_wkt.loads(value) if value else None
        x, y = item.get(mapping.x_column), item.get(mapping.y_column)
        return Point(float(x), float(y)) if x is not None and y is not None else None
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"{context}: Geometrie nicht lesbar in Objekt {item!r}: {exc}"
        ) from exc


def page_items(body: Any, mapping: ResponseMapping, context: str) -> list:
    """Objekte einer Antwortseite (Features bzw. JSON-Objekte)."""
    if mapping.format == "geojson":
        if not isinstance(body, dict) or body.get("type") != "FeatureCollection":
            found = body.get("type") if isinstance(body, dict) else type(body).__name__
            raise RuntimeError(
                f"{context}: Antwort ist keine GeoJSON-FeatureCollection (type={found!r})"
            )
        features = body.get("features")
        if not isinstance(features, list):
            raise RuntimeError(f"{context}: FeatureCollection ohne Liste 'features'")
        return features
    return _items_at(body, mapping.items, context)


def items_to_frame(items: list, mapping: ResponseMapping, context: str) -> GeoDataFrame:
    """Objekte aller Seiten -> GeoDataFrame im CRS der Antwort."""
    if mapping.format == "geojson":
        if not items:
            return gpd.GeoDataFrame(geometry=[], crs=mapping.crs)
        return gpd.GeoDataFrame.from_features(items, crs=mapping.crs)
    geometry_mapping = mapping.geometry
    rows, geometries = [], []
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError(f"{context}: Listeneintrag ist kein Objekt: {item!r}")
        geometries.append(_geometry_from_item(item, geometry_mapping, context))
        # Die WKT-Spalte ist die Geometrie selbst und wird nicht zusätzlich
        # als Attribut geführt; verschachtelte Werte haben keine Spaltenform.
        rows.append(
            {
                k: v
                for k, v in item.items()
                if k != geometry_mapping.wkt_column and not isinstance(v, (dict, list))
            }
        )
    return gpd.GeoDataFrame(rows, geometry=geometries, crs=geometry_mapping.crs)


def next_link(body: Any) -> str | None:
    """Folgeseite nach OGC API Features (links[rel=next])."""
    if not isinstance(body, dict):
        return None
    for link in body.get("links") or []:
        if isinstance(link, dict) and link.get("rel") == "next" and link.get("href"):
            return link["href"]
    return None
