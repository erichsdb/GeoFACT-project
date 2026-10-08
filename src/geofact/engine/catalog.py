"""Implements: FA12 (Web-Katalog), FA40 (Katalog aller Erweiterungspunkte), FA59/FA62/FA75 (Schema der Expansion: parameters, modules, instances, when, Selektor).

Introspektion des Operations- und Konfigurationskatalogs.

Liefert eine maschinenlesbare Beschreibung aller registrierten Operationen (Name,
Eingangs-Ports + Typen, Ausgabetyp, Parameter), Quellarten, Formate,
Transformationen und Ausgabeformate sowie das JSON-Schema des Scenario-Modells,
für den Konfig-Editor im Frontend und als Grounding der LLM-Konfig-Generierung.

Parameter (FA2): ``params`` und ``required_params`` einer Operation werden aus
``Params.model_json_schema()`` abgeleitet; es gibt keine zweite, von Hand
gepflegte Beschreibung, die abweichen könnte.

Jede Funktion liest eine Registry (None = Standard-Registry, FA40); Kern- und
Plugin-Bausteine erscheinen gleichrangig.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from geofact.core.expansion import SELECTOR, ModuleSpec, ParameterSpec
from geofact.core.registry import Registry, default_registry
from geofact.core.scenario import Scenario

_ITEM_TYPES = {
    "integer": "number",
    "number": "number",
    "string": "string",
    "boolean": "boolean",
}


def _simplify(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Löst $ref auf und faltet ``Optional[X]`` (anyOf mit null) zu X; die
    Angaben der äußeren Ebene (Beschreibung, Default) behalten Vorrang."""
    prop = dict(prop)
    if "$ref" in prop:
        target = defs.get(prop.pop("$ref").rsplit("/", 1)[-1], {})
        prop = {**_simplify(target, defs), **prop}
    if len(prop.get("allOf", [])) == 1:
        prop = {**_simplify(prop.pop("allOf")[0], defs), **prop}
    if "anyOf" in prop:
        options = [
            _simplify(option, defs)
            for option in prop.pop("anyOf")
            if option.get("type") != "null"
        ]
        if len(options) == 1:
            prop = {**options[0], **prop}
        elif options and all(o.get("type") in ("integer", "number") for o in options):
            prop = {**options[0], "type": "number", **prop}
        elif options:
            prop = {**options[0], **prop}
    return prop


def _param_spec(
    prop: dict[str, Any], defs: dict[str, Any], required: bool
) -> dict[str, Any]:
    """Eine JSON-Schema-Eigenschaft als Parameter-Beschreibung des Katalogs: type
    (number|integer|string|boolean|enum|array|object), required, default,
    description, choices, item_type, minimum."""
    prop = _simplify(prop, defs)
    spec: dict[str, Any] = {}
    if "enum" in prop or "const" in prop:
        spec["type"] = "enum"
        spec["choices"] = list(prop["enum"]) if "enum" in prop else [prop["const"]]
    elif prop.get("type") == "array":
        spec["type"] = "array"
        item_type = _ITEM_TYPES.get(
            _simplify(prop.get("items", {}), defs).get("type", "")
        )
        if item_type:
            spec["item_type"] = item_type
    else:
        spec["type"] = prop.get("type", "string")
    spec["required"] = required
    if prop.get("default") is not None:
        spec["default"] = prop["default"]
    if "description" in prop:
        spec["description"] = prop["description"]
    if "minimum" in prop:
        spec["minimum"] = prop["minimum"]
    elif "exclusiveMinimum" in prop:
        spec["minimum"] = prop["exclusiveMinimum"]
        spec["exclusive_minimum"] = True
    return spec


