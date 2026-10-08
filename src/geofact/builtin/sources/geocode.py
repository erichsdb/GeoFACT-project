"""Implements: FA52 (Orte als Datenquelle, Quellart 'geocode'), Quellart für FA40, FA65 (Default-Lizenz ODbL-1.0).

Löst Adressen und Ortsnamen über Nominatim (OpenStreetMap) zu Punkten auf:
``query: "Chemnitz Hauptbahnhof"`` oder ``queries: [...]``. Das Ergebnis ist ein
Punkt-Layer mit den Spalten ``name`` (die Anfrage, wie sie in der YAML steht) und
``display_name`` (die Bezeichnung des Treffers - sie ist Beschreibung, kein
strukturiertes Feld, und wird nicht weiter zerlegt).

Je Anfrage zählt der erste Treffer; ohne Treffer bricht der Layer ab. Jeder
Treffer muss in der BBox der Szenario-Region (+ ``margin_km``) liegen
(builtin/_places.py), ein Treffer in einer anderen Stadt ist ein Fehler.

Nominatim-Nutzungsrichtlinie: eigener User-Agent und höchstens eine Anfrage je
Sekunde (``_throttle``). Der Endpunkt lässt sich über GEOFACT_NOMINATIM_URL
ersetzen (Laufzeitkonfiguration, nicht Teil der Szenario-YAML).

Snapshot (FA7): abgelegt wird am Hash aus Endpunkt, Anfragen und Ländercodes,
nicht an der Region; die Lage in der Region wird bei jedem Laden neu geprüft."""

from __future__ import annotations

import os
import time
from typing import Literal, Optional

import geopandas as gpd
from geopandas import GeoDataFrame
from pydantic import Field, field_validator, model_validator
from shapely.geometry import Point

from geofact.builtin._licenses import OSM_LICENSE, License
from geofact.builtin._places import require_inside_region
from geofact.plugin_api import DataType, LayerBase, LoadContext, register_source
from geofact.support import http, snapshot

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MIN_INTERVAL_S = 1.1
"""Mindestabstand zweier Anfragen (Richtlinie: höchstens 1 pro Sekunde, mit Reserve)."""

_last_request_at: Optional[float] = None


class GeocodeLayer(LayerBase):
    """Orte per Adresse/Name über Nominatim (FA52). Genau eines von ``query`` und
    ``queries``."""

    id: str
    source: Literal["geocode"] = "geocode"
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR
    query: Optional[str] = Field(
        None, description="Eine Anfrage, z. B. 'Chemnitz Hauptbahnhof'."
    )
    queries: Optional[list[str]] = Field(
        None, min_length=1, description="Mehrere Anfragen, je eine Zeile im Ergebnis."
    )
    countrycodes: Optional[list[str]] = Field(
        None,
        description="Ländercodes (ISO 3166-1 alpha-2, z. B. de) zur Einschränkung der Suche; "
        "ein einzelner Code darf als Text angegeben werden.",
    )
    margin_km: float = Field(
        1.0,
        ge=0,
        description="Rand in km um die BBox der Region, innerhalb dessen ein Treffer noch als "
        "'in der Region' gilt.",
    )

    @field_validator("countrycodes", mode="before")
    @classmethod
    def countrycodes_as_list(cls, value):
        if isinstance(value, str):
            value = [part for part in value.split(",") if part.strip()]
        return value

    @field_validator("countrycodes")
    @classmethod
    def countrycodes_are_codes(cls, codes):
        if codes is None:
            return codes
        cleaned = [code.strip().lower() for code in codes]
        bad = [code for code in cleaned if len(code) != 2 or not code.isalpha()]
        if bad or not cleaned:
            raise ValueError(
                f"countrycodes: erwartet zweibuchstabige Ländercodes (z. B. de), nicht {bad or codes}"
            )
        return cleaned

    @model_validator(mode="after")
    def exactly_one_query_form(self) -> "GeocodeLayer":
        if (self.query is None) == (self.queries is None):
            raise ValueError(
                f"Layer '{self.id}': genau eines von 'query' und 'queries' angeben "
                f"(angegeben: {'beide' if self.query is not None else 'keines'})"
            )
        blank = [q for q in self.all_queries if not q.strip()]
        if blank:
            raise ValueError(f"Layer '{self.id}': Anfragen dürfen nicht leer sein")
        return self

    def default_license(self) -> Optional[License]:
        """FA65: Daten aus OpenStreetMap (Nominatim) stehen unter der ODbL-1.0."""
        return OSM_LICENSE

    @property
    def all_queries(self) -> list[str]:
        return [self.query] if self.query is not None else list(self.queries or [])


def nominatim_url() -> str:
    return os.environ.get("GEOFACT_NOMINATIM_URL") or NOMINATIM_URL


def _throttle() -> None:
    """Wartet MIN_INTERVAL_S seit der letzten Anfrage; auch vor der ersten, weil die
    Region gerade erst aufgelöst worden sein kann."""
    global _last_request_at
    if _last_request_at is None:
        wait = MIN_INTERVAL_S
    else:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _search(url: str, query: str, countrycodes: Optional[list[str]]) -> list[dict]:
    """Eine Nominatim-Anfrage (höchstens ein Treffer), gedrosselt, mit Wiederholung."""
    params = {"q": query, "format": "jsonv2", "limit": 1}
    if countrycodes:
        params["countrycodes"] = ",".join(countrycodes)
    _throttle()
    return http.get_json(url, params=params, context=f"Nominatim-Suche '{query}'")


def fetch(layer: GeocodeLayer) -> GeoDataFrame:
    """Löst alle Anfragen des Layers auf (ein Treffer je Anfrage)."""
    url = nominatim_url()
    names: list[str] = []
    display_names: list[str] = []
    points: list[Point] = []
    for query in layer.all_queries:
        results = _search(url, query, layer.countrycodes)
        if not results:
            raise ValueError(
                f"Layer '{layer.id}': Nominatim fand keinen Treffer für '{query}' - Schreibweise "
                "prüfen, die Anfrage um Ort oder Land ergänzen (z. B. 'Straße 1, Chemnitz') "
                "oder die Stelle als source: points mit Koordinaten angeben"
            )
        hit = results[0]
        try:
            point = Point(float(hit["lon"]), float(hit["lat"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Layer '{layer.id}': die Nominatim-Antwort für '{query}' enthält keine "
                f"verwertbaren Koordinaten ({exc!r})"
            ) from exc
        names.append(query)
        display_names.append(str(hit.get("display_name", "")))
        points.append(point)
    return gpd.GeoDataFrame(
        {"name": names, "display_name": display_names}, geometry=points, crs="EPSG:4326"
    )


@register_source(
    "geocode",
    GeocodeLayer,
    description="Orte per Adresse/Name über Nominatim (Punkt-Layer, Snapshot, 1 Anfrage/s)",
)
def load(layer: GeocodeLayer, ctx: LoadContext) -> GeoDataFrame:
    key = snapshot.payload_key(
        {
            "kind": "geocode",
            "url": nominatim_url(),
            "queries": layer.all_queries,
            "countrycodes": layer.countrycodes,
        }
    )
    places = snapshot.fetch_with_snapshot_key(
        key, lambda: fetch(layer), refresh=ctx.refresh
    )
    require_inside_region(places, ctx, layer_id=layer.id, margin_km=layer.margin_km)
    return places
