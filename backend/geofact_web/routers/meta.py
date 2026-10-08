"""Meta-Endpunkte: Health, Operationskatalog, Scenario-Schema, Server-Info."""

from __future__ import annotations

from fastapi import APIRouter

from geofact import api

from .. import generation
from .. import options as option_module
from ..models import (
    MAX_LLM_DEADLINE_S,
    MAX_TRIAL_RUN_S,
    HealthResponse,
    MetaResponse,
    OperationContract,
)
from ..settings import Settings, get_settings

router = APIRouter(prefix="/api", tags=["meta"])

# Kurzes Timeout, damit eine tote DB den Health-Endpunkt nicht hängen lässt.
_HEALTH_PG_TIMEOUT_S = 2


def _llm_status(settings: Settings) -> dict[str, object]:
    """Gemeinsame Grundlage für /api/meta und /api/health."""
    return {"configured": settings.llm_configured, "model": settings.llm_model}


def _postgis_status(_settings: Settings) -> dict[str, object]:
    """PostGIS-Erreichbarkeit; nur relevant bei GEOFACT_OSM_BACKEND=postgis. Die Prüfung
    liegt im Kern (``api.osm_backend_status``), Verbindungsfehler werden dort zum Ergebnis."""
    status = api.osm_backend_status(timeout_s=_HEALTH_PG_TIMEOUT_S)
    return {key: status[key] for key in ("enabled", "reachable", "detail")}


def _copernicus_status(settings: Settings) -> dict[str, object]:
    """Ob das Raster-Verzeichnis existiert und wie viele Rasterdateien es enthält (Regel 7)."""
    directory = settings.copernicus_dir
    if not directory.is_dir():
        return {"dir": str(directory), "exists": False, "raster_count": 0}

    raster_exts = {".tif", ".tiff", ".geotiff"}
    count = sum(1 for p in directory.rglob("*") if p.suffix.lower() in raster_exts)
    return {"dir": str(directory), "exists": True, "raster_count": count}


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Betriebszustand",
    description="PostGIS erreichbar, Raster-Verzeichnis vorhanden, LLM konfiguriert. Ohne "
    "Anmeldung erreichbar (Monitoring).",
)
def health() -> dict[str, object]:
    settings = get_settings()
    checks = {
        "postgis": _postgis_status(settings),
        "copernicus": _copernicus_status(settings),
        "llm": _llm_status(settings),
    }
    degraded = checks["postgis"]["reachable"] is False
    return {"status": "degraded" if degraded else "ok", "checks": checks}


@router.get(
    "/meta",
    response_model=MetaResponse,
    summary="Server-Konfiguration",
    description="Gewähltes OSM-Backend, LLM-Modell, Raster-Verzeichnis und nicht geladene Plugins.",
)
def meta() -> dict[str, object]:
    settings = get_settings()
    return {
        "osm_backend": settings.osm_backend,
        "llm_configured": settings.llm_configured,
        "llm_model": settings.llm_model,
        # FA81: Vorbelegung und Obergrenzen des Einstellungsdialogs.
        "generation_defaults": generation.option_defaults(settings),
        "generation_limits": {
            "llm_deadline_s": MAX_LLM_DEADLINE_S,
            "trial_run_s": MAX_TRIAL_RUN_S,
        },
        # FA82: wählbare OSM-Quellen; "default" ist GEOFACT_OSM_BACKEND.
        "osm_backends": {
            "default": settings.osm_backend,
            "available": option_module.osm_backends(settings),
        },
        # FA83: wählbare Sprachmodelle (GEOFACT_LLM_MODEL zuerst, dann GEOFACT_LLM_CHOICES).
        "llm_choices": [
            {"id": c.id, "model": c.model, "providers": list(c.providers)}
            for c in option_module.llm_choices(settings)
        ],
        "copernicus_dir": str(settings.copernicus_dir),
        # Unterscheidet "Verzeichnis fehlt" von "Verzeichnis leer" (Regel 7).
        "copernicus_dir_exists": settings.copernicus_dir.is_dir(),
        # FA40: übersprungene Plugins (Herkunft -> Ursache), im Betrieb sichtbar.
        "failed_plugins": dict(api.registry().failed),
    }


@router.get(
    "/operations",
    response_model=None,
    summary="Operationskatalog",
    description="Alle registrierten Operationen (Kern und Plugins) mit Eingängen, Ausgabetyp "
    "und Parametern.",
    responses={
        200: {
            "model": list[OperationContract],
            "description": "Verträge, nach Name sortiert.",
        }
    },
)
def operations() -> list[dict[str, object]]:
    return api.operation_catalog()


@router.get(
    "/schema",
    summary="Szenario-Schema",
    description="JSON-Schema der Szenario-Konfiguration, abgeleitet aus den registrierten "
    "Bausteinen. Die Antwort ist selbst ein JSON-Schema-Dokument.",
)
def scenario_schema() -> dict[str, object]:
    return api.scenario_json_schema()
