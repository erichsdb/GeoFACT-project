"""Implements: FA3 (OSM-Konnektor), Quellart 'osm' für FA40.

Bezieht Geodaten anhand konfigurierter Tag-Filter über ein austauschbares Backend
(Overpass API oder lokale PostGIS-Instanz). Die Wahl trifft GEOFACT_OSM_BACKEND
(Default: overpass), Laufzeitkonfiguration und nicht Teil der Szenario-YAML.

Implements: FA82 - ein einzelner Lauf kann das Backend über die Konnektor-Option
``osm.backend`` (``LoadContext.options``, gesetzt via ``api.run(connector_options=...)``)
abweichend wählen.

``tags`` ist ein Tag-Set (dict, AND) oder eine Liste alternativer Tag-Sets (OR); je
Alternative läuft eine Abfrage, die Ergebnisse werden vereinigt. Tag-Filter gelten
als nicht vertrauenswürdig (im Web kommen Szenarien aus Editor oder LLM): PostGIS
bindet Schlüssel, Werte und BBox als Parameter (nur der Tabellenname steht im SQL,
gegen ein enges Muster geprüft), Overpass maskiert Stringliterale und lehnt
Steuerzeichen ab. ``geometry`` und ``columns`` (FA3-Zusatz) beschränken Geometriearten
bzw. Tag-Spalten.

PostgisBackend baut SQL gegen das Schema des PostGIS-Imports (osm2pgsql flex
output: osm_points/osm_lines/osm_polygons, jsonb tags, EPSG:4326, Geometrie als WKB).
Beide Backends filtern per BBox vor und danach mit ``within_region`` auf die
Regionsgeometrie (FA3-Nachbedingung, wegen nicht-rechteckiger Regionen).

Relationen: Overpass liefert Multipolygon-/Grenz-Relationen als Mitgliedswege, die
``_relation_to_geometry`` zu (Multi)Polygonen zusammensetzt; nicht zusammensetzbare
oder Nicht-Flächenrelationen meldet eine Sammelwarnung (``OsmRelationWarning``).
PostGIS reicht die Flächen aus ``osm_polygons`` unverändert durch.

Overpass meldet Laufzeitfehler mit HTTP 200 und einem Teilergebnis in ``remark``
(``OverpassPartialResponse``): der Versuch wird wiederholt, ein Teilergebnis wird nie
zurückgegeben und nie als Snapshot gespeichert. 0 Objekte melden
``EmptyOsmResultWarning``. Der Snapshot (FA7) führt das Backend im Schlüssel, wenn es
nicht Overpass ist, und ``last_meta`` (Backend, Datenstand). Default-Lizenz: ODbL-1.0 (FA65).

Importausdehnung (FA56): der PostGIS-Import deckt nur sein Gebiet ab. Eine Region ganz
außerhalb ist ein Fehler, teilweise außerhalb eine ``RegionWarning``, ohne
Tabellenstatistik ein Hinweis (``PostgisExtentNotice``). ``backend_status()["extent"]``
nennt die Ausdehnung für den Health-Endpunkt.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import warnings
from collections import Counter
from dataclasses import dataclass
from functools import reduce
from typing import Any, Literal, Optional, Protocol, Sequence, Union

import geopandas as gpd
import requests
import shapely
from geopandas import GeoDataFrame
from pydantic import Field, field_validator
from shapely.geometry import LineString, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import polygonize_full, unary_union
from sqlalchemy import create_engine, text

from geofact.builtin._licenses import OSM_LICENSE, License
from geofact.builtin._region_filter import within_region
from geofact.core.errors import RegionWarning
from geofact.plugin_api import (
    DataType,
    LayerBase,
    LayerValidation,
    LoadContext,
    register_source,
)
from geofact.support import snapshot as snapshot_module


# --- Layer-Modell (Vertrag der Quellart, FA40) ---

GeometryKind = Literal["point", "line", "polygon"]
GEOMETRY_KINDS: tuple[str, ...] = ("point", "line", "polygon")


class OsmLayer(LayerBase):
    """tags: entweder ein einzelnes Tag-Set (dict, alle Paare per AND
    verknüpft - z. B. {power: substation}) oder eine Liste alternativer
    Tag-Sets (list[dict], die Sets per OR verknüpft - z. B. Parks ODER
    Gärten ODER Dorfangerflächen: [{leisure: park}, {leisure: garden},
    {landuse: village_green}]). Jedes Element der Liste ist selbst ein
    dict mit AND-Semantik, wie ein einzelnes Tag-Set."""

    id: str
    source: Literal["osm"] = "osm"
    tags: Union[dict[str, str], list[dict[str, str]]]
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR
    validation: Optional[LayerValidation] = None
    geometry: Optional[Union[GeometryKind, list[GeometryKind]]] = Field(
        None,
        description="Nur diese Geometriearten liefern (point | line | polygon oder eine Liste); "
        "Standard: alle",
    )
    columns: Optional[list[str]] = Field(
        None,
        min_length=1,
        description="Whitelist der Tag-Spalten; die Schlüssel der Tag-Filter und die Geometrie "
        "sind immer dabei. Standard: alle Tags",
    )

    @field_validator("tags")
    @classmethod
    def tags_not_empty(cls, v) -> Union[dict, list]:
        if not v:
            raise ValueError("OSM-Layer benötigt mindestens einen Tag-Filter")
        if isinstance(v, list) and not all(
            isinstance(item, dict) and item for item in v
        ):
            raise ValueError(
                "OSM-Layer: jede Tag-Alternative muss ein nicht-leeres dict sein"
            )
        return v

    @field_validator("geometry")
    @classmethod
    def geometry_not_empty(cls, v):
        if isinstance(v, list) and not v:
            raise ValueError(
                "OSM-Layer: geometry braucht mindestens eine Art (point, line, polygon)"
            )
        return v

    @field_validator("columns")
    @classmethod
    def columns_are_tag_keys(cls, v):
        if v is None:
            return v
        for column in v:
            if not column.strip():
                raise ValueError("OSM-Layer: columns enthält einen leeren Spaltennamen")
            if column == "geometry":
                raise ValueError(
                    "OSM-Layer: 'geometry' ist immer enthalten und gehört nicht in columns"
                )
        return v

    def geometry_kinds(self) -> Optional[tuple[str, ...]]:
        """Gewählte Geometriearten ohne Dubletten in fester Reihenfolge; None = alle."""
        if self.geometry is None:
            return None
        chosen = (
            {self.geometry} if isinstance(self.geometry, str) else set(self.geometry)
        )
        return tuple(kind for kind in GEOMETRY_KINDS if kind in chosen)

    def default_license(self) -> Optional[License]:
        """FA65: Daten aus OpenStreetMap stehen unter der ODbL-1.0."""
        return OSM_LICENSE


# Per GEOFACT_OVERPASS_URL überschreibbar; die Umgebung wird erst in
# OverpassBackend.__init__() gelesen, nicht beim Import.
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Die öffentliche Instanz antwortet unter Last oft mit 504/429. Ohne eigene URL
# wechseln die Versuche reihum zwischen diesen Instanzen (FA3).
OVERPASS_FALLBACK_URLS = (
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
)

TagFilter = Union[dict[str, str], list[dict[str, str]]]


def _tag_alternatives(tags: TagFilter) -> list[dict[str, str]]:
    """tags als Liste von Tag-Sets (ein einzelnes dict wird zur Liste mit einem Element)."""
    return tags if isinstance(tags, list) else [tags]


# Wildcard-Tagwert: "Tag ist gesetzt, Wert beliebig" (FA30). Beide Backends
# übersetzen ihn in eine Existenzprüfung; ein Vergleich gegen den String "*"
# lieferte still 0 Objekte.
TAG_WILDCARD = "*"


def _is_wildcard(value: str) -> bool:
    return value == TAG_WILDCARD


# Verfeinert ein per BBox vorgefiltertes Ergebnis auf die Regionsgeometrie
# (FA3-Nachbedingung); der gemeinsame Regionsfilter aller Konnektoren.
_refine_to_region = within_region


_GEOMETRY_KIND_OF_TYPE = {
    "Point": "point",
    "MultiPoint": "point",
    "LineString": "line",
    "MultiLineString": "line",
    "LinearRing": "line",
    "Polygon": "polygon",
    "MultiPolygon": "polygon",
}


def _output_columns(tags: TagFilter, columns: Sequence[str]) -> list[str]:
    """Spalten der Whitelist: zuerst die Schlüssel der Tag-Filter (in der
    Reihenfolge ihres Auftretens), dann ``columns`` - ohne Dubletten."""
    ordered: dict[str, None] = {}
    for tag_set in _tag_alternatives(tags):
        ordered.update(dict.fromkeys(tag_set))
    ordered.update(dict.fromkeys(columns))
    return list(ordered)


def _apply_tag_predicates(
    rows: list[dict],
    geometries: list,
    *,
    tags: TagFilter,
    geometry: Optional[Sequence[str]] = None,
    columns: Optional[Sequence[str]] = None,
) -> tuple[list[dict], list, Optional[list[str]]]:
    """Nachfilter auf den Zeilen-Dicts vor dem GeoDataFrame (FA3-Zusatz): ``geometry``
    behält nur die gewählten Arten, ``columns`` kürzt die Tags auf die Whitelist.
    Liefert (rows, geometries, Spaltenliste oder None)."""
    if geometry is not None:
        allowed = set(geometry)
        kept = [
            (row, geom)
            for row, geom in zip(rows, geometries)
            if geom is not None
            and _GEOMETRY_KIND_OF_TYPE.get(geom.geom_type) in allowed
        ]
        rows, geometries = [row for row, _ in kept], [geom for _, geom in kept]
    if columns is None:
        return rows, geometries, None
    keep = _output_columns(tags, columns)
    keep_set = set(keep)
    return (
        [{k: v for k, v in row.items() if k in keep_set} for row in rows],
        geometries,
        keep,
    )


def _frame(
    rows: list[dict], geometries: list, columns: Optional[list[str]]
) -> GeoDataFrame:
    """GeoDataFrame in EPSG:4326; mit Whitelist in deren Reihenfolge und mit
    jeder gelisteten Spalte, auch wenn kein Objekt sie trägt."""
    if columns is None:
        return gpd.GeoDataFrame(rows, geometry=geometries, crs="EPSG:4326")
    return gpd.GeoDataFrame(rows, columns=columns, geometry=geometries, crs="EPSG:4326")


class OsmBackend(Protocol):
    """Austauschbares Backend für FA3 (Overpass API | lokale PostGIS-
    Instanz). Die Layer-Struktur ist backend-unabhängig: beide liefern
    ein GeoDataFrame in EPSG:4326 mit den OSM-Tags als Spalten -
    Folgeoperationen brauchen kein Wissen über die Herkunft (FA3-
    Nachbedingung). tags: ein Tag-Set (AND) oder eine Liste
    alternativer Tag-Sets (OR über die Sets)."""

    def fetch(
        self,
        tags: dict[str, str] | list[dict[str, str]],
        region: GeoDataFrame,
        geometry: Optional[Sequence[str]] = None,
        columns: Optional[Sequence[str]] = None,
    ) -> GeoDataFrame: ...

    # ``geometry``/``columns`` werden nur übergeben, wenn gesetzt, damit Backends mit
    # ``fetch(tags, region)`` nutzbar bleiben. Optional: ``name`` (Standard overpass)
    # und ``last_meta`` (Quelleninfo des letzten Abrufs, FA7).


# --- OverpassBackend ---

# Escape-Sequenzen eines Overpass-QL-Stringliterals; jedes andere Steuerzeichen
# ist nicht darstellbar und wird abgelehnt statt verändert.
_OVERPASS_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\t": "\\t"}


def _overpass_literal(value: str, what: str) -> str:
    """Overpass-QL-Stringliteral für einen Tag-Schlüssel oder -Wert. Ein ``"`` im Wert
    beendete sonst das Literal und der Rest stünde als Anweisung; ``\\`` und ``"``
    werden maskiert, nicht darstellbare Steuerzeichen scheitern mit Meldung."""
    if not isinstance(value, str):
        raise ValueError(f"OSM-Tag-Filter: {what} muss ein Text sein, nicht {value!r}")
    parts = []
    for char in value:
        if char in _OVERPASS_ESCAPES:
            parts.append(_OVERPASS_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            raise ValueError(
                f"OSM-Tag-Filter: {what} {value!r} enthält das Steuerzeichen "
                f"U+{ord(char):04X}, das in einer Overpass-Abfrage nicht darstellbar ist"
            )
        else:
            parts.append(char)
    return '"' + "".join(parts) + '"'


def _overpass_tag_filter(key: str, value: str) -> str:
    """Ein Overpass-Tagfilter ``["key"="value"]``; Wildcard (FA30) wird zu ``["key"]``."""
    quoted_key = _overpass_literal(key, "Schlüssel")
    if _is_wildcard(value):
        return f"[{quoted_key}]"
    return f"[{quoted_key}={_overpass_literal(value, f'Wert zu {key!r}')}]"


def _checked_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) mit endlichen Zahlen; eine leere
    oder ungültige Region liefert sonst ``nan`` und damit eine kaputte Abfrage."""
    values = tuple(float(v) for v in bbox)
    if len(values) != 4 or not all(math.isfinite(v) for v in values):
        raise ValueError(
            f"OSM-Abfrage: die Regionsgrenzen {tuple(bbox)!r} sind keine endliche BBox "
            "(min_lon, min_lat, max_lon, max_lat) - ist die Region leer?"
        )
    return values


