"""Implements: FA46 (Satellitenbilder aus einem STAC-Katalog), FA4 (margin_km), FA5 (resampling), FA65 (Default-Lizenz), Quellart 'stac' für FA40.

Bezieht ein mehrbandiges Satellitenbild für die Szenario-Region aus einem
STAC-Katalog (Standard: Earth Search von Element 84, Sentinel-2 L2A als Cloud
Optimized GeoTIFF auf AWS S3, ohne Anmeldung):

    - id: sentinel2
      source: stac
      assets: { red: red, nir: nir, scl: scl }     # Bandname: Asset-Schlüssel
      datetime: "2024-08-12/2024-08-14"
      grid_code: MGRS-33UUS
      max_cloud_cover: 20
      mask_clouds: true
      resolution_m: 20                              # optional

Ablauf (FA46): Suche über STAC ``/search`` (BBox der Region, ``datetime``, optional
``max_cloud_cover``, alle Ergebnisseiten); ``item_id`` legt eine Szene fest,
``grid_code`` wählt die Kachel. Je Aufnahmedatum zählt die Szene, die die Region
abdeckt, sonst das Mosaik der Szenen des Datums; das wolkenärmste Datum gewinnt
(``scene_order: latest|earliest`` ändert das und ist nötig, wenn Szenen kein
``eo:cloud_cover`` tragen). Gelesen wird nur das Fenster der Region (+ ``margin_km``).
Die Zellgröße ist die des feinsten Bandes oder ``resolution_m`` (ein ganzzahliges
Vielfaches davon); Reflektanzbänder werden dabei gemittelt, Klassenbänder (``scl``)
abgetastet, nie gemittelt. Ergebnis ist ein 3D-Raster, Bandnamen = Schlüssel von ``assets``.

Wertebereich: rohe Digitalwerte (Nodata 0); ``apply_scale: true`` rechnet auf
Reflektanz um (float32, Nodata NaN). ``mask_clouds: true`` setzt SCL-Klassen
3, 8, 9, 10 (oder ``mask_classes``) in allen Bändern auf Nodata und braucht das
Band ``scl``; es muss eine Sentinel-2-Szenenklassifikation sein.

Zugang: ``s3://``-Hrefs werden zu ``/vsis3/`` (Zugangsdaten liest GDAL aus der
Umgebung), ``asset_signing: planetary_computer`` signiert http(s)-Hrefs, ``gdal_options``
ergänzt GDAL-Einstellungen. Geheimnisse lehnt die Validierung ab, sie gehören in die
Umgebung. Lizenz (FA65): Collections mit ``sentinel`` im Namen tragen die
Copernicus-Lizenz als Default.

Snapshot (FA7): das Raster liegt als GeoTIFF samt JSON-Begleitdatei (Provenienz)
unter GEOFACT_SNAPSHOT_DIR; ``--refresh-snapshots`` bezieht neu. Die gewählten
Szenen stehen als ``StacSceneNotice`` in den Warnungen jedes Laufs. Das Resampling
beim Wechsel ins Arbeits-CRS steuert ``resampling`` (FA5)."""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
import urllib.parse
import warnings
from contextlib import ExitStack
from typing import Any, Literal, NamedTuple, Optional

import numpy as np
import rasterio
from pydantic import Field, field_validator, model_validator
from pyproj import CRS
from rasterio.enums import Resampling
from rasterio.errors import RasterioError
from rasterio.io import MemoryFile
from rasterio.merge import merge
from rasterio.transform import Affine, array_bounds
from rasterio.warp import transform_bounds
from rasterio.windows import Window
from shapely.geometry import box, shape
from shapely.ops import unary_union

from geofact.builtin._raster_io import (
    GDAL_OPTIONS,
    MAX_CELLS,
    read_geotiff,
    region_window_bounds,
    write_geotiff,
)
from geofact.plugin_api import (
    DataType,
    LayerBase,
    License,
    LoadContext,
    RasterLayer,
    http,
    register_source,
    snapshot,
)

DEFAULT_STAC_URL = "https://earth-search.aws.element84.com/v1"
DEFAULT_COLLECTION = "sentinel-2-l2a"
CLOUD_CLASSES = (3, 8, 9, 10)
"""SCL-Klassen, die ``mask_clouds`` zu Nodata macht: Wolkenschatten, mittlere und
hohe Wolkenwahrscheinlichkeit, Zirrus."""
SCL_BAND = "scl"
SEARCH_PAGE_SIZE = 100
MAX_PAGES = 20
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GDAL_OPTION_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SECRET_OPTION = re.compile(
    r"SECRET|PASSWORD|PASSWD|TOKEN|ACCESS_KEY|KEY_ID|USERPWD|BEARER|HEADERS|CREDENTIAL|"
    r"CONNECTION_STRING|COOKIE|PRIVATE|SAS"
)
"""Namensteile von GDAL-Optionen, die Zugangsgeheimnisse tragen (``AWS_SECRET_ACCESS_KEY``,
``AWS_SESSION_TOKEN``, ``GDAL_HTTP_USERPWD``, ``GDAL_HTTP_HEADERS`` mit ``Authorization``,
``AZURE_STORAGE_SAS_TOKEN`` ...): sie gehören in die Umgebung, nicht in die YAML."""
DEFAULT_SCENE_ORDER = "cloud_cover"

COPERNICUS_LICENSE = License(
    name="CC-BY-SA-3.0-IGO",
    attribution="Contains modified Copernicus Sentinel data",
    url="https://creativecommons.org/licenses/by-sa/3.0/igo/",
)
"""Default-Lizenz der Sentinel-Collections (FA65): Copernicus-Sentinel-Daten, CC BY-SA 3.0 IGO."""


class StacSceneNotice(UserWarning):
    """Hinweis auf die gewählte(n) Szene(n) eines STAC-Layers (Provenienz)."""


# --- Layer-Modell (Vertrag der Quellart, FA40) ---


