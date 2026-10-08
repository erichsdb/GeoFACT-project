"""Implements: FA62 (Bedingungen zur Expansionszeit ``when``).

Contract tests for ``when`` (evaluator in src/geofact/core/conditions.py,
placement in core/expansion.py: top-level nodes, module-body nodes and
instances, FA75): dropped nodes are reported, expressions are evaluated by a
whitelisted AST walker (never
``eval``), comparisons are type-safe (hint "Zahl ohne Anfuehrungszeichen"),
and a dropped but referenced node is named in the "unbekannte Quelle" error.
The ``gate`` operation is tested in test_fa62_gate.py. Offline.
"""

from __future__ import annotations

import builtins

import pytest

from geofact import api
from geofact.core.errors import ConfigError
from geofact.core.conditions import ExpansionFail, Scope, evaluate_when
from geofact.core.expansion import expand

BBOX = "12.85,50.78,12.98,50.87"


def _raw(output_when=None, **extra) -> dict:
    karte = {"type": "map", "source": "puffer"}
    if output_when is not None:
        karte["when"] = output_when
    raw = {
        "parameters": {
            "with_map": {"type": "boolean", "default": False},
            "mode": {"type": "string", "default": "voll"},
            "n": {"type": "integer", "default": 3},
        },
        "scenario": {"name": "Bedingungen", "region": BBOX},
        "layers": [
            {
                "id": "punkte",
                "source": "points",
                "features": [{"name": "a", "lon": 12.9, "lat": 50.82}],
            }
        ],
        "steps": [
            {
                "id": "puffer",
                "op": "buffer",
                "inputs": {"geometry": "punkte"},
                "params": {"radius_km": 1},
            }
        ],
        "output": [{"type": "geojson", "source": "puffer"}, karte],
    }
    raw.update(extra)
    return raw


def _issues(raw: dict) -> list[tuple[str, str]]:
    with pytest.raises(ConfigError) as info:
        expand(raw)
    return info.value.issues


def test_fa62_when_false_drops_the_node_and_reports_it():
    report = expand(_raw(output_when="${with_map}"))
    assert [out["type"] for out in report.expanded["output"]] == ["geojson"]
    assert report.dropped == ((("output", "1"), "${with_map}"),)
    shown = expand(_raw(output_when="${with_map}"), {"with_map": True})
    assert [out["type"] for out in shown.expanded["output"]] == ["geojson", "map"]
    assert shown.dropped == ()


def test_fa62_when_expression_combines_parameters_and_literals():
    assert (
        len(expand(_raw(output_when="${mode} == 'voll' and n >= 3")).expanded["output"])
        == 2
    )
    assert (
        len(expand(_raw(output_when="mode != 'voll' or not n < 5")).expanded["output"])
        == 1
    )
    assert (
        len(expand(_raw(output_when="${mode} in ['kurz', 'voll']")).expanded["output"])
        == 2
    )


def _point_module() -> dict:
    return {
        "punkt": {
            "layers": [
                {
                    "id": "x",
                    "source": "points",
                    "features": [{"name": "a", "lon": 12.9, "lat": 50.82}],
                }
            ],
            "exports": ["x"],
        }
    }


def test_fa62_when_with_loop_variable_filters_instance_elements():
    instances = [
        {
            "id": "p",
            "module": "punkt",
            "key": "${c.id}",
            "when": "c.pop >= 100",
            "foreach": {
                "var": "c",
                "in": [{"id": "le", "pop": 600}, {"id": "zw", "pop": 90}],
            },
        }
    ]
    report = expand(_raw(modules=_point_module(), instances=instances))
    assert [layer["id"] for layer in report.expanded["layers"]] == ["p[le].x", "punkte"]
    assert report.dropped == ((("instances", "p[zw]"), "c.pop >= 100"),)
    assert report.dropped_ids == {"p[zw].x": "c.pop >= 100"}


def test_fa62_type_mix_in_a_comparison_is_an_error_with_hint():
    [(location, message)] = _issues(_raw(output_when="${n} >= '3'"))
    assert location == "output -> 1 -> when"
    assert "Zahl ohne Anführungszeichen" in message
    [(location, message)] = _issues(_raw(output_when="${mode} == true"))
    assert "denselben Typ" in message