_OVERPASS_ELEMENTS = {"point": ("node",), "line": ("way",), "polygon": ("way", "rel")}


def _overpass_element_types(geometry: Optional[Sequence[str]]) -> tuple[str, ...]:
    """Overpass-Elementarten für die gewählten Geometriearten, ohne Wahl ``nwr``.
    Flächen sind geschlossene Wege und Multipolygon-/Grenz-Relationen."""
    if geometry is None:
        return ("nwr",)
    wanted = {element for kind in geometry for element in _OVERPASS_ELEMENTS[kind]}
    return tuple(element for element in ("node", "way", "rel") if element in wanted)


def _build_overpass_query(
    tags: TagFilter,
    bbox: tuple[float, float, float, float],
    timeout: int = 60,
    geometry: Optional[Sequence[str]] = None,
) -> str:
    min_lon, min_lat, max_lon, max_lat = _checked_bbox(bbox)
    bbox_str = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    timeout = int(timeout)
    if timeout <= 0:
        raise ValueError(f"OSM-Abfrage: timeout muss positiv sein, nicht {timeout}")
    statements = []
    element_types = _overpass_element_types(geometry)
    for tag_set in _tag_alternatives(tags):
        if not tag_set:
            # ohne Filter lieferte nwr(bbox) alle Objekte der BBox
            raise ValueError("OSM-Tag-Filter: ein Tag-Set darf nicht leer sein")
        tag_filters = "".join(
            _overpass_tag_filter(key, value) for key, value in tag_set.items()
        )
        statements.extend(
            f"{element}{tag_filters}({bbox_str});" for element in element_types
        )
    return (
        f"[out:json][timeout:{timeout}];\n"
        "(\n  " + "\n  ".join(statements) + "\n);\n"
        "out geom;"
    )


