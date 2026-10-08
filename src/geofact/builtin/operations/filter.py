"""Implements: FA8 (räumliche Operationen: filter), FA29 (numerischer Vergleich auf String-Spalten), FA63 (typisierte Vergleiche).

Filtert Objekte per pandas-Ausdruck mit on_missing-Policy. Welche Spalten eine
Bedingung verwendet, steht im Syntaxbaum (``ast``), nicht in einer
Teilstring-Suche: eine Spalte 'a' gilt nicht als referenziert, nur weil der
Buchstabe in einem anderen Namen oder Stringliteral vorkommt.

FA63: Text wird nie stillschweigend mit einem Ordnungsoperator verglichen.
``types: {spalte: integer|float|string|boolean}`` deklariert den Typ einer Spalte
(Cast über ``geofact.support.numeric``). Ein zahlartiges Textliteral an
``<, <=, >, >=`` (``population >= '100000'``) ist ohne ``types: {population:
string}`` schon bei der Validierung ein Fehler an ``params -> condition``, eine
Text-Spalte an einem Ordnungsoperator ohne Deklaration ein Laufzeitfehler.
``==``/``!=``/``in`` auf Text bleiben erlaubt. ``on_missing: exclude`` zählt und
meldet die entfernten Objekte."""

from __future__ import annotations

import ast
import re
import warnings
from typing import ClassVar, Literal, Self

import numpy as np
import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field, model_validator

from geofact.core.errors import custom_line_error, located_error
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step
from geofact.support.numeric import NumericCoercionWarning, require_numeric


# pandas schreibt Spaltennamen mit Leerzeichen in Backticks, die ast nicht lesen
# kann: vor dem Parsen durch Platzhalter ersetzen, danach zurückübersetzen.
_BACKTICK_NAME = re.compile(r"`([^`]+)`")

_COMPARISON_OPERATORS = (ast.Eq, ast.NotEq, ast.Gt, ast.GtE, ast.Lt, ast.LtE)
_ORDERING_OPERATORS = (ast.Gt, ast.GtE, ast.Lt, ast.LtE)
_OPERATOR_SYMBOL = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Lt: "<",
    ast.LtE: "<=",
}
_NUMBER_TEXT = re.compile(r"^\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*$")
_BOOLEAN_TEXT = {
    "true": True,
    "yes": True,
    "1": True,
    "false": False,
    "no": False,
    "0": False,
}

DeclaredType = Literal["integer", "float", "string", "boolean"]


def _parse_condition(condition: str) -> tuple[ast.Expression, dict[str, str]]:
    """Syntaxbaum der Bedingung plus Platzhalter -> Spaltenname (Backticks). Eine
    nicht lesbare Bedingung ist ein Fehler, kein Rückfall auf Teilstring-Suche."""
    placeholders: dict[str, str] = {}

    def _replace(match: re.Match) -> str:
        key = f"__geofact_col_{len(placeholders)}__"
        placeholders[key] = match.group(1)
        return key

    text = _BACKTICK_NAME.sub(_replace, condition)
    try:
        return ast.parse(text.strip(), mode="eval"), placeholders
    except SyntaxError as exc:
        raise ValueError(
            f"filter: Bedingung '{condition}' ist kein gültiger Ausdruck "
            f"(Python-Syntax, nicht SQL): {exc.msg}"
        ) from exc


def _referenced_columns(condition: str, columns) -> list[str]:
    """Spalten, die condition als Namen verwendet (in Spaltenreihenfolge)."""
    tree, placeholders = _parse_condition(condition)
    used = {
        placeholders.get(node.id, node.id)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }
    return [column for column in columns if column in used]


