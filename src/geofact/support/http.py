"""Implements: Grundlage für FA16 (Open-Data-Konnektor: HTTP mit
Retry/Backoff, Timeout, Größenlimit).

Gemeinsamer HTTP-Helfer für wfs.py, ckan.py und stac.py (post_json). Das
Retry/Backoff-Muster ist analog zu builtin/sources/osm.py (OverpassBackend.fetch)
und engine/region.py (_default_fetcher); beide nutzen diesen Helfer nicht.

GEOFACT_OPENDATA_TIMEOUT_S (Default 60) und GEOFACT_OPENDATA_MAX_MB
(Default 200) sind Laufzeitkonfiguration (wie GEOFACT_OSM_BACKEND),
nicht Teil der Szenario-YAML."""

from __future__ import annotations

import os
import time

import requests

USER_AGENT = "GeoFACT/0.1 (Kontakt siehe README)"
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MAX_MB = 200.0
DEFAULT_MAX_RETRIES = 3


def timeout_s() -> float:
    return float(os.environ.get("GEOFACT_OPENDATA_TIMEOUT_S", DEFAULT_TIMEOUT_S))


def max_mb() -> float:
    return float(os.environ.get("GEOFACT_OPENDATA_MAX_MB", DEFAULT_MAX_MB))


def _exhausted_retries_message(
    context: str,
    url: str,
    status: int | None,
    error: Exception | None,
    max_retries: int,
) -> str:
    """Handlungsleitende Fehlermeldung nach Ausschöpfen aller Retries
    (Goldene Regel 7: keine stillen/unklaren Fehler) - Muster aus
    osm.py._exhausted_retries_message übernommen."""
    return (
        f"{context} nicht erreichbar nach {max_retries} Versuchen "
        f"(URL: {url}, Status: {status}): {error}. Prüfen, ob der Endpunkt "
        "erreichbar ist, oder --refresh-snapshots weglassen, um einen "
        "vorhandenen lokalen Snapshot zu verwenden."
    )


def get_json(
    url: str,
    *,
    params: dict | None = None,
    context: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """GET-Request mit Retry/Backoff (2**attempt Sekunden), erwartet eine
    JSON-Antwort. context beschreibt die Quelle in Fehlermeldungen
    (z. B. 'CKAN resource_show')."""
    last_error: Exception | None = None
    last_status: int | None = None
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(max_retries):
        try:
            response = requests.get(
                url, params=params, headers=headers, timeout=timeout_s()
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            last_status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
    raise RuntimeError(
        _exhausted_retries_message(context, url, last_status, last_error, max_retries)
    ) from last_error


def post_json(
    url: str,
    *,
    body: dict,
    context: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> dict:
    """POST-Request mit JSON-Körper (z. B. STAC ``/search``, FA46) und
    Retry/Backoff (2**attempt Sekunden); erwartet eine JSON-Antwort. Wie bei
    get_json beschreibt context die Quelle in der Fehlermeldung."""
    last_error: Exception | None = None
    last_status: int | None = None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for attempt in range(max_retries):
        try:
            response = requests.post(
                url, json=body, headers=headers, timeout=timeout_s()
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            last_status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
    raise RuntimeError(
        _exhausted_retries_message(context, url, last_status, last_error, max_retries)
    ) from last_error


class DownloadTooLargeError(RuntimeError):
    """Signalisiert, dass eine Ressource das konfigurierte Größenlimit
    (GEOFACT_OPENDATA_MAX_MB) überschreitet - Abbruch statt eines
    unbegrenzten Downloads (NFA2: Pipeline muss terminieren)."""


def download_limited(
    url: str,
    *,
    params: dict | None = None,
    context: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> bytes:
    """GET-Request mit Retry/Backoff, Streaming-Download mit hartem
    Größenlimit (GEOFACT_OPENDATA_MAX_MB). Ein Content-Length-Header
    wird vorab geprüft (schneller Abbruch ohne jeden Download), zusätzlich
    läuft ein Zähler während des Streamings mit (falls der Header fehlt
    oder lügt)."""
    limit_bytes = max_mb() * 1024 * 1024
    last_error: Exception | None = None
    last_status: int | None = None
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(max_retries):
        try:
            with requests.get(
                url, params=params, headers=headers, timeout=timeout_s(), stream=True
            ) as response:
                response.raise_for_status()
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > limit_bytes:
                    raise DownloadTooLargeError(
                        f"{context}: Ressource ist laut Content-Length "
                        f"{int(content_length) / 1024 / 1024:.1f} MB groß, "
                        f"Limit ist {max_mb():.0f} MB (GEOFACT_OPENDATA_MAX_MB). "
                        f"URL: {url}"
                    )
                chunks = []
                total = 0
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    total += len(chunk)
                    if total > limit_bytes:
                        raise DownloadTooLargeError(
                            f"{context}: Download überschreitet das Limit von "
                            f"{max_mb():.0f} MB (GEOFACT_OPENDATA_MAX_MB) während "
                            f"des Streamings. URL: {url}"
                        )
                    chunks.append(chunk)
                return b"".join(chunks)
        except DownloadTooLargeError:
            raise
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            last_status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
    raise RuntimeError(
        _exhausted_retries_message(context, url, last_status, last_error, max_retries)
    ) from last_error
