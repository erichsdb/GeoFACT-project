"""Implements: FA55 (Arbeits-CRS), FA56 (BBox-Literal), FA57 (Koordinatenplausibilität).

Reine Hilfsfunktionen rund um Koordinatenreferenzsysteme (Ring 0, nur
pyproj/numpy/geopandas/stdlib):

- ``parse_crs_spec``            Angabe ``scenario.crs`` -> CRS (``auto`` -> None)
- ``require_projected_metric``  Arbeits-CRS muss projiziert sein, Einheit Meter
- ``crs_label``                 lesbare Bezeichnung ``EPSG:25833 (ETRS89 / UTM zone 33N)``
- ``epsg_code``                 ``to_epsg()`` mit Zwischenspeicher je WKT (Provenienz FA58)
- ``parse_bbox_literal`` /      BBox-Literal ``minlon,minlat,maxlon,maxlat`` erkennen
  ``check_bbox``                und auf Plausibilität prüfen (FA56)
- ``coordinate_plausibility``   passen Rohkoordinaten zum deklarierten CRS? (FA57)
- ``nonfinite_vertices``        nicht endliche Stützpunkte nach einer Reprojektion

Die Presets (``CRS_PRESETS``) sind Formatvokabular des Szenarios, keine
Bausteinliste (NFA4): ein weiteres Arbeits-CRS braucht keinen Eintrag hier,
jede pyproj-Angabe wie ``EPSG:25833`` ist erlaubt."""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Mapping, Sequence

import numpy as np
from geopandas import GeoDataFrame
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError

AUTO = "auto"
"""Default von ``scenario.crs``: UTM-Zone aus dem Regionszentroid (FA5)."""

CRS_PRESETS: Mapping[str, str] = MappingProxyType(
    {
        "equal_area": "EPSG:6933",
        "equal_area_europe": "EPSG:3035",
    }
)
"""Benannte Arbeits-CRS: weltweit flächentreu (EASE-Grid 2.0) und
flächentreu für Europa (ETRS89-LAEA)."""

_METRE_NAMES = frozenset({"metre", "meter", "m"})
_AREA_OF_USE_MARGIN_DEG = 2.0
_MAX_ROWS_REPORTED = 10


# --- Arbeits-CRS (FA55) ---


def parse_crs_spec(spec: str) -> CRS | None:
    """``auto`` -> None (die Engine leitet die UTM-Zone ab, FA5), ein Preset
    -> sein CRS, sonst die pyproj-Angabe. Unbekannte Angaben sind ein
    ``ValueError`` mit der Liste der erlaubten Formen."""
    text = str(spec).strip()
    if text == AUTO:
        return None
    target = CRS_PRESETS.get(text, text)
    try:
        return CRS.from_user_input(target)
    except CRSError as exc:
        raise ValueError(
            f"unbekanntes CRS '{text}' (pyproj: {exc}); erlaubt sind {AUTO}, "
            f"{', '.join(CRS_PRESETS)} oder eine Angabe wie EPSG:25833"
        ) from None


_EPSG_CACHE: dict[str, int | None] = {}
_EPSG_CACHE_MAX = 512


def epsg_code(crs: CRS) -> int | None:
    """``crs.to_epsg()`` mit Zwischenspeicher je WKT. Bei einem CRS ohne EPSG-Code
    (z. B. Mollweide ESRI:54009) durchsucht pyproj jedes Mal die PROJ-Datenbank
    (rund 60 ms), und Provenienz, Beschriftung und Plausibilitätsprüfung fragen
    dasselbe CRS mehrfach."""
    key = crs.to_wkt()
    if key is None:  # pragma: no cover - pyproj liefert für gültige CRS immer WKT
        return crs.to_epsg()
    if key not in _EPSG_CACHE:
        if len(_EPSG_CACHE) >= _EPSG_CACHE_MAX:
            _EPSG_CACHE.clear()
        _EPSG_CACHE[key] = crs.to_epsg()
    return _EPSG_CACHE[key]