def _is_number_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _numerically_compared_columns(condition: str, columns) -> list[str]:
    """Spalten, die in condition gegen ein Zahlenliteral verglichen werden (FA29),
    in beiden Operandenreihenfolgen. Nur Vergleichsoperatoren, damit z. B.
    "name.str.contains('3')" nicht als numerisch gilt."""
    tree, placeholders = _parse_condition(condition)
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for operator, left, right in zip(node.ops, operands, operands[1:]):
            if not isinstance(operator, _COMPARISON_OPERATORS):
                continue
            for name_side, other_side in ((left, right), (right, left)):
                if isinstance(name_side, ast.Name) and _is_number_literal(other_side):
                    names.add(placeholders.get(name_side.id, name_side.id))
    return [column for column in columns if column in names]


def _column_comparisons(condition: str):
    """(Spalte, Operator, anderer Operand) je Vergleich "Name op X" bzw.
    "X op Name" in condition - für die FA63-Prüfungen."""
    tree, placeholders = _parse_condition(condition)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for operator, left, right in zip(node.ops, operands, operands[1:]):
            for name_side, other_side in ((left, right), (right, left)):
                if isinstance(name_side, ast.Name):
                    yield (
                        placeholders.get(name_side.id, name_side.id),
                        operator,
                        other_side,
                    )


def _literal_text(node: ast.AST) -> str:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        sign = "-" if isinstance(node.op, ast.USub) else ""
        return sign + _literal_text(node.operand)
    return repr(node.value) if isinstance(node, ast.Constant) else ast.unparse(node)


def _type_conflicts(condition: str, types: dict[str, str]) -> list[str]:
    """Implements: FA63 - Validierungsfehler der Bedingung: (a) zahlartiges
    Textliteral an einem Ordnungsoperator ohne types: {spalte: string}
    (würde lexikografisch verglichen: '9' > '100'); (b) als string
    deklarierte Spalte gegen ein Zahlenliteral (wäre immer ungleich)."""
    problems: list[str] = []
    for column, operator, other in _column_comparisons(condition):
        symbol = _OPERATOR_SYMBOL.get(type(operator))
        if symbol is None:
            continue
        declared = types.get(column)
        if (
            isinstance(operator, _ORDERING_OPERATORS)
            and isinstance(other, ast.Constant)
            and isinstance(other.value, str)
            and _NUMBER_TEXT.match(other.value)
            and declared != "string"
        ):
            problems.append(
                f"Textliteral '{other.value}' wird mit '{symbol}' verglichen (Spalte "
                f"'{column}') - Text wird lexikografisch verglichen ('9' > '100'): Zahl "
                f"ohne Anführungszeichen schreiben ({column} {symbol} {other.value.strip()}) "
                f"oder types: {{{column}: string}} angeben"
            )
        elif declared == "string" and _is_number_literal(other):
            problems.append(
                f"Spalte '{column}' ist als string deklariert (types), wird aber mit "
                f"'{symbol}' gegen die Zahl {_literal_text(other)} verglichen - Zahl in "
                f"Anführungszeichen setzen oder types: {{{column}: integer}} angeben"
            )
    return problems


def _boolean_values(original: pd.Series, column: str) -> pd.Series:
    """types: boolean - true/false, yes/no, 1/0 (Text, ohne Groß-/
    Kleinschreibung) und echte Wahrheitswerte; alles andere wird fehlend
    und gemeldet (FA63)."""

    def convert(value):
        if value is None or (isinstance(value, float) and value != value):
            return None
        if isinstance(value, (bool, np.bool_)):
            return bool(value)
        return _BOOLEAN_TEXT.get(str(value).strip().lower())

    converted = original.map(convert).astype("boolean")
    lost = original.notna() & converted.isna()
    if lost.any():
        examples = sorted({repr(v) for v in original[lost].head(3)})
        warnings.warn(
            f"filter: Spalte '{column}' (deklariert als boolean) wird als Wahrheitswert "
            f"gelesen; {int(lost.sum())} Wert(e) nicht lesbar (z. B. {', '.join(examples)}), "
            f"gelten als fehlend.",
            NumericCoercionWarning,
            stacklevel=3,
        )
    return converted


