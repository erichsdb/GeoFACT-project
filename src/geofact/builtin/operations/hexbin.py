"""Implements: FA8 (räumliche Operationen, Erweiterung hexbin).

Wabenraster-Dichte: ordnet jedes Objekt über Axialkoordinaten mit Cube-Rounding
(vektorisiert mit NumPy) seiner Sechseckzelle zu und zählt je Zelle. Es gibt
kein vorab erzeugtes Gitter; es entstehen nur belegte Zellen, die Laufzeit hängt
an der Objektanzahl. Rechnet im harmonisierten CRS der Region (FA5, Meter).
"""

from __future__ import annotations

import math
from typing import ClassVar, Literal

import geopandas as gpd
import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field

from geofact.builtin.operations._hexgeom import axial_round as _axial_round
from geofact.builtin.operations._hexgeom import hexagon as _hexagon
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class HexbinStep(Step):
    """Wabenraster-Dichte (FA8-Erweiterung): legt ein regelmäßiges
    Sechseckraster über 'features' und zählt Objekte je Zelle (Spalte
    'count'). Nicht-Punkt-Geometrien gehen über ihren
    representative_point() ein. Nur belegte Zellen werden erzeugt;
    min_count filtert zusätzlich dünn besetzte Zellen. Ergebnis ist ein
    normaler Vektor-Layer (Polygone) - auf der Karte nach 'count'
    eingefärbt ergibt das eine Dichtekarte statt tausender
    überlappender Einzelpunkte."""

    op: Literal["hexbin"] = "hexbin"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        cell_km: float = Field(
            ...,
            gt=0,
            description="Zellweite in Kilometern (Abstand gegenüberliegender Sechseck-Kanten).",
        )
        min_count: int = Field(
            1,
            ge=0,
            strict=True,
            description="Zellen mit weniger Objekten werden verworfen.",
        )

    def produced_fields(self) -> set[str]:
        # q/r sind interne Zwischenwerte und nicht im Ergebnis
        return {"count"}


@register_operation(HexbinStep)
def run_hexbin(inputs: dict, params: dict) -> GeoDataFrame:
    """Leerer Layer ergibt einen leeren Polygon-Layer mit 'count'-Spalte."""
    features: GeoDataFrame = inputs["features"]
    cell_km = params["cell_km"]
    min_count = params.get("min_count", 1)

    if features.empty:
        return gpd.GeoDataFrame(
            {"count": pd.Series([], dtype=int)}, geometry=[], crs=features.crs
        )

    # Linien/Flaechen über ihren inneren Punkt zählen (wie to_points).
    points = features.geometry.copy()
    non_point = points.geom_type != "Point"
    if non_point.any():
        points[non_point] = points[non_point].representative_point()

    radius = (cell_km * 1000.0) / math.sqrt(3)  # Zellweite -> Umkreisradius
    xs = points.x.to_numpy()
    ys = points.y.to_numpy()
    qf = (math.sqrt(3) / 3 * xs - ys / 3) / radius
    rf = (2 / 3 * ys) / radius
    q, r = _axial_round(qf, rf)

    cells = (
        pd.DataFrame({"q": q, "r": r})
        .groupby(["q", "r"])
        .size()
        .reset_index(name="count")
    )
    cells = cells[cells["count"] >= min_count]
    if cells.empty:
        return gpd.GeoDataFrame(
            {"count": pd.Series([], dtype=int)}, geometry=[], crs=features.crs
        )

    geometries = [
        _hexagon(int(row.q), int(row.r), radius) for row in cells.itertuples()
    ]
    return gpd.GeoDataFrame(
        {"count": cells["count"].to_numpy()}, geometry=geometries, crs=features.crs
    )