class OsmRelationWarning(UserWarning):
    """Relationen, die der Overpass-Pfad nicht als Fläche zusammensetzen
    konnte oder die keine Flächenrelation sind (eine Sammelwarnung je Abruf)."""


_AREA_RELATION_TYPES = ("multipolygon", "boundary")


def _relation_to_geometry(element: dict) -> tuple[BaseGeometry | None, bool]:
    """(Multi)Polygon einer Multipolygon-/Grenz-Relation aus den Mitgliedswegen
    (``out geom``) und ob alle Ringe geschlossen waren.

    Außen- (Rolle ``outer`` oder leer) und Innenwege werden zu Ringen verbunden; die
    Fläche folgt der Gerade-Ungerade-Regel (symmetrische Differenz aller Ringflächen),
    ein Ring im Ring ist ein Loch. None, wenn kein Ring geschlossen werden konnte."""
    outer: list[LineString] = []
    inner: list[LineString] = []
    for member in element.get("members", []):
        if member.get("type") != "way":
            continue
        coords = [(pt["lon"], pt["lat"]) for pt in member.get("geometry") or []]
        if len(coords) < 2:
            continue
        role = member.get("role", "")
        if role in ("outer", ""):
            outer.append(LineString(coords))
        elif role == "inner":
            inner.append(LineString(coords))
    if not outer:
        return None, False

    polygons: list[BaseGeometry] = []
    closed = True
    for lines in (outer, inner):
        if not lines:
            continue
        faces, cuts, dangles, invalid = polygonize_full(unary_union(lines))
        # polygonize liefert verschachtelte Ringe als Fläche MIT Loch; für die
        # Gerade-Ungerade-Regel braucht jeder Ring seine volle Fläche.
        polygons.extend(Polygon(face.exterior) for face in faces.geoms)
        if not (cuts.is_empty and dangles.is_empty and invalid.is_empty):
            closed = False
    if not polygons:
        return None, False
    area = reduce(lambda a, b: a.symmetric_difference(b), polygons)
    parts = [
        g
        for g in getattr(area, "geoms", [area])
        if g.geom_type in ("Polygon", "MultiPolygon")
    ]
    if not parts:
        return None, False
    return unary_union(parts), closed


