"""Implements: FA59 (scenario parameters and placeholders), FA75 (modules, instances and the fan-in selector), FA62 (placement of ``when``).

Macro expansion before validation

A pure function ``dict -> ExpansionReport``: a raw scenario with ``parameters:``,
``modules:``, ``instances:``, placeholders (``${name}``, ``${name.key}``),
``when`` and selectors becomes a flat scenario without these keys. Validation,
``ExecutionPlan`` and lazy loading work on the expanded, static DAG.

- Module: a named, typed subgraph (``args``, ``layers``, ``steps``, ``exports``),
  checked once in ``_check_module``. Instance: binds a module to arguments
  (``with``), optionally once per element of a static list (``foreach``);
  generated ids are ``<instance>[<key>].<inner id>`` (``<instance>.<inner id>``
  without foreach). Selector ``<instance>[*].<export>`` as the whole value of
  ``steps[].inputs`` becomes one input per element; no operation is named here.
- Placeholders: ``${name}`` as the whole value keeps its type, embedded it
  becomes text; ``$${`` is a literal ``${``. Parameters are visible everywhere,
  the loop variable only in ``key``, ``with`` and ``when``; no name may shadow
  a parameter.
- Inner references in a module body (``inputs`` values and ``within``, see
  ``_REFERENCE_KEYS``) are rewritten to generated ids. Authored references may
  name a generated node only if it is an export.
- ``when`` (FA62, evaluator in core/conditions.py) drops nodes and instance
  elements; they are listed in ``dropped`` and ``dropped_ids``.
- Hand-written ids must not contain ``[`` or ``]`` (reserved for the namespace).

Every error is a ``ConfigError`` with its position; generated nodes are named
by their domain position (``NodeOrigin.location``, ``ExpansionReport.relocate``).
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from geofact.core.conditions import (
    IDENTIFIER,
    ExpansionFail,
    Loc,
    Scope,
    evaluate_when,
    kind_of,
    lookup,
    text_of,
    value_label,
)
from geofact.core.errors import ConfigError, german_message

SECTIONS = ("layers", "steps", "output")
MODULE_SECTIONS = ("layers", "steps")
ROOT_EXPANSION_KEYS = frozenset({"parameters", "modules", "instances"})

MAX_NODES_ENV = "GEOFACT_MAX_EXPANDED_NODES"
DEFAULT_MAX_NODES = 5000

SELECTOR = re.compile(
    r"^(?P<instance>[A-Za-z_][A-Za-z0-9_]*)\[\*\]\.(?P<export>[A-Za-z_][A-Za-z0-9_-]*)$"
)
_INNER_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_PLACEHOLDER = re.compile(r"\$\$\{|\$\{([^{}]*)\}")
_REFERENCE_KEYS = ("within",)
_INSTANCE_KEYS = ("id", "module", "foreach", "key", "with", "when")

# node keys of the removed syntax (FA60/FA61): rejected with a pointer to FA75
_REMOVED_NODE_KEYS = {
    "foreach": "foreach gibt es nur an einer Instanz (instances)",
    "use": "use gibt es nicht mehr - ein Modul (modules) über eine Instanz (instances) einsetzen",
    "with": "with gibt es nur an einer Instanz (instances)",
    "collect": 'collect ist ein normaler Schritt: op: collect mit inputs: "instanz[*].export"',
}
_MODULE_FORBIDDEN = {
    **_REMOVED_NODE_KEYS,
    "instances": "Module enthalten keine instances (keine verschachtelten Module)",
    "modules": "Module enthalten keine modules (keine verschachtelten Module)",
}
_BRACKETS = "darf '[' und ']' nicht enthalten (reserviert für Instanz-ids wie stadt[berlin].anteil)"

_TYPE_LABELS = {
    "string": "Text",
    "integer": "ganze Zahl",
    "float": "Zahl",
    "boolean": "Wahrheitswert (true/false)",
    "list": "Liste",
    "mapping": "Mapping",
}


# --- Declarations ---


def _check_scalar(kind: str, value: Any) -> Any:
    """``value`` checked against a type name (float from int); ValueError otherwise."""
    ok = (
        (kind == "string" and isinstance(value, str))
        or (
            kind == "integer" and isinstance(value, int) and not isinstance(value, bool)
        )
        or (
            kind == "float"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
        or (kind == "boolean" and isinstance(value, bool))
        or (kind == "mapping" and isinstance(value, dict))
        or (kind == "list" and isinstance(value, list))
    )
    if not ok:
        raise ValueError(
            f"erwartet {_TYPE_LABELS[kind]}, erhalten {value_label(value)} ({value!r})"
        )
    return float(value) if kind == "float" else value


class ParameterSpec(BaseModel):
    """A declared scenario parameter (FA59) or a typed module argument (FA75).
    ``default`` None = required (override, or ``with`` at the instance)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "integer", "float", "boolean", "list", "mapping"] = Field(
        description="Typ des Werts"
    )
    items: Literal["string", "integer", "float", "boolean", "mapping"] = Field(
        "string", description="Typ der Listenelemente (nur bei type: list)"
    )
    default: Any = Field(
        None, description="Standardwert; ohne Angabe ist der Parameter Pflicht"
    )
    description: Optional[str] = Field(
        None, description="Beschreibung für Editor und Web-Formular"
    )
    choices: Optional[list[Any]] = Field(
        None, description="erlaubte Werte (bei Listen: erlaubte Elemente)"
    )

    @model_validator(mode="after")
    def check_default_and_choices(self) -> "ParameterSpec":
        if self.choices is not None:
            if not self.choices:
                raise ValueError("choices darf nicht leer sein")
            if self.type == "mapping":
                raise ValueError("choices ist bei type: mapping nicht möglich")
            element_kind = self.items if self.type == "list" else self.type
            for choice in self.choices:
                try:
                    _check_scalar(element_kind, choice)
                except ValueError as exc:
                    raise ValueError(f"choices: {exc}") from None
        if self.default is not None:
            try:
                self.default = self.check(self.default)
            except ValueError as exc:
                raise ValueError(f"default: {exc}") from None
        return self

    def check(self, value: Any) -> Any:
        """The checked value (float from int) or ValueError."""
        value = _check_scalar(self.type, value)
        if self.type == "list":
            checked = []
            for index, item in enumerate(value):
                try:
                    checked.append(_check_scalar(self.items, item))
                except ValueError as exc:
                    raise ValueError(f"Element {index}: {exc}") from None
            value = checked
        if self.choices is not None:
            for element in value if self.type == "list" else [value]:
                if element not in self.choices:
                    raise ValueError(
                        f"{element!r} ist nicht erlaubt (choices: {self.choices})"
                    )
        return value


