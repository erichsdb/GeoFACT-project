"""Implements: FA68 (Geometriemasse: length).

Schreibt je Linienobjekt die planare Länge im Arbeits-CRS in eine neue Spalte
(Einheit m oder km). Vorbedingung: projiziertes CRS (FA5). Nicht-Linien (auch
Flächen, deren Umfang ist nicht gemeint) erhalten NaN und werden in einer
Sammelwarnung gemeldet. Zeilen und Reihenfolge bleiben, die Eingabe bleibt
unverändert."""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

from geopandas import GeoDataFrame
from pydantic import Field

from geofact.builtin.operations._geometry import measure_geometries
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step

_FACTOR = {"m": 1.0, "km": 1e3}


class LengthStep(Step):
    """Länge je Linienobjekt (FA68); Ergebnisspalte ``output_field`` oder
    ``length_<unit>``."""

    op: Literal["length"] = "length"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        unit: Literal["m", "km"] = Field("m", description="Einheit der Länge.")
        output_field: Optional[str] = Field(
            None,
            min_length=1,
            description="Name der Ergebnisspalte (Default: length_<unit>).",
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
            self.params.get("output_field") or f"length_{self.params.get('unit', 'm')}"
        }


@register_operation(LengthStep)
def run_length(inputs: dict, params: dict) -> GeoDataFrame:
    unit = params.get("unit", "m")
    return measure_geometries(
        inputs["features"],
        op="length",
        dimension=1,
        measure=lambda gdf: gdf.geometry.length,
        factor=_FACTOR[unit],
        output_field=params.get("output_field") or f"length_{unit}",
        decimals=params.get("decimals"),
        expected="Linien",
    )