def _overpass_element_to_geometry(element: dict):
    if element["type"] == "node":
        return shape({"type": "Point", "coordinates": [element["lon"], element["lat"]]})
    if element["type"] == "relation":
        if element.get("tags", {}).get("type") not in _AREA_RELATION_TYPES:
            return None
        return _relation_to_geometry(element)[0]
    if "geometry" not in element:
        return None
    coords = [(pt["lon"], pt["lat"]) for pt in element["geometry"]]
    if element["type"] == "way":
        if coords and coords[0] == coords[-1] and len(coords) > 3:
            return shape({"type": "Polygon", "coordinates": [coords]})
        return shape({"type": "LineString", "coordinates": coords})
    return None


class OverpassPartialResponse(RuntimeError):
    """Overpass hat mit HTTP 200 nur ein Teilergebnis geliefert und den
    Laufzeitfehler im Feld ``remark`` gemeldet (Timeout, Speicher). Wird
    wiederholt und nie zurückgegeben - also auch nie als Snapshot gespeichert."""


class EmptyOsmResultWarning(UserWarning):
    """Ein OSM-Layer hat 0 Objekte geliefert (Tag-Schreibweise, Region prüfen)."""


class OsmRemarkWarning(UserWarning):
    """Overpass hat eine Bemerkung ohne Laufzeitfehler mitgeschickt."""


def _check_remark(payload: dict) -> None:
    remark = payload.get("remark")
    if not remark:
        return
    if "runtime error" in str(remark).lower():
        raise OverpassPartialResponse(str(remark))
    warnings.warn(f"Overpass-Bemerkung: {remark}", OsmRemarkWarning, stacklevel=3)


