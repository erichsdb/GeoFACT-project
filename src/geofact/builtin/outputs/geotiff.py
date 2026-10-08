"""Implements: FA49 (Raster-Ausgaben: GeoTIFF), FA55 (Ziel-CRS der Ausgabe), FA65 (Lizenz-Tags), FA73 (Tag der Ausgabelizenz).

Schreibt einen Raster-Layer als GeoTIFF (``type: geotiff``): alle Bänder mit
ihren Namen als Bandbeschreibung, CRS, Georeferenzierung und Nodata-Wert wie im
Layer, deflate-komprimiert. Das ist die verlustfreie Raster-Ausgabe (die
Kartenausgabe ``map`` zeigt nur eine verkleinerte Vorschau).

Ein Raster mit Nodata-NaN braucht einen Gleitkommatyp; ein ganzzahliges Raster
mit NaN-Nodata ist ein Fehler (die Ausgabe wird dann mit Grund übersprungen,
nie falsch geschrieben). Die Schreibfunktion teilt sich der Baustein mit dem
Snapshot der Quellart ``stac`` (builtin/_raster_io.py).

Ziel-CRS (FA55): ohne ``crs`` wird im Arbeits-CRS geschrieben, mit ``crs`` wird
das Raster vorher über ``_raster_io.warp_raster`` umprojiziert (``resampling``:
nearest | bilinear | average | sum, Default nearest). ``resampling`` ohne ``crs``
ist ein Fehler, weil es keine Wirkung hätte.

Lizenzen (FA65): ``spec.attribution`` ergibt ``TIFFTAG_COPYRIGHT`` (eine Zeile je
Lizenz, ``; `` getrennt) und die vollständige Attribution als JSON in
``GEOFACT_ATTRIBUTION``; ohne Lizenz keine Tags. Ausgabelizenz (FA73):
``spec.output_license`` steht im eigenen Tag ``GEOFACT_OUTPUT_LICENSE``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from geofact.builtin._raster_io import warp_raster, write_geotiff
from geofact.builtin.outputs._target_crs import check_output_crs
from geofact.plugin_api import DataType, NodeAttribution, RasterLayer, register_output


class GeotiffOptions(BaseModel):
    """Formatspezifische Felder einer geotiff-Ausgabe: Ziel-CRS und Resampling."""

    model_config = ConfigDict(extra="forbid")

    crs: Optional[str] = Field(
        None,
        description="Ziel-CRS der Datei, z. B. EPSG:4326 (Default: Arbeits-CRS des Laufs)",
    )
    resampling: Literal["nearest", "bilinear", "average", "sum"] = Field(
        "nearest",
        description="Resampling beim Umprojizieren (nur zusammen mit crs)",
    )

    @field_validator("crs")
    @classmethod
    def _check_crs(cls, value: Optional[str]) -> Optional[str]:
        return check_output_crs(value)

    @model_validator(mode="after")
    def _resampling_needs_crs(self) -> "GeotiffOptions":
        if self.crs is None and self.resampling != "nearest":
            raise ValueError(
                f"resampling: {self.resampling} wirkt nur zusammen mit crs (Umprojektion) - "
                "crs angeben oder resampling weglassen"
            )
        return self


def attribution_tags(attribution: Optional[NodeAttribution]) -> dict[str, str]:
    """GeoTIFF-Tags der Lizenzen (FA65); leer ohne Lizenz."""
    if attribution is None or attribution.empty:
        return {}
    return {
        "TIFFTAG_COPYRIGHT": "; ".join(attribution.lines()),
        "GEOFACT_ATTRIBUTION": json.dumps(attribution.to_dict(), ensure_ascii=False),
    }


@register_output(
    "geotiff",
    extension="tif",
    accepts=(DataType.RASTER,),
    options_model=GeotiffOptions,
    description="GeoTIFF (alle Bänder, CRS, Nodata, volle Auflösung; optional Ziel-CRS crs)",
)
def _write_geotiff_output(raster: RasterLayer, target: Path, spec) -> None:
    options: Optional[GeotiffOptions] = spec.options
    if options is not None and options.crs is not None:
        raster = warp_raster(raster, options.crs, options.resampling)
    tags = attribution_tags(getattr(spec, "attribution", None))
    output_license = getattr(spec, "output_license", None)
    if output_license is not None:
        tags["GEOFACT_OUTPUT_LICENSE"] = output_license.line()
    write_geotiff(raster, target, tags=tags or None)