class StacLayer(LayerBase):
    """Mehrbandiges Raster aus einem STAC-Katalog (siehe Modul-Docstring)."""

    id: str
    source: Literal["stac"] = "stac"
    data_type: Literal[DataType.RASTER] = DataType.RASTER
    url: str = Field(DEFAULT_STAC_URL, description="Basis-URL des STAC-API-Katalogs.")
    collection: str = Field(DEFAULT_COLLECTION, description="STAC-Collection.")
    assets: dict[str, str] = Field(
        ...,
        min_length=1,
        description="Bandname -> Asset-Schlüssel, z. B. {red: red, nir: nir, scl: scl}.",
    )
    datetime: Optional[str] = Field(
        None,
        description="Zeitraum 'YYYY-MM-DD/YYYY-MM-DD' (oder ein Tag); Pflicht ohne item_id.",
    )
    max_cloud_cover: Optional[float] = Field(
        None, ge=0, le=100, description="Höchste Wolkenbedeckung der Szene in Prozent."
    )
    mask_clouds: bool = Field(
        False,
        description="Wolken, Schatten und Zirrus (SCL 3, 8, 9, 10) zu Nodata machen; "
        "braucht das Band 'scl' (eine Sentinel-2-Szenenklassifikation oder "
        "mask_classes).",
    )
    mask_classes: Optional[list[int]] = Field(
        None,
        min_length=1,
        description="Werte des Bandes 'scl', die mask_clouds zu Nodata macht (genaue Werte, keine "
        "Bitmasken); ersetzt die SCL-Klassen 3, 8, 9, 10 - für Klassenbänder, die "
        "keine Sentinel-2-Szenenklassifikation sind.",
    )
    scene_order: Literal["cloud_cover", "latest", "earliest"] = Field(
        DEFAULT_SCENE_ORDER,
        description="Welches abdeckende Datum gewinnt: das wolkenärmste (Standard; jede Szene "
        "braucht eo:cloud_cover), das jüngste oder das älteste.",
    )
    asset_signing: Literal["none", "planetary_computer"] = Field(
        "none",
        description="Hrefs signieren: planetary_computer liest über GDALs "
        "/vsicurl?pc_url_signing=yes (Microsoft Planetary Computer).",
    )
    gdal_options: dict[str, str | int | float | bool] = Field(
        default_factory=dict,
        description="Zusätzliche GDAL-Konfigurationsoptionen beim Lesen, z. B. "
        "{AWS_NO_SIGN_REQUEST: YES}; keine Geheimnisse (die gehören in die Umgebung).",
    )
    item_id: Optional[str] = Field(
        None, description="Szene direkt festlegen (reproduzierbar); ersetzt die Suche."
    )
    grid_code: Optional[str] = Field(
        None, description="Nur Szenen dieser Kachel, z. B. 'MGRS-33UUS'."
    )
    apply_scale: bool = Field(
        False,
        description="Auf Reflektanz umrechnen (float32, Nodata NaN); sonst rohe Digitalwerte.",
    )
    resampling: Literal["nearest", "bilinear", "average", "sum"] = Field(
        "nearest", description="Resampling beim Wechsel ins Arbeits-CRS (FA5)."
    )
    margin_km: float = Field(
        0, ge=0, description="Rand in Kilometern um die Region für das Lesefenster."
    )
    resolution_m: Optional[float] = Field(
        None,
        gt=0,
        allow_inf_nan=False,
        description="Zellgröße des Ergebnisses in Metern, ein ganzzahliges Vielfaches der "
        "Zellgröße des feinsten Bandes (Sentinel-2: 20 statt 10); Reflektanzbänder "
        "werden gemittelt, Klassenbänder (scl) abgetastet. Ohne Angabe: die Zellgröße "
        "des feinsten Bandes.",
    )

    @field_validator("url")
    @classmethod
    def url_is_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(
                f"url muss mit http:// oder https:// beginnen, nicht '{value}'"
            )
        return value.rstrip("/")

    @field_validator("gdal_options")
    @classmethod
    def gdal_options_without_secrets(cls, value: dict[str, Any]) -> dict[str, str]:
        options: dict[str, str] = {}
        for key, raw in value.items():
            if not _GDAL_OPTION_NAME.match(key):
                raise ValueError(
                    f"gdal_options: '{key}' ist kein Name einer GDAL-Konfigurationsoption "
                    "(Großbuchstaben, Ziffern, Unterstrich, z. B. AWS_NO_SIGN_REQUEST)"
                )
            if _SECRET_OPTION.search(key):
                raise ValueError(
                    f"gdal_options: '{key}' trägt ein Zugangsgeheimnis und gehört nicht in die "
                    f"Szenario-YAML - in der Umgebung setzen (GDAL liest {key} von dort; "
                    "Earthdata-Login z. B. über GDAL_HTTP_NETRC)"
                )
            options[key] = (
                ("YES" if raw else "NO") if isinstance(raw, bool) else str(raw)
            )
        return options

    @field_validator("assets")
    @classmethod
    def assets_are_named_bands(cls, value: dict[str, str]) -> dict[str, str]:
        for band, key in value.items():
            if not band.isidentifier():
                raise ValueError(
                    f"Bandname '{band}' muss ein Name aus Buchstaben, Ziffern und Unterstrich "
                    "sein (er wird in raster_calc als Bandname verwendet)"
                )
            if not isinstance(key, str) or not key:
                raise ValueError(
                    f"Band '{band}': der Asset-Schlüssel darf nicht leer sein"
                )
        return value

    @model_validator(mode="after")
    def check_selection(self) -> "StacLayer":
        if self.item_id is not None:
            clashing = [
                name
                for name, value in (
                    ("datetime", self.datetime),
                    ("max_cloud_cover", self.max_cloud_cover),
                    ("grid_code", self.grid_code),
                    (
                        "scene_order",
                        None
                        if self.scene_order == DEFAULT_SCENE_ORDER
                        else self.scene_order,
                    ),
                )
                if value is not None
            ]
            if clashing:
                raise ValueError(
                    f"Layer '{self.id}': item_id legt die Szene fest - {clashing} gelten dann "
                    "nicht und müssen entfallen"
                )
        elif self.datetime is None:
            raise ValueError(
                f"Layer '{self.id}': datetime ('YYYY-MM-DD/YYYY-MM-DD') oder item_id ist Pflicht"
            )
        else:
            _parse_range(self.datetime, self.id)
        if self.mask_clouds and SCL_BAND not in self.assets:
            raise ValueError(
                f"Layer '{self.id}': mask_clouds braucht das Szenenklassifikations-Band "
                f"'{SCL_BAND}' in assets (z. B. {SCL_BAND}: scl), vorhanden: {sorted(self.assets)}"
            )
        if self.mask_classes is not None and not self.mask_clouds:
            raise ValueError(
                f"Layer '{self.id}': mask_classes wirkt nur mit mask_clouds: true"
            )
        return self

    def default_license(self) -> Optional[License]:
        """FA65: Sentinel-Collections tragen die Copernicus-Lizenz, andere keine."""
        return COPERNICUS_LICENSE if "sentinel" in self.collection.lower() else None


