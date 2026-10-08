"""Implements: FA69 (Spaltenauswahl: select_columns).

Behält (``keep``) oder entfernt (``drop``) Attributspalten eines Vektor-Layers,
z. B. um OSM-Layer mit Dutzenden Tags vor einer Ausgabe zu reduzieren.

- genau eines von ``keep``/``drop`` (Querprüfung bei ``validate``);
- die Geometriespalte bleibt immer (bei ``keep`` muss sie nicht genannt werden,
  in ``drop`` ist sie ein Fehler);
- jede genannte Spalte muss existieren, sonst Fehler mit der Liste der
  vorhandenen Spalten; Ausnahme: ein leerer Layer (Quellen ohne Treffer liefern
  oft keine Attributspalten) ergibt einen leeren Layer;
- ``keep`` legt auch die Spaltenreihenfolge fest; Zeilen und ihre Reihenfolge
  bleiben, die Eingabe bleibt unverändert."""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

from geopandas import GeoDataFrame
from pydantic import Field, model_validator

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class SelectColumnsStep(Step):
    """Spaltenauswahl (FA69); Vertrag siehe Modul-Docstring."""

    op: Literal["select_columns"] = "select_columns"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        keep: Optional[list[str]] = Field(
            None,
            min_length=1,
            description="Spalten, die behalten werden (Geometrie bleibt immer).",
        )
        drop: Optional[list[str]] = Field(
            None,
            min_length=1,
            description="Spalten, die entfernt werden (nicht die Geometrie).",
        )

        @model_validator(mode="after")
        def exactly_one_of_keep_and_drop(self) -> "SelectColumnsStep.Params":
            if (self.keep is None) == (self.drop is None):
                raise ValueError("genau eines von 'keep' und 'drop' angeben")
            if self.drop is not None and "geometry" in self.drop:
                raise ValueError(
                    "die Geometriespalte kann nicht entfernt werden ('drop: geometry')"
                )
            return self


@register_operation(SelectColumnsStep)
def run_select_columns(inputs: dict, params: dict) -> GeoDataFrame:
    features: GeoDataFrame = inputs["features"]
    geometry = features.geometry.name
    keep, drop = params.get("keep"), params.get("drop")
    named = keep if keep is not None else drop
    available = [column for column in features.columns if column != geometry]
    missing = [column for column in named if column not in features.columns]
    if missing and not features.empty:
        raise ValueError(
            f"select_columns: Spalte(n) {missing} existieren nicht im Layer "
            f"(vorhanden: {sorted(map(str, available))})"
        )
    if drop is not None and geometry in drop:
        raise ValueError(
            f"select_columns: die Geometriespalte '{geometry}' kann nicht entfernt werden"
        )
    if keep is not None:
        columns = [column for column in dict.fromkeys(keep) if column in available]
    else:
        columns = [column for column in available if column not in set(drop)]
    return features[columns + [geometry]].copy()
