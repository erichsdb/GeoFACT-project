"""Implements: FA35 (Sortieren und auf die N ersten Objekte begrenzen), FA63 (typisierte Vergleiche).

top_n sortiert einen Vektor-Layer nach einem numerischen Attribut und behält die
ersten N Objekte ("die 5 größten X nach Y"). Das Ergebnis ist nach dem Attribut
sortiert, damit es als Rangliste lesbar ist. Der Sortierschlüssel wird numerisch
gelesen (wie filter, FA29); Objekte ohne verwertbaren Wert fallen heraus und
werden gemeldet.
"""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal, Optional

import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step
from geofact.support.numeric import require_numeric


class TopNStep(Step):
    """Sortiert 'features' nach params.by und behält die ersten
    params.n Objekte (FA35). order=desc (Default) liefert die größten,
    order=asc die kleinsten Werte. Das Ergebnis ist nach 'by' sortiert.

    Ein Layer mit weniger als n Objekten wird vollständig
    zurückgegeben (kein Fehler - "die 5 größten" bei nur 3 Objekten
    sind eben diese 3). Objekte ohne numerisch verwertbaren Wert in 'by'
    können nicht eingeordnet werden und fallen heraus; ihre Anzahl wird
    gemeldet."""

    op: Literal["top_n"] = "top_n"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        by: str = Field(
            ..., description="Numerisches Attribut, nach dem sortiert wird."
        )
        n: int = Field(
            ..., ge=1, strict=True, description="Anzahl der zu behaltenden Objekte."
        )
        order: Literal["desc", "asc"] = Field(
            "desc", description="desc = größte Werte zuerst, asc = kleinste."
        )
        rank_field: Optional[str] = Field(
            None,
            description="Wenn gesetzt, wird der Rang (1..n) als dieses Attribut ausgegeben.",
        )

    def produced_fields(self) -> set[str]:
        rank_field = self.params.get("rank_field")
        return {rank_field} if rank_field else set()


@register_operation(TopNStep)
def run_top_n(inputs: dict, params: dict) -> GeoDataFrame:
    features: GeoDataFrame = inputs["features"]
    by = params["by"]
    n = params["n"]
    order = params.get("order", "desc")
    rank_field = params.get("rank_field")

    if features.empty:
        result = features.copy()
        if rank_field:
            result[rank_field] = pd.Series(dtype="int64")
        return result

    if by not in features.columns:
        raise ValueError(
            f"top_n: Attribut '{by}' existiert nicht im Layer "
            f"(vorhanden: {sorted(c for c in features.columns if c != 'geometry')})"
        )

    # Werte aus OSM und Tabellen kommen oft als String an; lexikographisch
    # sortiert wäre "9" > "100".
    numeric = require_numeric(features[by], where="top_n", column=by)
    # Ganzzahlen bleiben Ganzzahlen: über float64 fielen ids oberhalb 2**53
    # auf denselben Wert und der falsche Rang gewänne
    exact = pd.api.types.is_integer_dtype(numeric)
    sort_values = numeric if exact else numeric.astype("float64")
    unranked = int(sort_values.isna().sum())
    if unranked and unranked < len(features):
        warnings.warn(
            f"top_n: {unranked} Objekt(e) ohne numerischen Wert in '{by}' "
            f"können keinen Rang erhalten und entfallen.",
            stacklevel=2,
        )

    ranked = features.assign(_top_n_sort=sort_values)
    ranked = ranked[ranked["_top_n_sort"].notna()]
    if exact:
        ranked["_top_n_sort"] = ranked["_top_n_sort"].astype("int64")
    if ranked.empty:
        warnings.warn(
            f"top_n: kein Objekt hat einen numerisch verwertbaren Wert in "
            f"'{by}' - das Ergebnis ist leer.",
            stacklevel=2,
        )
        result = features.iloc[0:0].copy()
        if rank_field:
            result[rank_field] = pd.Series(dtype="int64")
        return result

    # mergesort ist stabil: bei Gleichstand bleibt die Eingabereihenfolge (NFA1).
    ranked = ranked.sort_values(
        "_top_n_sort", ascending=(order == "asc"), kind="mergesort"
    )
    result = ranked.head(n).drop(columns="_top_n_sort")
    if rank_field:
        result[rank_field] = range(1, len(result) + 1)
    return result