def test_fa62_calls_attributes_and_indexes_are_rejected_without_eval(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("eval darf nie aufgerufen werden")

    monkeypatch.setattr(builtins, "eval", forbidden)
    for expression in (
        "__import__('os').system('x')",
        "mode[0] == 'v'",
        "len(mode) > 1",
        "(lambda: True)()",
    ):
        [(location, message)] = _issues(_raw(output_when=expression))
        assert location == "output -> 1 -> when"
        assert "nicht erlaubt" in message, expression


def test_fa62_when_must_yield_a_boolean():
    [(_, message)] = _issues(_raw(output_when="${n}"))
    assert "muss true oder false ergeben" in message and "ganze Zahl" in message
    [(_, message)] = _issues(_raw(output_when=3))
    assert "true/false oder ein Ausdruck" in message


def test_fa62_dropped_but_referenced_node_is_named_in_unknown_source():
    raw = _raw()
    raw["steps"].append(
        {
            "id": "karte",
            "op": "buffer",
            "inputs": {"geometry": "puffer"},
            "params": {"radius_km": 2},
            "when": "${with_map}",
        }
    )
    raw["output"][1]["source"] = "karte"
    report = api.validate(raw)
    assert not report.valid
    [(location, message)] = report.issues
    assert location == "output -> 1 -> source"
    assert message.startswith("('karte' wurde durch when: ${with_map} entfernt)")
    assert "unbekannte Quelle 'karte'" in message


def test_fa62_when_on_an_instance_and_on_a_module_node():
    step = {
        "op": "buffer",
        "inputs": {"geometry": "puffer"},
        "params": {"radius_km": 1},
    }
    modules = {
        "extra": {
            "args": {"k": {"type": "integer", "default": 1}},
            "steps": [{"id": "a", **step}, {"id": "b", "when": "k > 1", **step}],
            "exports": ["a"],
        }
    }
    instances = [
        {"id": "e", "module": "extra", "when": "${with_map}"},
        {"id": "f", "module": "extra", "when": "n == 3"},
    ]
    report = expand(_raw(modules=modules, instances=instances))
    assert [s["id"] for s in report.expanded["steps"]] == ["f.a", "puffer"]
    assert report.dropped == (
        (("instances", "e"), "${with_map}"),
        (("instances", "f", "steps", "b"), "k > 1"),
    )
    assert report.dropped_ids == {
        "e.a": "${with_map}",
        "e.b": "${with_map}",
        "f.b": "k > 1",
    }


def test_fa62_evaluator_lives_in_core_conditions():
    scope = Scope({"n": 3}, {"n": "Parameter"})
    assert evaluate_when("n >= 3 and not n == 4", scope, ("x",)) is True
    with pytest.raises(ExpansionFail) as info:
        evaluate_when("n == '3'", scope, ("output", "1", "when"))
    assert info.value.location == "output -> 1 -> when"


def _city_raw(output_when: str, **parameters) -> dict:
    raw = _raw(output_when=output_when)
    raw["parameters"].update(parameters)
    return raw


def test_fa62_quoted_placeholder_is_replaced_by_its_text():
    """Nachtreview 3: '${city}' inside quotes compared the literal name 'city' and
    dropped the node silently."""
    city = {"city": {"type": "string", "default": "leipzig"}}
    assert (
        len(expand(_city_raw("'${city}' == 'leipzig'", **city)).expanded["output"]) == 2
    )
    assert (
        len(expand(_city_raw('"${city}" == "leipzig"', **city)).expanded["output"]) == 2
    )
    assert (
        len(expand(_city_raw("${city} == 'leip${city}'", **city)).expanded["output"])
        == 1
    )
    assert (
        len(expand(_city_raw("'x${city}' == 'xleipzig'", **city)).expanded["output"])
        == 2
    )
    quote = {"city": {"type": "string", "default": "it's"}}
    assert (
        len(expand(_city_raw("'${city}' == \"it's\"", **quote)).expanded["output"]) == 2
    )


def test_fa62_quoted_list_placeholder_is_an_error():
    names = {"names": {"type": "list", "default": ["a"]}}
    [(location, message)] = _issues(_city_raw("'${names}' == 'a'", **names))
    assert location == "output -> 1 -> when"
    assert "Liste" in message


def test_fa62_keyword_named_loop_variable_works_in_when():
    """Nachtreview 8: a loop variable named like a Python keyword (in, if, from)."""
    raw = _raw(
        modules=_point_module(),
        instances=[
            {
                "id": "p",
                "module": "punkt",
                "foreach": {"var": "in", "in": ["a", "b"]},
                "when": "${in} == 'a'",
            }
        ],
    )
    raw["layers"] = []
    raw["steps"][0]["inputs"] = {"geometry": "p[a].x"}
    assert [layer["id"] for layer in expand(raw).expanded["layers"]] == ["p[a].x"]


def test_fa62_keyword_named_mapping_variable_with_key_works_in_when():
    raw = _raw(
        modules=_point_module(),
        instances=[
            {
                "id": "p",
                "module": "punkt",
                "key": "${from.id}",
                "when": "${from.n} > 2",
                "foreach": {
                    "var": "from",
                    "in": [{"id": "a", "n": 1}, {"id": "b", "n": 5}],
                },
            }
        ],
    )
    raw["layers"] = []
    raw["steps"][0]["inputs"] = {"geometry": "p[b].x"}
    assert [layer["id"] for layer in expand(raw).expanded["layers"]] == ["p[b].x"]


def test_fa62_error_message_shows_the_placeholder_not_an_internal_name():
    [(_, message)] = _issues(_city_raw("${n} and ${with_map}"))
    assert "${n}" in message or "'n'" in message
    assert "__" not in message


def test_fa62_short_circuit_does_not_hide_a_type_error():
    """Nachtreview 6: the typed comparison in a later operand is checked even when
    the first operand already decides the result."""
    [(location, message)] = _issues(_city_raw("${with_map} and ${n} == '3'"))
    assert location == "output -> 1 -> when"
    assert "Zahl ohne Anführungszeichen" in message or "Zahl 3" in message
    [(_, message)] = _issues(_city_raw("not ${with_map} or ${n} == '3'"))
    assert "Zahl 3" in message
