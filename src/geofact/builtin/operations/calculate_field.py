"""Implements: FA68 (Feldrechnung: calculate_field).

Wertet einen Ausdruck über Attributspalten aus und schreibt das Ergebnis in eine
Spalte, z. B. ``gruen_m2 / area_m2`` oder ``where(einwohner > 0, kw / einwohner, 0)``.

Die Ausdruckssprache ist die von ``raster_calc`` (FA47), mit demselben Parser und
Auswerter: Zahlen, Spaltennamen, ``+ - * / **``, Vergleiche, ``& | ~`` auf
Vergleichen und die Funktionen ``where``, ``abs``, ``sqrt``, ``log``, ``clip``,
``minimum``, ``maximum``. Kein ``eval``; ein ungültiger Ausdruck ist schon bei
``validate`` ein Fehler (``ExpressionError`` mit Position).

Laufzeit:
- eine im Ausdruck genannte, im Layer fehlende Spalte ist ein Fehler, der die
  vorhandenen Spalten nennt;
- Spaltenwerte werden numerisch gelesen; nicht konvertierbare Werte werden NaN und
  in einer Warnung gezählt (``geofact.support.numeric.require_numeric``, FA63);
- nicht endliche Ergebnisse (Division durch 0, ``log(0)``) werden NaN und gemeldet;
  ein Vergleich mit fehlendem Wert ist NaN, nicht ``falsch``;
- ein Wahrheitswert-Ausdruck ergibt 1.0/0.0 (float, NaN bei fehlendem Wert);
- leerer Layer -> leeres Ergebnis mit der Spalte.
Zeilenzahl und Reihenfolge bleiben; die Eingabe wird nicht verändert."""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal, Optional

import numpy as np
import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field, field_validator

from geofact.builtin.operations._geometry import require_new_column
from geofact.builtin.operations.raster_calc import (
    _Evaluator,
    ExpressionError,
    parse_expression,
    referenced_names,
)
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step
from geofact.support.numeric import require_numeric


class CalculateFieldStep(Step):
    """Feldrechnung über Spalten (FA68); Vertrag siehe Modul-Docstring."""

    op: Literal["calculate_field"] = "calculate_field"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        expression: str = Field(
            ...,
            description="Ausdruck über Spaltennamen, z. B. 'gruen_m2 / area_m2'. Erlaubt: "
            "Zahlen, Spaltennamen, + - * / **, Vergleiche, & | ~ auf Vergleichen "
            "und die Funktionen where, abs, sqrt, log, clip, minimum, maximum.",
        )
        output_field: str = Field(
            ..., description="Name der Ergebnisspalte (Bezeichner)."
        )
        decimals: Optional[int] = Field(
            None,
            ge=0,
            le=12,
            strict=True,
            description="Nachkommastellen (Default: ungerundet).",
        )
        overwrite: bool = Field(
            False, description="Eine vorhandene Spalte gleichen Namens überschreiben."
        )

        @field_validator("expression")
        @classmethod
        def expression_is_valid(cls, value: str) -> str:
            try:
                parse_expression(value)
            except ExpressionError as exc:
                raise ValueError(
                    str(exc).replace(
                        "kein Band - er müsste ein Band-Name sein (z. B. nir)",
                        "keine Spalte - er müsste einen Spaltennamen nennen",
                    )
                ) from None
            return value

        @field_validator("output_field")
        @classmethod
        def output_field_is_identifier(cls, value: str) -> str:
            if not value.isidentifier():
                raise ValueError(
                    f"output_field '{value}' muss ein Name aus Buchstaben, Ziffern und Unterstrich "
                    "sein (er ist in Folgeausdrücken als Spaltenname verwendbar)"
                )
            return value


def _numeric_column(features: GeoDataFrame, column: str) -> np.ndarray:
    """Liest eine Spalte numerisch (float64) über den FA63-Helfer; Verluste
    meldet ``require_numeric`` in einer Warnung. Wahrheitswerte zählen hier als
    1.0/0.0 (Ausdrücke wie ``flag * 10``)."""
    original = features[column]
    if pd.api.types.is_bool_dtype(original):
        return original.to_numpy(dtype="float64", na_value=np.nan)
    if original.dtype == object:
        # Wahrheitswerte mit Lücken (GeoJSON/OSM) sind eine object-Spalte; auch
        # dort gelten sie als 1.0/0.0
        is_bool = original.map(lambda value: isinstance(value, (bool, np.bool_)))
        if is_bool.any():
            original = original.where(
                ~is_bool,
                original.map(
                    lambda value: (
                        float(value) if isinstance(value, (bool, np.bool_)) else value
                    )
                ),
            )
    values = require_numeric(original, where="calculate_field", column=column)
    return values.to_numpy(dtype="float64", na_value=np.nan)


@register_operation(CalculateFieldStep)
def run_calculate_field(inputs: dict, params: dict) -> GeoDataFrame:
    features: GeoDataFrame = inputs["features"]
    expression = params["expression"]
    output_field = params["output_field"]
    decimals = params.get("decimals")
    require_new_column(
        features,
        output_field,
        op="calculate_field",
        overwrite=params.get("overwrite", False),
    )
    result = features.copy()
    if features.empty:
        result[output_field] = pd.Series(dtype="float64", index=result.index)
        return result

    tree = parse_expression(expression)
    names = referenced_names(tree)
    available = [
        column for column in features.columns if column != features.geometry.name
    ]
    unknown = [name for name in names if name not in available]
    if unknown:
        raise ValueError(
            f"calculate_field: Ausdruck '{expression}' nennt unbekannte Spalte(n) {unknown}; "
            f"vorhanden: {sorted(map(str, available))}"
        )

    columns = {name: _numeric_column(features, name) for name in names}
    missing_input = np.zeros(len(features), dtype=bool)
    for values in columns.values():
        missing_input |= np.isnan(values)
    evaluator = _Evaluator(expression, columns, (len(features),))
    with np.errstate(all="ignore"):
        values = evaluator.evaluate(tree)
        if not isinstance(values, np.ndarray) or values.shape != (len(features),):
            raise ValueError(
                f"calculate_field: Ausdruck '{expression}' ergibt keinen Wert je Objekt"
            )
        values = values.astype("float64")
        values[evaluator.invalid] = np.nan
        not_finite = ~np.isfinite(values) & ~missing_input & ~evaluator.invalid
        values[~np.isfinite(values)] = np.nan
    if not_finite.any():
        warnings.warn(
            f"calculate_field: Ausdruck '{expression}' ergibt für {int(not_finite.sum())} "
            "Objekt(e) keinen endlichen Wert (z. B. Division durch 0); dort ist "
            f"'{output_field}' NaN",
            stacklevel=2,
        )
    if decimals is not None:
        values = np.round(values, decimals)
    result[output_field] = values
    return result
