"""Implements: FA8 (räumliche Operationen: classify), FA63 (typisierte Vergleiche).

Klassifiziert ein numerisches Attribut in benannte Klassen. Das Attribut wird
über ``geofact.support.numeric.require_numeric`` gelesen (FA63): Text-Zahlen
werden Zahlen, nicht lesbare Werte erhalten keine Klasse (NaN) und werden
gemeldet. ``breaks`` müssen streng aufsteigend sein."""

from __future__ import annotations

from typing import ClassVar, Literal, Self

import numpy as np
import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field, model_validator

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step
from geofact.support.numeric import require_numeric


class ClassifyStep(Step):
    op: Literal["classify"] = "classify"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        field: str = Field(
            ..., description="Numerisches Attribut, das klassifiziert wird."
        )
        breaks: list[float] = Field(..., description="Aufsteigende Klassengrenzen.")
        labels: list[str] = Field(
            ..., description="Klassennamen (Anzahl = breaks + 1)."
        )

        @model_validator(mode="after")
        def labels_match_breaks(self) -> Self:
            if any(
                later <= earlier for earlier, later in zip(self.breaks, self.breaks[1:])
            ):
                raise ValueError(
                    f"breaks müssen streng aufsteigend sein (erhalten: {self.breaks})"
                )
            if len(self.labels) != len(self.breaks) + 1:
                raise ValueError(
                    f"Anzahl labels ({len(self.labels)}) "
                    f"muss Anzahl breaks + 1 ({len(self.breaks) + 1}) sein"
                )
            return self

    def produced_fields(self) -> set[str]:
        # feste Spalte 'class', kein output_field-Parameter
        return {"class"}


@register_operation(ClassifyStep)
def run_classify(inputs: dict, params: dict) -> GeoDataFrame:
    gdf: GeoDataFrame = inputs["features"]
    field = params["field"]
    breaks = params["breaks"]
    labels = params["labels"]

    result = gdf.copy()
    bin_edges = [-np.inf, *breaks, np.inf]
    if gdf.empty and field not in gdf.columns:
        result["class"] = pd.Categorical([], categories=labels)
        return result
    # fehlendes Attribut bei Objekten: KeyError aus dem Zugriff (FA8-Vertrag)
    values = require_numeric(gdf[field], where="classify", column=field).astype(
        "float64"
    )
    result["class"] = pd.cut(values, bins=bin_edges, labels=labels, right=False)
    return result
