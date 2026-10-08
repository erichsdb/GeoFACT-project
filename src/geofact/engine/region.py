"""Implements: FA3 (Eingabe Region: BBox/Name), FA5 (UTM-Zone der Region als Arbeits-CRS), FA21,
FA55 (Arbeits-CRS aus ``scenario.crs``), FA56 (Regionsplausibilität).

Fester Kerndienst der Engine (kein Erweiterungspunkt): löst scenario.region zu
einer Geometrie auf und leitet aus ihr das metrische Arbeits-CRS ab.

- resolve_region(): ein BBox-Literal ("minlon,minlat,maxlon,maxlat") oder ein Name,
  der über Nominatim aufgelöst und unter GEOFACT_SNAPSHOT_DIR gecacht wird (Datei
  je Region-Hash, wie der OSM-Snapshot, FA7). Der Cache speichert
  ``{"geojson", "display_name", "class", "type"}``; alte Dateien (nackte Geometrie)
  bleiben lesbar. Ein Name, der auf einen Punkt oder eine Linie auflöst, ist ein
  Fehler mit ``display_name`` und OSM-Klasse. Unit-Tests nutzen nur eingecheckte
  Fixture-Antworten, kein Netz.
- Nominatim-Nutzungsrichtlinie: höchstens 1 Anfrage/Sekunde; Retry/Backoff bei
  transienten Fehlern (Timeout, 5xx, 429) spiegelt OverpassBackend.fetch() (osm.py).
- utm_zone_epsg(): UTM-Zone aus dem Zentroid der Region (FA5).
- working_crs(): ``auto`` ist exakt FA5, sonst gilt das deklarierte CRS.
- describe_region() prüft die Region vor dem ersten Layer: im Arbeits-CRS endlich,
  gültig und mit Fläche > 0 abbildbar (sonst ValueError), unter ``auto``
  Spannweite gegen eine UTM-Zone (> 6 Grad: Warnung) und Zentroid jenseits 84 Grad
  Breite (Fehler), dazu die OSM-Klasse (außerhalb boundary/place: Warnung).
  Warnungen stehen in ``RegionInfo.warnings``; der Executor meldet sie als
  ``region_warning``.
"""

from __future__ import annotations

import hashlib
import json
import os
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import geopandas as gpd
import numpy as np
import requests
import shapely
from geopandas import GeoDataFrame
from pyproj import CRS
from shapely.geometry import box, shape
from shapely.geometry.base import BaseGeometry

from geofact.core.crs import (
    AUTO,
    check_bbox,
    crs_label,
    parse_bbox_literal,
    parse_crs_spec,
    require_projected_metric,
)
from geofact.core.errors import RegionWarning  # noqa: F401 - re-export (FA56)


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
DEFAULT_SNAPSHOT_DIR = ".geofact/snapshots"
NOMINATIM_MAX_RETRIES = 3
# Nominatim-Nutzungsrichtlinie: maximal 1 Anfrage/Sekunde. 1.1 s Sicherheitsabstand
# statt exakt 1.0 s, damit Rundungs-/Scheduling-Jitter nicht knapp darunter fällt.
NOMINATIM_MIN_INTERVAL_S = 1.1

Fetcher = Callable[[str], list[dict]]

# Zeitstempel des letzten Nominatim-Requests. Bewusst ohne Sperre: Einzelprozess-Demo,
# der Kern läuft synchron.
_last_request_at: float = 0.0