def _short_name(crs: CRS) -> str:
    epsg = epsg_code(crs)
    return f"EPSG:{epsg}" if epsg is not None else (crs.name or crs.to_string())


def require_projected_metric(crs: CRS, *, what: str) -> None:
    """Das Arbeits-CRS rechnet in Metern: ein geographisches CRS oder eine andere
    Einheit ist ein ``ValueError``. ``what`` ist die Nutzerangabe für die Meldung."""
    label = what or _short_name(crs)
    if crs.is_geographic:
        raise ValueError(
            f"'{label}' ist ein geographisches CRS (Einheit Grad) - das Arbeits-CRS muss "
            f"projiziert sein (Einheit Meter), z. B. {AUTO}, equal_area oder EPSG:25833"
        )
    if not crs.is_projected:
        raise ValueError(
            f"'{label}' ist kein projiziertes CRS - das Arbeits-CRS muss projiziert sein "
            f"(Einheit Meter), z. B. {AUTO}, equal_area oder EPSG:25833"
        )
    units = {axis.unit_name for axis in crs.axis_info[:2]}
    if not units or not units <= _METRE_NAMES:
        unit = (
            ", ".join(sorted(u for u in units if u not in _METRE_NAMES)) or "unbekannt"
        )
        raise ValueError(
            f"'{label}' hat die Einheit '{unit}', das Arbeits-CRS braucht Meter"
        )


def crs_label(crs: CRS | None) -> str:
    """``EPSG:31468 (DHDN / 3-degree Gauss-Kruger zone 4)``; ohne EPSG-Code
    nur der Name; None -> ``ohne CRS``."""
    if crs is None:
        return "ohne CRS"
    epsg = epsg_code(crs)
    name = crs.name or crs.to_string()
    return f"EPSG:{epsg} ({name})" if epsg is not None else name


# --- BBox-Literal (FA56) ---


def parse_bbox_literal(text: str) -> tuple[float, float, float, float] | None:
    """Vier durch Komma getrennte Zahlen -> Tupel, sonst None (dann ist die Region
    ein Name). NaN/inf nimmt es noch an; ``check_bbox`` lehnt sie klar ab, statt sie
    als Ortsnamen zu behandeln."""
    if not isinstance(text, str):
        return None
    parts = text.split(",")
    if len(parts) != 4:
        return None
    try:
        values = tuple(float(part) for part in parts)
    except ValueError:
        return None
    return values  # type: ignore[return-value]


def _bbox_text(values: Sequence[float]) -> str:
    return ",".join(f"{value:g}" for value in values)


def check_bbox(values: tuple[float, ...], *, text: str | None = None) -> None:
    """Plausibilität eines BBox-Literals in EPSG:4326: endliche Werte, Wertebereich,
    Reihenfolge der Breiten, kein Antimeridian (``minlon > maxlon``, Grenze der
    Arbeit) und Ausdehnung > 0. ``text`` ist das Original für die Meldung."""
    shown = text if text is not None else _bbox_text(values)
    if len(values) != 4:
        raise ValueError(
            f"BBox '{shown}': genau vier Werte minlon,minlat,maxlon,maxlat erwartet"
        )
    if not all(math.isfinite(v) for v in values):
        raise ValueError(f"BBox '{shown}': alle vier Werte müssen endliche Zahlen sein")
    min_lon, min_lat, max_lon, max_lat = values
    if not (
        -180 <= min_lon <= 180
        and -180 <= max_lon <= 180
        and -90 <= min_lat <= 90
        and -90 <= max_lat <= 90
    ):
        raise ValueError(
            f"BBox '{shown}': Werte außerhalb des Bereichs (lon -180..180, lat -90..90); "
            "Reihenfolge ist minlon,minlat,maxlon,maxlat"
        )
    if max_lat < min_lat:
        raise ValueError(
            f"BBox '{shown}': maxlat ({max_lat:g}) ist kleiner als minlat ({min_lat:g})"
        )
    if min_lon > max_lon:
        raise ValueError(
            f"BBox '{shown}': minlon > maxlon - Regionen über den Antimeridian werden nicht "
            "unterstützt (Grenze der Arbeit); Region teilen oder bis 180 begrenzen"
        )
    if min_lon == max_lon or min_lat == max_lat:
        raise ValueError(f"BBox '{shown}' hat keine Ausdehnung")