def _apply_declared_types(gdf: GeoDataFrame, types: dict[str, str]) -> GeoDataFrame:
    """Implements: FA63 - führt params.types auf den Spalten aus. Eine
    deklarierte, aber fehlende Spalte ist ein Fehler."""
    if not types or gdf.empty:
        return gdf
    missing = [column for column in types if column not in gdf.columns]
    if missing:
        available = sorted(c for c in gdf.columns if c != gdf.geometry.name)
        raise ValueError(
            f"filter: types nennt Spalte(n) {missing!r}, die im Layer fehlen "
            f"(vorhanden: {available})"
        )
    result = gdf.copy()
    for column, declared in types.items():
        if declared in ("integer", "float"):
            result[column] = require_numeric(
                gdf[column], where="filter", column=column, declared=declared
            )
        elif declared == "boolean":
            result[column] = _boolean_values(gdf[column], column)
        else:
            result[column] = (
                gdf[column].map(lambda v: v if pd.isna(v) else str(v)).astype(object)
            )
    return result


def _check_text_ordering(
    gdf: GeoDataFrame, condition: str, types: dict[str, str]
) -> None:
    """Implements: FA63 - Laufzeit-Schutz: eine Text-Spalte an einem
    Ordnungsoperator ohne Deklaration wird nicht lexikografisch verglichen,
    sondern abgelehnt."""
    for column, operator, _ in _column_comparisons(condition):
        if not isinstance(operator, _ORDERING_OPERATORS) or column not in gdf.columns:
            continue
        if types.get(column) == "string":
            continue
        dtype = gdf[column].dtype
        if pd.api.types.is_object_dtype(dtype) or pd.api.types.is_string_dtype(dtype):
            symbol = _OPERATOR_SYMBOL[type(operator)]
            raise ValueError(
                f"filter: Spalte '{column}' hat Typ Text ({dtype}) und wird mit "
                f"'{symbol}' verglichen - types: {{{column}: integer}} angeben (oder "
                f"{{{column}: string}} für einen bewussten Textvergleich)"
            )


def _coerce_numeric_comparisons(
    gdf: GeoDataFrame, condition: str, declared: frozenset[str] = frozenset()
) -> GeoDataFrame:
    """Implements: FA29 - castet String-Spalten, die gegen ein Zahlenliteral
    verglichen werden, vor der eval()-Auswertung nach numerisch. OSM liefert alle
    Tagwerte als String, 'capacity > 250' brach sonst mit einem TypeError ab.
    Nicht konvertierbare Werte werden NaN, vom on_missing-Pfad wie fehlende
    Attribute behandelt und gemeldet. Numerische Spalten bleiben unangetastet;
    ein echter Stringvergleich ("capacity == '250'") löst den Cast nicht aus."""
    candidates = [
        column
        for column in _numerically_compared_columns(condition, gdf.columns)
        if column not in declared
    ]
    if not candidates:
        return gdf

    result = gdf
    for column in candidates:
        if pd.api.types.is_numeric_dtype(gdf[column]):
            continue
        original = gdf[column]
        converted = pd.to_numeric(original, errors="coerce")
        newly_missing = original.notna() & converted.isna()
        if newly_missing.any():
            examples = sorted({repr(v) for v in original[newly_missing].head(3)})
            warnings.warn(
                f"filter: Spalte '{column}' wird in der Bedingung "
                f"'{condition}' numerisch verglichen, enthielt aber "
                f"{int(newly_missing.sum())} nicht-numerische(n) Wert(e) "
                f"(z. B. {', '.join(examples)}); diese gelten als fehlend "
                f"(on_missing entscheidet).",
                stacklevel=2,
            )
        if result is gdf:
            result = gdf.copy()
        result[column] = converted
    return result