def params_catalog(
    params_model: type[BaseModel],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """(Parameter-Beschreibungen, sortierte Pflichtparameter) einer ``Params``-Klasse (FA2)."""
    schema = params_model.model_json_schema()
    defs = schema.get("$defs", {})
    required = set(schema.get("required", []))
    specs = {
        name: _param_spec(prop, defs, name in required)
        for name, prop in schema.get("properties", {}).items()
    }
    return specs, sorted(required)


def _option_names(model: type[BaseModel] | None) -> list[str]:
    """Namen der formatspezifischen Layer-/Ausgabefelder (``options_model``)."""
    return sorted(model.model_fields) if model is not None else []


def _registry(registry: Registry | None) -> Registry:
    return registry if registry is not None else default_registry()


def _format_option_properties(
    entries: list[Any], defs: dict[str, Any], taken: set[str]
) -> dict[str, dict[str, Any]]:
    """Die formatspezifischen Felder (``options_model`` je Eintrag) als optionale
    JSON-Schema-Eigenschaften, je Name eine. Welche Formate ein Feld kennen, steht in
    ``x-formats`` und, für Leser die ``x-`` verwerfen, als Präfix der
    ``description``; Pflicht steht in ``x-required-for``. Ist ein Feld je Format
    strukturell verschieden, wird es eine ``anyOf``. ``taken``: Namen, die der Layer
    selbst besitzt; sie gehen vor."""
    variants: dict[
        str, dict[str, dict[str, Any]]
    ] = {}  # Name -> Schema (json) -> Variante
    for entry in entries:
        if entry.options_model is None:
            continue
        schema = entry.options_model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(schema.pop("$defs", {}))
        required = set(schema.get("required", []))
        for name, prop in schema.get("properties", {}).items():
            if name in taken:
                continue
            # Dieselbe Struktur = dieselbe Variante (Titel und Beschreibung zählen nicht).
            structure = json.dumps(
                {k: v for k, v in prop.items() if k not in ("title", "description")},
                sort_keys=True,
            )
            slot = variants.setdefault(name, {}).setdefault(
                structure, {"prop": prop, "formats": [], "required": []}
            )
            if "description" not in slot["prop"] and "description" in prop:
                slot["prop"] = prop
            slot["formats"].append(entry.name)
            if name in required:
                slot["required"].append(entry.name)

    def render(slot: dict[str, Any]) -> dict[str, Any]:
        formats, required = sorted(slot["formats"]), sorted(slot["required"])
        label = ", ".join(formats)
        if required:
            label += (
                ", Pflicht"
                if required == formats
                else f"; Pflicht bei {', '.join(required)}"
            )
        description = slot["prop"].get("description")
        rendered = {
            **slot["prop"],
            "description": f"Formatfeld ({label})"
            + (f": {description}" if description else ""),
            "x-formats": formats,
        }
        if required:
            rendered["x-required-for"] = required
        return rendered

    properties: dict[str, dict[str, Any]] = {}
    for name, by_schema in sorted(variants.items()):
        slots = list(by_schema.values())
        properties[name] = (
            render(slots[0])
            if len(slots) == 1
            else {"anyOf": [render(slot) for slot in slots]}
        )
    return properties


def _prune_unreferenced_defs(schema: dict[str, Any]) -> None:
    """Entfernt ``$defs``-Einträge, auf die nichts mehr verweist (z. B. die offenen
    Basismodelle ``LayerBase``/``Step`` nach dem Ersetzen durch Baustein-Modelle)."""
    defs = schema.get("$defs", {})

    def refs_in(node: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    found.add(value.rsplit("/", 1)[-1])
                else:
                    found |= refs_in(value)
        elif isinstance(node, list):
            for item in node:
                found |= refs_in(item)
        return found

    reachable: set[str] = set()
    pending = refs_in({key: value for key, value in schema.items() if key != "$defs"})
    while pending:
        name = pending.pop()
        if name in reachable or name not in defs:
            continue
        reachable.add(name)
        pending |= refs_in(defs[name])
    for name in [n for n in defs if n not in reachable]:
        del defs[name]


def operation_catalog(registry: Registry | None = None) -> list[dict[str, Any]]:
    """Alle registrierten Operationen als sortierte Liste von Contracts."""
    entries: list[dict[str, Any]] = []
    for plugin in _registry(registry).entries("operation"):
        step_cls = plugin.step_model
        params, required = params_catalog(step_cls.Params)
        entries.append(
            {
                "op": plugin.name,
                "input_ports": {
                    port: dtype.value for port, dtype in step_cls.INPUT_PORTS.items()
                },
                "output_type": step_cls.OUTPUT_TYPE.value,
                "required_params": required,
                "params": params,
                "doc": (step_cls.__doc__ or "").strip() or None,
            }
        )
    return entries


def source_types(registry: Registry | None = None) -> list[dict[str, str]]:
    """Verfügbare Layer-Quellarten (Kern und Plugins, FA40)."""
    return [
        {"source": plugin.name, "description": plugin.description}
        for plugin in _registry(registry).entries("source")
    ]


def extension_catalog(
    registry: Registry | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Alle Erweiterungspunkte mit Name, Beschreibung und Herkunft (FA40); 'origin'
    nennt das Modul bzw. die Plugin-Datei, so ist Kern von Plugin unterscheidbar."""
    reg = _registry(registry)

    def describe(kind: str, extra) -> list[dict[str, Any]]:
        return [
            {
                "name": e.name,
                "description": e.description,
                "origin": e.origin,
                **extra(e),
            }
            for e in reg.entries(kind)
        ]

    return {
        "sources": describe(
            "source",
            lambda e: {
                "transform": e.transform if isinstance(e.transform, str) else "eigene",
            },
        ),
        "file_formats": describe(
            "file_format",
            lambda e: {
                "extensions": list(e.extensions),
                "data_type": e.data_type.value,
                "transform": e.transform if isinstance(e.transform, str) else "eigene",
                "options": _option_names(e.options_model),
            },
        ),
        "table_formats": describe(
            "table_format",
            lambda e: {
                "extensions": list(e.extensions),
                "options": _option_names(e.options_model),
            },
        ),
        "transforms": describe("transform", lambda e: {}),
        "outputs": describe(
            "output",
            lambda e: {
                "extension": e.extension,
                "accepts": sorted(t.value for t in e.accepts),
                "options": _option_names(e.options_model),
            },
        ),
        "operations": describe("operation", lambda e: {}),
    }


def scenario_json_schema(registry: Registry | None = None) -> dict[str, Any]:
    """JSON-Schema des Scenario-Modells (Editor-Autovervollständigung). Die im
    Modell offenen Listen (LayerBase, Step; FA40) werden durch die Modelle aller
    registrierten Quellarten bzw. Operationen samt ``Params`` ersetzt."""
    reg = _registry(registry)
    schema = Scenario.model_json_schema()
    defs = schema.setdefault("$defs", {})
    refs = []
    for plugin in reg.entries("source"):
        model = plugin.layer_model
        sub = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(sub.pop("$defs", {}))
        format_kind = getattr(model, "FORMAT_KIND", None)
        if format_kind is not None:
            # Die flachen Formatfelder von Datei-/Tabellen-Layern fehlten sonst im Schema.
            sub["properties"].update(
                _format_option_properties(
                    reg.entries(format_kind), defs, taken=set(sub["properties"])
                )
            )
        defs[model.__name__] = sub
        refs.append({"$ref": f"#/$defs/{model.__name__}"})
    schema["properties"]["layers"]["items"] = {"anyOf": refs}

    # Ausgabe-Deklaration: die Felder der Ausgabeformate stehen ebenfalls flach.
    output_def = defs[
        schema["properties"]["output"]["items"]["$ref"].rsplit("/", 1)[-1]
    ]
    output_def["properties"].update(
        _format_option_properties(
            reg.entries("output"), defs, taken=set(output_def["properties"])
        )
    )

    step_refs = []
    for plugin in reg.entries("operation"):
        model = plugin.step_model
        sub = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(sub.pop("$defs", {}))
        params_schema = model.Params.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(params_schema.pop("$defs", {}))
        params_name = f"{model.__name__}Params"
        defs[params_name] = params_schema
        sub["properties"]["params"] = {"$ref": f"#/$defs/{params_name}"}
        if params_schema.get("required") and "params" not in sub.get("required", []):
            sub["required"] = [*sub.get("required", []), "params"]
        defs[model.__name__] = sub
        step_refs.append({"$ref": f"#/$defs/{model.__name__}"})
    schema["properties"]["steps"]["items"] = {"anyOf": step_refs}
    _add_module_schema(schema, defs)
    _prune_unreferenced_defs(schema)
    return schema


def _add_module_schema(schema: dict[str, Any], defs: dict[str, Any]) -> None:
    """FA59/FA62/FA75: ``parameters``, ``modules`` und ``instances`` an der Wurzel,
    ``when`` an jedem Knoten, ``inputs`` alternativ als Selektor. Die Expansion
    läuft vor der Validierung (``core/expansion.py``), das Schema beschreibt daher
    die Quellform."""
    for model in (ParameterSpec, ModuleSpec):
        sub = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(sub.pop("$defs", {}))
        defs[model.__name__] = sub
    defs["ModuleSpec"]["properties"]["layers"]["items"] = {
        "$ref": "#/properties/layers/items"
    }
    defs["ModuleSpec"]["properties"]["steps"]["items"] = {
        "$ref": "#/properties/steps/items"
    }
    defs["ModuleSpec"]["required"] = ["exports"]
    defs["ModuleSpec"]["description"] = (
        "Modul (FA75): benannter Teilgraph aus Layern und Schritten mit typisierten Argumenten; "
        "nur die ids unter exports sind von außen sichtbar"
    )
    defs["ParameterSpec"]["description"] = (
        "Parameter (FA59) bzw. Modulargument (FA75): Typ, default (ohne default Pflicht), "
        "Beschreibung, erlaubte Werte"
    )
    defs["When"] = {
        "anyOf": [{"type": "boolean"}, {"type": "string"}],
        "description": "Bedingung zur Expansionszeit: true/false, ${bool_parameter} oder ein "
        "Ausdruck wie \"${modus} == 'voll' and n >= 3\"; falsch = Element entfällt",
    }
    selector = {
        "type": "string",
        "pattern": re.sub(r"\?P<\w+>", "", SELECTOR.pattern),  # ECMA regex
        "description": "Selektor instanz[*].export: ein Eingang je Element der Instanz",
    }
    node_defs = [
        ref["$ref"].rsplit("/", 1)[-1]
        for section in ("layers", "steps")
        for ref in schema["properties"][section]["items"]["anyOf"]
    ]
    node_defs.append(schema["properties"]["output"]["items"]["$ref"].rsplit("/", 1)[-1])
    step_defs = {
        ref["$ref"].rsplit("/", 1)[-1]
        for ref in schema["properties"]["steps"]["items"]["anyOf"]
    }
    for name in node_defs:
        properties = defs[name].setdefault("properties", {})
        properties["when"] = {"$ref": "#/$defs/When"}
        if name in step_defs and "inputs" in properties:
            properties["inputs"] = {"anyOf": [properties["inputs"], selector]}
    defs["InstanceSpec"] = {
        "type": "object",
        "description": "Ein Modul einsetzen, optional je Element einer Liste (ids instanz[key].id)",
        "properties": {
            "id": {
                "type": "string",
                "description": "Name der Instanz (Präfix der erzeugten ids)",
            },
            "module": {
                "type": "string",
                "description": "Name eines Moduls unter modules",
            },
            "foreach": {
                "type": "object",
                "properties": {
                    "var": {
                        "type": "string",
                        "description": "Schleifenvariable (${var}, ${var.key})",
                    },
                    "in": {
                        "anyOf": [{"type": "array"}, {"type": "string"}],
                        "description": "Liste oder ${listenparameter}; nicht leer, Elemente einheitlich",
                    },
                },
                "required": ["var", "in"],
                "additionalProperties": False,
            },
            "key": {
                "type": "string",
                "description": 'Schlüssel je Element, z. B. "${c.id}" '
                "(Pflicht bei Mapping-Elementen)",
            },
            "with": {"type": "object", "description": "Argumente des Moduls (args)"},
            "when": {"$ref": "#/$defs/When"},
        },
        "required": ["id", "module"],
        "additionalProperties": False,
    }
    schema["properties"]["parameters"] = {
        "type": "object",
        "description": "Szenario-Parameter (FA59): name -> {type, default, ...}; Verwendung "
        "als ${name} bzw. ${name.key}",
        "additionalProperties": {"$ref": "#/$defs/ParameterSpec"},
    }
    schema["properties"]["modules"] = {
        "type": "object",
        "description": "Module (FA75): name -> {args, layers, steps, exports}; eingesetzt "
        "über instances",
        "additionalProperties": {"$ref": "#/$defs/ModuleSpec"},
    }
    schema["properties"]["instances"] = {
        "type": "array",
        "description": "Instanzen (FA75): ein Modul je Element einer Liste einsetzen",
        "items": {"$ref": "#/$defs/InstanceSpec"},
    }
    # FA75: layers darf in der Quellform fehlen, wenn Instanzen Layer erzeugen
    schema["required"] = [
        name for name in schema.get("required", []) if name != "layers"
    ]
