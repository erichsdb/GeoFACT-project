"""Upload von Datenquellen (Vektor/Raster/Tabelle).

Speichert die Datei im Upload-Verzeichnis und liefert ein einbaufertiges Layer-Template.
Es werden nur Einzeldateien angenommen (kein gezipptes Shapefile).

Implements: FA28 (Eigentümerschaft + Löschung; Upload selbst ist FA12).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile

from .. import catalog_service
from ..auth import AuthenticatedUser, require_user
from ..models import UploadResponse
from ..openapi import errors
from ..settings import get_settings

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

_ALLOWED = {
    ".geojson",
    ".json",
    ".gpkg",
    ".shp",
    ".tif",
    ".tiff",
    ".geotiff",
    ".csv",
    ".sqlite",
    ".db",
    ".sql",
}


def _safe_filename(name: str) -> str:
    base = Path(name).name  # Directory-Traversal verhindern
    return (
        "".join(ch for ch in base if ch.isalnum() or ch in "._- ").strip() or "upload"
    )


@router.post(
    "",
    response_model=UploadResponse,
    summary="Datei hochladen",
    description="Speichert eine Vektor-, Raster- oder Tabellendatei und liefert das "
    "Layer-Fragment für die Konfiguration. Die Datei gehört dem anfragenden Nutzer.",
    responses=errors(
        {
            400: "Dateiendung wird nicht unterstützt.",
            413: "Datei größer als die Obergrenze des Servers.",
        }
    ),
)
async def upload(
    file: UploadFile, user: AuthenticatedUser = Depends(require_user)
) -> UploadResponse:
    settings = get_settings()
    filename = _safe_filename(file.filename or "upload")
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=f"Nicht unterstützte Dateiendung '{suffix}'. Erlaubt: {sorted(_ALLOWED)}",
        )

    # Eigenes Unterverzeichnis je Upload, sonst überschrieben sich gleichnamige Dateien
    # still. Es trägt auch die Eigentümer-Sidecar-Datei (FA28).
    upload_dir = settings.uploads_dir / uuid.uuid4().hex[:12]
    upload_dir.mkdir(parents=True, exist_ok=True)
    catalog_service.write_upload_owner(upload_dir, user.username)
    target = upload_dir / filename
    size = 0
    with target.open("wb") as sink:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                sink.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Datei zu groß.")
            sink.write(chunk)

    source = catalog_service.upload_source(target)
    if source is None:  # pragma: no cover - durch _ALLOWED abgedeckt
        raise HTTPException(
            status_code=400, detail="Datei konnte nicht als Layer interpretiert werden."
        )
    return UploadResponse(source=source)


@router.delete(
    "/{source_id:path}",
    status_code=204,
    summary="Upload löschen",
    description="Löscht einen eigenen Upload endgültig.",
    responses=errors({404: "Upload unbekannt oder gehört einem anderen Nutzer."}),
)
def delete(source_id: str, user: AuthenticatedUser = Depends(require_user)) -> None:
    """FA28: löscht einen Upload vollständig. 404 sowohl wenn die
    source_id nicht existiert als auch wenn sie einem anderen Nutzer
    gehört (analog zu FA24s run_id-Nichtbestätigung) - bestätigt einem
    Angreifer nicht, ob eine erratene/kopierte source_id überhaupt
    existiert."""
    settings = get_settings()
    if not catalog_service.delete_upload(settings, source_id, user.username):
        raise HTTPException(
            status_code=404, detail=f"Upload '{source_id}' nicht gefunden."
        )