def _throttle_nominatim() -> None:
    """Wartet bei Bedarf, damit mindestens NOMINATIM_MIN_INTERVAL_S Sekunden
    seit dem letzten tatsächlichen Nominatim-Request vergangen sind."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    remaining = NOMINATIM_MIN_INTERVAL_S - elapsed
    if remaining > 0:
        time.sleep(remaining)
    _last_request_at = time.monotonic()


def _snapshot_dir() -> Path:
    return Path(os.environ.get("GEOFACT_SNAPSHOT_DIR") or DEFAULT_SNAPSHOT_DIR)


def _region_cache_path(name: str) -> Path:
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return _snapshot_dir() / "regions" / f"{digest}.geojson"


def _is_bbox_literal(region: str) -> bool:
    return parse_bbox_literal(region) is not None


def _parse_bbox(region: str) -> gpd.GeoDataFrame:
    """BBox-Literal -> Region; ``check_bbox`` lehnt Unplausibles ab (FA56, ValueError)."""
    values = parse_bbox_literal(region)
    if values is None:
        raise ValueError(
            f"BBox '{region}': vier Zahlen minlon,minlat,maxlon,maxlat erwartet"
        )
    check_bbox(values, text=region)
    min_lon, min_lat, max_lon, max_lat = values
    geometry = box(min_lon, min_lat, max_lon, max_lat)
    return gpd.GeoDataFrame({"name": [region]}, geometry=[geometry], crs="EPSG:4326")


def _default_fetcher(name: str) -> list[dict]:
    """Fragt Nominatim ab, mit Rate-Limit und Retry/Backoff bei transienten Fehlern.
    Nach allen Versuchen folgt ein kontextreicher RuntimeError statt eines rohen
    requests.HTTPError (goldene Regel 7)."""
    last_error: Exception | None = None
    last_status: int | None = None
    for attempt in range(NOMINATIM_MAX_RETRIES):
        _throttle_nominatim()
        try:
            response = requests.get(
                NOMINATIM_URL,
                params={
                    "q": name,
                    "format": "jsonv2",
                    "polygon_geojson": 1,
                    "limit": 1,
                },
                headers={"User-Agent": "GeoFACT/0.1 (Kontakt siehe README)"},
                timeout=30,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            last_status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt < NOMINATIM_MAX_RETRIES - 1:
                time.sleep(2**attempt)
    raise RuntimeError(
        f"Nominatim-Anfrage für Region '{name}' fehlgeschlagen nach "
        f"{NOMINATIM_MAX_RETRIES} Versuchen (Status: {last_status}): {last_error}"
    ) from last_error


# Klassen einer Nominatim-Antwort, die eine Verwaltungs- oder Siedlungsfläche bezeichnen
# (FA56); alles andere (z. B. amenity, building) ist eine Warnung.
EXPECTED_OSM_CLASSES = frozenset({"boundary", "place"})

_AREAL_TYPES = frozenset({"Polygon", "MultiPolygon"})


def _meta_of(result: dict) -> dict[str, Any]:
    """Metadaten eines Treffers. ``format=jsonv2`` nennt die OSM-Klasse ``category``,
    ``format=json`` ``class``; beide werden gelesen, sonst wäre sie in echten Läufen None."""
    return {
        "display_name": result.get("display_name"),
        "class": result.get("category") or result.get("class"),
        "type": result.get("type"),
    }


def _read_cache(raw: Any) -> tuple[dict, Optional[dict]]:
    """Cache-Datei -> (Geometrie, Metadaten); eine alte Datei (nackte GeoJSON-Geometrie)
    ergibt (Geometrie, None), ohne Klassenwarnung."""
    if isinstance(raw, dict) and isinstance(raw.get("geojson"), dict):
        return raw["geojson"], {
            "display_name": raw.get("display_name"),
            "class": raw.get("class"),
            "type": raw.get("type"),
        }
    return raw, None


def _require_areal(region: str, geojson: dict, meta: Optional[dict]) -> None:
    """Eine Region ist eine Fläche; Punkt oder Linie sind ein Fehler mit
    ``display_name`` und OSM-Klasse (FA56)."""
    kind = geojson.get("type") if isinstance(geojson, dict) else None
    if kind in _AREAL_TYPES:
        return
    meta = meta or {}
    found = meta.get("display_name") or "unbekannt"
    osm_class = (
        "/".join(str(v) for v in (meta.get("class"), meta.get("type")) if v)
        or "unbekannt"
    )
    raise ValueError(
        f"Region '{region}' löst auf eine {kind or 'unbekannte'}-Geometrie auf, keine Fläche "
        f"(Treffer: '{found}', OSM-Klasse {osm_class}) - genaueren Namen angeben "
        "(z. B. mit Land) oder eine BBox minlon,minlat,maxlon,maxlat"
    )


_ANTIMERIDIAN_EPS_DEG = 1e-6


def _reject_antimeridian(region: str, geojson: dict) -> None:
    """Ein Name, dessen Geometrie an -180 und 180 Grad liegt, überquert den
    Antimeridian: wie beim BBox-Literal nicht unterstützt (FA56). Sonst entstünde
    eine BBox über 360 Grad, die Rasterfenster, Overpass und PostGIS als ganzes
    Breitenband lesen."""
    min_lon, min_lat, max_lon, max_lat = shape(geojson).bounds
    if (
        min_lon <= -180 + _ANTIMERIDIAN_EPS_DEG
        and max_lon >= 180 - _ANTIMERIDIAN_EPS_DEG
    ):
        raise ValueError(
            f"Region '{region}' überquert den Antimeridian (Geometrie an -180 und 180 Grad, "
            f"Breite {min_lat:g}..{max_lat:g}) - Regionen über den Antimeridian werden nicht "
            "unterstützt (Grenze der Arbeit); Region teilen oder als BBox bis 180 begrenzen"
        )


def _resolve_name(
    region: str, fetcher: Fetcher, refresh: bool = False
) -> tuple[gpd.GeoDataFrame, Optional[dict]]:
    """Name -> Region über den Cache (``regions/<sha256>.geojson``) oder den Fetcher.
    ``refresh`` (FA7) fragt neu und ersetzt den Cache."""
    cache_path = _region_cache_path(region)
    if cache_path.is_file() and not refresh:
        geojson, meta = _read_cache(json.loads(cache_path.read_text(encoding="utf-8")))
        _require_areal(region, geojson, meta)
        _reject_antimeridian(region, geojson)
        frame = gpd.GeoDataFrame(
            {"name": [region]}, geometry=[shape(geojson)], crs="EPSG:4326"
        )
        return frame, meta

    results = fetcher(region)
    if not results:
        raise ValueError(
            f"Region '{region}' konnte nicht aufgelöst werden (keine Treffer)"
        )

    geojson = results[0].get("geojson")
    meta = _meta_of(results[0])
    if geojson is None:
        raise ValueError(
            f"Region '{region}' hat keine Polygon-Geometrie in der Nominatim-Antwort"
        )
    # Vor dem Cachen prüfen: ein Punkt-Treffer landet nie im Cache.
    _require_areal(region, geojson, meta)
    _reject_antimeridian(region, geojson)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"geojson": geojson, **meta}), encoding="utf-8")
    frame = gpd.GeoDataFrame(
        {"name": [region]}, geometry=[shape(geojson)], crs="EPSG:4326"
    )
    return frame, meta


def resolve_region_with_meta(
    region: str, fetcher: Optional[Fetcher] = None, *, refresh: bool = False
) -> tuple[gpd.GeoDataFrame, Optional[dict]]:
    """Wie resolve_region(), zusätzlich die Nominatim-Metadaten (None für ein
    BBox-Literal und alte Cache-Dateien, FA56). ``refresh`` löst neu auf (FA7)."""
    if _is_bbox_literal(region):
        return _parse_bbox(region), None
    return _resolve_name(region, fetcher or _default_fetcher, refresh)


def resolve_region(
    region: str, fetcher: Optional[Fetcher] = None, *, refresh: bool = False
) -> gpd.GeoDataFrame:
    return resolve_region_with_meta(region, fetcher, refresh=refresh)[0]


def utm_zone_epsg(region: BaseGeometry | GeoDataFrame) -> CRS:
    """Leitet die UTM-Zone aus dem Zentroid der Region-Geometrie ab (FA5)."""
    if isinstance(region, GeoDataFrame):
        centroid = region.union_all().centroid
    else:
        centroid = region.centroid
    # min(..., 60): ein Zentroid genau auf 180 Grad gehört zu Zone 60.
    zone = min(int((centroid.x + 180) // 6) + 1, 60)
    hemisphere_offset = 32600 if centroid.y >= 0 else 32700
    return CRS.from_epsg(hemisphere_offset + zone)


# --- Arbeits-CRS und Regionsplausibilität (FA55, FA56) ---

UTM_ZONE_WIDTH_DEG = 6.0
"""Breite einer UTM-Zone; eine unter ``auto`` breitere Region erzeugt eine Warnung (FA56)."""

UTM_MAX_ABS_LAT_DEG = 84.0
"""UTM gilt bis 84 Grad Nord; jenseits ist ``auto`` ein Fehler (polare Regionen
brauchen ein deklariertes CRS)."""

_CHECK_SEGMENT_DEG = 0.5
"""Verdichtung der Regionskanten (Grad) für die Abbildbarkeitsprüfung; vier Ecken
verschwiegen Verzerrungen entlang der Kanten."""

_HINT_EQUAL_AREA = (
    "scenario.crs: equal_area (weltweit flächentreu, EPSG:6933) oder ein passendes "
    "projiziertes CRS deklarieren"
)


@dataclass(frozen=True)
class RegionInfo:
    """Ergebnis von describe_region(): das Arbeits-CRS des Laufs und die
    geprüften Eckdaten der Region (FA55/FA56). ``bbox`` in EPSG:4326,
    ``area_km2`` planar im Arbeits-CRS."""

    crs: CRS
    crs_mode: Literal["auto", "declared"]
    crs_spec: str
    bbox: tuple[float, float, float, float]
    area_km2: float
    display_name: str | None = None
    osm_class: str | None = None
    osm_type: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        epsg = self.crs.to_epsg()
        return {
            "crs": f"EPSG:{epsg}" if epsg is not None else self.crs.to_string(),
            "crs_label": crs_label(self.crs),
            "crs_mode": self.crs_mode,
            "crs_spec": self.crs_spec,
            "bbox": [float(v) for v in self.bbox],
            "area_km2": float(self.area_km2),
            "display_name": self.display_name,
            "osm_class": self.osm_class,
            "osm_type": self.osm_type,
            "warnings": list(self.warnings),
        }


def _region_geometry(region: GeoDataFrame) -> BaseGeometry:
    if region.crs is not None and region.crs.to_epsg() != 4326:
        region = region.to_crs("EPSG:4326")
    return region.union_all()


def working_crs(region: GeoDataFrame, spec: str = AUTO) -> tuple[CRS, list[str]]:
    """Arbeits-CRS aus ``scenario.crs`` (FA55): ``auto`` ist die UTM-Zone aus dem
    Zentroid (FA5), mit Warnung bei einer Region breiter als eine Zone und Fehler
    jenseits 84 Grad Breite. Sonst das deklarierte CRS, projiziert in Metern
    (ValueError). Liefert ``(crs, warnungen)``."""
    declared = parse_crs_spec(spec)
    if declared is not None:
        require_projected_metric(declared, what=str(spec).strip())
        return declared, []

    geometry = _region_geometry(region)
    centroid = geometry.centroid
    if abs(centroid.y) > UTM_MAX_ABS_LAT_DEG:
        raise ValueError(
            f"Region liegt bei {centroid.y:.1f} Grad Breite - UTM (scenario.crs: auto) ist "
            f"nur bis {UTM_MAX_ABS_LAT_DEG:g} Grad definiert; {_HINT_EQUAL_AREA}"
        )
    crs = utm_zone_epsg(geometry)
    warnings: list[str] = []
    min_lon, _, max_lon, _ = geometry.bounds
    span = max_lon - min_lon
    if span > UTM_ZONE_WIDTH_DEG:
        warnings.append(
            f"Region ist {span:.1f} Grad breit (Länge {min_lon:g}..{max_lon:g}), eine UTM-Zone "
            f"nur {UTM_ZONE_WIDTH_DEG:g} Grad - Flächen und Abstände am Rand sind unter "
            f"scenario.crs: auto ({crs_label(crs)}) verzerrt; für zonenübergreifende "
            "Regionen scenario.crs: equal_area oder equal_area_europe deklarieren"
        )
    return crs, warnings


def _project_region(geometry: BaseGeometry, crs: CRS) -> BaseGeometry:
    densified = shapely.segmentize(geometry, _CHECK_SEGMENT_DEG)
    return gpd.GeoSeries([densified], crs="EPSG:4326").to_crs(crs).iloc[0]


def describe_region(
    region: GeoDataFrame, spec: str = AUTO, meta: dict | None = None
) -> RegionInfo:
    """Prüft die aufgelöste Region gegen das Arbeits-CRS (FA55/FA56): endlich,
    gültig und mit Fläche > 0 abbildbar, sonst ValueError mit Hinweis auf
    ``scenario.crs: equal_area``. ``meta`` sind die Nominatim-Metadaten; eine
    OSM-Klasse außerhalb boundary/place ist eine Warnung. Warnungen stehen in
    ``RegionInfo.warnings``, hier wird nichts ausgegeben."""
    crs, warnings = working_crs(region, spec)
    geometry = _region_geometry(region)
    label = crs_label(crs)
    try:
        projected = _project_region(geometry, crs)
    except Exception as exc:  # noqa: BLE001 - pyproj-Fehler werden zur Regionsmeldung
        raise ValueError(
            f"Region lässt sich nicht nach {label} abbilden ({exc}); {_HINT_EQUAL_AREA}"
        ) from exc
    coords = shapely.get_coordinates(projected)
    finite = coords.size > 0 and bool(np.isfinite(coords).all())
    area_m2 = float(projected.area) if finite else math.nan
    if not finite or not projected.is_valid or not area_m2 > 0:
        reason = (
            "nicht endliche Koordinaten"
            if not finite
            else "ungültige Geometrie"
            if not projected.is_valid
            else "Fläche 0"
        )
        bounds = ", ".join(f"{v:g}" for v in geometry.bounds)
        raise ValueError(
            f"Region ({bounds}) ist in {label} nicht abbildbar ({reason}) - das Arbeits-CRS "
            f"deckt die Region nicht ab; {_HINT_EQUAL_AREA}"
        )
    area_km2 = area_m2 / 1e6

    meta = meta or {}
    display_name = meta.get("display_name")
    osm_class = meta.get("class")
    osm_type = meta.get("type")
    if osm_class and osm_class not in EXPECTED_OSM_CLASSES:
        warnings.append(
            f"Region '{display_name or '?'}' hat die OSM-Klasse {osm_class}/{osm_type or '?'} "
            f"(erwartet: {' oder '.join(sorted(EXPECTED_OSM_CLASSES))}), Fläche "
            f"{area_km2:.1f} km2 - ist das die gemeinte Region? Sonst genaueren Namen oder "
            "eine BBox angeben"
        )
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in geometry.bounds)
    return RegionInfo(
        crs=crs,
        crs_mode="auto" if parse_crs_spec(spec) is None else "declared",
        crs_spec=str(spec).strip(),
        bbox=(min_lon, min_lat, max_lon, max_lat),
        area_km2=area_km2,
        display_name=display_name,
        osm_class=osm_class,
        osm_type=osm_type,
        warnings=warnings,
    )