class ModuleSpec(BaseModel):
    """A module (FA75): a named, typed subgraph. Shape only; the structural
    rules (ids, exports, reserved keys) are checked by ``_check_module``."""

    model_config = ConfigDict(extra="forbid")

    description: Optional[str] = Field(None, description="Beschreibung des Moduls")
    args: dict[str, ParameterSpec] = Field(
        default_factory=dict,
        description="Argumente: name -> {type, default, ...}; ohne default Pflicht in with",
    )
    layers: list[dict[str, Any]] = Field(
        default_factory=list, description="Layer des Moduls (ids ohne Platzhalter)"
    )
    steps: list[dict[str, Any]] = Field(
        default_factory=list, description="Schritte des Moduls"
    )
    exports: list[str] = Field(
        default_factory=list,
        description="ids der Layer/Schritte, die von außen referenzierbar sind",
    )

    def inner_ids(self) -> list[str]:
        return [
            node["id"] for section in MODULE_SECTIONS for node in getattr(self, section)
        ]


# --- Origin and report ---


@dataclass(frozen=True)
class NodeOrigin:
    """Where a node of the expanded scenario comes from: the authored list
    item (``section``, ``index``) or a module instance (``module``,
    ``instance``, element ``key``, ``inner_id``; ``index`` = instances item)."""

    section: str
    index: int
    module: Optional[str] = None
    instance: Optional[str] = None
    key: Optional[str] = None
    inner_id: Optional[str] = None

    @property
    def generated(self) -> bool:
        return self.module is not None

    @property
    def instance_name(self) -> Optional[str]:
        """``stadt[berlin]`` with foreach, ``stadt`` without, None if authored."""
        if self.instance is None:
            return None
        return f"{self.instance}[{self.key}]" if self.key is not None else self.instance

    def location(self) -> tuple[Any, ...]:
        """Authored: ``(section, index)``; generated: ``("instances",
        "stadt[berlin]", section, inner_id)``."""
        if not self.generated:
            return (self.section, self.index)
        return ("instances", str(self.instance_name), self.section, str(self.inner_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "index": self.index,
            "module": self.module,
            "instance": self.instance,
            "key": self.key,
            "inner_id": self.inner_id,
            "location": " -> ".join(map(str, self.location())),
        }


@dataclass(frozen=True)
class InstanceInfo:
    """One instance: its module, the element keys in list order (empty
    without foreach) and the keys dropped by ``when``."""

    id: str
    module: str
    keys: tuple[str, ...]
    dropped_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpansionReport:
    """Result of ``expand``.

    parameters/values/overridden: declared parameters, effective values,
    overridden names. origins: (section, index in the expanded scenario) ->
    origin; node_origins: id -> origin. instances: one ``InstanceInfo`` per
    expanded instance. dropped: (location, when expression) per removed item;
    dropped_ids: id a removed item would have produced -> expression.
    node_count: emitted nodes; expanded: the flat scenario."""

    parameters: Mapping[str, ParameterSpec]
    values: Mapping[str, Any]
    overridden: tuple[str, ...]
    origins: Mapping[tuple[str, int], NodeOrigin]
    node_origins: Mapping[str, NodeOrigin]
    instances: tuple[InstanceInfo, ...]
    dropped: tuple[tuple[tuple[str, ...], str], ...]
    dropped_ids: Mapping[str, str]
    node_count: int
    expanded: dict

    def is_trivial(self) -> bool:
        return not self.parameters and not self.instances and not self.dropped

    def relocate(self, loc: tuple[Any, ...]) -> tuple[Any, ...]:
        """A position in the expanded scenario -> the authored position:
        ``("steps", 17, "params", "x")`` -> ``("instances", "stadt[berlin]",
        "steps", "anteil", "params", "x")``; an authored node gets its source
        index back. Unknown positions stay unchanged."""
        if len(loc) >= 2 and loc[0] in SECTIONS and isinstance(loc[1], int):
            origin = self.origins.get((loc[0], loc[1]))
            if origin is not None:
                return (*origin.location(), *loc[2:])
        return tuple(loc)


# --- Substitution ---


def _substitute_text(text: str, scope: Scope, loc: Loc) -> Any:
    matches = list(_PLACEHOLDER.finditer(text))
    if not matches:
        if "${" in text:
            raise ExpansionFail(
                loc, f"Platzhalter in '{text}' ist nicht geschlossen (fehlt '}}'?)"
            )
        return text
    first = matches[0]
    if len(matches) == 1 and first.group(0) != "$${" and first.span() == (0, len(text)):
        return lookup(first.group(1).strip(), scope, loc)
    parts: list[str] = []
    position = 0
    for match in matches:
        parts.append(text[position : match.start()])
        if match.group(0) == "$${":
            parts.append("${")
        else:
            reference = match.group(1).strip()
            value = lookup(reference, scope, loc)
            if isinstance(value, (dict, list)):
                raise ExpansionFail(
                    loc,
                    f"Platzhalter '${{{reference}}}' ist {value_label(value)} und kann nicht in "
                    f"den Text '{text}' eingesetzt werden - ${{name.key}} für einen einzelnen "
                    "Wert nutzen oder den Platzhalter als ganzen Wert schreiben",
                )
            if value is None:
                raise ExpansionFail(
                    loc, f"Platzhalter '${{{reference}}}' hat keinen Wert (null)"
                )
            parts.append(text_of(value))
        position = match.end()
    rest = text[position:]
    if "${" in rest.replace("$${", ""):
        raise ExpansionFail(
            loc, f"Platzhalter in '{text}' ist nicht geschlossen (fehlt '}}'?)"
        )
    parts.append(rest)
    return "".join(parts)


def _substitute(value: Any, scope: Scope, loc: Loc) -> Any:
    if isinstance(value, str):
        return _substitute_text(value, scope, loc)
    if isinstance(value, dict):
        result: dict[Any, Any] = {}
        for key, item in value.items():
            new_key = key
            if isinstance(key, str):
                new_key = _substitute_text(key, scope, (*loc, key))
                if isinstance(new_key, (dict, list)) or new_key is None:
                    raise ExpansionFail(
                        (*loc, key),
                        "ein Schlüssel muss nach dem Einsetzen ein Text sein",
                    )
                new_key = text_of(new_key)  # ${n} as a whole key: 5 -> '5'
            if new_key in result:
                raise ExpansionFail(
                    (*loc, str(key)),
                    f"Schlüssel '{new_key}' entsteht nach dem Einsetzen doppelt - ein Wert würde "
                    "still überschrieben",
                )
            result[new_key] = _substitute(item, scope, (*loc, str(key)))
        return result
    if isinstance(value, list):
        return [
            _substitute(item, scope, (*loc, str(index)))
            for index, item in enumerate(value)
        ]
    return copy.deepcopy(value)


# --- Module bodies (checked once) ---


def _check_module(
    name: str, module: ModuleSpec, parameters: Mapping[str, Any]
) -> list[tuple[Loc, str]]:
    """Structural rules of a module body, independent of any instance."""
    base = ("modules", name)
    issues: list[tuple[Loc, str]] = []
    for arg in module.args:
        if not IDENTIFIER.match(arg):
            issues.append(
                (
                    (*base, "args", arg),
                    f"Argumentname '{arg}' ist kein Bezeichner ([A-Za-z_][A-Za-z0-9_]*)",
                )
            )
        elif arg in parameters:
            issues.append(
                (
                    (*base, "args", arg),
                    f"Modulargument '{arg}' verdeckt Parameter gleichen Namens - anderen Namen wählen",
                )
            )
    if not module.layers and not module.steps:
        issues.append((base, "ein Modul braucht mindestens einen Layer oder Schritt"))
    seen: set[str] = set()
    for section in MODULE_SECTIONS:
        for index, node in enumerate(getattr(module, section)):
            where = (*base, section, str(index))
            for key, hint in _MODULE_FORBIDDEN.items():
                if key in node:
                    issues.append(((*where, key), hint))
            node_id = node.get("id")
            if node_id is None:
                issues.append(((*where, "id"), "Pflichtfeld fehlt"))
            elif isinstance(node_id, str) and ("[" in node_id or "]" in node_id):
                issues.append(((*where, "id"), f"id '{node_id}' {_BRACKETS}"))
            elif not isinstance(node_id, str) or not _INNER_ID.match(node_id):
                issues.append(
                    (
                        (*where, "id"),
                        f"id {node_id!r} im Modul muss ein Bezeichner ohne Platzhalter "
                        "sein ([A-Za-z_][A-Za-z0-9_-]*)",
                    )
                )
            elif node_id in seen:
                issues.append(((*where, "id"), f"id '{node_id}' ist im Modul doppelt"))
            else:
                seen.add(node_id)
            inputs = node.get("inputs")
            if (
                section == "steps"
                and inputs is not None
                and not isinstance(inputs, dict)
            ):
                issues.append(
                    (
                        (*where, "inputs"),
                        "inputs im Modul muss ein Mapping sein (port: quelle); "
                        "Selektoren gibt es nur in steps auf oberster Ebene",
                    )
                )
            elif isinstance(inputs, dict):
                for port, source in inputs.items():
                    if isinstance(source, str) and SELECTOR.match(source):
                        issues.append(
                            (
                                (*where, "inputs", str(port)),
                                "Selektor nur als ganzer Wert von steps[].inputs möglich",
                            )
                        )
    if not module.exports:
        issues.append(
            (
                (*base, "exports"),
                "exports ist Pflicht und nennt mindestens einen Layer oder Schritt des Moduls",
            )
        )
    ids = [
        node.get("id")
        for section in MODULE_SECTIONS
        for node in getattr(module, section)
    ]
    for index, export in enumerate(module.exports):
        if export not in ids:
            shown = ", ".join(str(i) for i in ids) or "keine"
            issues.append(
                (
                    (*base, "exports", str(index)),
                    f"'{export}' ist kein Layer oder Schritt des Moduls (vorhanden: {shown})",
                )
            )
    return issues


def _rewrite_references(value: Any, inner_ids: set[str], prefix: str) -> None:
    """In place: id references to inner ids -> generated ids."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "inputs" and isinstance(item, dict):
                for port, source in item.items():
                    if isinstance(source, str) and source in inner_ids:
                        item[port] = f"{prefix}.{source}"
            elif key in _REFERENCE_KEYS and isinstance(item, str) and item in inner_ids:
                value[key] = f"{prefix}.{item}"
            else:
                _rewrite_references(item, inner_ids, prefix)
    elif isinstance(value, list):
        for item in value:
            _rewrite_references(item, inner_ids, prefix)


def _references(node: dict, section: str, loc: Loc):
    """(position, value) of every id reference of an authored node."""
    if section == "output" and "source" in node:
        yield (*loc, "source"), node["source"]
    if section == "steps" and isinstance(node.get("inputs"), dict):
        for port, source in node["inputs"].items():
            yield (*loc, "inputs", str(port)), source

    def walk(value: Any, where: Loc):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in _REFERENCE_KEYS:
                    yield (*where, key), item
                else:
                    yield from walk(item, (*where, str(key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                yield from walk(item, (*where, str(index)))

    yield from walk(node, loc)


# --- Expansion ---


class _CapExceeded(ExpansionFail):
    """Node cap exceeded: stops the whole expansion."""


@dataclass
class _Instance:
    module: str
    foreach: bool
    keys: list[str] = field(default_factory=list)
    dropped_keys: list[str] = field(default_factory=list)
    broken: bool = False


@dataclass
class _Context:
    modules: dict[str, ModuleSpec]
    scope: Scope
    max_nodes: int
    node_count: int = 0
    generated: dict[str, list[tuple[Any, NodeOrigin]]] = field(
        default_factory=lambda: {section: [] for section in SECTIONS}
    )
    authored: dict[str, list[tuple[Any, NodeOrigin]]] = field(
        default_factory=lambda: {section: [] for section in SECTIONS}
    )
    instances: dict[str, _Instance] = field(default_factory=dict)
    namespace: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )  # generated id -> (module, inner)
    dropped: list[tuple[Loc, str]] = field(default_factory=list)
    dropped_ids: dict[str, str] = field(default_factory=dict)

    def emit(self, target: list, node: Any, origin: NodeOrigin) -> None:
        self.node_count += 1
        if self.node_count > self.max_nodes:
            raise _CapExceeded(
                origin.location(),
                f"die Expansion erzeugt mehr als {self.max_nodes} Knoten ({MAX_NODES_ENV}) - "
                "Liste verkleinern oder den Deckel erhöhen",
            )
        target.append((node, origin))

    def drop(self, loc: Loc, condition: Any, ids: list[str]) -> None:
        expression = text_of(condition)
        self.dropped.append((tuple(loc), expression))
        for node_id in ids:
            self.dropped_ids[node_id] = expression


def _elements(spec: Any, base: Loc, ctx: _Context) -> tuple[str, list[Any]]:
    where = (*base, "foreach")
    if not isinstance(spec, dict) or set(spec) != {"var", "in"}:
        raise ExpansionFail(
            where,
            "foreach braucht genau die Schlüssel var und in (foreach: {var: name, in: [...]})",
        )
    var = spec["var"]
    ctx.scope.child(
        var, None, "Schleifenvariable", (*where, "var")
    )  # identifier, no shadowing
    elements = _substitute(spec["in"], ctx.scope, (*where, "in"))
    if not isinstance(elements, list):
        raise ExpansionFail(
            (*where, "in"),
            f"in muss eine Liste sein (oder ${{listenparameter}}), erhalten {value_label(elements)}",
        )
    if not elements:
        raise ExpansionFail(
            (*where, "in"),
            "die Liste ist leer - foreach braucht mindestens ein Element",
        )
    kinds = {
        "mapping"
        if isinstance(e, dict)
        else "scalar"
        if kind_of(e) != "other"
        else "other"
        for e in elements
    }
    if "other" in kinds or len(kinds) > 1:
        raise ExpansionFail(
            (*where, "in"),
            "die Elemente müssen einheitlich sein: alle Werte (Text, Zahl, true/false) oder alle "
            f"Mappings (gefunden: {sorted({value_label(e) for e in elements})})",
        )
    return var, elements


def _arguments(
    item: dict, name: str, module: ModuleSpec, scope: Scope, loc: Loc
) -> dict[str, Any]:
    where = (*loc, "with")
    given = _substitute(item.get("with") or {}, scope, where)
    if not isinstance(given, dict):
        raise ExpansionFail(where, "with muss ein Mapping sein (with: {arg: wert})")
    unknown = sorted(set(given) - set(module.args))
    if unknown:
        raise ExpansionFail(
            where,
            f"Modul '{name}' kennt die Argumente {unknown} nicht "
            f"(Argumente: {', '.join(module.args) or 'keine'})",
        )
    missing = [
        arg
        for arg, spec in module.args.items()
        if arg not in given and spec.default is None
    ]
    if missing:
        raise ExpansionFail(
            where, f"Modul '{name}': Argument(e) {missing} fehlen in with"
        )
    values: dict[str, Any] = {}
    for arg, spec in module.args.items():
        value = given[arg] if arg in given else copy.deepcopy(spec.default)
        try:
            values[arg] = spec.check(value)
        except ValueError as exc:
            raise ExpansionFail(
                (*where, arg), f"Argument '{arg}' des Moduls '{name}': {exc}"
            ) from None
    return values


def _expand_instance(item: Any, index: int, ctx: _Context) -> None:
    base = ("instances", str(index))
    if not isinstance(item, dict):
        raise ExpansionFail(
            base,
            "eine Instanz ist ein Mapping {id, module, foreach?, key?, with?, when?}",
        )
    for key in item:
        if key not in _INSTANCE_KEYS:
            raise ExpansionFail(
                (*base, str(key)),
                f"unbekanntes Feld (erlaubt: {', '.join(_INSTANCE_KEYS)})",
            )
    inst_id = item.get("id")
    if inst_id is None:
        raise ExpansionFail((*base, "id"), "Pflichtfeld fehlt")
    if isinstance(inst_id, str) and ("[" in inst_id or "]" in inst_id):
        raise ExpansionFail((*base, "id"), f"id '{inst_id}' {_BRACKETS}")
    if not isinstance(inst_id, str) or not IDENTIFIER.match(inst_id):
        raise ExpansionFail(
            (*base, "id"),
            f"Instanz-id {inst_id!r} ist kein Bezeichner ([A-Za-z_][A-Za-z0-9_]*)",
        )
    if inst_id in ctx.instances:
        raise ExpansionFail((*base, "id"), f"Instanz-id '{inst_id}' ist doppelt")
    name = item.get("module")
    if name is None:
        raise ExpansionFail((*base, "module"), "Pflichtfeld fehlt")
    if not isinstance(name, str) or name not in ctx.modules:
        declared = ", ".join(sorted(ctx.modules)) or "keine"
        raise ExpansionFail(
            (*base, "module"), f"unbekanntes Modul {name!r} (deklariert: {declared})"
        )
    module = ctx.modules[name]
    state = _Instance(module=name, foreach="foreach" in item, broken=True)
    ctx.instances[inst_id] = state

    # 1. elements and keys (all of them, so duplicates are found before expanding)
    plan: list[tuple[Optional[str], Scope, Loc]] = []
    if state.foreach:
        var, elements = _elements(item["foreach"], base, ctx)
        mapping = isinstance(elements[0], dict)
        if mapping and "key" not in item:
            raise ExpansionFail(
                (*base, "key"),
                'foreach über Mappings braucht key (z. B. key: "${c.id}")',
            )
        if not mapping and "key" in item:
            raise ExpansionFail(
                (*base, "key"),
                "bei Werten als Elementen ist das Element selbst der "
                "Schlüssel - key nur bei Mapping-Elementen angeben",
            )
        seen: set[str] = set()
        for number, element in enumerate(elements):
            scope = ctx.scope.child(
                var, element, "Schleifenvariable", (*base, "foreach", "var")
            )
            key_loc = (*base[:1], f"{index} ({var}=#{number})", "key")
            key = _substitute(item["key"], scope, key_loc) if mapping else element
            if isinstance(key, (dict, list)) or key is None:
                raise ExpansionFail(
                    key_loc, f"key muss einen Wert ergeben, erhalten {value_label(key)}"
                )
            key = text_of(key)
            if not _KEY.match(key):
                raise ExpansionFail(
                    key_loc,
                    f"Schlüssel '{key}' ist ungültig - erlaubt sind Buchstaben, "
                    "Ziffern, '_' und '-'",
                )
            if key in seen:
                raise ExpansionFail(
                    (*base[:1], f"{index} ({var}={key})", "key"),
                    f"Schlüssel '{key}' entsteht in mehreren Elementen",
                )
            seen.add(key)
            plan.append((key, scope, (*base[:1], f"{index} ({var}={key})")))
    else:
        if "key" in item:
            raise ExpansionFail((*base, "key"), "key gibt es nur zusammen mit foreach")
        plan.append((None, ctx.scope, base))

    # 2. one copy of the module body per element
    inner_ids = set(module.inner_ids())
    for key, scope, loc in plan:
        prefix = f"{inst_id}[{key}]" if key is not None else inst_id
        generated_ids = [f"{prefix}.{inner}" for inner in module.inner_ids()]
        for inner in module.inner_ids():
            ctx.namespace[f"{prefix}.{inner}"] = (name, inner)
        if key is not None:
            state.keys.append(key)
        if "when" in item and not evaluate_when(item["when"], scope, (*loc, "when")):
            ctx.drop(("instances", prefix), item["when"], generated_ids)
            if key is not None:
                state.dropped_keys.append(key)
            continue
        module_scope = ctx.scope
        for arg, value in _arguments(item, name, module, scope, loc).items():
            module_scope = module_scope.child(
                arg, value, "Modulargument", ("modules", name, "args", arg)
            )
        for section in MODULE_SECTIONS:
            for node in getattr(module, section):
                origin = NodeOrigin(section, index, name, inst_id, key, node["id"])
                where = origin.location()
                body = {k: v for k, v in node.items() if k != "when"}
                if "when" in node and not evaluate_when(
                    node["when"], module_scope, (*where, "when")
                ):
                    ctx.drop(where, node["when"], [f"{prefix}.{node['id']}"])
                    continue
                body = _substitute(body, module_scope, where)
                body["id"] = f"{prefix}.{node['id']}"
                _rewrite_references(body, inner_ids, prefix)
                _check_references(body, section, where, ctx, frozenset(generated_ids))
                ctx.emit(ctx.generated[section], body, origin)
    state.broken = False


def _resolve_selector(text: str, loc: Loc, ctx: _Context) -> dict[str, str]:
    match = SELECTOR.match(text)
    if not match:
        raise ExpansionFail(
            loc,
            "inputs muss ein Mapping (port: quelle) oder ein Selektor instanz[*].export sein",
        )
    inst_id, export = match.group("instance"), match.group("export")
    state = ctx.instances.get(inst_id)
    if state is None:
        declared = ", ".join(ctx.instances) or "keine"
        raise ExpansionFail(
            loc,
            f"Selektor '{text}': unbekannte Instanz '{inst_id}' (deklariert: {declared})",
        )
    if state.broken:
        raise ExpansionFail(
            loc,
            f"Selektor '{text}': Instanz '{inst_id}' ist fehlerhaft (Meldung unter instances)",
        )
    if not state.foreach:
        raise ExpansionFail(
            loc,
            f"Selektor '{text}': Instanz '{inst_id}' hat kein foreach - "
            f"'{inst_id}.{export}' schreiben",
        )
    exports = ctx.modules[state.module].exports
    if export not in exports:
        raise ExpansionFail(
            loc,
            f"Selektor '{text}': '{export}' ist kein Export des Moduls {state.module} "
            f"(exports: {', '.join(exports)})",
        )
    inputs = {
        key: f"{inst_id}[{key}].{export}"
        for key in state.keys
        if key not in state.dropped_keys
        and f"{inst_id}[{key}].{export}" not in ctx.dropped_ids
    }
    if not inputs:
        raise ExpansionFail(
            loc,
            f"Selektor '{text}': alle Elemente der Instanz '{inst_id}' wurden durch "
            "when entfernt",
        )
    return inputs


def _expand_node(item: Any, section: str, index: int, ctx: _Context) -> None:
    loc = (section, str(index))
    origin = NodeOrigin(section, index)
    if not isinstance(item, dict):
        ctx.emit(
            ctx.authored[section], item, origin
        )  # model instance or an error Pydantic reports
        return
    for key, hint in _REMOVED_NODE_KEYS.items():
        if key in item:
            raise ExpansionFail((*loc, key), hint)
    body = {k: v for k, v in item.items() if k != "when"}
    if "when" in item and not evaluate_when(item["when"], ctx.scope, (*loc, "when")):
        try:
            node_id = _substitute(body.get("id"), ctx.scope, (*loc, "id"))
        except ExpansionFail:
            node_id = None
        ctx.drop(loc, item["when"], [node_id] if isinstance(node_id, str) else [])
        return
    body = _substitute(body, ctx.scope, loc)
    node_id = body.get("id")
    if isinstance(node_id, str) and ("[" in node_id or "]" in node_id):
        raise ExpansionFail((*loc, "id"), f"id '{node_id}' {_BRACKETS}")
    if section == "steps" and isinstance(body.get("inputs"), str):
        body["inputs"] = _resolve_selector(body["inputs"], (*loc, "inputs"), ctx)
    _check_references(body, section, loc, ctx)
    ctx.emit(ctx.authored[section], body, origin)


def _check_references(
    body: dict, section: str, loc: Loc, ctx: _Context, own: frozenset[str] = frozenset()
) -> None:
    """References from outside an instance element may only name exports;
    ``own`` holds the generated ids of the element the node belongs to."""
    for where, reference in _references(body, section, loc):
        if not isinstance(reference, str) or reference in own:
            continue
        if SELECTOR.match(reference):
            raise ExpansionFail(
                where, "Selektor nur als ganzer Wert von steps[].inputs möglich"
            )
        if reference in ctx.namespace:
            module, inner = ctx.namespace[reference]
            exports = ctx.modules[module].exports
            if inner not in exports:
                raise ExpansionFail(
                    where,
                    f"'{reference}' ist kein Export des Moduls {module} "
                    f"(exports: {', '.join(exports)})",
                )


def needs_expansion(raw: Mapping) -> bool:
    """True if the raw scenario has parameters, modules, instances, ``when`` or
    a selector (or a removed key, to report it). Idempotent on expanded input."""
    if not isinstance(raw, Mapping):
        return False
    if (ROOT_EXPANSION_KEYS | {"templates"}) & raw.keys():
        return True
    for section in SECTIONS:
        items = raw.get(section)
        for item in items if isinstance(items, list) else ():
            if not isinstance(item, dict):
                continue
            if "when" in item or _REMOVED_NODE_KEYS.keys() & item.keys():
                return True
            if section == "steps" and isinstance(item.get("inputs"), str):
                return True
    return False


def _max_nodes() -> int:
    text = os.environ.get(MAX_NODES_ENV)
    if not text:
        return DEFAULT_MAX_NODES
    try:
        value = int(text)
    except ValueError:
        raise ConfigError(
            [("(Umgebung)", f"{MAX_NODES_ENV}={text!r} ist keine ganze Zahl")]
        ) from None
    if value < 1:
        raise ConfigError([("(Umgebung)", f"{MAX_NODES_ENV} muss mindestens 1 sein")])
    return value


def _declared(
    raw: Mapping, key: str, model: type[BaseModel], label: str
) -> tuple[dict, list[tuple[Loc, str]]]:
    declared = raw.get(key) or {}
    if not isinstance(declared, dict):
        return {}, [((key,), f"{key} muss ein Mapping sein (name: {{...}})")]
    specs: dict[str, Any] = {}
    issues: list[tuple[Loc, str]] = []
    for name, body in declared.items():
        if not isinstance(name, str) or not IDENTIFIER.match(name):
            issues.append(
                (
                    (key, str(name)),
                    f"{label} '{name}' ist kein Bezeichner ([A-Za-z_][A-Za-z0-9_]*)",
                )
            )
            continue
        try:
            specs[name] = model.model_validate(body)
        except ValidationError as exc:
            issues.extend(
                ((key, name, *map(str, e["loc"])), german_message(e))
                for e in exc.errors()
            )
    return specs, issues


def _parameter_values(
    specs: dict[str, ParameterSpec], overrides: Mapping[str, Any]
) -> tuple[dict[str, Any], tuple[str, ...], list[tuple[Loc, str]]]:
    issues: list[tuple[Loc, str]] = []
    for name in sorted(set(overrides) - set(specs)):
        issues.append(
            (
                ("parameters", name),
                f"Überschreibung für unbekannten Parameter '{name}' "
                f"(deklariert: {', '.join(specs) or 'keine'})",
            )
        )
    values: dict[str, Any] = {}
    overridden: list[str] = []
    for name, spec in specs.items():
        if name in overrides:
            try:
                values[name] = spec.check(overrides[name])
                overridden.append(name)
            except ValueError as exc:
                issues.append((("parameters", name), f"Überschreibung ungültig: {exc}"))
        elif spec.default is not None:
            values[name] = copy.deepcopy(spec.default)
        else:
            issues.append(
                (
                    ("parameters", name),
                    "Parameter ohne default muss überschrieben werden (api parameters=..., "
                    f"--param {name}=WERT oder Web-Formular)",
                )
            )
    return values, tuple(overridden), issues


def _config_error(issues: list[tuple[Loc, str]]) -> ConfigError:
    return ConfigError([(" -> ".join(loc), message) for loc, message in issues])


def _report(
    expanded: dict,
    section_origins: Mapping[str, list[NodeOrigin]] | None = None,
    instances: tuple[InstanceInfo, ...] = (),
    **fields: Any,
) -> ExpansionReport:
    origins: dict[tuple[str, int], NodeOrigin] = {}
    node_origins: dict[str, NodeOrigin] = {}
    for section in SECTIONS:
        items = expanded.get(section)
        known = (section_origins or {}).get(section, [])
        for index, item in enumerate(items if isinstance(items, list) else ()):
            origin = known[index] if index < len(known) else NodeOrigin(section, index)
            origins[(section, index)] = origin
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                node_origins.setdefault(item["id"], origin)
    return ExpansionReport(
        parameters=MappingProxyType(fields.get("parameters", {})),
        values=MappingProxyType(fields.get("values", {})),
        overridden=fields.get("overridden", ()),
        origins=MappingProxyType(origins),
        node_origins=MappingProxyType(node_origins),
        instances=instances,
        dropped=fields.get("dropped", ()),
        dropped_ids=MappingProxyType(fields.get("dropped_ids", {})),
        node_count=fields.get("node_count", len(origins)),
        expanded=expanded,
    )


def expand(raw: dict, overrides: Mapping[str, Any] | None = None) -> ExpansionReport:
    """Expands a raw scenario (FA59, FA62, FA75); every violation is a
    ``ConfigError`` with its position. Without expansion keys and overrides
    the scenario is returned unchanged (as a copy)."""
    if not isinstance(raw, dict):
        raise ConfigError([("(Wurzel)", "Konfiguration muss ein Mapping sein")])
    overrides = dict(overrides or {})
    if not needs_expansion(raw) and not overrides:
        return _report(copy.deepcopy(raw))

    issues: list[tuple[Loc, str]] = []
    if "templates" in raw:
        issues.append(
            (
                ("templates",),
                "templates gibt es nicht mehr - Module (modules) und Instanzen "
                "(instances) nutzen (FA75)",
            )
        )
    specs, parameter_issues = _declared(
        raw, "parameters", ParameterSpec, "Parametername"
    )
    modules, module_issues = _declared(raw, "modules", ModuleSpec, "Modulname")
    issues += parameter_issues + module_issues
    for name, module in modules.items():
        issues.extend(_check_module(name, module, specs))
    values, overridden, value_issues = _parameter_values(specs, overrides)
    issues.extend(value_issues)
    if issues:
        raise _config_error(issues)

    ctx = _Context(
        modules=modules,
        scope=Scope(dict(values), {n: "Parameter" for n in values}),
        max_nodes=_max_nodes(),
    )
    others: dict[str, Any] = {}
    try:
        items = raw.get("instances") or []
        if not isinstance(items, list):
            issues.append(
                (("instances",), "instances muss eine Liste sein ([{id, module, ...}])")
            )
            items = []
        for index, item in enumerate(items):
            try:
                _expand_instance(item, index, ctx)
            except _CapExceeded:
                raise
            except ExpansionFail as fail:
                issues.append((fail.loc, fail.message))
        for key, value in raw.items():
            if key in ROOT_EXPANSION_KEYS or key == "templates":
                continue
            if key in SECTIONS and value is not None and not isinstance(value, list):
                issues.append(((key,), f"{key} muss eine Liste sein"))
            elif key in SECTIONS and isinstance(value, list):
                for index, item in enumerate(value):
                    try:
                        _expand_node(item, key, index, ctx)
                    except _CapExceeded:
                        raise
                    except ExpansionFail as fail:
                        issues.append((fail.loc, fail.message))
            else:
                try:
                    others[key] = _substitute(value, ctx.scope, (str(key),))
                except ExpansionFail as fail:
                    issues.append((fail.loc, fail.message))
    except _CapExceeded as fail:
        issues.append((fail.loc, fail.message))
    if issues:
        raise _config_error(issues)

    # generated nodes before the authored ones of the same section (layers may
    # be omitted in the source when instances add some)
    expanded = dict(others)
    section_origins: dict[str, list[NodeOrigin]] = {}
    for section in SECTIONS:
        produced = ctx.generated[section] + ctx.authored[section]
        if isinstance(raw.get(section), list) or produced:
            expanded[section] = [node for node, _ in produced]
            section_origins[section] = [origin for _, origin in produced]
    instances = tuple(
        InstanceInfo(
            inst_id, state.module, tuple(state.keys), tuple(state.dropped_keys)
        )
        for inst_id, state in ctx.instances.items()
    )
    return _report(
        expanded,
        section_origins,
        instances,
        parameters=specs,
        values=values,
        overridden=overridden,
        dropped=tuple(ctx.dropped),
        dropped_ids=ctx.dropped_ids,
        node_count=ctx.node_count,
    )