class OverpassBackend:
    name = "overpass"

    def __init__(
        self, url: str | None = None, timeout: int = 60, max_retries: int | None = None
    ) -> None:
        explicit = url or os.environ.get("GEOFACT_OVERPASS_URL")
        self.urls: list[str] = (
            [explicit] if explicit else [OVERPASS_URL, *OVERPASS_FALLBACK_URLS]
        )
        self.url = self.urls[0]
        self.timeout = timeout
        # Mehrere Instanzen: zwei Runden, bei genau einer 3 Versuche.
        self.max_retries = (
            max_retries if max_retries is not None else max(3, 2 * len(self.urls))
        )
        self.last_meta: dict[str, Any] = {}

    def build_query(
        self,
        tags: TagFilter,
        region: GeoDataFrame,
        geometry: Optional[Sequence[str]] = None,
    ) -> str:
        bbox = region.total_bounds
        # Query-Timeout und HTTP-Timeout (self.timeout) müssen übereinstimmen, sonst
        # bricht der Client vor dem Server ab oder wartet auf eine beendete Abfrage.
        return _build_overpass_query(
            tags, tuple(bbox), timeout=self.timeout, geometry=geometry
        )

    def fetch(
        self,
        tags: TagFilter,
        region: GeoDataFrame,
        geometry: Optional[Sequence[str]] = None,
        columns: Optional[Sequence[str]] = None,
    ) -> GeoDataFrame:
        query = self.build_query(tags, region, geometry=geometry)
        last_error: Exception | None = None
        last_status: int | None = None
        headers = {"User-Agent": "GeoFACT/0.1 (Kontakt siehe README)"}
        for attempt in range(self.max_retries):
            try:
                url = self.urls[attempt % len(self.urls)]
                response = requests.post(
                    url,
                    data={"data": query},
                    headers=headers,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                payload = response.json()
                _check_remark(payload)
                data = self._parse_response(
                    payload, tags, geometry=geometry, columns=columns
                )
                self.last_meta = {
                    "backend": self.name,
                    "osm_base": (payload.get("osm3s") or {}).get("timestamp_osm_base"),
                    "endpoint": url,
                }
                return within_region(data, region)
            except (
                requests.RequestException,
                ValueError,
                OverpassPartialResponse,
            ) as exc:
                last_error = exc
                last_status = getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
        raise RuntimeError(
            self._exhausted_retries_message(last_status, last_error)
        ) from last_error

    def _exhausted_retries_message(
        self, status: int | None, error: Exception | None
    ) -> str:
        """Fehlermeldung nach ausgeschöpften Retries; bei 429/504 (Ueberlastung der
        öffentlichen API) wird das PostGIS-Backend vorgeschlagen."""
        base = f"Overpass API nicht erreichbar nach {self.max_retries} Versuchen"
        if len(self.urls) > 1:
            base += f" (Instanzen: {', '.join(self.urls)})"
        if isinstance(error, OverpassPartialResponse):
            return (
                f"Overpass lieferte nach {self.max_retries} Versuchen nur ein Teilergebnis "
                f"(Laufzeitfehler: {error}) - es wird weder verwendet noch als Snapshot "
                "gespeichert. Region oder Tag-Filter enger fassen, später erneut versuchen "
                "oder GEOFACT_OSM_BACKEND=postgis nutzen."
            )
        if status == 429:
            return (
                f"{base}: Server überlastet (HTTP 429, Rate-Limit). "
                "Später erneut versuchen oder GEOFACT_OSM_BACKEND=postgis "
                "nutzen, falls ein lokaler PostGIS-Dump konfiguriert ist. "
                f"Letzter Fehler: {error}"
            )
        if status == 504:
            return (
                f"{base}: Gateway-Timeout (HTTP 504, Server überlastet oder "
                "Abfrage zu groß). GEOFACT_OSM_BACKEND=postgis nutzen, falls "
                f"ein lokaler PostGIS-Dump konfiguriert ist. Letzter Fehler: {error}"
            )
        return (
            f"{base} (Status: {status}). GEOFACT_OSM_BACKEND=postgis nutzen, "
            f"falls ein lokaler PostGIS-Dump konfiguriert ist. Letzter Fehler: {error}"
        )

    def _parse_response(
        self,
        payload: dict,
        tags: TagFilter,
        geometry: Optional[Sequence[str]] = None,
        columns: Optional[Sequence[str]] = None,
    ) -> GeoDataFrame:
        kinds, whitelist = (
            geometry,
            columns,
        )  # 'geometry' ist unten die Schleifenvariable
        rows = []
        geometries = []
        unassembled: list[int] = []
        incomplete: list[int] = []
        ignored: Counter[str] = Counter()
        for element in payload.get("elements", []):
            if element["type"] == "relation":
                kind = element.get("tags", {}).get("type", "")
                if kind not in _AREA_RELATION_TYPES:
                    ignored[kind or "ohne type"] += 1
                    continue
                geometry, closed = _relation_to_geometry(element)
                if geometry is None:
                    unassembled.append(element["id"])
                    continue
                if not closed:
                    incomplete.append(element["id"])
            else:
                geometry = _overpass_element_to_geometry(element)
            if geometry is None:
                continue
            geometries.append(geometry)
            rows.append(element.get("tags", {}))
        _warn_about_relations(unassembled, incomplete, ignored)
        rows, geometries, output_columns = _apply_tag_predicates(
            rows, geometries, tags=tags, geometry=kinds, columns=whitelist
        )
        return _frame(rows, geometries, output_columns)


def _warn_about_relations(
    unassembled: list[int], incomplete: list[int], ignored: Counter[str]
) -> None:
    """Eine Sammelwarnung für Relationen, die nicht (vollständig) im Ergebnis stehen."""
    parts = []
    if unassembled:
        parts.append(
            f"{len(unassembled)} Multipolygon-/Grenz-Relation(en) nicht als Fläche "
            f"zusammensetzbar und ausgelassen (ids {unassembled[:5]})"
        )
    if incomplete:
        parts.append(
            f"{len(incomplete)} Relation(en) mit nicht geschlossenen Ringen, nur die "
            f"geschlossenen Ringe wurden übernommen (ids {incomplete[:5]})"
        )
    if ignored:
        kinds = ", ".join(f"{kind}: {count}" for kind, count in sorted(ignored.items()))
        parts.append(f"Relationen ohne Flächengeometrie übersprungen (type {kinds})")
    if parts:
        warnings.warn(
            "OSM-Abruf: " + "; ".join(parts), OsmRelationWarning, stacklevel=2
        )


# --- PostgisBackend ---

_TAG_TABLES = ("osm_points", "osm_lines", "osm_polygons")
_TABLE_OF_KIND = {"point": "osm_points", "line": "osm_lines", "polygon": "osm_polygons"}


def _postgis_tables(geometry: Optional[Sequence[str]]) -> tuple[str, ...]:
    """Tabellen des Flex-Imports für die gewählten Geometriearten (ohne Wahl alle)."""
    if geometry is None:
        return _TAG_TABLES
    wanted = {_TABLE_OF_KIND[kind] for kind in geometry}
    return tuple(table for table in _TAG_TABLES if table in wanted)


@dataclass(frozen=True)
class PostgisQuery:
    """Eine PostGIS-Abfrage als SQL mit benannten Platzhaltern (``:name``)
    und den zugehörigen Werten. Tag-Schlüssel, Tag-Werte und BBox stehen nie
    im SQL-Text, sondern nur in ``params`` und werden vom Treiber gebunden
    (INT-16)."""

    sql: str
    params: dict[str, object]


# Tabellenname (optional schema-qualifiziert): Bezeichner lassen sich nicht
# binden, daher werden sie gegen ein enges Muster geprüft statt eingesetzt.
_SAFE_SQL_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?")


def _checked_table(table: str) -> str:
    if not isinstance(table, str) or not _SAFE_SQL_IDENTIFIER.fullmatch(table):
        raise ValueError(
            f"PostGIS-Abfrage: ungültiger Tabellenname {table!r} - erlaubt sind nur "
            "Buchstaben, Ziffern und Unterstrich (optional 'schema.tabelle')"
        )
    return table


class _Binder:
    """Vergibt eindeutige Platzhalternamen und sammelt die Werte."""

    def __init__(self) -> None:
        self.params: dict[str, object] = {}

    def bind(self, prefix: str, value: object) -> str:
        name = f"{prefix}_{len(self.params)}"
        self.params[name] = value
        return f":{name}"


def _postgis_tag_set_condition(tag_set: dict[str, str], binder: _Binder) -> str:
    """SQL-Bedingung für ein Tag-Set (AND), Werte als gebundene Parameter.

    Exakte Paare sind eine einzige jsonb-Containment-Prüfung (@>, GIN-indexfreundlich).
    Wildcards (FA30) werden jsonb-Existenzprüfungen, da '@>' mit "*" still 0 Zeilen
    lieferte."""
    for key, value in tag_set.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(
                f"OSM-Tag-Filter: Schlüssel und Wert müssen Texte sein, nicht {key!r}: {value!r}"
            )
    if not tag_set:
        raise ValueError("OSM-Tag-Filter: ein Tag-Set darf nicht leer sein")
    exact = {key: value for key, value in tag_set.items() if not _is_wildcard(value)}
    wildcards = [key for key, value in tag_set.items() if _is_wildcard(value)]

    conditions = []
    if exact:
        conditions.append(
            f"tags @> CAST({binder.bind('tags', json.dumps(exact))} AS jsonb)"
        )
    # jsonb_exists() statt des Operators ?, der in psycopg 3 ein Platzhalter ist.
    conditions.extend(
        f"jsonb_exists(tags, {binder.bind('key', key)})" for key in wildcards
    )
    return " AND ".join(conditions)


def build_postgis_query(
    table: str,
    tags: TagFilter,
    bbox: tuple[float, float, float, float],
    columns: Optional[Sequence[str]] = None,
) -> PostgisQuery:
    table = _checked_table(table)
    min_lon, min_lat, max_lon, max_lat = _checked_bbox(bbox)
    binder = _Binder()
    tag_conditions = " OR ".join(
        f"({condition})"
        if " AND " in (condition := _postgis_tag_set_condition(tag_set, binder))
        else condition
        for tag_set in _tag_alternatives(tags)
    )
    envelope = ", ".join(
        binder.bind(name, value)
        for name, value in (
            ("min_lon", min_lon),
            ("min_lat", min_lat),
            ("max_lon", max_lon),
            ("max_lat", max_lat),
        )
    )
    if columns is None:
        tags_expr = "tags"
    else:
        # Whitelist schon im SQL (gebunden als text[])
        keys = binder.bind("keys", _output_columns(tags, columns))
        tags_expr = (
            "(SELECT jsonb_object_agg(e.key, e.value) FROM jsonb_each(tags) AS e "
            f"WHERE e.key = ANY(CAST({keys} AS text[]))) AS tags"
        )
    sql = (
        f"SELECT {tags_expr}, ST_AsBinary(geom) AS geom_wkb FROM {table}\n"
        f"WHERE ({tag_conditions})\n"
        f"  AND geom && ST_MakeEnvelope({envelope}, 4326)"
    )
    return PostgisQuery(sql=sql, params=binder.params)


def _as_psycopg_dsn(dsn: str) -> str:
    """SQLAlchemy nimmt ohne Treiberangabe psycopg2; das Projekt nutzt psycopg 3,
    daher wird 'postgresql://' zu 'postgresql+psycopg://'."""
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn[len("postgresql://") :]
    return dsn


DEFAULT_PG_QUERY_TIMEOUT_S = 120
DEFAULT_PG_CONNECT_TIMEOUT_S = 10


def _query_timeout_s(raw: object) -> int:
    """GEOFACT_PG_QUERY_TIMEOUT_S als positive ganze Zahl; der Wert steht als Text in
    der Verbindungsoption ``-c statement_timeout=...`` und wird deshalb streng gelesen."""
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        raise ValueError(
            f"GEOFACT_PG_QUERY_TIMEOUT_S muss eine positive ganze Zahl (Sekunden) sein, nicht {raw!r}"
        )
    return seconds


class PostgisExtentNotice(UserWarning):
    """FA56: ohne Tabellenstatistik ist die Importausdehnung unbekannt - die
    Abdeckung der Region wird dann nicht geprüft (Hinweis, kein Fehler)."""


# Tabelle/Spalte des osm2pgsql-Imports; osm_polygons deckt das Importgebiet ab.
EXTENT_TABLE = "osm_polygons"
EXTENT_COLUMN = "geom"
_EXTENT_SQL = (
    "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM "
    f"(SELECT ST_EstimatedExtent('{EXTENT_TABLE}', '{EXTENT_COLUMN}') AS e) AS extent"
)


def read_import_extent(conn: Any) -> Optional[tuple[float, float, float, float]]:
    """``ST_EstimatedExtent`` über eine offene Verbindung; None ohne Statistik."""
    try:
        row = conn.execute(text(_EXTENT_SQL)).first()
    except Exception:  # noqa: BLE001 - fehlende Statistik ist ein Hinweis, kein Abbruch
        return None
    if row is None or len(row) != 4 or any(value is None for value in row):
        return None
    extent = tuple(float(value) for value in row)
    if not all(math.isfinite(value) for value in extent):
        return None
    return extent  # type: ignore[return-value]


def import_coverage(
    region_bounds: tuple[float, ...], extent: tuple[float, ...]
) -> Optional[Literal["outside", "partial"]]:
    """Lage der Regions-BBox zur Importausdehnung (beide minlon, minlat,
    maxlon, maxlat): None = ganz innerhalb, ``partial``, ``outside``."""
    r_minx, r_miny, r_maxx, r_maxy = region_bounds
    e_minx, e_miny, e_maxx, e_maxy = extent
    if r_maxx < e_minx or r_minx > e_maxx or r_maxy < e_miny or r_miny > e_maxy:
        return "outside"
    if r_minx < e_minx or r_maxx > e_maxx or r_miny < e_miny or r_maxy > e_maxy:
        return "partial"
    return None


def _degrees(bounds: Sequence[float]) -> str:
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bounds)
    return f"lon {min_lon:.2f}..{max_lon:.2f}, lat {min_lat:.2f}..{max_lat:.2f}"


