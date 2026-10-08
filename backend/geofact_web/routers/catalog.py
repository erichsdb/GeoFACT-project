"""Datenquellen-Katalog: OSM-Presets, lokale Copernicus-Raster, Uploads, Beispieldaten.

Implements: FA28 (Uploads sind nach Eigentümer gefiltert), FA90 (mitgelieferte
Beispieldaten als Gruppe ``samples``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import catalog_service
from ..auth import AuthenticatedUser, require_user
from ..models import CatalogResponse
from ..settings import get_settings

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get(
    "",
    response_model=CatalogResponse,
    summary="Datenquellen auflisten",
    description="OSM-Presets, lokale Raster, mitgelieferte Beispieldaten und die Uploads des "
    "anfragenden Nutzers, je mit einbaufertigem Layer-Fragment.",
)
def get_catalog(user: AuthenticatedUser = Depends(require_user)) -> CatalogResponse:
    settings = get_settings()
    return CatalogResponse(
        osm_backend=settings.osm_backend,
        osm_presets=catalog_service.osm_presets(),
        copernicus=catalog_service.copernicus_catalog(settings),
        uploads=catalog_service.list_uploads(settings, user.username),
        samples=catalog_service.sample_sources(settings),
    )
