"""Implements: FA22 (Regelmäßiges Sechseckraster über eine Region).

Legt ein lückenloses Sechseckraster über 'region' und gibt jede Zelle aus,
deren Mittelpunkt die (vereinigte) Eingabegeometrie schneidet. Reine
Flächenkachelung ohne Zähllogik, anders als hexbin; die axiale Sechseck-
Mathematik kommt aus _hexgeom.py. Rechnet im harmonisierten CRS der Region
(FA5, Meter), cell_km * 1000 ist die Zellweite.
"""

from __future__ import annotations

import math
from typing import ClassVar, Literal

import geopandas as gpd
import numpy as np
from pydantic import Field
import shapely
from geopandas import GeoDataFrame

from geofact.builtin.operations._hexgeom import hexagon as _hexagon
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


def _empty_result(crs) -> GeoDataFrame:
    return gpd.GeoDataFrame({"q": [], "r": []}, geometry=[], crs=crs)


class HexGridStep(Step):
    """Regelmäßiges, lückenloses Sechseckraster über 'region' (FA22):
    jede Zelle, deren Mittelpunkt innerhalb der Eingabegeometrie liegt,
    erscheint in der Ausgabe - im Unterschied zu hexbin (FA8-Erweiterung)
    keine Zähl-/Belegungslogik, sondern reine Flächenkachelung. 'region'
    ist typischerweise der neue region-Layer (FA21) oder ein per dissolve
    vereinigter Grenz-Layer."""

    op: Literal["hex_grid"] = "hex_grid"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"region": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        cell_km: float = Field(
            ...,
            gt=0,
            description="Zellweite in Kilometern (Abstand gegenüberliegender Sechseck-Kanten).",
        )

    def produced_fields(self) -> set[str]:
        # axiale Zellindizes
        return {"q", "r"}


@register_operation(HexGridStep)
def run_hex_grid(inputs: dict, params: dict) -> GeoDataFrame:
    """Leere oder flächenlose Eingabe ergibt einen leeren Polygon-Layer
    (gleiches CRS), keinen Fehler."""
    region: GeoDataFrame = inputs["region"]
    cell_km = params["cell_km"]

    if region.empty:
        return _empty_result(region.crs)

    geometry = region.geometry.union_all()
    if geometry.is_empty:
        return _empty_result(region.crs)

    radius = (cell_km * 1000.0) / math.sqrt(3)  # Zellweite -> Umkreisradius
    min_x, min_y, max_x, max_y = geometry.bounds

    # Axiale Indexbereiche mit einem Ring Sicherheitsabstand: die q-Achse
    # verläuft schräg zur x-Achse.
    q_min = math.floor((math.sqrt(3) / 3 * min_x - max_y / 3) / radius) - 1
    q_max = math.ceil((math.sqrt(3) / 3 * max_x - min_y / 3) / radius) + 1
    r_min = math.floor((2 / 3 * min_y) / radius) - 1
    r_max = math.ceil((2 / 3 * max_y) / radius) + 1

    q_grid, r_grid = np.meshgrid(
        np.arange(q_min, q_max + 1), np.arange(r_min, r_max + 1)
    )
    q_flat = q_grid.ravel()
    r_flat = r_grid.ravel()

    cx = radius * math.sqrt(3) * (q_flat + r_flat / 2)
    cy = radius * 1.5 * r_flat

    in_bounds = (cx >= min_x) & (cx <= max_x) & (cy >= min_y) & (cy <= max_y)
    q_flat, r_flat, cx, cy = (
        q_flat[in_bounds],
        r_flat[in_bounds],
        cx[in_bounds],
        cy[in_bounds],
    )
    if len(cx) == 0:
        return _empty_result(region.crs)

    # intersects statt contains_xy: Mittelpunkte genau auf dem Rand zählen
    # mit (FA22).
    contains = shapely.intersects(geometry, shapely.points(cx, cy))
    q_flat, r_flat = q_flat[contains], r_flat[contains]
    if len(q_flat) == 0:
        return _empty_result(region.crs)

    geometries = [_hexagon(int(q), int(r), radius) for q, r in zip(q_flat, r_flat)]
    return gpd.GeoDataFrame(
        {"q": q_flat.tolist(), "r": r_flat.tolist()},
        geometry=geometries,
        crs=region.crs,
    )
