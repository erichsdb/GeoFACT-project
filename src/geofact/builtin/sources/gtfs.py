"""Implements: FA20 (Open-Data-Konnektor, GTFS-Teil), Quellart 'gtfs' für FA40.

Leitet aus einem GTFS-Zip ein Liniennetz ab: Kanten verbinden direkt aufeinander-
folgende Haltestellen einer Fahrt (stop_times.txt + trips.txt). Busverkehr lässt
sich aus OSM nicht als Liniengeometrie gewinnen, GTFS liefert die echten Fahrplandaten.

Kanten sind bewusst Luftlinien zwischen zwei Haltestellen, nicht shapes.txt: für
Erreichbarkeit zählt die Stop-zu-Stop-Topologie (Hops), nicht der Straßenverlauf,
und nicht jeder Feed hat shapes.txt. Das ist die Approximation des Modells, kein
Platzhalter.

Eine Kante je Fahrt wäre massenhaft Duplikat (dieselbe Linie fährt oft mit gleicher
Haltestellenfolge). Gebildet wird die Menge eindeutiger, gerichteter
(stop_id_a, stop_id_b)-Paare über alle gefilterten Fahrten; die Richtung kommt aus
stop_sequence, a->b und b->a bleiben getrennt.

route_types ist Pflicht: ein Feed mischt mehrere Verkehrsmittel (GTFS: 0=Tram,
1=U-Bahn, 2=Bahn, 3=Bus, plus anbieterspezifische Werte), ein "alles laden"-Default
würde die Netze vermischen. stop_times.txt ist meist die größte Datei und wird
zeilenweise gestreamt statt als DataFrame geladen.

Download über support/http.py; das Zip wird direkt aus dem Speicher gelesen (keine
temporäre Datei). Snapshot (FA7) mit url/path und route_types im Payload-Schlüssel."""

from __future__ import annotations

import csv
import io
import warnings
import zipfile
from pathlib import Path
from typing import Literal, Optional

import geopandas as gpd
from geopandas import GeoDataFrame
from pydantic import field_validator, model_validator
from shapely.geometry import LineString

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


