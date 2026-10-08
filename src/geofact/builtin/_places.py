"""Implements: FA52 (Orte als Datenquelle).

Gemeinsame Prüfung der Quellarten ``geocode`` und ``points``: jeder Ort muss in
der BBox der Szenario-Region liegen (plus einem Rand ``margin_km``). Ein Ort
außerhalb der Region ist fast immer ein Fehler (falscher Treffer des
Geocoders, vertauschte Koordinaten) und ließe sich später auch nicht
routen, weil das Straßennetz an der Region endet - die Meldung nennt Ort und
Koordinaten, statt dass der Lauf erst in route mit "nicht erreichbar"
scheitert.

Ein Modul mit führendem Unterstrich wird nicht als Baustein gescannt und
registriert nichts."""

from __future__ import annotations

import math

from geopandas import GeoDataFrame

from geofact.plugin_api import LoadContext

KM_PER_DEGREE = 111.32
"""Kilometer je Breitengrad (Näherung für den Rand um die Region)."""


def require_inside_region(
    places: GeoDataFrame, ctx: LoadContext, *, layer_id: str, margin_km: float
) -> None:
    """Bricht ab, wenn ein Ort außerhalb der Region-BBox (+ margin_km) liegt.

    places liegt in EPSG:4326 (vor der Harmonisierung). Ohne Region im Kontext
    (direkter Aufruf außerhalb eines Laufs) gibt es nichts zu prüfen."""
    if ctx.region is None or places.empty:
        return
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in ctx.region.total_bounds)
    lat_margin = margin_km / KM_PER_DEGREE
    lon_margin = margin_km / (
        KM_PER_DEGREE * max(math.cos(math.radians((min_lat + max_lat) / 2)), 0.01)
    )
    outside = []
    for name, point in zip(places["name"], places.geometry):
        if not (
            min_lon - lon_margin <= point.x <= max_lon + lon_margin
            and min_lat - lat_margin <= point.y <= max_lat + lat_margin
        ):
            outside.append(f"'{name}' (lon {point.x:.5f}, lat {point.y:.5f})")
    if outside:
        raise ValueError(
            f"Layer '{layer_id}': {len(outside)} Ort(e) liegen außerhalb der Szenario-Region "
            f"(BBox {min_lon:.4f},{min_lat:.4f},{max_lon:.4f},{max_lat:.4f} + {margin_km:g} km Rand): "
            f"{', '.join(outside)}. Anfrage bzw. Koordinaten prüfen oder die Region erweitern "
            "(scenario.region); margin_km erlaubt einen größeren Rand."
        )
