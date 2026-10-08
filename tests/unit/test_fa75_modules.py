"""Implements: FA75 (Module und Instanzen, Fan-in-Selektor).

Contract tests for src/geofact/core/expansion.py (modules, instances,
selector, encapsulation, origins, report) and its hook in core/scenario.py
(report passed through once, domain positions, collapsed errors). The
``collect`` operation is tested in test_fa60_collect.py, ``when`` in
test_fa62_conditions.py. Offline: point layers and a bbox region.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from geofact import api
from geofact.core import scenario as scenario_module
from geofact.core.errors import ConfigError
from geofact.core.expansion import (
    MAX_NODES_ENV,
    InstanceInfo,
    NodeOrigin,
    expand,
    needs_expansion,
)
from geofact.engine import catalog
from geofact.engine.executor import run_scenario

BBOX = "12.85,50.78,12.98,50.87"
CITIES = {
    "cities": {
        "type": "list",
        "items": "mapping",
        "default": [{"id": "le", "name": "Leipzig"}, {"id": "dd", "name": "Dresden"}],
    }
}
INSTANCE = {
    "id": "stadt",
    "module": "stadt_gruen",
    "foreach": {"var": "c", "in": "${cities}"},
    "key": "${c.id}",
    "with": {"name": "${c.name}"},
}
SELECT_ALL = [{"id": "alle", "op": "collect", "inputs": "stadt[*].anteil"}]


def _buffer(step_id: str, source: str, radius=1) -> dict:
    return {
        "id": step_id,
        "op": "buffer",
        "inputs": {"geometry": source},
        "params": {"radius_km": radius},
    }


def _module(**changes) -> dict:
    body = {
        "args": {"name": {"type": "string"}},
        "layers": [
            {
                "id": "grenze",
                "source": "points",
                "features": [{"name": "${name}", "lon": 12.9, "lat": 50.82}],
            }
        ],
        "steps": [_buffer("gruen", "grenze"), _buffer("anteil", "gruen")],
        "exports": ["anteil"],
    }
    body.update(changes)
    return body


def _raw(
    modules=None, instances=None, steps=None, output=None, layers=None, **extra
) -> dict:
    raw = {
        "parameters": dict(CITIES),
        "modules": modules if modules is not None else {"stadt_gruen": _module()},
        "scenario": {"name": "Module", "region": BBOX},
        "instances": instances if instances is not None else [dict(INSTANCE)],
        "steps": steps if steps is not None else list(SELECT_ALL),
        "output": output
        if output is not None
        else [{"type": "geojson", "source": "alle"}],
    }
    if layers is not None:
        raw["layers"] = layers
    raw.update(extra)
    return raw


def _issues(raw: dict, overrides: dict | None = None) -> dict[str, str]:
    with pytest.raises(ConfigError) as info:
        expand(raw, overrides)
    return dict(info.value.issues)


def _validation_issues(raw: dict) -> list[tuple[str, str]]:
    report = api.validate(raw)
    assert not report.valid
    return report.issues


def _ids(nodes: list[dict]) -> list[str]:
    return [node["id"] for node in nodes]


# =====================================================================
# Happy path
# =====================================================================


def test_fa75_instance_with_foreach_creates_one_copy_per_element_in_list_order():
    report = expand(_raw())
    assert _ids(report.expanded["layers"]) == ["stadt[le].grenze", "stadt[dd].grenze"]
    assert _ids(report.expanded["steps"]) == [
        "stadt[le].gruen",
        "stadt[le].anteil",
        "stadt[dd].gruen",
        "stadt[dd].anteil",
        "alle",
    ]
    assert report.expanded["layers"][1]["features"][0]["name"] == "Dresden"
    assert report.node_origins["stadt[dd].anteil"] == NodeOrigin(
        "steps", 0, "stadt_gruen", "stadt", "dd", "anteil"
    )
    assert report.node_origins["alle"] == NodeOrigin("steps", 0)
    assert not {"modules", "instances", "parameters"} & set(report.expanded)


def test_fa75_single_instance_uses_dot_ids():
    raw = _raw(
        instances=[{"id": "stadt", "module": "stadt_gruen", "with": {"name": "Jena"}}],
        steps=[],
        output=[{"type": "geojson", "source": "stadt.anteil"}],
    )
    report = expand(raw)
    assert _ids(report.expanded["steps"]) == ["stadt.gruen", "stadt.anteil"]
    origin = report.node_origins["stadt.anteil"]
    assert origin.key is None and origin.location() == (
        "instances",
        "stadt",
        "steps",
        "anteil",
    )
    assert report.instances == (InstanceInfo("stadt", "stadt_gruen", (), ()),)
    assert api.validate(raw).valid


def test_fa75_inner_references_are_rewritten_and_outer_ones_kept():
    module = _module(
        layers=[
            {
                "id": "grenze",
                "source": "points",
                "features": [{"name": "${name}", "lon": 12.9, "lat": 50.82}],
            },
            {
                "id": "punkt",
                "source": "points",
                "features": [{"name": "p", "lon": 12.9, "lat": 50.82}],
                "validation": {"spatial_check": {"within": "grenze"}},
            },
        ],
        steps=[
            _buffer("gruen", "grenze"),
            _buffer("anteil", "land"),
            _buffer("p", "punkt"),
        ],
    )
    land = {
        "id": "land",
        "source": "points",
        "features": [{"name": "l", "lon": 12.9, "lat": 50.82}],
    }
    raw = _raw(modules={"stadt_gruen": module}, layers=[land])
    expanded = expand(raw).expanded
    steps = {step["id"]: step for step in expanded["steps"]}
    assert steps["stadt[le].gruen"]["inputs"] == {"geometry": "stadt[le].grenze"}
    assert steps["stadt[le].anteil"]["inputs"] == {
        "geometry": "land"
    }  # shared top-level layer
    layers = {layer["id"]: layer for layer in expanded["layers"]}
    assert (
        layers["stadt[dd].punkt"]["validation"]["spatial_check"]["within"]
        == "stadt[dd].grenze"
    )
    assert api.validate(raw).valid


def test_fa75_selector_becomes_an_inputs_mapping_in_element_order():
    steps = {step["id"]: step for step in expand(_raw()).expanded["steps"]}
    assert steps["alle"] == {
        "id": "alle",
        "op": "collect",
        "inputs": {"le": "stadt[le].anteil", "dd": "stadt[dd].anteil"},
    }


def test_fa75_scalar_elements_use_the_element_as_key():
    modules = {
        "puffer": {
            "args": {"radius": {"type": "integer"}},
            "layers": [
                {
                    "id": "p",
                    "source": "points",
                    "features": [{"name": "a", "lon": 12.9, "lat": 50.82}],
                }
            ],
            "steps": [_buffer("x", "p", "${radius}")],
            "exports": ["x"],
        }
    }
    instances = [
        {
            "id": "puffer",
            "module": "puffer",
            "foreach": {"var": "r", "in": [1, 2]},
            "with": {"radius": "${r}"},
        }
    ]
    raw = _raw(
        modules=modules,
        instances=instances,
        steps=[{"id": "alle", "op": "collect", "inputs": "puffer[*].x"}],
    )
    steps = {step["id"]: step for step in expand(raw).expanded["steps"]}
    assert steps["puffer[2].x"]["params"] == {"radius_km": 2}
    assert steps["alle"]["inputs"] == {"1": "puffer[1].x", "2": "puffer[2].x"}


def test_fa75_expanded_scenario_runs_collect_and_keeps_every_row():
    scenario = api.load_scenario(_raw()).scenario
    result = run_scenario(scenario).store["alle"]
    assert list(result["source"]) == ["le", "dd"]


def test_fa75_plan_loads_each_instance_layer_once():
    raw = _raw(
        layers=[
            {
                "id": "ungenutzt",
                "source": "points",
                "features": [{"name": "u", "lon": 12.9, "lat": 50.82}],
            }
        ]
    )
    report = api.validate(raw)
    assert report.valid, report.issues
    plan = report.plan
    assert sorted(plan.load_order) == ["stadt[dd].grenze", "stadt[le].grenze"]
    assert len(plan.load_order) == len(set(plan.load_order))
    assert list(plan.loads["stadt[le].gruen"]) == [
        "stadt[le].grenze"
    ]  # right before its reader
    assert "ungenutzt" in plan.unused_layers


def test_fa75_expansion_is_deterministic_and_idempotent():
    assert expand(_raw()).expanded == expand(_raw()).expanded
    once = expand(_raw()).expanded
    assert not needs_expansion(once)
    assert expand(once).expanded == once


def test_fa75_expanded_form_round_trips_through_scenario():
    dump = api.load_scenario(_raw()).scenario.model_dump(mode="json")
    reloaded = api.load_scenario(
        yaml.safe_dump(dump, allow_unicode=True, sort_keys=False)
    )
    assert reloaded.scenario.model_dump(mode="json") == dump
    assert "stadt[le].anteil" in _ids(dump["steps"])


def test_fa75_source_without_top_level_layers_is_valid_when_instances_add_some():
    raw = _raw()
    assert "layers" not in raw
    assert api.validate(raw).valid
    plain = {
        "scenario": {"name": "x", "region": BBOX},
        "steps": [_buffer("b", "a")],
        "output": [{"type": "geojson", "source": "b"}],
    }
    assert dict(_validation_issues(plain))["layers"] == "Pflichtfeld fehlt"


def test_fa75_when_on_an_instance_drops_elements_and_the_selector_skips_them():
    raw = _raw(instances=[{**INSTANCE, "when": "c.id != 'dd'"}])
    report = expand(raw)
    steps = {step["id"]: step for step in report.expanded["steps"]}
    assert steps["alle"]["inputs"] == {"le": "stadt[le].anteil"}
    assert "stadt[dd].anteil" not in steps
    assert report.dropped == ((("instances", "stadt[dd]"), "c.id != 'dd'"),)


def test_fa75_when_inside_a_module_body_is_evaluated_per_instance():
    module = _module(
        steps=[
            _buffer("gruen", "grenze"),
            _buffer("anteil", "gruen"),
            {**_buffer("extra", "gruen"), "when": "${name} == 'Leipzig'"},
        ]
    )
    report = expand(_raw(modules={"stadt_gruen": module}))
    ids = _ids(report.expanded["steps"])
    assert "stadt[le].extra" in ids and "stadt[dd].extra" not in ids
    assert report.dropped == (
        (("instances", "stadt[dd]", "steps", "extra"), "${name} == 'Leipzig'"),
    )
    assert report.dropped_ids == {"stadt[dd].extra": "${name} == 'Leipzig'"}


def test_fa75_typed_arguments_are_checked_like_parameters():
    raw = _raw(
        instances=[{"id": "stadt", "module": "stadt_gruen", "with": {"name": 5}}],
        steps=[],
        output=[{"type": "geojson", "source": "stadt.anteil"}],
    )
    issues = _issues(raw)
    message = issues["instances -> 0 -> with -> name"]
    assert "Argument 'name' des Moduls 'stadt_gruen'" in message and "Text" in message


def test_fa75_typed_arguments_defaults_fill_missing_args():
    module = _module(
        args={
            "name": {"type": "string", "default": "Stadt"},
            "radius": {"type": "float", "default": 1},
        },
        steps=[_buffer("gruen", "grenze", "${radius}"), _buffer("anteil", "gruen")],
    )
    raw = _raw(
        modules={"stadt_gruen": module},
        instances=[{"id": "stadt", "module": "stadt_gruen"}],
        steps=[],
        output=[{"type": "geojson", "source": "stadt.anteil"}],
    )
    expanded = expand(raw).expanded
    assert expanded["steps"][0]["params"] == {"radius_km": 1.0}
    assert expanded["layers"][0]["features"][0]["name"] == "Stadt"


def test_fa75_module_body_does_not_see_the_loop_variable():
    module = _module(
        steps=[_buffer("gruen", "grenze", "${c}"), _buffer("anteil", "gruen")]
    )
    issues = _issues(_raw(modules={"stadt_gruen": module}))
    message = issues["instances -> stadt[le] -> steps -> gruen -> params -> radius_km"]
    assert "'c' ist weder" in message
    assert "name (Modulargument)" in message and "cities (Parameter)" in message


def test_fa75_report_lists_instances_with_keys_and_dropped_keys():
    report = expand(_raw(instances=[{**INSTANCE, "when": "c.id == 'le'"}]))
    assert report.instances == (
        InstanceInfo("stadt", "stadt_gruen", ("le", "dd"), ("dd",)),
    )
    assert not report.is_trivial()


def test_fa75_report_passed_through_once(monkeypatch):
    calls = []
    original = scenario_module.expand

    def counting(raw, overrides=None):
        calls.append(1)
        return original(raw, overrides)

    monkeypatch.setattr(scenario_module, "expand", counting)
    document = api.load_scenario(_raw())
    assert len(calls) == 1
    assert document.expansion is document.scenario.expansion
    assert (
        document.expansion.node_count == 8
    )  # 2 layers, 2 x 2 steps, collect, 1 output


def test_fa75_core_expansion_names_no_operation():
    """NFA4: the selector yields an ``inputs`` mapping; core never writes ``op``."""
    import geofact.core.expansion as expansion

    source = Path(expansion.__file__).read_text(encoding="utf-8")
    assert '"op"' not in source and "'op'" not in source


# =====================================================================
# Errors (position + message)
# =====================================================================


def test_fa75_unknown_module_lists_declared_ones():
    issues = _issues(_raw(instances=[{**INSTANCE, "module": "x"}]))
    assert (
        issues["instances -> 0 -> module"]
        == "unbekanntes Modul 'x' (deklariert: stadt_gruen)"
    )


def test_fa75_missing_and_unknown_arguments_are_rejected_at_with():
    issues = _issues(_raw(instances=[{**INSTANCE, "with": {}}]))
    assert (
        issues["instances -> 0 (c=le) -> with"]
        == "Modul 'stadt_gruen': Argument(e) ['name'] fehlen in with"
    )
    issues = _issues(_raw(instances=[{**INSTANCE, "with": {"name": "x", "foo": 1}}]))
    assert issues["instances -> 0 (c=le) -> with"] == (
        "Modul 'stadt_gruen' kennt die Argumente ['foo'] nicht (Argumente: name)"
    )


def test_fa75_mapping_elements_require_key():
    instance = {key: value for key, value in INSTANCE.items() if key != "key"}
    issues = _issues(_raw(instances=[instance]))
    assert (
        issues["instances -> 0 -> key"]
        == 'foreach über Mappings braucht key (z. B. key: "${c.id}")'
    )


def test_fa75_key_is_forbidden_without_foreach_or_for_scalars():
    single = {"id": "stadt", "module": "stadt_gruen", "key": "x", "with": {"name": "a"}}
    issues = _issues(
        _raw(
            instances=[single],
            steps=[],
            output=[{"type": "geojson", "source": "stadt.anteil"}],
        )
    )
    assert issues["instances -> 0 -> key"] == "key gibt es nur zusammen mit foreach"
    scalars = {
        **INSTANCE,
        "foreach": {"var": "c", "in": ["a", "b"]},
        "with": {"name": "${c}"},
    }
    issues = _issues(_raw(instances=[scalars]))
    assert "key nur bei Mapping-Elementen" in issues["instances -> 0 -> key"]


def test_fa75_invalid_or_duplicate_key_is_rejected():
    bad = _raw(instances=[{**INSTANCE, "key": "${c.name}"}])
    issues = _issues(bad, {"cities": [{"id": "a", "name": "Bad Homburg"}]})
    assert issues["instances -> 0 (c=#0) -> key"] == (
        "Schlüssel 'Bad Homburg' ist ungültig - erlaubt sind Buchstaben, Ziffern, '_' und '-'"
    )
    issues = _issues(
        _raw(), {"cities": [{"id": "le", "name": "A"}, {"id": "le", "name": "B"}]}
    )
    assert (
        issues["instances -> 0 (c=le) -> key"]
        == "Schlüssel 'le' entsteht in mehreren Elementen"
    )


def test_fa75_empty_or_mixed_foreach_list_is_rejected():
    issues = _issues(_raw(), {"cities": []})
    assert issues["instances -> 0 -> foreach -> in"] == (
        "die Liste ist leer - foreach braucht mindestens ein Element"
    )
    mixed = {**INSTANCE, "foreach": {"var": "c", "in": ["a", {"id": "b"}]}}
    issues = _issues(_raw(instances=[mixed]))
    assert "einheitlich" in issues["instances -> 0 -> foreach -> in"]


def test_fa75_exports_are_required_and_must_name_module_nodes():
    issues = _issues(_raw(modules={"stadt_gruen": _module(exports=[])}))
    assert issues["modules -> stadt_gruen -> exports"] == (
        "exports ist Pflicht und nennt mindestens einen Layer oder Schritt des Moduls"
    )
    issues = _issues(_raw(modules={"stadt_gruen": _module(exports=["anteil", "x"])}))
    assert issues["modules -> stadt_gruen -> exports -> 1"] == (
        "'x' ist kein Layer oder Schritt des Moduls (vorhanden: grenze, gruen, anteil)"
    )


def test_fa75_reference_to_a_non_exported_id_is_rejected_with_the_export_list():
    issues = _issues(_raw(output=[{"type": "geojson", "source": "stadt[le].gruen"}]))
    assert issues["output -> 0 -> source"] == (
        "'stadt[le].gruen' ist kein Export des Moduls stadt_gruen (exports: anteil)"
    )
    issues = _issues(_raw(steps=[_buffer("b", "stadt[dd].grenze")]))
    assert "ist kein Export" in issues["steps -> 0 -> inputs -> geometry"]


def test_fa75_module_body_reference_to_a_non_exported_id_of_another_instance_is_rejected():
    other = {"args": {}, "steps": [_buffer("z", "stadt[le].gruen")], "exports": ["z"]}
    raw = _raw(
        modules={"stadt_gruen": _module(), "fremd": other},
        instances=[dict(INSTANCE), {"id": "t", "module": "fremd"}],
    )
    assert _issues(raw)["instances -> t -> steps -> z -> inputs -> geometry"] == (
        "'stadt[le].gruen' ist kein Export des Moduls stadt_gruen (exports: anteil)"
    )
    other["steps"] = [_buffer("z", "stadt[le].anteil")]
    assert "t.z" in _ids(expand(raw).expanded["steps"])


@pytest.mark.parametrize(
    ("section", "value"),
    [
        ("layers", {"id": "q", "source": "points", "features": []}),
        ("steps", "oops"),
    ],
)
def test_fa75_non_list_section_next_to_instances_is_rejected_not_dropped(
    section, value
):
    raw = _raw(**{section: value})
    assert _issues(raw)[section] == f"{section} muss eine Liste sein"


@pytest.mark.parametrize(
    ("instances", "selector", "fragment"),
    [
        (
            None,
            "x[*].anteil",
            "Selektor 'x[*].anteil': unbekannte Instanz 'x' (deklariert: stadt)",
        ),
        (
            None,
            "stadt[*].gruen",
            "Selektor 'stadt[*].gruen': 'gruen' ist kein Export des Moduls stadt_gruen (exports: anteil)",
        ),
        (
            [{"id": "stadt", "module": "stadt_gruen", "with": {"name": "a"}}],
            "stadt[*].anteil",
            "Selektor 'stadt[*].anteil': Instanz 'stadt' hat kein foreach - 'stadt.anteil' schreiben",
        ),
        (
            [{**INSTANCE, "when": False}],
            "stadt[*].anteil",
            "Selektor 'stadt[*].anteil': alle Elemente der Instanz 'stadt' wurden durch when entfernt",
        ),
    ],
)
def test_fa75_selector_errors_unknown_instance_unknown_export_single_instance_all_dropped(
    instances, selector, fragment
):
    raw = _raw(
        instances=instances, steps=[{"id": "alle", "op": "collect", "inputs": selector}]
    )
    assert _issues(raw)["steps -> 0 -> inputs"] == fragment


def test_fa75_selector_outside_step_inputs_is_rejected():
    issues = _issues(_raw(output=[{"type": "geojson", "source": "stadt[*].anteil"}]))
    assert (
        issues["output -> 0 -> source"]
        == "Selektor nur als ganzer Wert von steps[].inputs möglich"
    )
    issues = _issues(
        _raw(
            steps=[{"id": "alle", "op": "collect", "inputs": {"a": "stadt[*].anteil"}}]
        )
    )
    assert (
        issues["steps -> 0 -> inputs -> a"]
        == "Selektor nur als ganzer Wert von steps[].inputs möglich"
    )
    issues = _issues(_raw(steps=[{"id": "alle", "op": "collect", "inputs": "stadt"}]))
    assert (
        "inputs muss ein Mapping (port: quelle) oder ein Selektor"
        in issues["steps -> 0 -> inputs"]
    )


def test_fa75_brackets_in_authored_ids_are_rejected():
    layer = {
        "id": "a[b]",
        "source": "points",
        "features": [{"name": "a", "lon": 12.9, "lat": 50.82}],
    }
    issues = _issues(_raw(layers=[layer]))
    assert issues["layers -> 0 -> id"].startswith(
        "id 'a[b]' darf '[' und ']' nicht enthalten"
    )
    issues = _issues(_raw(instances=[{**INSTANCE, "id": "s[x]"}]))
    assert "darf '[' und ']' nicht enthalten" in issues["instances -> 0 -> id"]
    module = _module(steps=[_buffer("gruen", "grenze"), _buffer("anteil[1]", "gruen")])
    issues = _issues(_raw(modules={"stadt_gruen": module}))
    assert (
        "darf '[' und ']' nicht enthalten"
        in issues["modules -> stadt_gruen -> steps -> 1 -> id"]
    )


def test_fa75_reserved_keys_inside_a_module_are_rejected():
    for key, value, fragment in (
        ("foreach", {"var": "a", "in": [1]}, "foreach gibt es nur an einer Instanz"),
        ("use", "x", "use gibt es nicht mehr"),
        ("collect", {"id": "a"}, "collect ist ein normaler Schritt"),
        ("instances", [], "keine verschachtelten Module"),
    ):
        module = _module(
            steps=[
                _buffer("gruen", "grenze"),
                {**_buffer("anteil", "gruen"), key: value},
            ]
        )
        issues = _issues(_raw(modules={"stadt_gruen": module}))
        assert fragment in issues[f"modules -> stadt_gruen -> steps -> 1 -> {key}"], key


def test_fa75_former_syntax_is_rejected_with_a_pointer_to_modules():
    issues = _issues(_raw(templates={"x": {}}))
    assert "modules" in issues["templates"] and "instances" in issues["templates"]
    issues = _issues(_raw(layers=[{"foreach": {"var": "a", "in": [1]}, "id": "p"}]))
    assert (
        issues["layers -> 0 -> foreach"]
        == "foreach gibt es nur an einer Instanz (instances)"
    )


def test_fa75_loop_variable_or_argument_must_not_shadow_a_parameter():
    issues = _issues(
        _raw(instances=[{**INSTANCE, "foreach": {"var": "cities", "in": "${cities}"}}])
    )
    assert issues["instances -> 0 -> foreach -> var"] == (
        "Schleifenvariable 'cities' verdeckt Parameter gleichen Namens - anderen Namen wählen"
    )
    module = _module(
        args={"name": {"type": "string"}, "cities": {"type": "string", "default": "x"}}
    )
    issues = _issues(_raw(modules={"stadt_gruen": module}))
    assert "verdeckt Parameter" in issues["modules -> stadt_gruen -> args -> cities"]


def test_fa75_node_cap_is_enforced(monkeypatch):
    monkeypatch.setenv(MAX_NODES_ENV, "4")
    issues = _issues(_raw())
    [(location, message)] = issues.items()
    assert location == "instances -> stadt[dd] -> steps -> gruen"  # node 5
    assert MAX_NODES_ENV in message and "mehr als 4 Knoten" in message


def test_fa75_pydantic_error_in_a_generated_node_names_the_domain_position():
    module = _module(
        steps=[_buffer("gruen", "grenze"), _buffer("anteil", "gruen", "${name}")]
    )
    issues = _validation_issues(_raw(modules={"stadt_gruen": module}))
    assert [location for location, _ in issues] == [
        "instances -> stadt[le] -> steps -> anteil -> params -> radius_km",
        "instances -> stadt[dd] -> steps -> anteil -> params -> radius_km",
    ]
    assert "muss eine Zahl sein" in issues[0][1] and "Leipzig" in issues[0][1]


def test_fa75_identical_errors_in_all_instances_are_reported_once():
    module = _module(
        steps=[_buffer("gruen", "grenze"), _buffer("anteil", "gruen", "zwei")]
    )
    [(location, message)] = _validation_issues(_raw(modules={"stadt_gruen": module}))
    assert location == "instances -> stadt -> steps -> anteil -> params -> radius_km"
    assert "muss eine Zahl sein" in message
    assert message.endswith("(in allen 2 Instanzen von stadt)")


def test_fa75_duplicate_generated_and_authored_id_names_both_positions():
    raw = _raw(
        instances=[{"id": "stadt", "module": "stadt_gruen", "with": {"name": "a"}}],
        steps=[_buffer("stadt.anteil", "stadt.gruen")],
        output=[{"type": "geojson", "source": "stadt.anteil"}],
    )
    raw["modules"]["stadt_gruen"]["exports"] = ["anteil", "gruen"]
    [(location, message)] = _validation_issues(raw)
    assert location == "steps -> 0 -> id"
    assert (
        message
        == "ID 'stadt.anteil' ist doppelt (zuerst bei instances -> stadt -> steps -> anteil)"
    )


def test_fa75_dropped_but_referenced_generated_node_is_named_in_unknown_source():
    raw = _raw(
        instances=[{**INSTANCE, "when": "c.id != 'dd'"}],
        output=[
            {"type": "geojson", "source": "alle"},
            {"type": "geojson", "source": "stadt[dd].anteil", "path": "dd.geojson"},
        ],
    )
    [(location, message)] = _validation_issues(raw)
    assert location == "output -> 1 -> source"
    assert message.startswith(
        "('stadt[dd].anteil' wurde durch when: c.id != 'dd' entfernt)"
    )


def test_fa75_json_schema_lists_modules_instances_when_and_selector():
    schema = catalog.scenario_json_schema()
    assert schema["properties"]["modules"]["additionalProperties"] == {
        "$ref": "#/$defs/ModuleSpec"
    }
    assert schema["properties"]["instances"]["items"] == {
        "$ref": "#/$defs/InstanceSpec"
    }
    assert "layers" not in schema["required"]
    module = schema["$defs"]["ModuleSpec"]
    assert module["properties"]["steps"]["items"] == {
        "$ref": "#/properties/steps/items"
    }
    assert module["required"] == ["exports"]
    assert set(schema["$defs"]["InstanceSpec"]["properties"]) == {
        "id",
        "module",
        "foreach",
        "key",
        "with",
        "when",
    }
    buffer = schema["$defs"]["BufferStep"]["properties"]
    assert buffer["when"] == {"$ref": "#/$defs/When"}
    selector = buffer["inputs"]["anyOf"][1]
    assert selector["type"] == "string" and "?P<" not in selector["pattern"]
    assert schema["$defs"]["OutputSpec"]["properties"]["when"] == {
        "$ref": "#/$defs/When"
    }
    for gone in (
        "TemplateSpec",
        "ForeachItem",
        "UseItem",
        "ForeachSpec",
        "CollectSpec",
    ):
        assert gone not in schema["$defs"]
