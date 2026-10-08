"""Implements: FA16 (Open-Data-Konnektor, WFS-Teil), Quellart 'wfs' für FA40.

Bezieht Geodaten von einem OGC-WFS-2.0-Endpunkt per GetFeature
(outputFormat=application/json). Die BBox stammt aus der Szenario-Region (Vorfilter
auf dem Server); danach verfeinert builtin/_region_filter.py::within_region auf die
Regionsgeometrie.

WFS 2.0 erwartet die BBox in der per srsName deklarierten Achsenreihenfolge; für
EPSG:4326 ist das lat,lon,lat,lon, nicht die lon,lat-Reihenfolge aus GeoJSON. Ein
falscher Request liefert meist 0 Features statt eines Fehlers, deshalb wird bei
leerem Ergebnis gewarnt.

Paging: Server kappen eine GetFeature-Antwort oft auf ein Default-Limit; eine
unpaginierte Anfrage wäre still unvollständig. fetch() fordert daher Seiten von
PAGE_SIZE an (count/startIndex), bis eine Seite kürzer ist oder numberMatched
erreicht ist.

GML-Fallback: Server ohne JSON-Ausgabe antworten mit einem ows:ExceptionReport.
fetch() wiederholt die Anfrage dann ohne outputFormat (Server-Default GML) und liest
das Ergebnis über GDAL.

Der Snapshot (FA7) speichert das vollständig paginierte Ergebnis; das Paging
beeinflusst den Payload-Schlüssel nicht."""

from __future__ import annotations

import json
import os
import tempfile
import warnings
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal, Optional

import geopandas as gpd
from geopandas import GeoDataFrame
from pyproj import CRS
from pyproj.exceptions import CRSError

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


class WfsLayer(LayerBase):
    """NEU (FA16): OGC-WFS-2.0-Quelle. BBox für GetFeature wird aus der
    Szenario-Region abgeleitet (siehe builtin/sources/wfs.py); typename ist
    Pflicht, weil ein WFS-Endpunkt i. d. R. mehrere Feature-Types
    anbietet und es keinen sinnvollen Default gibt."""

    id: str
    source: Literal["wfs"] = "wfs"
    url: str  # GetFeature-Endpunkt
    typename: str
    srsname: str = "EPSG:4326"
    version: Literal["2.0.0"] = "2.0.0"
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR
    validation: Optional[LayerValidation] = None


DEFAULT_PAGE_SIZE = 10000
MAX_PAGES = 1000


class EmptyWfsResultWarning(UserWarning):
    """Signalisiert, dass eine WFS-GetFeature-Antwort 0 Features enthielt -
    häufigste Ursache: falscher typename oder BBox-Achsenreihenfolge."""


def page_size() -> int:
    """Seitengröße für das Paging, überschreibbar via GEOFACT_WFS_PAGE_SIZE."""
    return int(os.environ.get("GEOFACT_WFS_PAGE_SIZE", DEFAULT_PAGE_SIZE))


def build_getfeature_params(layer: WfsLayer, region: GeoDataFrame) -> dict[str, str]:
    """WFS-2.0-GetFeature-Parameter; die BBox in lat,lon-Reihenfolge mit
    explizitem CRS-Suffix, damit der Server nicht raten muss."""
    min_lon, min_lat, max_lon, max_lat = region.total_bounds
    bbox = f"{min_lat},{min_lon},{max_lat},{max_lon},urn:ogc:def:crs:EPSG::4326"
    return {
        "service": "WFS",
        "version": layer.version,
        "request": "GetFeature",
        "typeNames": layer.typename,
        "outputFormat": "application/json",
        "srsName": layer.srsname,
        "bbox": bbox,
    }


def _exception_report_text(raw: bytes) -> str | None:
    """Exception-Text eines ows:ExceptionReport, sonst None. Vergleicht den lokalen
    Tag-Namen, weil die ows-Versionen unterschiedliche Namespaces nutzen."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    local_tag = root.tag.rsplit("}", 1)[-1]
    if local_tag != "ExceptionReport":
        return None
    texts = [
        (el.text or "").strip()
        for el in root.iter()
        if el.tag.rsplit("}", 1)[-1] == "ExceptionText" and (el.text or "").strip()
    ]
    return " / ".join(texts) if texts else "(kein ExceptionText im ExceptionReport)"


def _looks_like_json(raw: bytes) -> bool:
    stripped = raw.lstrip()
    return bool(stripped) and stripped[0:1] in (b"{", b"[")


def _read_gml_bytes(raw: bytes, layer: WfsLayer) -> GeoDataFrame:
    """Liest GML über eine temporäre Datei (GDAL parst kein In-Memory-GML; unter
    Windows muss sie vor dem Lesen geschlossen sein, daher delete=False)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".gml", delete=False)
    try:
        tmp.write(raw)
        tmp.close()
        data = gpd.read_file(tmp.name)
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    if data.crs is None:
        data = data.set_crs(layer.srsname)
    if data.crs.to_epsg() != 4326:
        data = data.to_crs("EPSG:4326")
    return data