class PostgisBackend:
    name = "postgis"

    def __init__(self, dsn: str | None = None) -> None:
        raw_dsn = dsn or os.environ.get("GEOFACT_PG_DSN")
        if not raw_dsn:
            raise ValueError(
                "PostgisBackend braucht eine DSN (GEOFACT_PG_DSN oder Parameter)"
            )
        self.dsn = _as_psycopg_dsn(raw_dsn)
        self.query_timeout_s = _query_timeout_s(
            os.environ.get("GEOFACT_PG_QUERY_TIMEOUT_S", DEFAULT_PG_QUERY_TIMEOUT_S)
        )
        self.last_meta: dict[str, Any] = {}
        self._extent: Optional[tuple[float, float, float, float]] = None
        self._extent_read = False
        self._extent_notice_given = False

    def _engine(self, connect_timeout_s: int = DEFAULT_PG_CONNECT_TIMEOUT_S):
        return create_engine(
            self.dsn,
            connect_args={
                "connect_timeout": connect_timeout_s,
                "options": f"-c statement_timeout={self.query_timeout_s * 1000}",
            },
        )

    def import_extent(self) -> Optional[tuple[float, float, float, float]]:
        """Geschätzte Ausdehnung des Imports (minlon, minlat, maxlon, maxlat), einmal je
        Instanz. None ohne Statistik (kein ANALYZE); ein Verbindungsfehler bleibt ein Fehler."""
        if not self._extent_read:
            engine = self._engine()
            try:
                with engine.connect() as conn:
                    self._extent = read_import_extent(conn)
            finally:
                engine.dispose()
            self._extent_read = True
        return self._extent

    def check_import_coverage(self, region: GeoDataFrame) -> None:
        """FA56: liegt die Region im Importgebiet? Ganz außerhalb ->
        ValueError mit beiden Ausdehnungen, teilweise -> ``RegionWarning``,
        ohne Statistik -> einmal ``PostgisExtentNotice``, keine Prüfung."""
        extent = self.import_extent()
        if extent is None:
            if not self._extent_notice_given:
                self._extent_notice_given = True
                warnings.warn(
                    f"PostGIS: Ausdehnung des Imports unbekannt (keine Tabellenstatistik für "
                    f"{EXTENT_TABLE}; ANALYZE {EXTENT_TABLE} ausführen) - Abdeckung der Region "
                    "nicht geprüft",
                    PostgisExtentNotice,
                    stacklevel=2,
                )
            return
        problem = import_coverage(tuple(float(v) for v in region.total_bounds), extent)
        if problem == "outside":
            raise ValueError(
                f"Region ({_degrees(region.total_bounds)}) liegt ganz außerhalb des "
                f"PostGIS-Imports ({_degrees(extent)}) - die Abfrage lieferte still 0 Objekte; "
                "Import erweitern oder GEOFACT_OSM_BACKEND=overpass setzen"
            )
        if problem == "partial":
            warnings.warn(
                f"Region ({_degrees(region.total_bounds)}) liegt teilweise außerhalb des "
                f"PostGIS-Imports ({_degrees(extent)}) - Objekte jenseits der Importgrenze "
                "fehlen im Ergebnis",
                RegionWarning,
                stacklevel=2,
            )

    def build_queries(
        self,
        tags: TagFilter,
        region: GeoDataFrame,
        geometry: Optional[Sequence[str]] = None,
        columns: Optional[Sequence[str]] = None,
    ) -> dict[str, PostgisQuery]:
        bbox = tuple(region.total_bounds)
        return {
            table: build_postgis_query(table, tags, bbox, columns=columns)
            for table in _postgis_tables(geometry)
        }

    def fetch(
        self,
        tags: TagFilter,
        region: GeoDataFrame,
        geometry: Optional[Sequence[str]] = None,
        columns: Optional[Sequence[str]] = None,
    ) -> GeoDataFrame:
        # FA56: vor der Abfrage, eine Region außerhalb des Imports lieferte still 0 Objekte.
        self.check_import_coverage(region)
        queries = self.build_queries(tags, region, geometry=geometry, columns=columns)
        # statement_timeout (in _engine): gegen die deutschlandweiten Tabellen könnte
        # eine Abfrage sonst unbegrenzt laufen (NFA2: die Pipeline muss terminieren).
        engine = self._engine()
        rows = []
        geometries = []
        try:
            with engine.connect() as conn:
                for query in queries.values():
                    result = conn.execute(text(query.sql), query.params)
                    for tag_dict, geom_wkb in result:
                        # WKB statt GeoJSON-Text
                        geometries.append(shapely.from_wkb(bytes(geom_wkb)))
                        rows.append(dict(tag_dict or {}))
        finally:
            engine.dispose()
        rows, geometries, output_columns = _apply_tag_predicates(
            rows, geometries, tags=tags, geometry=geometry, columns=columns
        )
        # Der Datenstand des Imports ist in der Datenbank nicht verlässlich ablesbar.
        self.last_meta = {"backend": self.name, "osm_base": None}
        return within_region(_frame(rows, geometries, output_columns), region)