def _parse_range(value: str, layer_id: str) -> tuple[_dt.date, _dt.date]:
    parts = value.split("/")
    if len(parts) > 2 or not all(_DATE.match(part) for part in parts):
        raise ValueError(
            f"Layer '{layer_id}': datetime '{value}' muss 'YYYY-MM-DD/YYYY-MM-DD' "
            "(oder ein einzelner Tag 'YYYY-MM-DD') sein"
        )
    try:
        start = _dt.date.fromisoformat(parts[0])
        end = _dt.date.fromisoformat(parts[-1])
    except ValueError as exc:
        raise ValueError(f"Layer '{layer_id}': datetime '{value}': {exc}") from None
    if end < start:
        raise ValueError(
            f"Layer '{layer_id}': datetime '{value}': das Ende liegt vor dem Anfang"
        )
    return start, end


# --- 1. Suche ---


def _cloud_cover(item: dict) -> float | None:
    """``eo:cloud_cover`` der Szene oder None. Nie still 0 %: sonst wählte ein
    Datumsbereich bei Klassenrastern ohne Wolkenangabe still das älteste Jahr."""
    value = item["properties"].get("eo:cloud_cover")
    return float(value) if value is not None else None


def _epsg_from(meta: dict) -> int | None:
    if meta.get("proj:epsg") is not None:
        return int(meta["proj:epsg"])
    for key in ("proj:code", "proj:wkt2", "proj:projjson"):
        value = meta.get(key)
        if value:
            try:
                return CRS.from_user_input(value).to_epsg()
            except Exception:  # noqa: BLE001 - unlesbare Angabe wie fehlend: das Raster selbst trägt sein CRS
                return None
    return None


def _epsg_of(item: dict) -> int | None:
    """EPSG der Szene aus der Projection-Extension (``proj:epsg``, v2: ``proj:code``,
    ``proj:wkt2``, ``proj:projjson``), am Item, sonst am ersten Asset mit Angabe."""
    epsg = _epsg_from(item.get("properties") or {})
    if epsg is not None:
        return epsg
    for asset in (item.get("assets") or {}).values():
        if isinstance(asset, dict):
            epsg = _epsg_from(asset)
            if epsg is not None:
                return epsg
    return None


_SORTBY = {
    "cloud_cover": {"field": "properties.eo:cloud_cover", "direction": "asc"},
    "latest": {"field": "properties.datetime", "direction": "desc"},
    "earliest": {"field": "properties.datetime", "direction": "asc"},
}


def _search_body(layer: StacLayer, bbox: list[float]) -> dict:
    start, end = _parse_range(layer.datetime, layer.id)
    body: dict[str, Any] = {
        "collections": [layer.collection],
        "bbox": bbox,
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "limit": SEARCH_PAGE_SIZE,
        "sortby": [_SORTBY[layer.scene_order]],
    }
    if layer.max_cloud_cover is not None:
        # ``query`` statt CQL2-``filter``: Earth Search ignoriert einen filter still
        body["query"] = {"eo:cloud_cover": {"lte": layer.max_cloud_cover}}
    return body


def _search_items(layer: StacLayer, bbox: list[float]) -> list[dict]:
    """Alle Treffer der Suche (alle Seiten) oder die eine feste Szene."""
    if layer.item_id is not None:
        url = f"{layer.url}/collections/{layer.collection}/items/{layer.item_id}"
        return [http.get_json(url, context=f"STAC-Szene {layer.item_id}")]

    search_url = f"{layer.url}/search"
    body = _search_body(layer, bbox)
    page = http.post_json(search_url, body=body, context="STAC-Suche")
    items = list(page.get("features", []))
    pages = 1
    while True:
        link = next(
            (item for item in page.get("links", []) if item.get("rel") == "next"), None
        )
        if link is None:
            break
        if pages >= MAX_PAGES:
            raise ValueError(
                f"Layer '{layer.id}': die STAC-Suche liefert mehr als {MAX_PAGES} Seiten "
                f"({len(items)} Szenen) - datetime, max_cloud_cover oder grid_code einschränken"
            )
        if str(link.get("method", "GET")).upper() == "POST":
            next_body = link.get("body") or {}
            page = http.post_json(
                link.get("href", search_url),
                body={**body, **next_body} if link.get("merge") else next_body,
                context="STAC-Suche (nächste Seite)",
            )
        else:
            page = http.get_json(link["href"], context="STAC-Suche (nächste Seite)")
        items.extend(page.get("features", []))
        pages += 1
    matched = page.get("numberMatched") or (page.get("context") or {}).get("matched")
    if isinstance(matched, int) and matched > len(items):
        raise ValueError(
            f"Layer '{layer.id}': die STAC-Suche meldet {matched} Treffer, gelesen wurden "
            f"{len(items)} - die Ergebnisliste wäre unvollständig"
        )
    return items


# --- 2. Auswahl der Szene(n) ---


def _footprint(item: dict):
    if item.get("geometry"):
        return shape(item["geometry"]).buffer(0)
    if item.get("bbox"):
        return box(*item["bbox"])
    raise ValueError(f"Szene '{item.get('id')}' hat weder geometry noch bbox")


def _item_date(item: dict) -> str:
    stamp = item["properties"].get("datetime") or item["properties"].get(
        "start_datetime"
    )
    if not stamp:
        raise ValueError(
            f"Szene '{item.get('id')}' hat kein Aufnahmedatum (properties.datetime)"
        )
    return str(stamp)[:10]


def _mosaic_scenes(
    group: list[dict], region_box, target_epsg: int | None
) -> list[dict] | None:
    """Die Szenen eines Datums, die zusammen die Region abdecken, sonst None.

    Bei mehreren UTM-Zonen zählt zuerst die Teilmenge im Ziel-CRS: deckt sie die
    Region ab, ist sie das Mosaik und eine Szene der Nachbarzone wird ignoriert.
    Sonst gilt die ganze Gruppe; mehrere CRS scheitern in ``_assemble``."""
    if len(group) < 2:
        return None
    same_zone = [i for i in group if _epsg_of(i) == target_epsg]
    if (
        target_epsg is not None
        and 0 < len(same_zone) < len(group)
        and unary_union([_footprint(i) for i in same_zone]).covers(region_box)
    ):
        return sorted(same_zone, key=lambda i: i["id"])
    if unary_union([_footprint(i) for i in group]).covers(region_box):
        return sorted(group, key=lambda i: i["id"])
    return None