class FilterStep(Step):
    """condition wird per pandas DataFrame.eval() ausgewertet - PYTHON-
    Syntax, NICHT SQL: Gleichheit ist '==' (nicht '='), Verknüpfung ist
    'and'/'or' (nicht 'AND'/'OR'), Ungleichheit ist '!='. Beispiele:
    "risk_score >= 0.5", "place == 'village' and distance > 10000",
    "category != 'excluded' or score > 0.8"."""

    op: Literal["filter"] = "filter"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        condition: str = Field(
            ...,
            description="pandas-Ausdruck (Python-Syntax: ==, and, or - nicht SQL), "
            'z. B. "risk_score >= 0.5" oder '
            "\"place == 'village' and distance > 10000\".",
        )
        on_missing: Literal["exclude", "include", "error"] = Field(
            "exclude",
            description="Policy für Objekte ohne das gefilterte Attribut: exclude entfernt sie "
            "(gezählt und gemeldet), include lässt sie passieren, error bricht ab.",
        )
        types: dict[str, DeclaredType] = Field(
            default_factory=dict,
            description="Typ je Spalte (integer, float, string, boolean): die Spalte wird vor "
            "dem Vergleich so gelesen; Text an <, <=, >, >= nur mit string.",
        )

        @model_validator(mode="after")
        def condition_is_typed(self) -> Self:
            try:
                problems = _type_conflicts(self.condition, self.types)
            except ValueError:
                # unlesbare Bedingung: die Meldung kommt zur Laufzeit
                return self
            if problems:
                raise located_error(
                    "FilterStep.Params",
                    [
                        custom_line_error(
                            "typed_comparison", problem, ("condition",), self.condition
                        )
                        for problem in problems
                    ],
                )
            return self


@register_operation(FilterStep)
def run_filter(inputs: dict, params: dict) -> GeoDataFrame:
    condition = params["condition"]
    on_missing = params.get("on_missing", "exclude")
    types = params.get("types") or {}

    # Erst nach dem Cast steht fest, welche Werte fehlen. Deklarierte Typen (types)
    # gelten auch für das Ergebnis, der implizite Cast (FA29) nur für die
    # Auswertung: zurück kommen die Originalwerte ('0100' bleibt '0100').
    original = _apply_declared_types(inputs["features"], types)
    gdf = _coerce_numeric_comparisons(original, condition, frozenset(types))
    if not gdf.empty:
        _check_text_ordering(gdf, condition, types)

    referenced_columns = _referenced_columns(condition, gdf.columns)
    if referenced_columns:
        has_missing = gdf[referenced_columns].isna().any(axis=1)
    else:
        has_missing = pd.Series(False, index=gdf.index)

    if has_missing.any() and on_missing == "error":
        raise ValueError(
            f"filter: Attribut für condition '{condition}' fehlt bei "
            f"{int(has_missing.sum())} Objekten (on_missing=error)"
        )

    missing = has_missing.to_numpy(dtype=bool)
    keep = np.zeros(len(gdf), dtype=bool)
    complete = gdf[~missing]
    if not complete.empty:
        mask = complete.eval(condition)
        if np.isscalar(mask):
            # condition ohne Spalte (z. B. "1 == 1"): eval() liefert einen bool statt
            # einer Maske; complete[bool] würde als Spaltenname gelesen (KeyError)
            mask = np.full(len(complete), bool(mask))
        keep[~missing] = np.asarray(mask, dtype=bool)

    if on_missing == "include":
        return original[keep | missing]
    if has_missing.any():
        dropped = int(has_missing.sum())
        everything = " (alle)" if dropped == len(gdf) else ""
        columns = ", ".join(f"'{c}'" for c in referenced_columns)
        warnings.warn(
            f"filter: {dropped} von {len(gdf)} Objekt(en){everything} ohne Wert für "
            f"{columns} in condition '{condition}' entfernt (on_missing: exclude)",
            stacklevel=2,
        )
    return original[keep]
