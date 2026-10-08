"""Implements: FA59 (Nachbedingung 5: parameter overrides as text from the command line or a web form).

Input of parameter values is a concern of the delivery side (CLI ``--param``,
web form), not of the configuration core: ``coerce_cli_value`` reads one text
by the declared ``ParameterSpec`` and ``parameter_overrides`` turns a list of
``NAME=VALUE`` (or ``{name: text}``) into typed overrides for
``load_scenario(parameters=...)``. ``geofact.api`` re-exports
``parameter_overrides``.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

import yaml

from geofact.core.conditions import kind_of, text_of
from geofact.core.errors import ConfigError
from geofact.core.expansion import ParameterSpec


def coerce_cli_value(spec: ParameterSpec, text: Any) -> Any:
    """A value from the command line or a form. ``text`` is the raw text or already
    the result of ``yaml.safe_load``; lists/mappings are read as JSON (lists also as
    ``a,b,c``). Afterwards the strict type check of the parameter applies (``ValueError``)."""
    if not isinstance(text, str):
        if spec.type == "string" and kind_of(text) in ("number", "bool"):
            return spec.check(text_of(text))
        if spec.type == "list" and isinstance(text, list) and spec.items != "mapping":
            item_spec = ParameterSpec(type=spec.items)
            items = []
            for index, item in enumerate(text):
                try:
                    items.append(coerce_cli_value(item_spec, item))
                except ValueError as exc:
                    raise ValueError(f"Element {index}: {exc}") from None
            return spec.check(items)
        return spec.check(text)
    raw = text.strip()
    if spec.type == "string":
        return spec.check(text)
    if spec.type == "integer":
        try:
            return spec.check(int(raw))
        except ValueError:
            raise ValueError(f"'{text}' ist keine ganze Zahl") from None
    if spec.type == "float":
        try:
            return spec.check(float(raw))
        except ValueError:
            raise ValueError(f"'{text}' ist keine Zahl") from None
    if spec.type == "boolean":
        lowered = raw.lower()
        if lowered in ("true", "yes", "1", "ja"):
            return spec.check(True)
        if lowered in ("false", "no", "0", "nein"):
            return spec.check(False)
        raise ValueError(f"'{text}' ist kein Wahrheitswert (true/false)")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
        if spec.type != "list":
            raise ValueError(
                f"'{text}' ist kein Mapping (JSON-Objekt erwartet)"
            ) from None
    if spec.type == "list" and isinstance(value, list):
        # JSON list: coerce the elements like single values ([14713000] -> ['14713000'] for items: string)
        return coerce_cli_value(spec, value)
    if spec.type == "list":
        # No JSON array: text a,b,c or [a, b]; a single element is a one-element list.
        inner = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw
        pieces = [
            piece.strip().strip("'\"") for piece in inner.split(",") if piece.strip()
        ]
        if spec.items == "mapping":
            raise ValueError(
                f"'{text}' ist keine Liste von Mappings (JSON erwartet)"
            ) from None
        item_spec = ParameterSpec(type=spec.items)
        value = [coerce_cli_value(item_spec, piece) for piece in pieces]
    return spec.check(value)


def _split_assignment(text: str) -> tuple[str, str]:
    name, sep, value = text.partition("=")
    if not sep or not name.strip():
        raise ConfigError(
            [
                (
                    "--param",
                    f"'{text}' ist keine Zuweisung - erwartet wird NAME=WERT (z. B. --param radius=2)",
                )
            ]
        )
    return name.strip(), value


def parameter_overrides(
    raw: Mapping[str, Any], assignments: Sequence[str] | Mapping[str, Any]
) -> dict[str, Any]:
    """Overrides as text (``NAME=VALUE`` or ``{name: text}``) -> typed values for
    ``load_scenario(parameters=...)``, read by the parameters declared in ``raw``.
    An unknown name stays unchanged; ``load_scenario`` reports it. Errors:
    ``ConfigError`` at ``--param`` (no assignment) or ``parameters -> <name>``."""
    pairs = (
        list(assignments.items())
        if isinstance(assignments, Mapping)
        else [_split_assignment(text) for text in assignments]
    )
    if not pairs:
        return {}
    declared = raw.get("parameters")
    declared = declared if isinstance(declared, dict) else {}
    values: dict[str, Any] = {}
    issues: list[tuple[str, str]] = []
    for name, text in pairs:
        try:
            spec = (
                ParameterSpec.model_validate(declared[name])
                if name in declared
                else None
            )
        except ValueError:
            spec = (
                None  # load_scenario reports the broken declaration with its position
            )
        if spec is None:
            values[name] = text
            continue
        value: Any = text
        # YAML only for mappings and lists of mappings: for other lists YAML would turn
        # a single element such as 'NO' into a boolean; coerce_cli_value reads those itself.
        if isinstance(text, str) and (
            spec.type == "mapping" or (spec.type == "list" and spec.items == "mapping")
        ):
            try:
                value = yaml.safe_load(text)
            except yaml.YAMLError:
                value = text
        try:
            values[name] = coerce_cli_value(spec, value)
        except ValueError as exc:
            issues.append((f"parameters -> {name}", f"Überschreibung ungültig: {exc}"))
    if issues:
        raise ConfigError(issues)
    return values
