"""Implements: FA68 (Geometriemasse: area).

Schreibt je Objekt die planare Fläche im Arbeits-CRS in eine neue Spalte
(Einheit m2, ha oder km2). Vorbedingung: projiziertes CRS (FA5). Nicht-Flächen
erhalten NaN und werden in einer Sammelwarnung gemeldet (kein stiller Umfang).
Zeilen und Reihenfolge bleiben, die Eingabe bleibt unverändert. Geodätische
Flächen sind nicht Teil der Operation."""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

from geopandas import GeoDataFrame
from pydantic import Field

from geofact.builtin.operations._geometry import measure_geometries
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step

_FACTOR = {"m2": 1.0, "ha": 1e4, "km2": 1e6}


class AreaStep(Step):
    """Fläche je Objekt (FA68); Ergebnisspalte ``output_field`` oder
    ``area_<unit>``."""

    op: Literal["area"] = "area"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        unit: Literal["m2", "ha", "km2"] = Field(
            "m2", description="Einheit der Fläche."
        )
        output_field: Optional[str] = Field(
            None,
            min_length=1,
            description="Name der Ergebnisspalte (Default: area_<unit>).",
        )
        decimals: Optional[int] = Field(
            None,
            ge=0,
            le=12,
            strict=True,
            description="Nachkommastellen (Default: ungerundet).",
        )

    def produced_fields(self) -> set[str]:
        return {
            self.params.get("output_field") or f"area_{self.params.get('unit', 'm2')}"
        }


@register_operation(AreaStep)
def run_area(inputs: dict, params: dict) -> GeoDataFrame:
    unit = params.get("unit", "m2")
    return measure_geometries(
        inputs["features"],
        op="area",
        dimension=2,
        measure=lambda gdf: gdf.geometry.area,
        factor=_FACTOR[unit],
        output_field=params.get("output_field") or f"area_{unit}",
        decimals=params.get("decimals"),
        expected="Flächen",
    )
