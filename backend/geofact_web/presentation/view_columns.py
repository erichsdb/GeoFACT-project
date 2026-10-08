"""Implements: FA12 (Ergebnisse visualisieren - Attributauswahl der Web-Ansicht).

Auswahl der Attributspalten, die die interaktive Knotenansicht
(/api/runs/{id}/nodes/{node}) ausliefert.

OSM-Layer sind breit und dünn besetzt (ein place=*-Layer hat über 800 Spalten), und
GeoJSON schreibt alle Spalten pro Feature aus. Die Auswahl ist zweistufig:

1. Pflichtspalten: alles, worauf die Konfiguration Bezug nimmt (Filter, classify.field,
   ranking.by, color_field, validation ...) plus die Felder aus Step.produced_fields().
   Sie bleiben immer erhalten, auch bei dünner Belegung.
2. Auffüllen bis zu einem Budget mit den am dichtesten belegten Spalten.

Verworfene Spalten werden gemeldet (Regel 7); der ZIP-Download enthält alle Attribute.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Iterable

from geopandas import GeoDataFrame

if TYPE_CHECKING:  # pragma: no cover - nur für Typprüfung
    from geofact.api import Scenario

# Standardbudget an Attributspalten (ohne Geometrie) für die Ansicht.
DEFAULT_COLUMN_BUDGET = 10

# Parameternamen, deren Wert Spaltennamen enthält; neue Operationen erben das
# Verhalten ohne Sonderbehandlung, wenn sie dieselben Namen verwenden (NFA4).
_COLUMN_PARAMS = ("field", "by", "output_field", "group_field")
# Parameter mit pandas-Ausdruck, aus dem Bezeichner extrahiert werden.
_EXPRESSION_PARAMS = ("condition",)

# Schlüsselwörter und Literale in Filterausdrücken, die keine Spaltennamen sind.
_EXPRESSION_STOPWORDS = frozenset(
    {"and", "or", "not", "in", "is", "None", "True", "False", "nan"}
)

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_:]*")


def _identifiers_in_expression(expression: str) -> set[str]:
    """Bezeichner eines pandas-Ausdrucks, ohne String-Literale.

    "place == 'village' and distance > 5000" -> {"place", "distance"};
    'village' ist ein Wert, kein Spaltenname."""
    without_literals = re.sub(r"'[^']*'|\"[^\"]*\"", " ", expression)
    return {
        match.group(0)
        for match in _IDENTIFIER.finditer(without_literals)
        if match.group(0) not in _EXPRESSION_STOPWORDS
    }


def _as_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple)):
        return {v for v in value if isinstance(v, str)}
    return set()


def required_columns(scenario: "Scenario") -> set[str]:
    """Alle Spaltennamen, auf die die Konfiguration Bezug nimmt.

    Bewusst großzügig (alle Schritte, nicht nur die dem Knoten vorgelagerten).
    Falsch-positive Namen schaden nicht, sie fehlen im Layer und fallen beim Schnitt
    mit den echten Spalten weg."""
    names: set[str] = set()

    for step in scenario.steps:
        params = getattr(step, "params", {}) or {}
        for key in _COLUMN_PARAMS:
            names |= _as_names(params.get(key))
        for key in _EXPRESSION_PARAMS:
            value = params.get(key)
            if isinstance(value, str):
                names |= _identifiers_in_expression(value)
        # Erzeugte Felder deklariert jede Operation selbst (Step.produced_fields(), NFA4).
        names |= step.produced_fields()

    for layer in scenario.layers:
        validation = getattr(layer, "validation", None)
        if validation is not None:
            names |= set(validation.required_fields)
            names |= set(validation.field_types)
            names |= set(validation.constraints)
            names |= set(validation.precision)
        # TableLayer: deklarativ gemappte Spalten.
        names |= set(getattr(layer, "columns", {}) or {})

    for spec in scenario.output:
        # color_field ist eine Option des Formats 'map' (FA40).
        color_field = getattr(spec.options, "color_field", None)
        if color_field:
            names.add(color_field)

    return names


def select_columns(
    gdf: GeoDataFrame,
    pinned: Iterable[str] = (),
    budget: int = DEFAULT_COLUMN_BUDGET,
) -> tuple[list[str], int]:
    """Wählt die auszuliefernden Spalten aus.

    pinned bleibt immer erhalten (soweit im Layer vorhanden), auch bei
    sehr dünner Belegung. Der Rest wird nach Belegungsdichte aufgefüllt,
    bis budget Attributspalten erreicht sind. Vollständig leere Spalten
    werden nie aufgefüllt - sie tragen keine Information.

    Rückgabe: (Spaltenliste inkl. Geometriespalte, Anzahl verworfener
    Attributspalten)."""
    geom_col = gdf.geometry.name if gdf.geometry is not None else "geometry"
    attribute_cols = [c for c in gdf.columns if c != geom_col]
    if not attribute_cols:
        return list(gdf.columns), 0

    pinned_present = [c for c in attribute_cols if c in set(pinned)]
    keep = list(pinned_present)

    if len(keep) < budget:
        non_null = gdf[attribute_cols].notna().sum()
        candidates = [
            c for c in attribute_cols if c not in set(keep) and non_null[c] > 0
        ]
        # Dichteste zuerst; bei Gleichstand die Layer-Reihenfolge (deterministisch, Golden-Tests).
        candidates.sort(key=lambda c: (-int(non_null[c]), attribute_cols.index(c)))
        keep.extend(candidates[: budget - len(keep)])

    # Ausgabereihenfolge = Reihenfolge im Layer, nicht Auswahlreihenfolge.
    keep_set = set(keep)
    ordered = [c for c in attribute_cols if c in keep_set]
    dropped = len(attribute_cols) - len(ordered)
    return ordered + [geom_col], dropped
