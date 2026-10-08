"""Implements: FA73 (Lizenzkompatibilität über die DALICC-API: Kennungen, Abfrage, Ablage).

Infrastruktur für die Kompatibilitätsprüfung von Lizenzen gegen die
öffentliche DALICC-API (https://api.dalicc.net, ohne Anmeldung). Sie liegt hier,
weil Engine und Web-Backend sie teilen und ``builtin`` nicht importieren dürfen (FA45):

1. **Zuordnung Lizenzname -> DALICC-Kennung** (``DALICC_IDS``), als Tabelle, weil
   DALICC uneinheitlich benennt (``CC-BY-4.0``, aber ``OdcOpenDatabaseLicense``).
   Lizenzen, die DALICC nicht führt (``NOT_IN_DALICC``), sind "nicht prüfbar",
   nie "vereinbar". Eine ausdrückliche ``license.id`` geht der Tabelle vor.
2. **Abfrage** der Lizenzliste (``GET /licenselibrary/list?limit=500``) und der
   Prüfung (``POST /compatibilitycheck/``) über ``support/http.py``.
3. **Ablage** der Antworten unter ``<GEOFACT_SNAPSHOT_DIR>/dalicc/``. Ein Lauf liest
   nur die Ablage (``offline=True``) und fragt nie selbst im Netz.

Falle der API: eine unbekannte oder verkürzte Kennung liefert HTTP 200 ohne
Konflikte. Jede Kennung wird deshalb vor der Prüfung gegen die Lizenzliste
abgeglichen; eine unbekannte ist "nicht prüfbar", sonst sahe eine
Tippfehler-Kennung wie eine vereinbare Lizenz aus.

``GEOFACT_DALICC_URL`` ersetzt die Basisadresse (nicht Teil der Szenario-YAML)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from geofact.support import http
from geofact.support.snapshot import payload_key, snapshot_dir

DEFAULT_API_URL = "https://api.dalicc.net"
LIBRARY_PREFIX = "https://dalicc.net/licenselibrary/"
LIST_LIMIT = 500
MAX_RETRIES = 2


def api_url() -> str:
    return (os.environ.get("GEOFACT_DALICC_URL") or DEFAULT_API_URL).rstrip("/")


def _canonical(name: str) -> str:
    """Vergleichsform eines Lizenznamens: klein, ohne Leerzeichen, ``_`` als
    ``-`` (``CC0_1.0`` == ``cc0-1.0``)."""
    return "".join(
        ch for ch in name.strip().casefold().replace("_", "-") if not ch.isspace()
    )


DALICC_IDS: dict[str, str] = {
    _canonical(name): LIBRARY_PREFIX + dalicc_id
    for name, dalicc_id in (
        ("ODbL-1.0", "OdcOpenDatabaseLicense"),
        ("ODbL", "OdcOpenDatabaseLicense"),
        ("CC0-1.0", "Cc010Universal"),
        ("CC0", "Cc010Universal"),
        ("CC-BY-4.0", "CC-BY-4.0"),
        ("CC-BY-SA-4.0", "CC-BY-SA-4.0"),
        ("PDDL", "OdcPublicDomainDedicationAndLicence"),
        ("PDDL-1.0", "OdcPublicDomainDedicationAndLicence"),
        ("ODC-By", "OpenDataCommonsAttributionLicenseV10"),
        ("ODC-By-1.0", "OpenDataCommonsAttributionLicenseV10"),
        ("CC-BY-3.0-DE", "CreativeCommonsAttribution30Germany"),
    )
}
"""Lizenzname (SPDX-artig, Vergleich ohne Groß-/Kleinschreibung) -> DALICC-URI."""

NOT_IN_DALICC: dict[str, str] = {
    _canonical(name): reason
    for name, reason in (
        (
            "CC-BY-SA-3.0-IGO",
            "CC BY-SA 3.0 IGO (Copernicus) fehlt in der DALICC-Bibliothek",
        ),
        ("dl-de/by-2-0", "Datenlizenz Deutschland fehlt in der DALICC-Bibliothek"),
        ("dl-de-by-2.0", "Datenlizenz Deutschland fehlt in der DALICC-Bibliothek"),
        ("dl-de/zero-2-0", "Datenlizenz Deutschland fehlt in der DALICC-Bibliothek"),
        ("dl-de-zero-2.0", "Datenlizenz Deutschland fehlt in der DALICC-Bibliothek"),
        ("GeoNutzV", "GeoNutzV fehlt in der DALICC-Bibliothek"),
    )
}
"""Bekannte Lizenzen ohne DALICC-Entsprechung: immer "nicht prüfbar"."""


@dataclass(frozen=True)
class Resolution:
    """Ergebnis der Zuordnung eines Lizenznamens: ``uri`` oder ``reason``."""

    uri: Optional[str]
    reason: Optional[str] = None


def resolve(name: str, explicit_id: Optional[str] = None) -> Resolution:
    """DALICC-URI einer Lizenz: die ausdrückliche ``license.id`` (nur mit dem
    Präfix der DALICC-Bibliothek), sonst die Tabelle ``DALICC_IDS``. Ohne
    Treffer ``Resolution(None, Grund)``."""
    if explicit_id is not None:
        if explicit_id.startswith(LIBRARY_PREFIX) and len(explicit_id) > len(
            LIBRARY_PREFIX
        ):
            return Resolution(explicit_id)
        return Resolution(
            None,
            f"license.id '{explicit_id}' ist keine DALICC-Kennung "
            f"(erwartet {LIBRARY_PREFIX}<Name>)",
        )
    key = _canonical(name)
    if key in DALICC_IDS:
        return Resolution(DALICC_IDS[key])
    if key in NOT_IN_DALICC:
        return Resolution(None, NOT_IN_DALICC[key])
    return Resolution(
        None,
        f"Lizenzname '{name}' ist keiner DALICC-Kennung zugeordnet - "
        f"license.id mit einer URI aus {LIBRARY_PREFIX} angeben",
    )


class DaliccUnavailable(RuntimeError):
    """Die DALICC-API ist nicht erreichbar oder antwortet unbrauchbar."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DaliccClient:
    """Abfrage und Ablage der DALICC-API (FA73).

    ``offline=True``: nur die Ablage lesen, nie das Netz (ein Lauf vermerkt so
    "geprüft am ..." ohne Netzzugriff); ein Fehltreffer liefert None.
    ``refresh=True``: Ablage ignorieren und neu fragen. ``get``/``post`` sind
    Nahtstellen für Tests (Standard: ``support.http.get_json``/``post_json``)."""

    def __init__(
        self,
        *,
        cache_dir: Optional[Path] = None,
        refresh: bool = False,
        offline: bool = False,
        get: Optional[Callable[..., Any]] = None,
        post: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.cache_dir = (
            Path(cache_dir) if cache_dir is not None else snapshot_dir() / "dalicc"
        )
        self.refresh = refresh
        self.offline = offline
        self._get = get or http.get_json
        self._post = post or http.post_json
        # Antworten dieses Clients: ``refresh`` fragt je Client einmal neu,
        # nicht bei jeder Ausgabe erneut
        self._memo: dict[str, dict[str, Any]] = {}

    # --- Ablage ---------------------------------------------------------

    def _read(self, name: str) -> Optional[dict[str, Any]]:
        path = self.cache_dir / name
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None  # unlesbare Ablage = Fehltreffer, die Abfrage ersetzt sie
        return data if isinstance(data, dict) else None

    def _write(self, name: str, data: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / name).write_text(
                json.dumps(data, sort_keys=True, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # Ablage ist Komfort: das Ergebnis liegt dem Aufrufer trotzdem vor

    # --- Lizenzliste ------------------------------------------------------

    def library(self) -> Optional[dict[str, str]]:
        """Alle Lizenzen der DALICC-Bibliothek als URI -> Titel; None nur
        offline ohne Ablage. ``DaliccUnavailable`` bei Netz- oder Formatfehler."""
        name = "licenselist.json"
        if name in self._memo:
            return dict(self._memo[name]["licenses"])
        cached = None if self.refresh else self._read(name)
        if cached is not None and isinstance(cached.get("licenses"), dict):
            self._memo[name] = cached
            return dict(cached["licenses"])
        if self.offline:
            return None
        try:
            raw = self._get(
                f"{api_url()}/licenselibrary/list",
                params={"limit": LIST_LIMIT},
                context="DALICC Lizenzliste",
                max_retries=MAX_RETRIES,
            )
        except RuntimeError as exc:
            raise DaliccUnavailable(str(exc)) from exc
        licenses = _parse_library(raw)
        record = {"fetched_at": _now(), "licenses": licenses}
        self._write(name, record)
        self._memo[name] = record
        return dict(licenses)

    # --- Kompatibilitätsprüfung ----------------------------------------

    def check(self, uris: Iterable[str]) -> Optional[dict[str, Any]]:
        """Prüfung einer Kennungsmenge: ``{"checked_at": ..., "response": ...}``
        (Antwort der API unverändert). Die Menge wird sortiert und ohne
        Doppelte gesendet (gleiche Menge = gleicher Ablageschlüssel). None nur
        offline ohne Ablage.

        Eine einzelne Kennung ist mit sich selbst vereinbar: sie wird nicht
        gesendet, die Prüfung aber genauso unter ihrem eigenen Schlüssel
        abgelegt (``"local": true``), damit ein Lauf "geprüft am ..." nur für
        genau diese Menge vermerkt, nie aufgrund einer fremden Prüfung."""
        ordered = sorted(set(uris))
        name = f"check-{payload_key({'licenses': ordered})}.json"
        if name in self._memo:
            return self._memo[name]
        cached = None if self.refresh else self._read(name)
        if cached is not None and isinstance(cached.get("response"), dict):
            self._memo[name] = cached
            return cached
        if self.offline:
            return None
        if len(ordered) < 2:
            record = {
                "checked_at": _now(),
                "licenses": ordered,
                "local": True,
                "response": {"conflicting_statements": {"direct": {}, "derived": {}}},
            }
            self._write(name, record)
            self._memo[name] = record
            return record
        try:
            response = self._post(
                f"{api_url()}/compatibilitycheck/",
                body={"licenses": ordered},
                context="DALICC Kompatibilitätsprüfung",
                max_retries=MAX_RETRIES,
            )
        except RuntimeError as exc:
            raise DaliccUnavailable(str(exc)) from exc
        if not isinstance(response, dict) or not isinstance(
            response.get("conflicting_statements"), dict
        ):
            raise DaliccUnavailable(
                "DALICC Kompatibilitätsprüfung: Antwort ohne 'conflicting_statements' "
                f"({str(response)[:200]})"
            )
        record = {"checked_at": _now(), "licenses": ordered, "response": response}
        self._write(name, record)
        self._memo[name] = record
        return record


def _parse_library(raw: Any) -> dict[str, str]:
    """SPARQL-JSON der Lizenzliste -> URI -> Titel; leer oder fremd ist ein Fehler."""
    try:
        bindings = raw["results"]["bindings"]
        licenses = {
            str(item["id"]["value"]): str(item.get("title", {}).get("value", ""))
            for item in bindings
        }
    except (KeyError, TypeError) as exc:
        raise DaliccUnavailable(
            f"DALICC Lizenzliste: unerwartetes Antwortformat ({type(exc).__name__}: {exc})"
        ) from exc
    if not licenses:
        raise DaliccUnavailable("DALICC Lizenzliste ist leer - Prüfung nicht möglich")
    return licenses
