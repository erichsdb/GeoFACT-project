"""Implements: FA63 (typisierte Vergleiche, kein lexikografischer Zahlenvergleich).

Shared helper for every operation that compares or computes on attribute
values: a column that is meant as a number is read as a number, values
that cannot be read are counted and reported, never silently dropped
(Golden Rule 7).

OSM tags and many table sources deliver every value as text
(``capacity="250"``). Comparing or sorting such a column without a cast is
either a crash (``'>' not supported between 'str' and 'int'``) or, worse, a
silently wrong lexicographic answer (``"9" > "100"``). ``coerce_numeric``
does the cast and counts the losses, ``require_numeric`` additionally
emits one warning per column naming the caller (``where``), the column, the
count and up to three example values.

Infrastructure (FA45): registers nothing, imports only pandas/numpy.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Literal

import pandas as pd

_EXAMPLES = 3
"""Example values per warning."""

_INT64_LIMIT = 2.0**63
"""Floats with an absolute value from here on do not fit into int64."""


class NumericCoercionWarning(UserWarning):
    """Values of a column meant as numbers could not be read as numbers and
    now count as missing (FA63)."""


@dataclass(frozen=True)
class Coercion:
    """Result of a numeric cast: the cast ``series`` (same index), how many
    values were set before and are missing after the cast (``lost``) and up
    to three of those original values as ``repr`` strings (``examples``)."""

    series: pd.Series
    lost: int
    examples: tuple[str, ...]


def is_numeric_series(series: pd.Series) -> bool:
    """True for int/float dtypes (incl. nullable Int64/Float64), False for
    booleans and text - a boolean column is not a number to compare with."""
    return pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(
        series
    )


def coerce_numeric(
    series: pd.Series, *, kind: Literal["float", "integer"] = "float"
) -> Coercion:
    """Read ``series`` as numbers. ``float`` yields float64, ``integer``
    yields pandas' nullable ``Int64`` (a missing value must not turn the
    whole column into floats). With ``integer`` a real fraction (``110000.5``)
    or a value outside int64 (``1e30``) is not rounded silently but counts as
    not convertible. Booleans are not
    numbers here: ``True`` is lost, not read as 1."""
    original = series
    if pd.api.types.is_bool_dtype(original):
        values = pd.Series(float("nan"), index=original.index, dtype="float64")
    elif is_numeric_series(original):
        values = original
    else:
        as_object = original.astype(object)
        # True/False inside a text column are not numbers either
        is_bool = as_object.map(lambda value: isinstance(value, bool))
        values = pd.to_numeric(as_object.where(~is_bool), errors="coerce")
    if kind == "integer":
        # no detour via float64: large integers (ids) stay exact
        if not pd.api.types.is_integer_dtype(values):
            fractional = values.notna() & (values != values.round())
            # outside int64 ('1e30') there is no integer to read: lost, not a crash
            out_of_range = values.notna() & (values.abs() >= _INT64_LIMIT)
            values = values.where(~(fractional | out_of_range))
        values = values.astype("Int64")
    else:
        values = values.astype("float64")
    lost_mask = original.notna() & values.isna()
    lost = int(lost_mask.sum())
    examples: tuple[str, ...] = ()
    if lost:
        examples = tuple(
            sorted({repr(value) for value in original[lost_mask].head(_EXAMPLES)})
        )
    return Coercion(series=values, lost=lost, examples=examples)


def require_numeric(
    series: pd.Series, *, where: str, column: str, declared: str | None = None
) -> pd.Series:
    """``series`` as numbers for a comparison or calculation in ``where``
    (e.g. ``"classify"``). An already numeric column is returned unchanged.
    A text column is cast (``declared == "integer"`` -> ``Int64``, otherwise
    float64); values that cannot be read become missing and are reported in
    one ``NumericCoercionWarning``: "<where>: Spalte '<column>' ...
    n Wert(e) nicht numerisch (z. B. ...), gelten als fehlend"."""
    if is_numeric_series(series):
        if declared == "integer" and not pd.api.types.is_integer_dtype(series):
            result = coerce_numeric(series, kind="integer")
        else:
            return series
    else:
        result = coerce_numeric(
            series, kind="integer" if declared == "integer" else "float"
        )
    if result.lost:
        typed = f" (deklariert als {declared})" if declared else ""
        warnings.warn(
            f"{where}: Spalte '{column}'{typed} wird numerisch gelesen; "
            f"{result.lost} Wert(e) nicht numerisch (z. B. {', '.join(result.examples)}), "
            f"gelten als fehlend.",
            NumericCoercionWarning,
            stacklevel=3,
        )
    return result.series