def _json_features_in_wgs84(
    features: list[dict], payload: dict, layer: WfsLayer
) -> list[dict]:
    """GeoJSON-Features einer JSON-Seite in EPSG:4326.

    Viele Server liefern GeoJSON im per ``srsName`` angefragten CRS; ohne Umrechnung
    verglich der Regionsfilter Meter mit Grad und verwarf alles still. Liegen die
    Koordinaten eines projizierten CRS im Grad-Bereich, ist es eine RFC-7946-Antwort
    (immer WGS 84) und bleibt unverändert."""
    if not features:
        return features
    declared = payload.get("crs")
    name = None
    if isinstance(declared, dict):
        name = (declared.get("properties") or {}).get("name")
    try:
        crs = CRS.from_user_input(name or layer.srsname)
    except CRSError:
        return features
    if crs.is_geographic:
        return features
    frame = gpd.GeoDataFrame.from_features(features, crs=crs)
    geometries = frame.geometry[~(frame.geometry.isna() | frame.geometry.is_empty)]
    if geometries.empty:
        return features
    min_x, min_y, max_x, max_y = geometries.total_bounds
    if -180 <= min_x and max_x <= 180 and -90 <= min_y and max_y <= 90:
        return features
    return json.loads(frame.to_crs("EPSG:4326").to_json())["features"]


def _fetch_page_json(
    layer: WfsLayer, params: dict[str, str], *, gml_mode: bool
) -> tuple[list[dict], int | None, bool]:
    """Fordert eine Seite an; gml_mode=True überspringt den JSON-Versuch. Rückgabe:
    (Features als GeoJSON-dicts, numberMatched oder None, ob im GML-Modus geantwortet wurde)."""
    if not gml_mode:
        raw = http.download_limited(
            layer.url, params=params, context=f"WFS-Layer '{layer.id}'"
        )
        if _looks_like_json(raw):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                pass
            else:
                features = _json_features_in_wgs84(
                    payload.get("features", []), payload, layer
                )
                return features, payload.get("numberMatched"), False

        exception_text = _exception_report_text(raw)
        if exception_text is None and not raw.lstrip().startswith(b"<"):
            raise RuntimeError(
                f"WFS-Layer '{layer.id}': Antwort ist weder als JSON noch als "
                f"XML erkennbar (URL: {layer.url}). Erste 200 Zeichen: "
                f"{raw[:200]!r}"
            )
        # JSON nicht unterstützt: ohne outputFormat erneut anfragen (Server-Default GML).
        gml_params = {k: v for k, v in params.items() if k != "outputFormat"}
        gml_raw = http.download_limited(
            layer.url,
            params=gml_params,
            context=f"WFS-Layer '{layer.id}' (GML-Fallback)",
        )
        return _parse_gml_page(gml_raw, layer, exception_text)

    gml_params = {k: v for k, v in params.items() if k != "outputFormat"}
    raw = http.download_limited(
        layer.url, params=gml_params, context=f"WFS-Layer '{layer.id}' (GML)"
    )
    features, number_matched, _ = _parse_gml_page(raw, layer, None)
    return features, number_matched, True