# --- Backend-Auswahl ---

_BACKENDS = {
    "overpass": OverpassBackend,
    "postgis": PostgisBackend,
}


# FA82: Schlüssel der Konnektor-Option, mit der ein Lauf das Backend wählt.
BACKEND_OPTION = "osm.backend"


def backend_names() -> list[str]:
    """Die Namen der vorhandenen Backends (für Auswahllisten)."""
    return sorted(_BACKENDS)


def get_backend_name(name: str | None = None) -> str:
    """Name des gewählten Backends: Parameter, sonst GEOFACT_OSM_BACKEND,
    sonst overpass (Laufzeitkonfiguration, nicht Teil der YAML)."""
    return name or os.environ.get("GEOFACT_OSM_BACKEND") or "overpass"


def get_backend(name: str | None = None) -> OsmBackend:
    backend_name = get_backend_name(name)
    backend_cls = _BACKENDS.get(backend_name)
    if backend_cls is None:
        raise ValueError(
            f"Unbekanntes OSM-Backend '{backend_name}', verfügbar: {sorted(_BACKENDS)}"
        )
    return backend_cls()


def backend_status(timeout_s: float = 2.0) -> dict[str, object]:
    """Betriebszustand des gewählten OSM-Backends (``geofact.api.osm_backend_status``).

    Liefert ``backend``, ``enabled`` (PostGIS aktiv), ``reachable`` (None ohne PostGIS),
    ``detail`` und ``extent`` (FA56, ``[minlon, minlat, maxlon, maxlat]`` oder None).
    Ein Verbindungsfehler ist das Ergebnis, keine Ausnahme: der Health-Endpunkt darf an
    einer toten Datenbank nicht scheitern oder hängen (daher das kurze Timeout)."""
    name = os.environ.get("GEOFACT_OSM_BACKEND", "overpass")
    if name != "postgis":
        return {
            "backend": name,
            "enabled": False,
            "reachable": None,
            "detail": "GEOFACT_OSM_BACKEND != postgis",
            "extent": None,
        }
    dsn = os.environ.get("GEOFACT_PG_DSN")
    if not dsn:
        return {
            "backend": name,
            "enabled": True,
            "reachable": False,
            "detail": "GEOFACT_PG_DSN nicht gesetzt",
            "extent": None,
        }
    try:
        from sqlalchemy import create_engine as connect_engine
        from sqlalchemy import text as sql_text

        engine = connect_engine(
            _as_psycopg_dsn(dsn),
            connect_args={"connect_timeout": max(1, int(timeout_s))},
        )
        try:
            with engine.connect() as conn:
                conn.execute(sql_text("SELECT 1"))
                extent = read_import_extent(conn)
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - der Betriebszustand ist das Ergebnis
        return {
            "backend": name,
            "enabled": True,
            "reachable": False,
            "detail": str(exc),
            "extent": None,
        }
    return {
        "backend": name,
        "enabled": True,
        "reachable": True,
        "detail": None,
        "extent": list(extent) if extent is not None else None,
    }


