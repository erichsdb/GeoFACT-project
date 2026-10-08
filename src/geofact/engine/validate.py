"""Implements: FA6 (Layer-Validator), FA29/FA63 (deklarierte Typen casten über support.numeric).

Prüft einen geladenen Vektor-Layer gegen die in LayerValidation deklarierten
Regeln: Pflichtattribute, Datentyp, Wertebereich, Koordinatenpräzision (an den
Roh-Strings der Quelle, da abgeschnittene Nachkommastellen nach dem Parsen nicht
mehr erkennbar sind) und räumliche Plausibilität (Containment gegen eine
Referenzgeometrie). on_violation steuert, ob verletzende Objekte markiert,
entfernt oder die Pipeline abgebrochen wird (keine stillen
Fehler); für die räumliche Prüfung kann spatial_check.on_violation es
überschreiben.

Jede Verletzung wird gemeldet: je verletzter Regel eine gesammelte Warnung
(LayerValidationWarning), bei 'drop' zusätzlich die Zahl entfernter Objekte. Der
Executor leitet sie als layer_warning-Ereignisse weiter.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Sequence

from geopandas import GeoDataFrame
from shapely.geometry.base import BaseGeometry

from geofact.core.contracts import LayerValidation
from geofact.support.numeric import coerce_numeric

_PYTHON_TYPE = {"integer": int, "float": float, "string": str}

_SAMPLE_INDICES = 5
"""Beispiel-Indizes je Regel in der gesammelten Warnung."""


class ValidationAbortError(Exception):
    """Wird geworfen, wenn on_violation=abort greift."""


class LayerValidationWarning(UserWarning):
    """Gesammelter Bericht einer verletzten FA6-Regel (eine Warnung je Regel
    und Layer) bzw. der Zusammenfassung entfernter Objekte bei 'drop'."""


@dataclass
class Violation:
    index: int
    rule: str
    detail: str


@dataclass
class ValidationResult:
    gdf: GeoDataFrame
    violations: list[Violation] = field(default_factory=list)


def validate(
    layer_id: str,
    gdf: GeoDataFrame,
    rules: LayerValidation,
    raw_strings: dict[str, Sequence[str]] | None = None,
    region_geometry: BaseGeometry | GeoDataFrame | None = None,
) -> ValidationResult:
    violations: list[Violation] = []

    violations.extend(_check_required_fields(gdf, rules.required_fields))
    violations.extend(_check_field_types(gdf, rules.field_types))
    violations.extend(_check_constraints(gdf, rules.constraints))
    violations.extend(_check_precision(layer_id, gdf, rules.precision, raw_strings))
    if rules.spatial_check is not None:
        violations.extend(
            _check_spatial(layer_id, gdf, rules.spatial_check.within, region_geometry)
        )

    result = _apply_policy(layer_id, gdf, violations, rules)
    # Cast nach der Policy (FA29): bei 'drop' sind verletzende Objekte schon entfernt;
    # bei 'warn' werden nicht konvertierbare Werte zu NA, gemeldet (cast_declared_types).
    result.gdf = cast_declared_types(layer_id, result.gdf, rules.field_types)
    return result


def _check_required_fields(
    gdf: GeoDataFrame, required_fields: list[str]
) -> list[Violation]:
    violations = []
    for field_name in required_fields:
        if field_name not in gdf.columns:
            violations.extend(
                Violation(
                    idx, "required_fields", f"Feld '{field_name}' fehlt in der Quelle"
                )
                for idx in gdf.index
            )
            continue
        for idx, value in gdf[field_name].items():
            if value is None or (isinstance(value, float) and value != value):  # NaN
                violations.append(
                    Violation(
                        idx, "required_fields", f"Feld '{field_name}' fehlt (Wert leer)"
                    )
                )
    return violations


def _matches_declared_type(value, type_name: str) -> bool:
    """Numerische Attribute kommen je nach Quelle als str (OSM-Tags), float
    (GeoJSON/GPKG-Zahlenfelder) oder int an; integer/float akzeptieren alle drei
    Formen, eine strikte isinstance-Prüfung verwürfe reale Layer großflächig."""
    expected = _PYTHON_TYPE[type_name]
    if expected is int:
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        if isinstance(value, float):
            return value.is_integer()
        if isinstance(value, str):
            try:
                return float(value).is_integer()
            except ValueError:
                return False
        return False
    if expected is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if isinstance(value, str):
            try:
                float(value)
                return True
            except ValueError:
                return False
        return False
    return isinstance(value, expected)


def _check_field_types(
    gdf: GeoDataFrame, field_types: dict[str, str]
) -> list[Violation]:
    violations = []
    for field_name, type_name in field_types.items():
        if field_name not in gdf.columns:
            continue
        for idx, value in gdf[field_name].items():
            if value is None or (isinstance(value, float) and value != value):
                continue
            if not _matches_declared_type(value, type_name):
                violations.append(
                    Violation(
                        idx,
                        "field_types",
                        f"'{field_name}': erwartet {type_name}, erhalten {value!r}",
                    )
                )
    return violations


def cast_declared_types(
    layer_id: str, gdf: GeoDataFrame, field_types: dict[str, str]
) -> GeoDataFrame:
    """FA29: führt die in field_types deklarierten Typen auf der Spalte aus. OSM-Tags
    sind ausnahmslos Strings, ein filter wie 'capacity > 250' scheiterte sonst an
    str gegen int; erst der Cast macht die deklarierte Semantik wirksam.

    integer wird auf pandas' nullable Int64 gecastet: OSM-Attribute fehlen oft, und
    numpy-int kann kein NA tragen (die Spalte würde zu float). Nicht konvertierbare
    Werte werden zu NA, aber nicht still (Goldene Regel 7): Anzahl und Beispiele
    gehen als Warnung raus, die Objekte gelten danach als ohne Attribut
    (filter.on_missing entscheidet, FA8)."""
    if not field_types or gdf.empty:
        return gdf

    result = gdf
    for field_name, type_name in field_types.items():
        if field_name not in gdf.columns or type_name == "string":
            continue
        # Gemeinsamer FA63-Helfer. Echte Bruchwerte (z. B. 110000.5) sind schon als
        # field_types-Verletzung gemeldet und werden nicht gerundet (Goldene Regel 7),
        # sondern gelten als fehlend. Gemeldet werden nur Werte, die durch den Cast
        # zu NA wurden, nicht bereits fehlende.
        coercion = coerce_numeric(
            result[field_name], kind="integer" if type_name == "integer" else "float"
        )
        if coercion.lost:
            warnings.warn(
                f"Layer '{layer_id}': Attribut '{field_name}' ist als "
                f"{type_name} deklariert; {coercion.lost} Wert(e) "
                f"ließen sich nicht umwandeln (z. B. {', '.join(coercion.examples)}) "
                f"und gelten ab jetzt als fehlend.",
                stacklevel=2,
            )
        if result is gdf:
            result = gdf.copy()
        result[field_name] = coercion.series
    return result


def _check_constraints(gdf: GeoDataFrame, constraints: dict) -> list[Violation]:
    violations = []
    for field_name, constraint in constraints.items():
        if field_name not in gdf.columns:
            continue
        for idx, raw_value in gdf[field_name].items():
            if raw_value is None or (
                isinstance(raw_value, float) and raw_value != raw_value
            ):
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                # Nicht-numerisch ist ein field_types-Fall.
                continue
            if constraint.min is not None and value < constraint.min:
                violations.append(
                    Violation(
                        idx,
                        "constraints",
                        f"'{field_name}'={value} < min {constraint.min}",
                    )
                )
            if constraint.max is not None and value > constraint.max:
                violations.append(
                    Violation(
                        idx,
                        "constraints",
                        f"'{field_name}'={value} > max {constraint.max}",
                    )
                )
    return violations


def _decimal_places(raw_value: str) -> int:
    if "." not in raw_value:
        return 0
    return len(raw_value.rsplit(".", 1)[-1])


def _check_precision(
    layer_id: str,
    gdf: GeoDataFrame,
    precision: dict,
    raw_strings: dict[str, Sequence[str]] | None,
) -> list[Violation]:
    if not precision:
        return []
    violations = []
    for field_name, rule in precision.items():
        if raw_strings is None or field_name not in raw_strings:
            raise ValueError(
                f"Layer '{layer_id}': precision-Regel auf '{field_name}' braucht "
                "Roh-Strings der Quelle, die hier nicht verfügbar sind"
            )
        raw_values = raw_strings[field_name]
        for idx, raw_value in zip(gdf.index, raw_values):
            if _decimal_places(str(raw_value)) < rule.min_decimals:
                violations.append(
                    Violation(
                        idx,
                        "precision",
                        f"'{field_name}'='{raw_value}' hat weniger als {rule.min_decimals} "
                        "Nachkommastellen",
                    )
                )
    return violations


def _region_geometry_union(
    region_geometry: BaseGeometry | GeoDataFrame,
) -> BaseGeometry:
    if isinstance(region_geometry, GeoDataFrame):
        return region_geometry.union_all()
    return region_geometry


def _check_spatial(
    layer_id: str,
    gdf: GeoDataFrame,
    within: str,
    region_geometry: BaseGeometry | GeoDataFrame | None,
) -> list[Violation]:
    if region_geometry is None:
        raise ValueError(
            f"Layer '{layer_id}': spatial_check.within='{within}' braucht eine "
            "Referenzgeometrie, die hier nicht übergeben wurde"
        )
    reference = _region_geometry_union(region_geometry)
    violations = []
    for idx, geometry in gdf.geometry.items():
        if geometry is None or not reference.contains(geometry):
            violations.append(
                Violation(
                    idx, "spatial_check", f"Geometrie liegt außerhalb von '{within}'"
                )
            )
    return violations


def _policy_for(rule: str, rules: LayerValidation) -> str:
    """Wirksame Policy einer Regel: spatial_check.on_violation überschreibt die des
    Layers nur für die räumliche Prüfung."""
    if rule == "spatial_check" and rules.spatial_check is not None:
        override = rules.spatial_check.on_violation
        if override is not None:
            return override
    return rules.on_violation


def _sample(indices: list) -> str:
    shown = ", ".join(repr(index) for index in indices[:_SAMPLE_INDICES])
    return shown + (", ..." if len(indices) > _SAMPLE_INDICES else "")


def _report(
    layer_id: str,
    violations: list[Violation],
    rules: LayerValidation,
    total: int,
    dropped: int,
) -> None:
    """Je verletzter Regel eine gesammelte Warnung (Anzahl, bis zu fünf Beispiel-
    Indizes, Aktion), nicht je Objekt: 100000 Verletzungen sollen nicht 100000
    Meldungen erzeugen, aber keine soll still verschwinden."""
    by_rule: dict[str, list[Violation]] = {}
    for violation in violations:
        by_rule.setdefault(violation.rule, []).append(violation)
    for rule, items in by_rule.items():
        indices = list(dict.fromkeys(v.index for v in items))
        policy = _policy_for(rule, rules)
        action = "entfernt" if policy == "drop" else "behalten"
        warnings.warn(
            f"Layer '{layer_id}': Regel '{rule}': {len(indices)} Objekt(e) verletzen die Regel "
            f"(Index {_sample(indices)}; z. B. {items[0].detail}) - {action} "
            f"(on_violation: {policy})",
            LayerValidationWarning,
            stacklevel=4,
        )
    if dropped:
        warnings.warn(
            f"Layer '{layer_id}': {dropped} von {total} Objekt(en) entfernt (on_violation: drop)",
            LayerValidationWarning,
            stacklevel=4,
        )


def _apply_policy(
    layer_id: str,
    gdf: GeoDataFrame,
    violations: list[Violation],
    rules: LayerValidation,
) -> ValidationResult:
    if not violations:
        return ValidationResult(gdf=gdf, violations=[])

    aborting = [v for v in violations if _policy_for(v.rule, rules) == "abort"]
    if aborting:
        details = "; ".join(f"[{v.rule}] Index {v.index}: {v.detail}" for v in aborting)
        raise ValidationAbortError(
            f"Layer '{layer_id}': Validierung fehlgeschlagen: {details}"
        )

    bad_indices = {v.index for v in violations if _policy_for(v.rule, rules) == "drop"}
    removable = [i for i in bad_indices if i in gdf.index]
    _report(layer_id, violations, rules, total=len(gdf), dropped=len(removable))
    if removable:
        # drop entfernt verletzende Objekte, warn behält sie.
        gdf = gdf.drop(index=removable)
    return ValidationResult(gdf=gdf, violations=violations)
