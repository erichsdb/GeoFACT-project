"""Implements: FA48 (Rasterklassifikation: raster_classify).

Ordnet die Zellwerte eines Rasterbandes Klassen zu: ``breaks`` sind aufsteigende
Klassengrenzen, ``values`` die Klassenwerte (Anzahl = breaks + 1). Die
Intervalle sind rechts offen wie bei der Vektor-Operation ``classify``:

    breaks [0.2, 0.5], values [1, 2, 3]
    x < 0.2 -> 1,   0.2 <= x < 0.5 -> 2,   x >= 0.5 -> 3

Nodata-Zellen (und NaN) bleiben Nodata. Das Ergebnis ist ein Ein-Band-Raster:
``uint8`` mit Nodata 255, wenn alle Klassenwerte ganze Zahlen von 0 bis 254 sind,
sonst ``float32`` mit Nodata NaN."""

from __future__ import annotations

from typing import ClassVar, Literal, Optional, Self, Union

import numpy as np
from pydantic import Field, field_validator, model_validator

from geofact.plugin_api import (
    DataType,
    ParamsBase,
    RasterLayer,
    register_operation,
    Step,
)

UINT8_NODATA = 255


def _fits_uint8(values: list[float]) -> bool:
    return all(float(v).is_integer() and 0 <= v <= UINT8_NODATA - 1 for v in values)


def classify_raster(
    raster: RasterLayer,
    breaks: list[float],
    values: list[float],
    band: int | str | None = None,
    output_band: str = "class",
) -> RasterLayer:
    """Klassifiziert ein Band von ``raster`` (siehe Modul-Docstring)."""
    if len(values) != len(breaks) + 1:
        raise ValueError(
            f"raster_classify: {len(values)} values passen nicht zu {len(breaks)} breaks "
            f"(erwartet {len(breaks) + 1})"
        )
    data = raster.band_data(band)
    nodata_cells = ~raster.valid_mask(band)
    if np.issubdtype(data.dtype, np.floating):
        nodata_cells = nodata_cells | np.isnan(data)

    class_index = np.digitize(data, np.asarray(breaks, dtype=np.float64), right=False)
    if _fits_uint8(values):
        lookup = np.asarray(values, dtype=np.uint8)
        result = lookup[class_index]
        result[nodata_cells] = UINT8_NODATA
        nodata: float = UINT8_NODATA
    else:
        lookup = np.asarray(values, dtype=np.float32)
        result = lookup[class_index]
        result[nodata_cells] = np.nan
        nodata = float("nan")
    return RasterLayer(
        data=result,
        transform=raster.transform,
        crs=raster.crs,
        path=raster.path,
        nodata=nodata,
        band_names=[output_band],
    )


class RasterClassifyStep(Step):
    op: Literal["raster_classify"] = "raster_classify"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"raster": DataType.RASTER}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.RASTER

    class Params(ParamsBase):
        breaks: list[float] = Field(
            ...,
            min_length=1,
            description="Aufsteigende Klassengrenzen (Intervalle rechts offen: untere Grenze "
            "gehört zur oberen Klasse).",
        )
        values: list[float] = Field(
            ..., description="Klassenwerte im Ergebnisraster (Anzahl = breaks + 1)."
        )
        band: Optional[Union[int, str]] = Field(
            None,
            description="Band eines Mehrband-Rasters: Bandname oder 1-basierte Nummer. "
            "Ohne Angabe nur bei einem Ein-Band-Raster erlaubt.",
        )
        output_band: str = Field("class", description="Name des Ergebnisbandes.")

        @field_validator("breaks")
        @classmethod
        def breaks_ascending(cls, value: list[float]) -> list[float]:
            if any(b <= a for a, b in zip(value, value[1:])):
                raise ValueError(f"breaks müssen streng aufsteigend sein: {value}")
            return value

        @model_validator(mode="after")
        def values_match_breaks(self) -> Self:
            if len(self.values) != len(self.breaks) + 1:
                raise ValueError(
                    f"Anzahl values ({len(self.values)}) muss Anzahl breaks + 1 "
                    f"({len(self.breaks) + 1}) sein"
                )
            return self

        @field_validator("output_band")
        @classmethod
        def output_band_is_identifier(cls, value: str) -> str:
            if not value.isidentifier():
                raise ValueError(
                    f"output_band '{value}' muss ein Name aus Buchstaben, Ziffern und Unterstrich sein"
                )
            return value


@register_operation(RasterClassifyStep)
def run_raster_classify(inputs: dict, params: dict) -> RasterLayer:
    return classify_raster(
        inputs["raster"],
        params["breaks"],
        params["values"],
        band=params.get("band"),
        output_band=params.get("output_band", "class"),
    )
