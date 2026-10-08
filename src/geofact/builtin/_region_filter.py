"""Implements: FA3 (Nachbedingung: nur Objekte, die die tatsächliche Region schneiden), FA16, FA20, FA42.

Gemeinsamer Regionsfilter der Konnektoren osm, wfs, ckan, gtfs und rest. Vorher
hatte jeder Konnektor eine eigene Kopie (``data[data.intersects(region.union_all())]``);
jetzt gibt es EINE Funktion mit demselben Prädikat: ein Objekt bleibt, wenn es
die vereinigte Regionsgeometrie schneidet (Randpunkte zählen, fehlende oder
leere Geometrien fallen heraus). Schneller ist nur der Weg dorthin: Punkte
werden über ``shapely.intersects_xy`` geprüft (keine Punktobjekte je Zeile),
alle anderen Geometrien gegen die einmal vorbereitete Region
(``shapely.prepare``).

Hilfsmodul (führender Unterstrich): registriert nichts und wird von der
Erkennung (FA40) nicht gescannt."""

from __future__ import annotations

import numpy as np
import shapely
from geopandas import GeoDataFrame

_POINT_TYPE_ID = 0  # shapely.GeometryType.POINT


def within_region(data: GeoDataFrame, region: GeoDataFrame) -> GeoDataFrame:
    """Objekte von ``data``, die die vereinigte Geometrie von ``region``
    schneiden, mit neu durchnummeriertem Index. Ein leerer Eingang kommt
    unverändert zurück. Beide Eingaben müssen dasselbe CRS haben (der
    Aufrufer reprojiziert vorher, z. B. ``rest``); ein Unterschied ist ein
    Fehler statt eines stillen Vergleichs in verschiedenen Einheiten."""
    if data.empty:
        return data
    if data.crs is not None and region.crs is not None and data.crs != region.crs:
        raise ValueError(
            f"Regionsfilter: Daten-CRS {data.crs.to_string()} und Regions-CRS "
            f"{region.crs.to_string()} unterscheiden sich - vorher reprojizieren"
        )
    area = region.union_all()
    shapely.prepare(area)
    geometries = np.asarray(data.geometry.array, dtype=object)
    is_point = shapely.get_type_id(geometries) == _POINT_TYPE_ID
    mask = np.zeros(len(geometries), dtype=bool)
    if is_point.any():
        points = geometries[is_point]
        mask[is_point] = shapely.intersects_xy(
            area, shapely.get_x(points), shapely.get_y(points)
        )
    others = ~is_point
    if others.any():
        mask[others] = shapely.intersects(area, geometries[others])
    return data[mask].reset_index(drop=True)