def _parse_gml_page(
    raw: bytes, layer: WfsLayer, json_exception_text: str | None
) -> tuple[list[dict], int | None, bool]:
    if not raw.lstrip().startswith(b"<"):
        raise RuntimeError(
            f"WFS-Layer '{layer.id}': weder JSON noch GML/XML lesbar (URL: "
            f"{layer.url}). Erste 200 Zeichen: {raw[:200]!r}"
        )
    gml_exception_text = _exception_report_text(raw)
    if gml_exception_text is not None:
        reason = (
            json_exception_text
            or "outputFormat=application/json wird vermutlich nicht unterstützt"
        )
        raise RuntimeError(
            f"WFS-Layer '{layer.id}': Server lehnt sowohl JSON- als auch "
            f"GML-GetFeature ab (URL: {layer.url}). JSON-Fehler: {reason}. "
            f"GML-Fehler: {gml_exception_text}"
        )

    number_matched = None
    try:
        root = ET.fromstring(raw)
        matched_attr = root.attrib.get("numberMatched")
        if matched_attr is not None and matched_attr.isdigit():
            number_matched = int(matched_attr)
    except ET.ParseError as exc:
        raise RuntimeError(
            f"WFS-Layer '{layer.id}': GML-Antwort nicht als XML parsebar (URL: "
            f"{layer.url}). Erste 200 Zeichen: {raw[:200]!r}"
        ) from exc

    data = _read_gml_bytes(raw, layer)
    features = json.loads(data.to_json())["features"]
    return features, number_matched, True


def fetch(layer: WfsLayer, region: GeoDataFrame) -> GeoDataFrame:
    """Lädt alle Features des Layers innerhalb der Region (Paging, GML-Fallback)."""
    base_params = build_getfeature_params(layer, region)
    size = page_size()

    all_features: list[dict] = []
    previous_page_ids: frozenset | None = None
    gml_mode = False
    offset = 0
    pages_fetched = 0

    while True:
        if pages_fetched >= MAX_PAGES:
            raise RuntimeError(
                f"WFS-Layer '{layer.id}': Abbruch nach {pages_fetched} Seiten "
                f"(Sicherheitslimit MAX_PAGES={MAX_PAGES}) - Server liefert "
                f"vermutlich mehr Daten als sinnvoll paginierbar, oder das "
                f"Paging terminiert nicht wie erwartet (URL: {layer.url})."
            )

        params = dict(base_params)
        params["count"] = str(size)
        params["startIndex"] = str(offset)

        page_features, number_matched, gml_mode = _fetch_page_json(
            layer, params, gml_mode=gml_mode
        )
        pages_fetched += 1

        page_ids = frozenset(json.dumps(f, sort_keys=True) for f in page_features)
        if (
            page_features
            and previous_page_ids is not None
            and page_ids == previous_page_ids
        ):
            raise RuntimeError(
                f"WFS-Layer '{layer.id}': Seite {pages_fetched} liefert dieselben "
                f"Features wie die vorherige Seite (URL: {layer.url}). Der Server "
                f"ignoriert vermutlich den startIndex-Parameter - Paging kann so "
                f"nicht fortgesetzt werden. Manuellen Download prüfen oder als "
                f"file-Layer einbinden."
            )
        previous_page_ids = page_ids

        all_features.extend(page_features)

        if len(page_features) < size:
            break
        if number_matched is not None and len(all_features) >= number_matched:
            break
        offset += size

    if not all_features:
        warnings.warn(
            f"WFS-Layer '{layer.id}': GetFeature liefert 0 Features - typename "
            f"('{layer.typename}') und BBox-Achsenreihenfolge prüfen "
            f"(URL: {layer.url})",
            EmptyWfsResultWarning,
            stacklevel=2,
        )
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    data = gpd.GeoDataFrame.from_features(all_features, crs="EPSG:4326")
    return within_region(data, region)


@register_source(
    "wfs",
    WfsLayer,
    description="OGC-WFS-2.0-Endpunkt (GetFeature, BBox aus der Region)",
)
def load(layer: WfsLayer, ctx: LoadContext) -> GeoDataFrame:
    """Einziger Einstieg der Quellart (FA45): Region und refresh kommen aus dem
    LoadContext."""
    region_gdf = ctx.region
    refresh = ctx.refresh
    key_payload = {
        "kind": "wfs",
        "url": layer.url,
        "typename": layer.typename,
        "srsname": layer.srsname,
        "region_wkt": region_gdf.union_all().wkt,
    }
    key = snapshot_module.payload_key(key_payload)
    try:
        return snapshot_module.fetch_with_snapshot_key(
            key, lambda: fetch(layer, region_gdf), refresh=refresh
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"WFS-Layer '{layer.id}' konnte nicht geladen werden: {exc} Alternativ: "
            "vorhandenen Snapshot ohne --refresh-snapshots verwenden, oder die "
            "Ressource manuell herunterladen und als file-Layer einbinden."
        ) from exc
