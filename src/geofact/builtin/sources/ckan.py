"""Implements: FA16 (Open-Data-Konnektor, CKAN-Teil), Quellart 'ckan' für FA40.

Löst eine CKAN-Ressource auf (per resource_id, oder per dataset-Name plus
Format-Hint über package_show) und lädt sie als Vektor-Layer (GeoJSON, GeoPackage).
Meldet die CKAN-API 'success: false', wird das als Fehler gemeldet.

Der Snapshot ist region-unabhängig (eine CKAN-Ressource ist eine feste Datei);
auf die Region wird nach jedem Snapshot-Read neu eingeschränkt."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Literal, Optional

import geopandas as gpd
from geopandas import GeoDataFrame
from pydantic import model_validator

from geofact.plugin_api import (
    DataType,
    LayerBase,
    LayerValidation,
    LoadContext,
    register_source,
)
from geofact.support import http
from geofact.builtin._region_filter import within_region
from geofact.support import snapshot as snapshot_module


# --- Layer-Modell (Vertrag der Quellart, FA40) ---


class CkanLayer(LayerBase):
    """NEU (FA16): CKAN-Open-Data-Portal-Quelle. Entweder resource_id
    (direkte Ressourcen-ID) oder dataset (Paketname, Ressource wird über
    einen optionalen format-Hint ausgewählt) - genau eines von beiden,
    damit die Auflösung eindeutig ist (siehe builtin/sources/ckan.py)."""

    id: str
    source: Literal["ckan"] = "ckan"
    base_url: str  # z. B. https://opendata.leipzig.de
    resource_id: Optional[str] = None
    dataset: Optional[str] = None
    format: Optional[str] = None  # Format-Hint bei dataset-Auflösung
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR
    validation: Optional[LayerValidation] = None

    @model_validator(mode="after")
    def resource_or_dataset(self) -> "CkanLayer":
        has_resource = self.resource_id is not None
        has_dataset = self.dataset is not None
        if has_resource == has_dataset:  # beides oder keins
            raise ValueError(
                f"Layer '{self.id}': CKAN-Quelle braucht entweder resource_id ODER dataset"
            )
        return self


_PREFERRED_FORMATS = ("geojson", "json", "gpkg")


def _action_url(base_url: str, action: str) -> str:
    return f"{base_url.rstrip('/')}/api/3/action/{action}"


def _require_success(payload: dict, *, context: str, base_url: str) -> dict:
    if not payload.get("success"):
        error = payload.get("error", {})
        raise RuntimeError(f"{context} meldet Fehler: {error} (Portal: {base_url})")
    return payload["result"]


def resolve_resource(layer: CkanLayer) -> dict:
    """Liefert {'url': ..., 'format': ...} der aufzulösenden Ressource."""
    if layer.resource_id:
        payload = http.get_json(
            _action_url(layer.base_url, "resource_show"),
            params={"id": layer.resource_id},
            context=f"CKAN resource_show (Layer '{layer.id}')",
        )
        result = _require_success(
            payload, context="CKAN resource_show", base_url=layer.base_url
        )
        return {"url": result["url"], "format": (result.get("format") or "").lower()}

    payload = http.get_json(
        _action_url(layer.base_url, "package_show"),
        params={"id": layer.dataset},
        context=f"CKAN package_show (Layer '{layer.id}')",
    )
    result = _require_success(
        payload, context="CKAN package_show", base_url=layer.base_url
    )
    resources = result.get("resources", [])
    if not resources:
        raise RuntimeError(
            f"CKAN-Dataset '{layer.dataset}' hat keine Ressourcen (Portal: {layer.base_url})"
        )

    wanted = (layer.format or "").lower()
    candidates = (
        [r for r in resources if (r.get("format") or "").lower() == wanted]
        if wanted
        else []
    )
    if not candidates:
        # Ohne treffenden Format-Hint: bevorzugte Formate der Reihe nach, sonst die erste.
        by_format = {(r.get("format") or "").lower(): r for r in resources}
        for fmt in _PREFERRED_FORMATS:
            if fmt in by_format:
                candidates = [by_format[fmt]]
                break
    if not candidates:
        candidates = [resources[0]]

    chosen = candidates[0]
    return {"url": chosen["url"], "format": (chosen.get("format") or "").lower()}


def fetch(layer: CkanLayer) -> GeoDataFrame:
    resolved = resolve_resource(layer)
    url, fmt = resolved["url"], resolved["format"]
    raw = http.download_limited(url, context=f"CKAN-Ressource (Layer '{layer.id}')")

    if fmt in ("geojson", "json"):
        payload = json.loads(raw)
        data = gpd.GeoDataFrame.from_features(
            payload.get("features", []), crs="EPSG:4326"
        )
    elif fmt == "gpkg":
        # geopandas/fiona liest kein In-Memory-GPKG; unter Windows muss die Datei
        # vor dem Lesen geschlossen sein, daher delete=False und unlink im finally.
        tmp = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
        try:
            tmp.write(raw)
            tmp.close()
            data = gpd.read_file(tmp.name)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    else:
        raise RuntimeError(
            f"CKAN-Layer '{layer.id}': Ressourcenformat '{fmt}' wird nicht unterstützt "
            f"(unterstützt: geojson, gpkg). Ressource manuell herunterladen und als "
            f"file-Layer einbinden. URL: {url}"
        )

    if data.crs is None:
        raise RuntimeError(
            f"CKAN-Layer '{layer.id}': Ressource ({url}) hat kein erkennbares CRS"
        )
    return data


@register_source(
    "ckan",
    CkanLayer,
    description="CKAN-Open-Data-Portal (Dataset/Resource -> Vektordatei-Download)",
)
def load(layer: CkanLayer, ctx: LoadContext) -> GeoDataFrame:
    """Einziger Einstieg der Quellart (FA45): Region und refresh kommen aus dem
    LoadContext."""
    region_gdf = ctx.region
    refresh = ctx.refresh
    key_payload = {
        "kind": "ckan",
        "base_url": layer.base_url,
        "resource_id": layer.resource_id,
        "dataset": layer.dataset,
        "format": layer.format,
    }
    key = snapshot_module.payload_key(key_payload)
    try:
        data = snapshot_module.fetch_with_snapshot_key(
            key, lambda: fetch(layer), refresh=refresh
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"CKAN-Layer '{layer.id}' konnte nicht geladen werden: {exc} Alternativ: "
            "vorhandenen Snapshot ohne --refresh-snapshots verwenden, oder die "
            "Ressource manuell herunterladen und als file-Layer einbinden."
        ) from exc

    # Die Ressource behält ihr CRS; die Region wird für den Filter in dieses CRS gebracht.
    if (
        data.crs is not None
        and region_gdf.crs is not None
        and data.crs != region_gdf.crs
    ):
        region_gdf = region_gdf.to_crs(data.crs)
    return within_region(data, region_gdf)
