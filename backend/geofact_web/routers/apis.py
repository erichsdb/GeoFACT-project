"""API-Katalog: behoerdliche/offene Schnittstellen als Layer-Quelle.

Implements: FA36 (kuratierter API-Katalog mit sichtbaren Endpunkten und
Spezifikationen).

Zwei Endpunkte: GET /api/apis (der Katalog) und GET /api/apis/{id}/spec (die
Spezifikation der Schnittstelle, auf Anfrage geladen). Der Spec-Abruf ist ein eigener
Schritt, damit der Katalog offline nutzbar bleibt und ein langsamer Fremd-Endpunkt die
Oberfläche nicht blockiert (FA36).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import api_catalog
from ..auth import AuthenticatedUser, require_user
from ..openapi import errors
from ..models import ApiCatalogResponse, ApiSpecResponse, CatalogApi, CatalogApiEndpoint

router = APIRouter(prefix="/api/apis", tags=["apis"])

# Harte Obergrenze, da GetCapabilities einige MB groß sein kann (NFA2); der Rest wird
# abgeschnitten und im Payload gemeldet (Regel 7).
_MAX_SPEC_CHARS = 400_000


def _to_model(entry: dict) -> CatalogApi:
    endpoints = [
        CatalogApiEndpoint(
            name=str(endpoint.get("name")),
            title=endpoint.get("title"),
            method=endpoint.get("method"),
            geometry=endpoint.get("geometry"),
            attributes=[str(a) for a in (endpoint.get("attributes") or [])],
            attribute_notes={
                str(k): str(v)
                for k, v in (endpoint.get("attribute_notes") or {}).items()
            },
            notes=endpoint.get("notes"),
        )
        for endpoint in (entry.get("endpoints") or [])
        if isinstance(endpoint, dict)
    ]
    return CatalogApi(
        id=entry["id"],
        label=entry["label"],
        provider=entry.get("provider"),
        protocol=str(entry["protocol"]),
        base_url=entry["base_url"],
        description=(entry.get("description") or "").strip() or None,
        license=entry.get("license"),
        documentation_url=entry.get("documentation_url"),
        spec_url=entry.get("spec_url"),
        spec_type=entry.get("spec_type"),
        auth=entry.get("auth"),
        data_type=entry.get("data_type"),
        verified=str(entry["verified"]) if entry.get("verified") else None,
        notes=(entry.get("notes") or "").strip() or None,
        endpoints=endpoints,
        layer_template=entry.get("example_layer"),
    )


@router.get(
    "",
    response_model=ApiCatalogResponse,
    summary="API-Katalog",
    description="Kuratierte offene Schnittstellen mit Endpunkten, Attributen, Lizenz und "
    "Layer-Fragment. Braucht kein Netz.",
    responses=errors({500: "Der Katalog des Servers fehlt oder ist nicht lesbar."}),
)
def list_apis(user: AuthenticatedUser = Depends(require_user)) -> ApiCatalogResponse:
    try:
        entries = api_catalog.api_entries()
    except api_catalog.ApiCatalogError as exc:
        # Ein fehlender Katalog ist ein Konfigurationsfehler, kein leeres Ergebnis (Regel 7).
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ApiCatalogResponse(apis=[_to_model(entry) for entry in entries])


@router.get(
    "/{api_id}/spec",
    response_model=ApiSpecResponse,
    summary="Spezifikation einer Schnittstelle laden",
    description="Lädt GetCapabilities bzw. OpenAPI der Schnittstelle vom Anbieter. Der Text "
    "wird bei 400.000 Zeichen abgeschnitten (truncated).",
    responses=errors(
        {
            404: "Unbekannte Schnittstelle, oder für sie ist keine Spezifikation hinterlegt.",
            502: "Der Anbieter hat die Spezifikation nicht geliefert.",
        }
    ),
)
def get_api_spec(
    api_id: str, user: AuthenticatedUser = Depends(require_user)
) -> ApiSpecResponse:
    """Lädt die Spezifikation der Schnittstelle (GetCapabilities/OpenAPI).

    Nutzt die FA16-HTTP-Infrastruktur (Timeout, Retries, Größenlimit)
    statt eines eigenen Abrufs - ein Spec-Endpunkt ist für die Pipeline
    nicht vertrauenswürdiger als ein Open-Data-Download."""
    entry = api_catalog.find_api(api_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unbekannte API '{api_id}'.")
    spec_url = entry.get("spec_url")
    if not spec_url:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Für '{api_id}' ist keine maschinenlesbare Spezifikation "
                f"hinterlegt. Die Dokumentation steht unter "
                f"{entry.get('documentation_url') or 'keiner hinterlegten URL'}."
            ),
        )

    # Import hier, damit der Router auch ohne requests importierbar bleibt.
    from geofact.support import http as http_module

    try:
        raw = http_module.download_limited(
            spec_url, context=f"API-Spezifikation '{api_id}'"
        )
    except Exception as exc:  # noqa: BLE001 - Fremd-Endpunkt, Ursache weiterreichen
        raise HTTPException(
            status_code=502,
            detail=(
                f"Spezifikation von '{api_id}' konnte nicht geladen werden: {exc} "
                f"Die Dokumentation steht unter "
                f"{entry.get('documentation_url') or spec_url}."
            ),
        ) from exc

    # Tolerant dekodieren: GetCapabilities halten ihre deklarierte Kodierung nicht immer ein.
    text = raw.decode("utf-8", errors="replace")
    truncated = len(text) > _MAX_SPEC_CHARS
    if truncated:
        text = text[:_MAX_SPEC_CHARS]
    return ApiSpecResponse(
        api_id=api_id,
        spec_url=spec_url,
        spec_type=entry.get("spec_type"),
        content=text,
        truncated=truncated,
    )
