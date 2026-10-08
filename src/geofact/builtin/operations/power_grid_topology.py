"""Implements: FA11 (Plugin-Schnittstelle).

Domänenspezifische Operation für Stromnetz-Topologie: filtert Umspannwerke nach
Mindestspannung und baut in einem Schritt das Netzwerk auf (Spannungsfilter plus
build_network). Als reine Neuerstellung ohne Änderung an bestehenden Dateien
Nachweis für NFA4.

OSM liefert ``voltage`` als Text, oft mit mehreren Ebenen (``"110000;20000"``) oder
ohne Zahl (``"low"``). parse_voltage() liest Mehrfachwerte als die höchste Ebene
(ein Umspannwerk 380/110 kV gehört zum 380-kV-Netz). Nicht lesbare und fehlende
Werte werden in einer VoltageParseWarning gezählt; diese Umspannwerke sind nicht
einbezogen, weil min_voltage für sie nicht prüfbar ist.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from typing import ClassVar, Literal

import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field

from geofact.builtin.operations.graph import run_build_network
from geofact.plugin_api import DataType, Graph, ParamsBase, register_operation, Step


_EXAMPLES = 3
"""Beispielwerte je Kategorie in der Warnung."""


class VoltageParseWarning(UserWarning):
    """Spannungswerte, die mehrdeutig (Mehrfachwerte) oder nicht lesbar sind."""


@dataclass(frozen=True)
class VoltageParse:
    """Ergebnis von parse_voltage: ``values`` in Volt (float64, NaN = nicht
    lesbar oder fehlend), dazu je Kategorie die Anzahl und bis zu drei
    Beispielwerte."""

    values: pd.Series
    multi: int
    multi_examples: tuple[str, ...]
    unreadable: int
    unreadable_examples: tuple[str, ...]
    missing: int


def _parse_one(value: object) -> tuple[float, str]:
    """(Spannung in Volt, Kategorie) für einen Rohwert; Kategorie ist
    'ok', 'multi', 'unreadable' oder 'missing'."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return math.nan, "missing"
    if isinstance(value, bool):
        return math.nan, "unreadable"
    if isinstance(value, (int, float)):
        return float(value), "ok"
    text = str(value).strip()
    if not text:
        return math.nan, "missing"
    parts = [part.strip() for part in text.split(";") if part.strip()]
    numbers = []
    for part in parts:
        try:
            numbers.append(float(part))
        except ValueError:
            continue
    numbers = [number for number in numbers if not math.isnan(number)]
    if not numbers:
        return math.nan, "unreadable"
    if len(parts) > 1:
        # OSM-Mehrfachwert: die höchste lesbare Ebene zählt
        return max(numbers), "multi"
    return numbers[0], "ok"


def parse_voltage(series: pd.Series) -> VoltageParse:
    """Liest eine OSM-``voltage``-Spalte als Volt. ``"110000"`` -> 110000,
    ``"110000;20000"`` -> 110000 (höchste Ebene, gezählt als Mehrfachwert),
    ``"low"`` -> NaN (nicht lesbar), leer/fehlend -> NaN (fehlend). Ein Teil
    eines Mehrfachwerts, der keine Zahl ist (``"110000;unknown"``), fällt weg,
    solange ein anderer Teil lesbar ist."""
    parsed = [_parse_one(value) for value in series.tolist()]
    values = pd.Series(
        [number for number, _ in parsed], index=series.index, dtype="float64"
    )
    kinds = pd.Series([kind for _, kind in parsed], index=series.index, dtype="object")

    def examples(kind: str) -> tuple[str, ...]:
        raw = series[kinds == kind]
        return tuple(sorted({repr(value) for value in raw.tolist()})[:_EXAMPLES])

    return VoltageParse(
        values=values,
        multi=int((kinds == "multi").sum()),
        multi_examples=examples("multi"),
        unreadable=int((kinds == "unreadable").sum()),
        unreadable_examples=examples("unreadable"),
        missing=int((kinds == "missing").sum()),
    )


def _report(parse: VoltageParse, total: int) -> None:
    """Eine VoltageParseWarning über Mehrfachwerte, nicht lesbare und fehlende
    Spannungen."""
    parts = []
    if parse.multi:
        parts.append(
            f"{parse.multi} mit mehreren Spannungen (z. B. {', '.join(parse.multi_examples)}) "
            "nach der höchsten Ebene bewertet"
        )
    dropped = parse.unreadable + parse.missing
    if parse.unreadable:
        parts.append(
            f"{parse.unreadable} mit nicht numerischer Spannung "
            f"(z. B. {', '.join(parse.unreadable_examples)})"
        )
    if parse.missing:
        parts.append(f"{parse.missing} ohne Spannungsangabe")
    if not parts:
        return
    tail = (
        f"; diese {dropped} von {total} werden nicht einbezogen (min_voltage nicht prüfbar)"
        if dropped
        else ""
    )
    warnings.warn(
        "power_grid_topology: Spalte 'voltage' der Umspannwerke: "
        + ", ".join(parts)
        + tail
        + ".",
        VoltageParseWarning,
        stacklevel=3,
    )


class PowerGridTopologyStep(Step):
    op: Literal["power_grid_topology"] = "power_grid_topology"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "substations": DataType.VECTOR,
        "power_lines": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.GRAPH

    class Params(ParamsBase):
        min_voltage: float = Field(
            ...,
            gt=0,
            description=(
                "Mindestspannung (V), um Umspannwerke einzubeziehen. OSM-Mehrfachwerte "
                "('110000;20000') zählen mit der höchsten Ebene; nicht lesbare Werte "
                "('low') fallen mit Warnung heraus."
            ),
        )
        tolerance_m: float = Field(
            0, ge=0, description="Fangtoleranz in Metern für den Netzaufbau."
        )


@register_operation(PowerGridTopologyStep)
def run_power_grid_topology(inputs: dict, params: dict) -> Graph:
    substations: GeoDataFrame = inputs["substations"]
    power_lines: GeoDataFrame = inputs["power_lines"]
    min_voltage = params["min_voltage"]
    tolerance_m = params.get("tolerance_m", 0)

    if "voltage" not in substations.columns:
        raise ValueError(
            "power_grid_topology: Layer 'substations' braucht ein 'voltage'-Attribut"
        )

    parse = parse_voltage(substations["voltage"])
    _report(parse, len(substations))
    filtered = substations[parse.values >= min_voltage]

    return run_build_network(
        {"lines": power_lines, "nodes": filtered}, {"tolerance_m": tolerance_m}
    )