# --- Snapshot (FA7), OSM-spezifischer Teil ---


def _canonical_tags(
    tags: dict[str, str] | list[dict[str, str]],
) -> list[dict[str, str]]:
    """Kanonische Form von tags für den Hash: Liste von Tag-Sets, nach JSON sortiert,
    damit die Reihenfolge der Alternativen den Hash nicht beeinflusst."""
    tag_sets = tags if isinstance(tags, list) else [tags]
    return sorted(tag_sets, key=lambda t: json.dumps(t, sort_keys=True))


def snapshot_key(
    tags: dict[str, str] | list[dict[str, str]],
    region: GeoDataFrame,
    *,
    geometry: Optional[Sequence[str]] = None,
    columns: Optional[Sequence[str]] = None,
    backend: str = "overpass",
) -> str:
    """SHA-256 über kanonisches JSON von {tags, region}; die Region geht als WKT der
    vereinigten Geometrie ein (deterministisch). Die Payload-Form bleibt stabil, damit
    bestehende Snapshots denselben Dateinamen treffen (Regressionstest in
    test_fa16_opendata.py).

    FA3/FA7-Zusatz: ``geometry`` (sortiert), ``columns`` (in gegebener Reihenfolge) und
    ``backend`` gehen nur ein, wenn gesetzt bzw. nicht ``overpass``. So bedient ein
    Overpass-Snapshot keinen PostGIS-Lauf und umgekehrt."""
    canonical: dict[str, Any] = {
        "tags": _canonical_tags(tags),
        "region_wkt": region.union_all().wkt,
    }
    if geometry is not None:
        canonical["geometry"] = sorted(set(geometry))
    if columns is not None:
        canonical["columns"] = list(dict.fromkeys(columns))
    if backend != "overpass":
        canonical["backend"] = backend
    return snapshot_module.payload_key(canonical)


def fetch_with_snapshot(
    tags: dict[str, str] | list[dict[str, str]],
    region: GeoDataFrame,
    backend: OsmBackend,
    refresh: bool = False,
    *,
    geometry: Optional[Sequence[str]] = None,
    columns: Optional[Sequence[str]] = None,
) -> GeoDataFrame:
    """Dünner Wrapper um snapshot.fetch_with_snapshot_key() mit dem OSM-Schlüssel.
    Der Backend-Name kommt aus ``backend.name`` (fehlt er: overpass), die
    Snapshot-Metadaten aus ``backend.last_meta`` nach dem Abruf."""
    backend_name = getattr(backend, "name", "overpass")
    key = snapshot_key(
        tags, region, geometry=geometry, columns=columns, backend=backend_name
    )
    extra: dict[str, Any] = {}
    if geometry is not None:
        extra["geometry"] = geometry
    if columns is not None:
        extra["columns"] = columns

    def meta() -> dict[str, Any]:
        return {"backend": backend_name, **(getattr(backend, "last_meta", None) or {})}

    return snapshot_module.fetch_with_snapshot_key(
        key, lambda: backend.fetch(tags, region, **extra), refresh=refresh, meta=meta
    )


# --- Registrierung als Quellart (FA40) ---


@register_source(
    "osm",
    OsmLayer,
    description="OpenStreetMap über Tag-Filter (Overpass/PostGIS)",
)
def load(layer: OsmLayer, ctx: LoadContext) -> GeoDataFrame:
    """Backend über die Konnektor-Option ``osm.backend`` (FA82), sonst GEOFACT_OSM_BACKEND;
    0 Objekte sind eine Warnung."""
    data = fetch_with_snapshot(
        layer.tags,
        ctx.region,
        get_backend(ctx.options.get(BACKEND_OPTION)),
        refresh=ctx.refresh,
        geometry=layer.geometry_kinds(),
        columns=layer.columns,
    )
    if data.empty:
        warnings.warn(
            f"OSM-Layer '{layer.id}': 0 Objekte für die Tag-Filter {layer.tags!r} in der "
            "Region - Schreibweise der Tags (OSM-Wiki), geometry und die Region prüfen",
            EmptyOsmResultWarning,
            stacklevel=2,
        )
    return data