def select_scenes(
    layer: StacLayer,
    items: list[dict],
    window_bbox: tuple[float, float, float, float],
    target_epsg: int | None,
) -> tuple[str, list[dict]]:
    """(Datum, Szenen) nach ``scene_order``; Gleichstand bei der Wolkenbedeckung
    entscheidet das frühere Datum."""
    if not items:
        raise ValueError(
            f"Layer '{layer.id}': der Katalog '{layer.url}' liefert für Collection "
            f"'{layer.collection}', Region und Zeitraum '{layer.datetime}' keine Szene"
            + (
                f" mit Wolkenbedeckung <= {layer.max_cloud_cover} % (Szenen ohne eo:cloud_cover, "
                "etwa Klassenraster, fallen dabei immer heraus - dann max_cloud_cover weglassen und "
                "scene_order: latest oder earliest angeben)"
                if layer.max_cloud_cover is not None
                else ""
            )
        )
    if layer.grid_code is not None:
        codes = sorted({str(i["properties"].get("grid:code")) for i in items})
        items = [
            i for i in items if i["properties"].get("grid:code") == layer.grid_code
        ]
        if not items:
            raise ValueError(
                f"Layer '{layer.id}': keine Szene der Kachel '{layer.grid_code}'; "
                f"gefundene Kacheln: {codes}"
            )
    region_box = box(*window_bbox)

    if layer.item_id is not None:
        item = items[0]
        if not _footprint(item).covers(region_box):
            raise ValueError(
                f"Layer '{layer.id}': die Szene '{layer.item_id}' deckt die Region nicht ab "
                "(andere Szene wählen oder datetime/grid_code statt item_id angeben)"
            )
        return _item_date(item), [item]

    if layer.scene_order == "cloud_cover":
        missing = [i.get("id") for i in items if _cloud_cover(i) is None]
        if missing:
            raise ValueError(
                f"Layer '{layer.id}': {len(missing)} von {len(items)} Szenen tragen kein "
                f"eo:cloud_cover (z. B. {missing[:3]}) - die Auswahl nach Wolkenbedeckung "
                "(scene_order: cloud_cover, Standard) wäre geraten; scene_order: latest oder "
                "earliest angeben (oder item_id)"
            )

    by_date: dict[str, list[dict]] = {}
    for item in items:
        by_date.setdefault(_item_date(item), []).append(item)

    def cloud_or_inf(item: dict) -> float:
        cloud = _cloud_cover(item)
        return cloud if cloud is not None else math.inf

    candidates: list[tuple[float, str, list[dict]]] = []
    for date, group in sorted(by_date.items()):
        covering = [i for i in group if _footprint(i).covers(region_box)]
        if covering:
            covering.sort(
                key=lambda i: (_epsg_of(i) != target_epsg, cloud_or_inf(i), i["id"])
            )
            chosen = [covering[0]]
        else:
            chosen = _mosaic_scenes(group, region_box, target_epsg)
            if chosen is None:
                continue
        candidates.append((max(cloud_or_inf(i) for i in chosen), date, chosen))
    if not candidates:
        raise ValueError(
            f"Layer '{layer.id}': {len(items)} Szenen an {len(by_date)} Tagen gefunden, aber an "
            f"keinem Tag deckt eine Szene (oder alle Szenen des Tages zusammen) die Region ab "
            f"(Tage: {sorted(by_date)}) - Region verkleinern oder anderen Zeitraum wählen"
        )
    if layer.scene_order == "latest":
        candidates.sort(key=lambda c: c[1], reverse=True)
    elif layer.scene_order == "earliest":
        candidates.sort(key=lambda c: c[1])
    else:
        candidates.sort(key=lambda c: (c[0], c[1]))
    _, date, chosen = candidates[0]
    return date, chosen


# --- 3. Lesen: Fenster im Gitter des feinsten Bandes ---


class _Scene:
    """Die gelesenen Bänder EINER Szene auf dem Zielgitter (Zellgröße des feinsten
    Bandes oder ``resolution_m``)."""

    def __init__(
        self,
        arrays: dict[str, np.ndarray],
        transform,
        crs,
        nodata: dict[str, float | None],
    ):
        self.arrays = arrays
        self.transform = transform
        self.crs = crs
        self.nodata = nodata


def _asset_href(layer: StacLayer, item: dict, band: str) -> str:
    key = layer.assets[band]
    assets = item.get("assets", {})
    if key not in assets:
        raise ValueError(
            f"Layer '{layer.id}': Szene '{item['id']}' hat kein Asset '{key}' (Band '{band}'); "
            f"vorhanden: {sorted(assets)}"
        )
    return _resolve_href(layer, assets[key]["href"])


def _resolve_href(layer: StacLayer, href: str) -> str:
    """Href so, wie GDAL ihn öffnet: ``s3://`` -> ``/vsis3/``; mit
    ``asset_signing: planetary_computer`` jeder http(s)-Href über GDALs Signierung
    (``/vsicurl?pc_url_signing=yes&url=...``, prozentkodiert)."""
    if href.startswith("s3://"):
        return "/vsis3/" + href[len("s3://") :]
    if layer.asset_signing == "planetary_computer" and href.startswith(
        ("http://", "https://")
    ):
        return "/vsicurl?pc_url_signing=yes&url=" + urllib.parse.quote(href, safe="")
    return href


def _gdal_env(layer: StacLayer) -> dict[str, str]:
    """GDAL-Einstellungen beim Lesen: die Standards, ergänzt um ``gdal_options``."""
    return {**GDAL_OPTIONS, **layer.gdal_options}


def _is_scene_classification(item: dict, asset_key: str) -> bool:
    """Ist das Asset eine Sentinel-2-Szenenklassifikation (SCL)? Schlüssel oder Titel
    nennen sie, oder ``classification:classes`` beschreibt 8 und 9 als Wolken."""
    asset = (item.get("assets") or {}).get(asset_key) or {}
    if "scl" in asset_key.lower():
        return True
    text = f"{asset.get('title', '')} {asset.get('description', '')}".lower()
    if (
        "(scl)" in text
        or "scene classification" in text
        or "scene classfication" in text
    ):
        return True
    classes = {
        entry.get(
            "value"
        ): f"{entry.get('description', '')} {entry.get('name', '')}".lower()
        for entry in asset.get("classification:classes") or []
        if isinstance(entry, dict)
    }
    return "cloud" in classes.get(8, "") and "cloud" in classes.get(9, "")


def _check_mask_band(layer: StacLayer, item: dict) -> None:
    """Die SCL-Klassen bedeuten nur in einer Sentinel-2-Szenenklassifikation Wolken
    (Landsat ``QA_PIXEL``, HLS ``Fmask`` nicht); sonst ist ``mask_classes`` Pflicht."""
    if not layer.mask_clouds or layer.mask_classes is not None:
        return
    key = layer.assets[SCL_BAND]
    if not _is_scene_classification(item, key):
        title = ((item.get("assets") or {}).get(key) or {}).get("title")
        raise ValueError(
            f"Layer '{layer.id}': mask_clouds maskiert die SCL-Klassen {list(CLOUD_CLASSES)}, aber das "
            f"Asset '{key}' der Szene '{item.get('id')}'"
            + (f" ('{title}')" if title else "")
            + " ist keine Sentinel-2-Szenenklassifikation - dort bedeuten die Werte etwas anderes; "
            "mask_classes: [...] mit den zu maskierenden Werten angeben oder mask_clouds weglassen"
        )


