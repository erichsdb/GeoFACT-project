"""Implements: FA62 (Durchlassknoten ``gate``, Laufzeitteil).

Datenabhängige Verzweigung bleibt außerhalb des DAG (``when`` wertet zur
Expansionszeit nur Parameter aus, ``core/conditions.py``). ``op: gate`` reicht
einen Vektor-Layer durch, wenn seine Zeilen- und Spaltenbedingungen gelten:

    steps:
      - id: nur_mit_treffern
        op: gate
        inputs: {features: treffer}
        params: {min_rows: 1, require_fields: [name], on_fail: empty}

Bedingungen: ``min_rows`` (Standard 1), ``max_rows`` (optional),
``require_fields``. Gilt eine nicht, entscheidet ``on_fail``:

- ``empty`` (Standard): leerer Rahmen mit denselben Spalten und demselben CRS,
  der Zweig dahinter läuft auf null Zeilen weiter; gemeldet als
  ``GateClosedNotice``.
- ``warn``: der Layer geht unverändert weiter, ``GateWarning`` nennt die
  verletzten Bedingungen.
- ``error``: der Schritt scheitert.

``min_rows: 0`` lässt auch den leeren Layer passieren. Das Ergebnis ist immer
eine Kopie.
"""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal, Optional, Self

from geopandas import GeoDataFrame
from pydantic import Field, model_validator

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class GateClosedNotice(UserWarning):
    """gate hat den Durchlass geschlossen (on_fail: empty): der Schritt liefert
    den leeren Rahmen mit denselben Spalten und demselben CRS."""


class GateWarning(UserWarning):
    """gate-Bedingung verletzt, der Layer geht trotzdem weiter (on_fail: warn)."""


class GateStep(Step):
    """Reicht einen Vektor-Layer durch, wenn Zeilen-/Spaltenbedingungen gelten
    (FA62): min_rows, max_rows, require_fields. Sonst nach on_fail: leerer
    Rahmen mit Hinweis (empty), Durchlass mit Warnung (warn) oder Fehler
    (error). min_rows: 0 lässt auch den leeren Layer passieren."""

    op: Literal["gate"] = "gate"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        min_rows: int = Field(
            1, ge=0, description="Mindestzahl Zeilen (0 = auch leer durchlassen)."
        )
        max_rows: Optional[int] = Field(
            None, ge=0, description="Höchstzahl Zeilen (optional)."
        )
        require_fields: list[str] = Field(
            default_factory=list, description="Spalten, die vorhanden sein müssen."
        )
        on_fail: Literal["empty", "warn", "error"] = Field(
            "empty",
            description="Verhalten bei verletzter Bedingung: empty = leerer Rahmen mit Hinweis, "
            "warn = Durchlass mit Warnung, error = Fehler.",
        )

        @model_validator(mode="after")
        def check_bounds(self) -> Self:
            if self.max_rows is not None and self.max_rows < self.min_rows:
                raise ValueError(
                    f"max_rows ({self.max_rows}) ist kleiner als min_rows ({self.min_rows})"
                )
            return self


def _rows(n: int) -> str:
    return f"{n} Zeile" if n == 1 else f"{n} Zeilen"


def _failed_conditions(features: GeoDataFrame, params: dict) -> list[str]:
    failed: list[str] = []
    n = len(features)
    min_rows = params.get("min_rows", 1)
    max_rows = params.get("max_rows")
    if n < min_rows:
        failed.append(f"min_rows: {min_rows}, der Layer hat {_rows(n)}")
    if max_rows is not None and n > max_rows:
        failed.append(f"max_rows: {max_rows}, der Layer hat {_rows(n)}")
    missing = [f for f in params.get("require_fields", []) if f not in features.columns]
    if missing:
        present = sorted(
            str(c) for c in features.columns if c != features.geometry.name
        )
        failed.append(
            f"require_fields: {', '.join(repr(f) for f in missing)} fehlt "
            f"(vorhanden: {present})"
        )
    return failed


@register_operation(GateStep)
def run_gate(inputs: dict[str, GeoDataFrame], params: dict) -> GeoDataFrame:
    features: GeoDataFrame = inputs["features"]
    failed = _failed_conditions(features, params)
    if not failed:
        return features.copy()

    reason = "; ".join(failed)
    on_fail = params.get("on_fail", "empty")
    if on_fail == "error":
        raise ValueError(f"gate: Bedingung nicht erfüllt ({reason})")
    if on_fail == "warn":
        warnings.warn(
            f"gate: Bedingung nicht erfüllt ({reason}) - der Layer geht unverändert weiter "
            "(on_fail: warn)",
            GateWarning,
            stacklevel=2,
        )
        return features.copy()
    warnings.warn(
        f"gate: Bedingung nicht erfüllt ({reason}) - Durchlass geschlossen, der Schritt "
        "liefert den leeren Layer (on_fail: empty)",
        GateClosedNotice,
        stacklevel=2,
    )
    return features.iloc[0:0].copy()
