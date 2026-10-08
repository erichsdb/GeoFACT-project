"""Implements: FA36 (kuratierter API-Katalog).

Lädt den eingecheckten API-Katalog (backend/geofact_web/data/api_catalog.yaml, per
``importlib.resources`` gelesen) und stellt ihn zwei Konsumenten bereit: der
Web-Oberfläche (Schnittstellen, Endpunkte, Spezifikation) und dem LLM-Grounding, damit
generierte Konfigurationen echte Endpunkte und Attributnamen referenzieren.

Der Katalog ist bewusst statisch und nicht per Netzabruf aufgebaut: offline nutzbar,
reproduzierbar und ohne Mock testbar. Die Spezifikationen (spec_url) lädt erst die
Web-Schicht auf Anfrage (FA36, /api/apis/{id}/spec).

Kein eigener Konnektor: die Einträge beschreiben Quellen, die die bestehenden Konnektoren
(wfs, ckan, table; FA14/FA16) bedienen, ``example_layer`` ist ein einbaufertiges Fragment.
"""

from __future__ import annotations

import os
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

# Default ist die Paketdatei; per GEOFACT_API_CATALOG überschreibbar (Laufzeitkonfiguration,
# nicht Teil der Szenario-YAML).
PACKAGED = "<paketdaten>"
_PACKAGE = "geofact_web"
_PACKAGED_PARTS = ("data", "api_catalog.yaml")
_PACKAGED_LABEL = "geofact_web/data/api_catalog.yaml"

# Protokolle, die ein bestehender Konnektor bedient. 'rest' ist als Tabellenquelle (FA14)
# einbindbar, braucht aber manuelles Mapping; das LLM soll keinen 'rest'-Layer erfinden.
SUPPORTED_PROTOCOLS = ("wfs", "ckan", "rest")


class ApiCatalogError(RuntimeError):
    """Katalogdatei fehlt oder ist unlesbar/ungueltig."""


def catalog_path() -> str:
    """Quelle des Katalogs: der Pfad aus GEOFACT_API_CATALOG oder ``PACKAGED``
    (die mitgelieferte Paketdatei)."""
    override = os.environ.get("GEOFACT_API_CATALOG")
    return str(Path(override).expanduser()) if override else PACKAGED


def _read_catalog_text(source: str) -> tuple[str, str]:
    """(Anzeigename, YAML-Text) der Quelle; ApiCatalogError, wenn sie fehlt."""
    if source == PACKAGED:
        resource = resources.files(_PACKAGE).joinpath(*_PACKAGED_PARTS)
        if not resource.is_file():
            raise ApiCatalogError(
                f"API-Katalog '{_PACKAGED_LABEL}' (Paketdaten) nicht gefunden - "
                f"das Paket '{_PACKAGE}' wurde ohne seine Datendatei installiert. "
                f"Alternativ einen Pfad in GEOFACT_API_CATALOG setzen."
            )
        return _PACKAGED_LABEL, resource.read_text(encoding="utf-8")
    path = Path(source)
    if not path.is_file():
        raise ApiCatalogError(
            f"API-Katalog '{path}' nicht gefunden. Erwartet wird die "
            f"mitgelieferte Paketdatei {_PACKAGED_LABEL} oder ein Pfad in "
            f"GEOFACT_API_CATALOG."
        )
    return str(path), path.read_text(encoding="utf-8")