class _Grid(NamedTuple):
    """Lesefenster einer Szene: ``window`` in Zellen des feinsten Bandes (auf die Szene
    zugeschnitten), ``full_shape`` (Zeilen, Spalten) das ungeschnittene Fenster der
    Region in Zielzellen, ``factors`` die Zellgröße je Band, ``target`` die der
    Zielzelle - beide in Zellen des feinsten Bandes -, ``ref`` das feinste Band."""

    window: Window
    full_shape: tuple[int, int]
    factors: dict[str, int]
    target: int
    ref: Any


def _target_factor(
    layer_id: str, finest: str, ref_res: float, resolution_m: float | None
) -> int:
    """Zielzelle in Zellen des feinsten Bandes (1 ohne ``resolution_m``)."""
    if resolution_m is None:
        return 1
    ratio = resolution_m / ref_res
    if ratio < 1 and not math.isclose(ratio, 1, rel_tol=1e-6):
        raise ValueError(
            f"Layer '{layer_id}': resolution_m {resolution_m:g} m ist feiner als die Zellgröße "
            f"des feinsten Bandes '{finest}' ({ref_res:g} m) - ein Raster lässt sich nicht "
            "verfeinern, resolution_m muss mindestens so groß sein"
        )
    if not math.isclose(ratio, round(ratio), rel_tol=1e-6):
        raise ValueError(
            f"Layer '{layer_id}': resolution_m {resolution_m:g} m ist kein ganzzahliges Vielfaches "
            f"der Zellgröße des feinsten Bandes '{finest}' ({ref_res:g} m) - z. B. "
            f"{ref_res * 2:g} oder {ref_res * 4:g}"
        )
    return int(round(ratio))