# --- Koordinatenplausibilität (FA57) ---


def coordinate_plausibility(
    bounds: Sequence[float] | None, crs: CRS | None
) -> tuple[list[str], list[str]]:
    """Passen die Rohkoordinaten ``bounds`` (minx, miny, maxx, maxy) zum deklarierten
    CRS? Liefert ``(errors, warnings)``:

    - geographisches CRS mit |x| > 180 oder |y| > 90 -> Fehler (metrische
      Koordinaten unter EPSG:4326)
    - projiziertes CRS außerhalb seines ``area_of_use`` (+2 Grad) -> Warnung

    Leere oder nicht endliche Ausdehnung und ``crs`` None werden nicht bewertet."""
    errors: list[str] = []
    warnings: list[str] = []
    if crs is None or bounds is None or len(bounds) != 4:
        return errors, warnings
    values = [float(v) for v in bounds]
    if not all(math.isfinite(v) for v in values):
        return errors, warnings
    min_x, min_y, max_x, max_y = values
    shown = f"({min_x:g}, {min_y:g}, {max_x:g}, {max_y:g})"
    name = _short_name(crs)
    if crs.is_geographic:
        if max(abs(min_x), abs(max_x)) > 180 or max(abs(min_y), abs(max_y)) > 90:
            errors.append(
                f"Koordinaten passen nicht zu {name}: Ausdehnung {shown} liegt außerhalb "
                "lon -180..180 / lat -90..90 - die Daten sind vermutlich projiziert (Meter); "
                "Quell-CRS am Layer mit 'crs' angeben, bei falscher Dateiangabe zusätzlich "
                "'crs_override: true'"
            )
        return errors, warnings
    area = crs.area_of_use
    if area is None:
        return errors, warnings
    try:
        transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
        lon_min, lat_min, lon_max, lat_max = transformer.transform_bounds(
            min_x, min_y, max_x, max_y
        )
    except Exception:  # noqa: BLE001 - nicht transformierbar: als Warnung melden, nie still
        warnings.append(
            f"Koordinaten {shown} lassen sich aus {name} nicht nach EPSG:4326 umrechnen - "
            "Quell-CRS prüfen"
        )
        return errors, warnings
    margin = _AREA_OF_USE_MARGIN_DEG
    corners = [lon_min, lat_min, lon_max, lat_max]
    outside = (
        not all(math.isfinite(v) for v in corners)
        or lon_max < area.west - margin
        or lon_min > area.east + margin
        or lat_max < area.south - margin
        or lat_min > area.north + margin
    )
    if outside:
        warnings.append(
            f"Koordinaten {shown} liegen außerhalb des Gültigkeitsbereichs von {name} "
            f"({area.west:g}, {area.south:g}, {area.east:g}, {area.north:g} Grad) - "
            "passt das deklarierte Quell-CRS?"
        )
    return errors, warnings


def nonfinite_vertices(gdf: GeoDataFrame) -> tuple[int, list[int]]:
    """Anzahl nicht endlicher Stützpunkte (NaN/inf) und die 0-basierten Zeilen der
    ersten betroffenen Objekte (höchstens 10)."""
    if len(gdf) == 0:
        return 0, []
    coords = gdf.geometry.reset_index(drop=True).get_coordinates()
    if coords.empty:
        return 0, []
    bad = ~np.isfinite(coords.to_numpy(dtype=float)).all(axis=1)
    count = int(bad.sum())
    if count == 0:
        return 0, []
    rows = sorted({int(i) for i in coords.index[bad]})[:_MAX_ROWS_REPORTED]
    return count, rows