@lru_cache(maxsize=4)
def _load(source: str) -> tuple[dict[str, Any], ...]:
    """Liest und validiert die Katalogdatei; gecacht je Quelle, damit Tests mit eigener Datei funktionieren."""
    path, text = _read_catalog_text(source)
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ApiCatalogError(
            f"API-Katalog '{path}' ist kein gültiges YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict) or "apis" not in raw:
        raise ApiCatalogError(
            f"API-Katalog '{path}' braucht einen Schlüssel 'apis' mit einer Liste von Einträgen"
        )
    entries = raw["apis"]
    if not isinstance(entries, list) or not entries:
        raise ApiCatalogError(
            f"API-Katalog '{path}': 'apis' muss eine nicht-leere Liste sein"
        )

    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ApiCatalogError(
                f"API-Katalog '{path}': Eintrag {index} ist kein Objekt"
            )
        for required in ("id", "label", "protocol", "base_url"):
            if not entry.get(required):
                raise ApiCatalogError(
                    f"API-Katalog '{path}': Eintrag {index} "
                    f"({entry.get('id') or 'ohne id'}) braucht das Feld '{required}'"
                )
        api_id = entry["id"]
        if api_id in seen:
            raise ApiCatalogError(f"API-Katalog '{path}': doppelte id '{api_id}'")
        seen.add(api_id)
        validated.append(entry)
    return tuple(validated)


def api_entries() -> list[dict[str, Any]]:
    """Alle Katalogeinträge, nach Anbieter und Label sortiert (stabil für UI und Prompt)."""
    entries = list(_load(str(catalog_path())))
    return sorted(
        entries, key=lambda e: (e.get("provider") or "", e.get("label") or "")
    )


def find_api(api_id: str) -> dict[str, Any] | None:
    for entry in _load(str(catalog_path())):
        if entry["id"] == api_id:
            return entry
    return None


def endpoints(api_id: str) -> list[dict[str, Any]]:
    """Endpunkte/Feature-Types einer API."""
    entry = find_api(api_id)
    if entry is None:
        raise ApiCatalogError(
            f"Unbekannte API '{api_id}', im Katalog stehen: "
            f"{sorted(e['id'] for e in _load(str(catalog_path())))}"
        )
    found = entry.get("endpoints") or []
    return [e for e in found if isinstance(e, dict)]


def _endpoint_line(endpoint: dict[str, Any]) -> str:
    parts = [f"    - {endpoint.get('name')}"]
    if endpoint.get("title"):
        parts.append(f": {endpoint['title']}")
    details: list[str] = []
    if endpoint.get("geometry"):
        details.append(f"Geometrie {endpoint['geometry']}")
    attributes = endpoint.get("attributes") or []
    if attributes:
        details.append("Attribute " + ", ".join(str(a) for a in attributes))
    if details:
        parts.append(f" [{'; '.join(details)}]")
    return "".join(parts)


def prompt_text(selected_ids: list[str] | None = None) -> str:
    """Kompakte Katalogbeschreibung für das LLM-Grounding (FA36).

    Je API Protokoll, Basis-URL, Zweck und Endpunkte mit realen Attributnamen. Ohne
    selected_ids alle Einträge, sonst nur die gewählten (kurzer Prompt).

    Fehlt der Katalog, kommt ein leerer String: das Grounding blockiert die Generierung
    nicht, die Fehlermeldung erscheint an /api/apis."""
    try:
        entries = api_entries()
    except ApiCatalogError:
        return ""

    if selected_ids:
        wanted = set(selected_ids)
        entries = [e for e in entries if e["id"] in wanted]
    if not entries:
        return ""

    lines = [
        "Verfügbare behoerdliche/offene Schnittstellen (API-Katalog). Nutze "
        "AUSSCHLIESSLICH diese URLs und Endpunktnamen, erfinde keine "
        "weiteren. Layer-Quelltyp je Protokoll: wfs -> source: wfs (url + "
        "typename), ckan -> source: ckan (base_url + dataset/resource_id). "
        "Ein 'rest'-Protokoll ist kein eigener Layer-Quelltyp, aber die "
        "Daten sind trotzdem direkt nutzbar: die Endpunkt-URL kommt als "
        "'path' in einen table-Layer (URLs werden heruntergeladen). "
        "Bringt die Datei eigene Koordinatenspalten mit, ergibt das mit "
        "einem geometry-Block sofort einen Punkt-Layer - dann ist KEIN "
        "attribute_join nötig. Nur Tabellen ohne Koordinaten (reine "
        "Statistik mit Gebietsname/Schluessel) werden per attribute_join "
        "an eine Geometriequelle gehängt.",
    ]
    for entry in entries:
        header = f"- {entry['id']} ({entry['protocol']}): {entry['label']}"
        if entry.get("provider"):
            header += f" - {entry['provider']}"
        lines.append(header)
        lines.append(f"    URL: {entry['base_url']}")
        description = (entry.get("description") or "").strip()
        if description:
            lines.append(f"    Zweck: {' '.join(description.split())}")
        if entry.get("auth"):
            lines.append(
                f"    Zugang: {entry['auth']} - Zugangsdaten gehören in eine "
                f"Umgebungsvariable, NIEMALS in die Szenario-YAML."
            )
        for endpoint in entry.get("endpoints") or []:
            if isinstance(endpoint, dict):
                lines.append(_endpoint_line(endpoint))
        example = entry.get("example_layer")
        if isinstance(example, dict):
            fragment = yaml.safe_dump(
                [example], allow_unicode=True, sort_keys=False, default_flow_style=False
            ).strip()
            indented = "\n".join(f"    {line}" for line in fragment.splitlines())
            lines.append("    Einbaufertiges Layer-Fragment:")
            lines.append(indented)
    return "\n".join(lines)


def layer_templates(selected_ids: list[str]) -> list[dict[str, Any]]:
    """example_layer-Fragmente der gewählten APIs; Einträge ohne example_layer liefern nichts."""
    templates: list[dict[str, Any]] = []
    for api_id in selected_ids:
        entry = find_api(api_id)
        if entry is None:
            continue
        example = entry.get("example_layer")
        if isinstance(example, dict):
            templates.append(dict(example))
    return templates


def reset_cache() -> None:
    """Cache leeren (Tests, die den Katalogpfad umschalten)."""
    _load.cache_clear()