class GtfsLayer(LayerBase):
    """NEU (FA20): GTFS-Fahrplandaten (Zip), umgewandelt in ein Linien-
    Netz. Entweder url (HTTPS-Download über support/http.py, analog
    zum CKAN-Ressourcen-Download aus FA16) oder path (lokale Zip-Datei) -
    genau eines von beiden, analog zu CkanLayer.resource_id/dataset und
    GeometryMapping.x_column+y_column/wkt_column.

    route_types ist PFLICHT und darf nicht leer sein: ein GTFS-Feed
    enthält typischerweise mehrere Verkehrsmittel (Tram, Bus, S-Bahn, ...)
    im selben route_type-Wertebereich (GTFS-Spezifikation: 0=Tram,
    1=U-Bahn, 2=Bahn, 3=Bus, ..., plus erweiterte Werte einzelner
    Anbieter) - ein stillschweigendes "alles laden" würde Netze
    unterschiedlicher Verkehrsmittel vermischen (vgl. OsmLayer.tags:
    explizite Filterung statt impliziter Default).

    Das Modul (builtin/sources/gtfs.py) leitet Kanten aus stop_times.txt +
    trips.txt ab (direkt aufeinanderfolgende Haltestellen je Fahrt,
    dedupliziert über alle Fahrten), NICHT aus shapes.txt - siehe
    Modul-Docstring für die Begründung (motivierender Feed hat kein
    shapes.txt, und Stop-zu-Stop-Topologie ist für eine Erreichbarkeits-
    analyse das korrekte, nicht ein vorläufiges Modell)."""

    id: str
    source: Literal["gtfs"] = "gtfs"
    url: Optional[str] = None
    path: Optional[str] = None
    route_types: list[int]  # PFLICHT, kein Default (s. o.)
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR
    validation: Optional[LayerValidation] = None

    @model_validator(mode="after")
    def url_or_path(self) -> "GtfsLayer":
        has_url = self.url is not None
        has_path = self.path is not None
        if has_url == has_path:  # beides oder keins
            raise ValueError(
                f"Layer '{self.id}': GTFS-Quelle braucht entweder url ODER path"
            )
        return self

    @field_validator("route_types")
    @classmethod
    def route_types_not_empty(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError(
                "GTFS-Layer benötigt mindestens einen route_type-Filter "
                "(z. B. [3] für Bus) - kein impliziter Ganz-Feed-Default"
            )
        return v


_REQUIRED_FILES = ("stops.txt", "routes.txt", "trips.txt", "stop_times.txt")

# Anteil gefilterter Fahrten ohne Kante, ab dem gewarnt wird (deutet auf ein
# stop_times-Parsing-Problem, nicht auf normale Kurzfahrten).
_ZERO_EDGE_TRIP_WARN_FRACTION = 0.5


class ZeroEdgeTripsWarning(UserWarning):
    """Signalisiert, dass ein auffällig hoher Anteil der nach route_types
    gefilterten Fahrten keine einzige Kante beigetragen hat (FA20-
    Modul-Docstring: >50% deutet typischerweise auf ein stop_times-
    Parsing-Problem hin, nicht auf normale Einzel-Halt-Fahrten)."""


def _open_zip(layer: GtfsLayer) -> zipfile.ZipFile:
    """Geöffnetes ZipFile für layer.url (Download in den Speicher) oder layer.path."""
    if layer.path is not None:
        p = Path(layer.path)
        if not p.is_file():
            raise RuntimeError(
                f"GTFS-Layer '{layer.id}': Datei nicht gefunden: {layer.path}"
            )
        return zipfile.ZipFile(p)

    raw = http.download_limited(layer.url, context=f"GTFS-Zip (Layer '{layer.id}')")
    return zipfile.ZipFile(io.BytesIO(raw))


def _require_files(zf: zipfile.ZipFile, layer_id: str) -> None:
    names = set(zf.namelist())
    missing = [f for f in _REQUIRED_FILES if f not in names]
    if missing:
        raise RuntimeError(
            f"GTFS-Layer '{layer_id}': Zip fehlt Pflichtdatei(en) {missing} "
            f"(benötigt: {list(_REQUIRED_FILES)})"
        )


def _read_csv_rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        yield from csv.DictReader(text)


def _parse_stops(zf: zipfile.ZipFile) -> dict[str, dict]:
    """stop_id -> {'lon': float, 'lat': float, 'name': str}."""
    stops: dict[str, dict] = {}
    for row in _read_csv_rows(zf, "stops.txt"):
        stop_id = row.get("stop_id")
        lat = row.get("stop_lat")
        lon = row.get("stop_lon")
        if not stop_id or lat in (None, "") or lon in (None, ""):
            continue
        stops[stop_id] = {
            "lon": float(lon),
            "lat": float(lat),
            "name": row.get("stop_name") or stop_id,
        }
    return stops


def _parse_route_types(zf: zipfile.ZipFile) -> dict[str, int]:
    """route_id -> route_type."""
    route_types: dict[str, int] = {}
    for row in _read_csv_rows(zf, "routes.txt"):
        route_id = row.get("route_id")
        rt = row.get("route_type")
        if route_id is None or rt in (None, ""):
            continue
        route_types[route_id] = int(rt)
    return route_types


def _filtered_trip_ids(
    zf: zipfile.ZipFile, route_types_by_route: dict[str, int], wanted_types: set[int]
) -> tuple[set[str], set[int]]:
    """trip_id-Menge der Fahrten, deren Route zu wanted_types gehört,
    plus die Menge aller im Feed tatsächlich vorkommenden route_types
    (für eine hilfreiche Fehlermeldung bei 0 Treffern)."""
    trip_ids: set[str] = set()
    seen_types: set[int] = set(route_types_by_route.values())
    for row in _read_csv_rows(zf, "trips.txt"):
        route_id = row.get("route_id")
        trip_id = row.get("trip_id")
        if route_id is None or trip_id is None:
            continue
        rt = route_types_by_route.get(route_id)
        if rt is not None and rt in wanted_types:
            trip_ids.add(trip_id)
    return trip_ids, seen_types


def _ordered_stop_sequences(
    zf: zipfile.ZipFile, wanted_trip_ids: set[str]
) -> dict[str, list[tuple[int, str]]]:
    """trip_id -> Liste von (stop_sequence, stop_id), unsortiert; stop_times.txt
    wird zeilenweise gestreamt."""
    sequences: dict[str, list[tuple[int, str]]] = {}
    for row in _read_csv_rows(zf, "stop_times.txt"):
        trip_id = row.get("trip_id")
        if trip_id not in wanted_trip_ids:
            continue
        stop_id = row.get("stop_id")
        seq = row.get("stop_sequence")
        if stop_id is None or seq in (None, ""):
            continue
        sequences.setdefault(trip_id, []).append((int(seq), stop_id))
    return sequences


def _derive_unique_edges(
    sequences: dict[str, list[tuple[int, str]]],
) -> tuple[list[tuple[str, str]], int]:
    """Liefert (eindeutige (from,to)-Paare in erster-Auftreten-Reihenfolge,
    Anzahl Fahrten mit >=1 Kante)."""
    seen: dict[tuple[str, str], None] = {}
    trips_with_edges = 0
    for trip_id, stops in sequences.items():
        ordered = [stop_id for _, stop_id in sorted(stops, key=lambda t: t[0])]
        had_edge = False
        for a, b in zip(ordered, ordered[1:]):
            if a == b:
                continue
            had_edge = True
            seen.setdefault((a, b), None)
        if had_edge:
            trips_with_edges += 1
    return list(seen.keys()), trips_with_edges


def fetch(layer: GtfsLayer) -> GeoDataFrame:
    wanted_types = set(layer.route_types)
    zf = _open_zip(layer)
    try:
        _require_files(zf, layer.id)
        stops = _parse_stops(zf)
        route_types_by_route = _parse_route_types(zf)
        trip_ids, feed_route_types = _filtered_trip_ids(
            zf, route_types_by_route, wanted_types
        )
        if not trip_ids:
            raise RuntimeError(
                f"GTFS-Layer '{layer.id}': route_types {sorted(wanted_types)} "
                f"treffen keine Fahrt im Feed. Im Feed vorhandene route_types: "
                f"{sorted(feed_route_types)}."
            )

        sequences = _ordered_stop_sequences(zf, trip_ids)
    finally:
        zf.close()

    # Einzel-Halt-Fahrten sind kein Fehler; nur ein hoher Anteil ohne Kante warnt.
    # n_trips zählt alle gefilterten Fahrten, auch solche ohne stop_times-Zeilen
    # (die nicht in sequences landen).
    n_trips = len(trip_ids)
    edges, trips_with_edges = _derive_unique_edges(sequences)
    trips_without_edges = n_trips - trips_with_edges
    if n_trips > 0 and (trips_without_edges / n_trips) > _ZERO_EDGE_TRIP_WARN_FRACTION:
        warnings.warn(
            f"GTFS-Layer '{layer.id}': {trips_without_edges} von {n_trips} "
            f"gefilterten Fahrten ({trips_without_edges / n_trips:.0%}) tragen "
            "keine einzige Kante bei - mögliches Problem beim Parsen von "
            "stop_times.txt prüfen (FA20)",
            ZeroEdgeTripsWarning,
            stacklevel=2,
        )

    rows = []
    geometries = []
    for from_id, to_id in edges:
        from_stop = stops.get(from_id)
        to_stop = stops.get(to_id)
        if from_stop is None or to_stop is None:
            continue
        rows.append(
            {
                "from_stop_id": from_id,
                "to_stop_id": to_id,
                "from_stop_name": from_stop["name"],
                "to_stop_name": to_stop["name"],
            }
        )
        geometries.append(
            LineString(
                [
                    (from_stop["lon"], from_stop["lat"]),
                    (to_stop["lon"], to_stop["lat"]),
                ]
            )
        )

    return gpd.GeoDataFrame(rows, geometry=geometries, crs="EPSG:4326")


@register_source(
    "gtfs",
    GtfsLayer,
    description="GTFS-Fahrplandaten (Zip, URL oder Pfad) -> abgeleitetes Liniennetz aus stop_times",
)
def load(layer: GtfsLayer, ctx: LoadContext) -> GeoDataFrame:
    """Einziger Einstieg der Quellart (FA45)."""
    region_gdf = ctx.region
    refresh = ctx.refresh
    key_payload = {
        "kind": "gtfs",
        "url": layer.url,
        "path": layer.path,
        "route_types": sorted(layer.route_types),
    }
    key = snapshot_module.payload_key(key_payload)
    try:
        data = snapshot_module.fetch_with_snapshot_key(
            key, lambda: fetch(layer), refresh=refresh
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"GTFS-Layer '{layer.id}' konnte nicht geladen werden: {exc} Alternativ: "
            "vorhandenen Snapshot ohne --refresh-snapshots verwenden, oder die "
            "Zip manuell herunterladen und per path referenzieren."
        ) from exc

    return within_region(data, region_gdf)