def _grid_window(
    datasets: dict[str, Any],
    bounds: tuple[float, float, float, float],
    layer_id: str,
    resolution_m: float | None = None,
) -> _Grid:
    """Fenster im feinsten Band auf dem Vielfachen aller Zellgrößen UND der
    Zielzelle (``resolution_m``); nur der Teil innerhalb der Szene. Prüfungen vor
    dem Lesen: quadratische Zellen, ein Gitter für alle Bänder, ganzzahlige
    Zellverhältnisse (Band zu feinstem Band, Band zu Zielzelle)."""
    resolution = {band: abs(ds.transform.a) for band, ds in datasets.items()}
    for band, ds in datasets.items():
        if not math.isclose(abs(ds.transform.e), resolution[band], rel_tol=1e-9):
            raise ValueError(
                f"Layer '{layer_id}': Band '{band}' hat keine quadratischen Zellen"
            )
    finest = min(resolution, key=resolution.get)
    ref = datasets[finest]
    ref_res = resolution[finest]
    factors: dict[str, int] = {}
    for band, ds in datasets.items():
        factor = resolution[band] / ref_res
        if not math.isclose(factor, round(factor), rel_tol=1e-6):
            raise ValueError(
                f"Layer '{layer_id}': Band '{band}' ({resolution[band]} m) liegt nicht auf einem "
                f"ganzzahligen Vielfachen der feinsten Zellgröße ({ref_res} m)"
            )
        factors[band] = int(round(factor))
        if not (
            math.isclose(ds.transform.c, ref.transform.c, abs_tol=1e-6 * ref_res)
            and math.isclose(ds.transform.f, ref.transform.f, abs_tol=1e-6 * ref_res)
        ):
            raise ValueError(
                f"Layer '{layer_id}': Band '{band}' hat einen anderen Ursprung als '{finest}' - "
                "die Bänder liegen nicht auf demselben Gitter"
            )
    target = _target_factor(layer_id, finest, ref_res, resolution_m)
    for band, factor in factors.items():
        if target % factor and factor % target:
            raise ValueError(
                f"Layer '{layer_id}': Band '{band}' ({resolution[band]:g} m) und resolution_m "
                f"{resolution_m:g} m stehen nicht im ganzzahligen Verhältnis (die eine Zellgröße "
                "muss die andere teilen) - eine Zielzelle, die eine Bandzelle schneidet, "
                "verfälschte Klassencodes und Mittelwerte"
            )
    lcm = math.lcm(target, *factors.values())
    left, bottom, right, top = bounds
    origin_x, origin_y = ref.transform.c, ref.transform.f
    col_start = math.floor((left - origin_x) / ref_res) // lcm * lcm
    col_stop = -(-math.ceil((right - origin_x) / ref_res) // lcm) * lcm
    row_start = math.floor((origin_y - top) / ref_res) // lcm * lcm
    row_stop = -(-math.ceil((origin_y - bottom) / ref_res) // lcm) * lcm
    full_shape = ((row_stop - row_start) // target, (col_stop - col_start) // target)
    col_start, row_start = max(col_start, 0), max(row_start, 0)
    col_stop = min(col_stop, ref.width // lcm * lcm)
    row_stop = min(row_stop, ref.height // lcm * lcm)
    if col_stop <= col_start or row_stop <= row_start:
        raise ValueError(
            f"Layer '{layer_id}': die Region liegt außerhalb des Rasters der Szene"
        )
    window = Window(col_start, row_start, col_stop - col_start, row_stop - row_start)
    return _Grid(window, full_shape, factors, target, ref)


def _read_scene(
    layer: StacLayer, item: dict, window_bbox: tuple[float, float, float, float]
) -> _Scene:
    bands = list(layer.assets)
    with ExitStack() as stack:
        stack.enter_context(rasterio.Env(**_gdal_env(layer)))
        datasets: dict[str, Any] = {}
        for band in bands:
            href = _asset_href(layer, item, band)
            try:
                datasets[band] = stack.enter_context(rasterio.open(href))
            except RasterioError as exc:
                raise ValueError(
                    f"Layer '{layer.id}': Asset '{layer.assets[band]}' der Szene '{item['id']}' "
                    f"nicht lesbar ({href}): {exc}"
                ) from exc
        crs_set = {ds.crs.to_string() for ds in datasets.values()}
        if len(crs_set) != 1:
            raise ValueError(
                f"Layer '{layer.id}': die Bänder der Szene '{item['id']}' haben verschiedene CRS {sorted(crs_set)}"
            )
        any_ds = next(iter(datasets.values()))
        bounds = transform_bounds("EPSG:4326", any_ds.crs, *window_bbox, densify_pts=21)
        grid = _grid_window(datasets, bounds, layer.id, layer.resolution_m)
        window, ref = grid.window, grid.ref
        rows, cols = grid.full_shape
        cells = rows * cols * len(bands)
        if cells > MAX_CELLS:
            cell_size = (
                layer.resolution_m
                if layer.resolution_m is not None
                else abs(ref.transform.a)
            )
            raise ValueError(
                f"Layer '{layer.id}': das Lesefenster der Region hat {cells:,} Zellen "
                f"({len(bands)} Bänder x {rows} x {cols} bei {cell_size:g} m Zellgröße), erlaubt sind "
                f"{MAX_CELLS:,} - Region verkleinern oder eine gröbere resolution_m wählen"
            )
        arrays: dict[str, np.ndarray] = {}
        nodata: dict[str, float | None] = {}
        for band, ds in datasets.items():
            arrays[band] = _read_band(
                layer,
                item,
                band,
                ds,
                window,
                grid.factors[band],
                grid.target,
                is_class=_is_class_band(layer, item, band),
            )
            nodata[band] = _declared_nodata(item, layer.assets[band], ds)
        transform = ref.window_transform(window) * Affine.scale(grid.target)
        return _Scene(arrays, transform, ref.crs, nodata)


def _is_class_band(layer: StacLayer, item: dict, band: str) -> bool:
    """Klassenband (Codes, nicht mittelbar): ``scl`` und jedes Band ohne ``scale`` in
    ``raster:bands`` (dasselbe Merkmal, an dem ``apply_scale`` Reflektanz erkennt)."""
    return band == SCL_BAND or _scale_offset(item, layer.assets[band])[0] is None


def _read_band(
    layer: StacLayer,
    item: dict,
    band: str,
    dataset,
    window: Window,
    factor: int,
    target: int,
    is_class: bool,
) -> np.ndarray:
    """Ein Band im Fenster ``window`` auf dem Zielgitter; ``factor`` (Bandzelle) und
    ``target`` (Zielzelle) in Zellen des feinsten Bandes, von ``_grid_window`` geprüft."""
    native = Window(
        window.col_off // factor,
        window.row_off // factor,
        window.width // factor,
        window.height // factor,
    )
    try:
        if factor == target:
            return dataset.read(1, window=native)
        if factor > target:
            # Band gröber als das Ziel (SCL bei 10 m): exakte Zellwiederholung
            step = factor // target
            data = dataset.read(1, window=native)
            return np.repeat(np.repeat(data, step, axis=0), step, axis=1)
        step = target // factor
        if is_class:
            # Stichprobe: die Quellzelle in der Mitte der Zielzelle (wie GDALs nearest),
            # aus der Quellauflösung - keine Overview, die Klassencodes mischen könnte
            data = dataset.read(1, window=native)
            # .copy(): sonst hält die Stichprobe als View das ganze Quellfenster im Speicher
            return data[step // 2 :: step, step // 2 :: step].copy()
        if _gdal_decimates_exactly(dataset, step):
            shape = (int(native.height) // step, int(native.width) // step)
            return dataset.read(
                1, window=native, out_shape=shape, resampling=Resampling.average
            )
        data = dataset.read(1, window=native)
        return _mean_blocks(
            data, step, _declared_nodata(item, layer.assets[band], dataset)
        )
    except RasterioError as exc:
        raise ValueError(
            f"Layer '{layer.id}': Lesen von '{layer.assets[band]}' (Szene '{item['id']}') "
            f"scheiterte: {exc}"
        ) from exc


def _gdal_decimates_exactly(dataset, step: int) -> bool:
    """Liest GDAL ein um ``step`` verkleinertes Fenster ohne Versatz der Zellblöcke?

    Ohne passende Overview rechnet GDAL aus der vollen Auflösung (exakt). Mit
    Overviews bildet es das Fenster über das Größenverhältnis ab; ist die
    Rasterkante kein Vielfaches des Overview-Faktors (oder passt keine Overview genau
    auf ``step``), rutscht es um Bruchteile einer Zelle und mittelt den falschen Block.
    Dann mittelt ``_mean_blocks`` in numpy, exakt aber aus der vollen Auflösung."""
    usable = [factor for factor in dataset.overviews(1) if factor <= step]
    if not usable:
        return True
    return step in usable and dataset.width % step == 0 and dataset.height % step == 0


def _mean_blocks(data: np.ndarray, step: int, nodata: float | None) -> np.ndarray:
    """Mittelwert je ``step`` x ``step`` Zellen (Zeilen und Spalten sind Vielfache von
    ``step``), Nodata-Zellen zählen nicht mit (wie bei GDALs ``average``); ein Block nur
    aus Nodata bleibt Nodata. Ganzzahlen werden gerundet, der Datentyp bleibt."""
    rows, cols = data.shape
    blocks = data.reshape(rows // step, step, cols // step, step)
    if nodata is None:
        mean = blocks.mean(axis=(1, 3), dtype=np.float64)
    else:
        valid = (
            ~np.isnan(blocks)
            if np.isnan(nodata)
            else blocks != np.asarray(nodata, dtype=data.dtype)
        )
        count = valid.sum(axis=(1, 3))
        total = np.where(valid, blocks, 0).sum(axis=(1, 3), dtype=np.float64)
        mean = np.where(count > 0, total / np.maximum(count, 1), nodata)
    if np.issubdtype(data.dtype, np.integer):
        mean = np.rint(mean)
    return mean.astype(data.dtype)


def _declared_nodata(item: dict, asset_key: str, dataset) -> float | None:
    if dataset.nodata is not None:
        return float(dataset.nodata)
    raster_bands = item.get("assets", {}).get(asset_key, {}).get("raster:bands") or []
    if raster_bands and raster_bands[0].get("nodata") is not None:
        return float(raster_bands[0]["nodata"])
    return None


# --- 4./5. Nodata, Wolkenmaske, Skalierung -> RasterLayer ---


def _common_nodata(
    layer: StacLayer, item_id: str, nodata: dict[str, float | None]
) -> float | None:
    declared = {band: value for band, value in nodata.items() if value is not None}
    if not declared:
        return None
    if len(set(declared.values())) != 1:
        raise ValueError(
            f"Layer '{layer.id}': die Bänder der Szene '{item_id}' deklarieren verschiedenes Nodata "
            f"{declared} - ein Raster hat nur einen Nodata-Wert"
        )
    if len(declared) != len(nodata):
        missing = sorted(set(nodata) - set(declared))
        raise ValueError(
            f"Layer '{layer.id}': die Bänder {missing} der Szene '{item_id}' deklarieren kein Nodata, "
            f"die übrigen schon ({declared})"
        )
    return next(iter(declared.values()))


def _scale_offset(item: dict, asset_key: str) -> tuple[float | None, float]:
    raster_bands = item.get("assets", {}).get(asset_key, {}).get("raster:bands") or []
    if not raster_bands or raster_bands[0].get("scale") is None:
        return None, 0.0
    offset = float(raster_bands[0].get("offset", 0.0))
    # Tragen die Werte den Offset schon, ergäbe ein weiteres Abziehen negative
    # Reflektanz und NDVI +-inf.
    if item["properties"].get("earthsearch:boa_offset_applied") is True:
        offset = 0.0
    return float(raster_bands[0]["scale"]), offset


def _masked_classes(layer: StacLayer) -> list[int]:
    return (
        list(layer.mask_classes)
        if layer.mask_classes is not None
        else list(CLOUD_CLASSES)
    )


def _finish_scene(
    layer: StacLayer, item: dict, scene: _Scene
) -> tuple[np.ndarray, float | None]:
    """Wolkenmaske und Skalierung auf die gelesenen Bänder einer Szene
    anwenden; (3D-Array in der Reihenfolge von ``assets``, Nodata)."""
    bands = list(layer.assets)
    nodata = _common_nodata(layer, item["id"], scene.nodata)
    arrays = dict(scene.arrays)

    if layer.mask_clouds:
        if nodata is None:
            raise ValueError(
                f"Layer '{layer.id}': mask_clouds braucht einen Nodata-Wert, die Assets der Szene "
                f"'{item['id']}' deklarieren keinen"
            )
        cloudy = np.isin(arrays[SCL_BAND], _masked_classes(layer))
        for band in bands:
            arrays[band] = np.where(
                cloudy, np.asarray(nodata, dtype=arrays[band].dtype), arrays[band]
            )

    if layer.apply_scale:
        for band in bands:
            raw = arrays[band]
            scale, offset = _scale_offset(item, layer.assets[band])
            value = raw.astype(np.float32)
            if scale is not None:
                value = value * np.float32(scale) + np.float32(offset)
            if nodata is not None:
                value[raw == np.asarray(nodata, dtype=raw.dtype)] = np.nan
            arrays[band] = value
        nodata = float("nan")
    return np.stack([arrays[band] for band in bands]), nodata


def _assemble(layer: StacLayer, scenes: list[tuple[dict, _Scene]]) -> RasterLayer:
    """Ein oder mehrere Szenen (Mosaik) zu einem RasterLayer."""
    bands = list(layer.assets)
    stacks = []
    for item, scene in scenes:
        data, nodata = _finish_scene(layer, item, scene)
        stacks.append((item, scene, data, nodata))
    first_item, first_scene, data, nodata = stacks[0]
    if len(stacks) == 1:
        transform, crs = first_scene.transform, first_scene.crs
    else:
        crs_set = {s.crs.to_string() for _, s, _, _ in stacks}
        if len(crs_set) != 1:
            raise ValueError(
                f"Layer '{layer.id}': die Szenen {[i['id'] for i, *_ in stacks]} liegen in verschiedenen "
                f"CRS {sorted(crs_set)} und lassen sich nicht zu einem Mosaik zusammenfassen - "
                "grid_code wählen"
            )
        _check_common_grid(layer, stacks)
        data, transform = _mosaic(stacks)
        crs = first_scene.crs
        nodata = stacks[0][3]
    return RasterLayer(
        data=data,
        transform=transform,
        crs=CRS.from_user_input(crs),
        path="stac:"
        + layer.collection
        + "/"
        + "+".join(item["id"] for item, *_ in stacks),
        nodata=nodata,
        band_names=bands,
    )


def _check_common_grid(layer: StacLayer, stacks: list) -> None:
    """Die Szenen eines Mosaiks müssen auf demselben Zellgitter liegen, sonst
    verschiebt ``rasterio.merge`` sie still um Zellbruchteile. Sentinel-2-Kacheln
    versetzen sich um 99,96 km (ein Vielfaches von 40 m, nicht von 100 m)."""
    first_item, first, _, _ = stacks[0]
    cell = abs(first.transform.a)
    for item, scene, _, _ in stacks[1:]:
        t = scene.transform
        shift_x = (t.c - first.transform.c) / cell
        shift_y = (first.transform.f - t.f) / cell
        if (
            not math.isclose(abs(t.a), cell, rel_tol=1e-9)
            or not math.isclose(shift_x, round(shift_x), abs_tol=1e-6)
            or not math.isclose(shift_y, round(shift_y), abs_tol=1e-6)
        ):
            raise ValueError(
                f"Layer '{layer.id}': die Szenen '{first_item['id']}' und '{item['id']}' liegen "
                f"nicht auf demselben Zellgitter (Zellgröße {cell:g} m, Versatz "
                f"{t.c - first.transform.c:g} m / {first.transform.f - t.f:g} m) - ein Mosaik "
                "würde um Zellbruchteile verschoben; resolution_m kleiner wählen (ein Teiler "
                "des Versatzes)"
            )


def _mosaic(stacks: list) -> tuple[np.ndarray, Any]:
    """Mosaik mit ``rasterio.merge``: die erste Szene hat Vorrang, Nodata-Zellen
    füllt die nächste."""
    with ExitStack() as stack:
        datasets = []
        for _, scene, data, nodata in stacks:
            memory = stack.enter_context(MemoryFile())
            with memory.open(
                driver="GTiff",
                height=data.shape[1],
                width=data.shape[2],
                count=data.shape[0],
                dtype=data.dtype,
                crs=scene.crs,
                transform=scene.transform,
                nodata=nodata,
            ) as writer:
                writer.write(data)
            datasets.append(stack.enter_context(memory.open()))
        merged, transform = merge(datasets, nodata=stacks[0][3], method="first")
    return merged, transform


def _verify_coverage(layer: StacLayer, raster: RasterLayer, window_bbox: tuple) -> None:
    """Das gelesene Raster muss die Region (+ Rand) abdecken - sonst würden
    Zonen am Rand stillschweigend Zellen verlieren."""
    left, bottom, right, top = transform_bounds(
        "EPSG:4326", raster.crs, *window_bbox, densify_pts=21
    )
    r_left, r_bottom, r_right, r_top = array_bounds(*raster.shape, raster.transform)
    pixel = abs(raster.transform.a)
    if (
        r_left > left + pixel
        or r_bottom > bottom + pixel
        or r_right < right - pixel
        or r_top < top - pixel
    ):
        raise ValueError(
            f"Layer '{layer.id}': die gewählte Szene deckt die Region nur teilweise ab "
            f"(Raster {r_left:.0f},{r_bottom:.0f},{r_right:.0f},{r_top:.0f}; "
            f"Region {left:.0f},{bottom:.0f},{right:.0f},{top:.0f}) - anderen Zeitraum, grid_code "
            "oder eine kleinere Region wählen"
        )


# --- Snapshot (FA7) und Provenienz ---


def snapshot_key(layer: StacLayer, region, target_epsg: int | None) -> str:
    """SHA-256 über alle Angaben, die das Ergebnis bestimmen, die Region
    (WKT) und die UTM-Zone der Region (sie beeinflusst die Szenenwahl)."""
    payload = {
        "kind": "stac",
        "url": layer.url,
        "collection": layer.collection,
        "assets": dict(sorted(layer.assets.items())),
        "datetime": layer.datetime,
        "max_cloud_cover": layer.max_cloud_cover,
        "mask_clouds": layer.mask_clouds,
        "item_id": layer.item_id,
        "grid_code": layer.grid_code,
        "apply_scale": layer.apply_scale,
        "margin_km": layer.margin_km,
        "target_epsg": target_epsg,
        "region_wkt": region.union_all().wkt,
    }
    if layer.resolution_m is not None:
        # Nur wenn angegeben, damit der Schlüssel (und vorhandene Snapshots) stabil bleibt.
        payload["resolution_m"] = float(layer.resolution_m)
    if layer.scene_order != DEFAULT_SCENE_ORDER:
        payload["scene_order"] = (
            layer.scene_order
        )  # nur ungleich Standard: Schlüssel stabil
    if layer.mask_classes is not None:
        payload["mask_classes"] = sorted(layer.mask_classes)
    return snapshot.payload_key(payload)


def _provenance(layer: StacLayer, date: str, items: list[dict]) -> dict:
    return {
        "url": layer.url,
        "collection": layer.collection,
        "date": date,
        "items": [
            {
                "id": item["id"],
                "datetime": item["properties"].get("datetime"),
                "cloud_cover": item["properties"].get("eo:cloud_cover"),
                "grid_code": item["properties"].get("grid:code"),
                "epsg": _epsg_of(item),
            }
            for item in items
        ],
        "assets": dict(layer.assets),
        "resolution_m": layer.resolution_m,
        "mask_clouds": layer.mask_clouds,
        "apply_scale": layer.apply_scale,
        "cloud_classes_masked": _masked_classes(layer) if layer.mask_clouds else [],
        **(
            {"scene_order": layer.scene_order}
            if layer.scene_order != DEFAULT_SCENE_ORDER
            else {}
        ),
    }


def _announce(layer: StacLayer, provenance: dict, from_snapshot: bool) -> None:
    """Die gewählte Szene als Hinweis (Provenienz), frisch wie aus Snapshot."""
    scenes = ", ".join(
        f"{item['id']} (Wolken {item['cloud_cover']:.2f} %)"
        if item.get("cloud_cover") is not None
        else item["id"]
        for item in provenance["items"]
    )
    origin = "aus Snapshot" if from_snapshot else "frisch bezogen"
    resolution = provenance.get("resolution_m")
    cell = f", Zellgröße {resolution:g} m" if resolution is not None else ""
    warnings.warn(
        f"Layer '{layer.id}': Szene {scenes} vom {provenance['date']} ({origin}; "
        f"{provenance['collection']}, Bänder {list(provenance['assets'])}{cell})",
        StacSceneNotice,
        stacklevel=2,
    )


def _in_asset_order(layer: StacLayer, raster: RasterLayer) -> RasterLayer:
    """Bänder eines Snapshots in der Reihenfolge von ``layer.assets``: der Schlüssel
    sortiert die Bänder, Layer mit anderer Reihenfolge teilen sich also einen Snapshot."""
    names = list(raster.band_names or [])
    order = [band for band in layer.assets if band in names]
    if raster.data.ndim != 3 or order == names or sorted(order) != sorted(names):
        return raster
    data = raster.data[[names.index(band) for band in order]]
    return RasterLayer(
        data=data,
        transform=raster.transform,
        crs=raster.crs,
        path=raster.path,
        nodata=raster.nodata,
        band_names=order,
    )


def _write_snapshot(key: str, raster: RasterLayer, provenance: dict) -> None:
    tif, meta = snapshot.binary_path(key, ".tif"), snapshot.binary_path(key, ".json")
    partial = tif.with_name(tif.name + ".part")
    write_geotiff(raster, partial)
    partial.replace(tif)
    meta.write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# --- Einstieg ---


def fetch(layer: StacLayer, ctx: LoadContext) -> tuple[RasterLayer, dict]:
    """Suchen, auswählen, lesen; (Raster, Provenienz)."""
    bbox = [float(v) for v in ctx.region.total_bounds]
    if not all(math.isfinite(v) for v in bbox):
        raise ValueError(
            f"Layer '{layer.id}': die Regionsgrenzen {bbox} sind keine endliche BBox - ist die Region leer?"
        )
    window_bbox = region_window_bounds(tuple(bbox), layer.margin_km)
    target_epsg = ctx.target_crs.to_epsg() if ctx.target_crs is not None else None
    items = _search_items(layer, list(window_bbox))
    date, chosen = select_scenes(layer, items, window_bbox, target_epsg)
    for item in chosen:
        _check_mask_band(layer, item)
    scenes = [(item, _read_scene(layer, item, window_bbox)) for item in chosen]
    raster = _assemble(layer, scenes)
    _verify_coverage(layer, raster, window_bbox)
    return raster, _provenance(layer, date, chosen)


@register_source(
    "stac",
    StacLayer,
    description="Satellitenbild (mehrbandig) aus einem STAC-Katalog, z. B. Sentinel-2 L2A (Earth Search)",
)
def load(layer: StacLayer, ctx: LoadContext) -> RasterLayer:
    """Einziger Einstieg der Quellart (FA45): Snapshot lesen, sonst frisch
    beziehen und als Snapshot ablegen (``ctx.refresh`` erzwingt Neubezug)."""
    if ctx.region is None:
        raise ValueError(
            f"Layer '{layer.id}': source: stac braucht die Szenario-Region (nur innerhalb eines Laufs)"
        )
    target_epsg = ctx.target_crs.to_epsg() if ctx.target_crs is not None else None
    key = snapshot_key(layer, ctx.region, target_epsg)
    tif, meta = snapshot.binary_path(key, ".tif"), snapshot.binary_path(key, ".json")
    if tif.is_file() and meta.is_file() and not ctx.refresh:
        raster = _in_asset_order(layer, read_geotiff(tif))
        provenance = json.loads(meta.read_text(encoding="utf-8"))
        provenance["assets"] = dict(layer.assets)
        raster.path = (
            "stac:"
            + layer.collection
            + "/"
            + "+".join(i["id"] for i in provenance["items"])
        )
        _announce(layer, provenance, from_snapshot=True)
        return raster
    raster, provenance = fetch(layer, ctx)
    _write_snapshot(key, raster, provenance)
    _announce(layer, provenance, from_snapshot=False)
    return raster
