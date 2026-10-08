"""Implements: FA8 (räumliche Operationen: ranking), FA63 (typisierte Vergleiche).

Gewichteter Score über mehrere min-max-normalisierte Attribute. Standard ist
"größer ist besser"; ``ascending`` kehrt einzelne Kriterien um (z. B.
Entfernung). Die Kriterien werden über ``geofact.support.numeric.require_numeric``
gelesen (FA63): Text-Zahlen werden Zahlen, ein Objekt ohne lesbaren Wert erhält
den Score NaN und wird gemeldet."""

from __future__ import annotations

from typing import ClassVar, Literal, Optional, Self

import warnings

import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field, StrictBool, model_validator

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step
from geofact.support.numeric import require_numeric


class RankingStep(Step):
    op: Literal["ranking"] = "ranking"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        by: list[str] = Field(..., description="Zu kombinierende numerische Attribute.")
        weights: list[float] = Field(
            ..., description="Gewichte je Attribut; müssen sich zu 1.0 summieren."
        )
        ascending: Optional[list[StrictBool]] = Field(
            None,
            description="Richtung je Attribut aus 'by': false (Standard) = größerer "
            "Wert ist besser, true = kleinerer Wert ist besser.",
        )
        output_field: str = Field(
            "score", description="Name des erzeugten Score-Attributs."
        )

        @model_validator(mode="after")
        def weights_valid(self) -> Self:
            if len(self.by) != len(self.weights):
                raise ValueError("Anzahl 'by' und 'weights' müssen gleich sein")
            if self.weights and abs(sum(self.weights) - 1.0) > 1e-6:
                raise ValueError("weights müssen sich zu 1.0 summieren")
            if self.ascending is not None and len(self.ascending) != len(self.by):
                raise ValueError(
                    f"Anzahl 'ascending' ({len(self.ascending)}) und 'by' "
                    f"({len(self.by)}) müssen gleich sein"
                )
            return self


@register_operation(RankingStep)
def run_ranking(inputs: dict, params: dict) -> GeoDataFrame:
    gdf: GeoDataFrame = inputs["features"]
    by = params["by"]
    weights = params["weights"]
    output_field = params.get("output_field", "score")
    ascending = params.get("ascending")
    if ascending is None:
        ascending = [False] * len(by)
    if len(ascending) != len(by):
        raise ValueError(
            f"ranking: Anzahl 'ascending' ({len(ascending)}) und 'by' ({len(by)}) müssen gleich sein"
        )

    result = gdf.copy()
    score = pd.Series(0.0, index=gdf.index)
    for field, weight, smaller_is_better in zip(by, weights, ascending):
        if gdf.empty and field not in gdf.columns:
            continue
        # fehlendes Attribut bei Objekten: KeyError aus dem Zugriff (FA9-Test)
        column = require_numeric(gdf[field], where="ranking", column=field).astype(
            "float64"
        )
        span = column.max() - column.min()
        if not span:
            normalized = pd.Series(0.0, index=gdf.index)
        elif smaller_is_better:
            normalized = (column.max() - column) / span
        else:
            normalized = (column - column.min()) / span
        score = score + normalized * weight
    unscored = int(score.isna().sum())
    if unscored:
        warnings.warn(
            f"ranking: {unscored} von {len(gdf)} Objekt(en) ohne numerischen Wert in "
            f"{by} erhalten keinen Score (NaN)",
            stacklevel=2,
        )
    result[output_field] = score
    return result
