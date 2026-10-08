"""Speichern/Laden benannter Szenario-Konfigurationen (Dateisystem).

Ablage unter <data_dir>/scenarios/<name>.yaml, genug für die lokale Demo. Dazu die
eingecheckten Referenzszenarien aus examples/ als schreibgeschützte zweite Kategorie;
Name und description stammen direkt aus dem YAML.

Pfadregel (FA4/FA44): relative Pfade in einem Beispiel gelten gegen das Verzeichnis seiner
YAML-Datei. Weil der Editor nur Text schickt, setzt der Beispiel-Lader die Kopfzeile
``# geofact-origin: <Beispiel-id>`` voran (siehe scenario_service.py)."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException

from .. import scenario_service
from ..models import (
    SavedScenario,
    SaveScenarioRequest,
    ScenarioContent,
    ScenarioDeleted,
)
from ..openapi import errors
from ..settings import EXAMPLES_DIR as _EXAMPLES_DIR
from ..settings import get_settings

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])

# Beispiele, die Tests und Skripte noch brauchen, in der Web-Demo-Auswahl aber nicht erscheinen sollen.
# szenario_plugins braucht GEOFACT_PLUGIN_PATH und den lokalen Demo-Dienst (examples/plugin_demo) und
# wäre aus der Oberfläche heraus nicht ausführbar.
_HIDDEN_EXAMPLES = {"szenario2_gruenflaechen", "szenario_plugins"}


def _scenarios_dir() -> Path:
    directory = get_settings().data_dir / "scenarios"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _safe_name(name: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in "._- " else "_" for ch in name
    ).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Ungültiger Szenario-Name.")
    return cleaned


def _example_id(path: Path) -> str:
    """Pfad relativ zu examples/ ohne Endung, z. B. 'leipzig/leipzig10_erreichbarkeit';
    kollidiert so nicht mit eigenen Szenario-Namen."""
    return path.relative_to(_EXAMPLES_DIR).with_suffix("").as_posix()


def _by_name(scenario: SavedScenario) -> tuple[str, str]:
    """Sortierschlüssel der Auswahl (FA92): Name ohne Groß-/Kleinschreibung, dann id."""
    return scenario.name.casefold(), scenario.id


def _list_examples() -> list[SavedScenario]:
    """Liest name/description aus jedem YAML ohne volle Validierung. Ein kaputtes Beispiel
    legt die Liste nicht lahm, sondern erscheint mit einer Ersatz-Beschreibung (Regel 7).
    Die Liste ist nach dem angezeigten Namen sortiert (FA92), nicht nach dem Dateipfad."""
    result: list[SavedScenario] = []
    if not _EXAMPLES_DIR.is_dir():
        return result
    for path in sorted(_EXAMPLES_DIR.rglob("*.yaml")):
        if path.stem in _HIDDEN_EXAMPLES:
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            scenario_block = raw.get("scenario", {})
            name = scenario_block.get("name") or path.stem
            description = scenario_block.get("description")
        except Exception as exc:  # noqa: BLE001 - siehe Docstring, bewusst weit
            name = path.stem
            description = f"Konnte nicht gelesen werden: {exc}"
        result.append(
            SavedScenario(
                id=_example_id(path),
                name=name,
                source="example",
                description=description,
            )
        )
    return sorted(result, key=_by_name)


_BAD_NAME = "Der Name enthält kein verwertbares Zeichen."


@router.get(
    "",
    response_model=list[SavedScenario],
    summary="Szenarien auflisten",
    description="Gespeicherte Szenarien (source=user, gemeinsamer Pool aller Nutzer) und die "
    "mitgelieferten Beispiele (source=example), je Gruppe nach Namen sortiert.",
)
def list_scenarios() -> list[SavedScenario]:
    user_scenarios = [
        SavedScenario(id=path.stem, name=path.stem, source="user")
        for path in _scenarios_dir().glob("*.yaml")
    ]
    return sorted(user_scenarios, key=_by_name) + _list_examples()


@router.post(
    "",
    response_model=SavedScenario,
    summary="Szenario speichern",
    description="Speichert den YAML-Text unter dem Namen; ein vorhandenes Szenario gleichen "
    "Namens wird überschrieben. Der Text wird nicht geprüft.",
    responses=errors({400: _BAD_NAME}),
)
def save_scenario(request: SaveScenarioRequest) -> SavedScenario:
    name = _safe_name(request.name)
    (_scenarios_dir() / f"{name}.yaml").write_text(
        request.config_yaml, encoding="utf-8"
    )
    return SavedScenario(id=name, name=name, source="user")


@router.get(
    "/{name}",
    response_model=ScenarioContent,
    summary="Gespeichertes Szenario laden",
    description="Liefert den gespeicherten YAML-Text.",
    responses=errors({400: _BAD_NAME, 404: "Kein Szenario dieses Namens."}),
)
def load_scenario(name: str) -> ScenarioContent:
    path = _scenarios_dir() / f"{_safe_name(name)}.yaml"
    if path.is_file():
        return ScenarioContent(
            name=path.stem, config_yaml=path.read_text(encoding="utf-8")
        )
    raise HTTPException(status_code=404, detail=f"Szenario '{name}' nicht gefunden.")


@router.get(
    "/examples/{example_id:path}",
    response_model=ScenarioContent,
    summary="Mitgeliefertes Beispiel laden",
    description="example_id ist der Pfad unter examples/ ohne Endung. Dem Text ist die Kopfzeile "
    "'# geofact-origin: <id>' vorangestellt, gegen die relative Pfade aufgelöst werden.",
    responses=errors({404: "Kein Beispiel mit dieser id."}),
)
def load_example_scenario(example_id: str) -> ScenarioContent:
    path = (_EXAMPLES_DIR / example_id).with_suffix(".yaml")
    resolved = path.resolve()
    if _EXAMPLES_DIR not in resolved.parents or not resolved.is_file():
        raise HTTPException(
            status_code=404, detail=f"Beispiel '{example_id}' nicht gefunden."
        )
    text = resolved.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    name = raw.get("scenario", {}).get("name") or resolved.stem
    # Ursprung als Kopfzeile (FA4/FA44): die Beispiel-id, wie /api/scenarios sie nennt.
    header = scenario_service.origin_header(_example_id(resolved))
    return ScenarioContent(name=name, config_yaml=header + text)


@router.delete(
    "/{name}",
    response_model=ScenarioDeleted,
    summary="Gespeichertes Szenario löschen",
    description="Ein unbekannter Name ist kein Fehler: deleted ist dann false.",
    responses=errors({400: _BAD_NAME}),
)
def delete_scenario(name: str) -> dict:
    path = _scenarios_dir() / f"{_safe_name(name)}.yaml"
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"name": name, "deleted": existed}
