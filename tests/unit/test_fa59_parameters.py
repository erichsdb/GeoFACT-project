"""Implements: FA59 (Szenario-Parameter und Platzhalter).

Contract tests for src/geofact/core/expansion.py (parameters, placeholders,
overrides), src/geofact/engine/parameters.py (CLI coercion) and the hook in
core/scenario.py. CLI and web parts
of FA59 belong to W3-01/W3-02. Offline, no data is loaded.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from geofact import api
from geofact.core.errors import ConfigError
from geofact.core.expansion import ParameterSpec, expand, needs_expansion
from geofact.core.scenario import Scenario
from geofact.engine import catalog
from geofact.engine.parameters import coerce_cli_value

BBOX = "12.85,50.78,12.98,50.87"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def _points(layer_id: str) -> dict:
    return {
        "id": layer_id,
        "source": "points",
        "features": [{"name": "a", "lon": 12.90, "lat": 50.82}],
    }


def _raw(**extra) -> dict:
    raw = {
        "scenario": {"name": "Parameter", "region": BBOX},
        "layers": [_points("punkte")],
        "steps": [
            {
                "id": "puffer",
                "op": "buffer",
                "inputs": {"geometry": "punkte"},
                "params": {"radius_km": 1},
            }
        ],
        "output": [{"type": "geojson", "source": "puffer"}],
    }
    raw.update(extra)
    return raw


def _issues(raw: dict, overrides: dict | None = None) -> list[tuple[str, str]]:
    with pytest.raises(ConfigError) as info:
        expand(raw, overrides)
    return info.value.issues


def test_fa59_whole_value_placeholder_keeps_its_type():
    raw = _raw(
        parameters={
            "radius": {"type": "float", "default": 2.5},
            "names": {"type": "list", "items": "string", "default": ["a", "b"]},
            "flag": {"type": "boolean", "default": True},
            "city": {"type": "mapping", "default": {"id": "l", "n": 3}},
        }
    )
    raw["steps"][0]["params"] = {"radius_km": "${radius}"}
    raw["scenario"]["description"] = {"x": "${names}", "y": "${flag}", "z": "${city}"}
    expanded = expand(raw).expanded
    assert expanded["steps"][0]["params"]["radius_km"] == 2.5
    assert expanded["scenario"]["description"] == {
        "x": ["a", "b"],
        "y": True,
        "z": {"id": "l", "n": 3},
    }


def test_fa59_embedded_placeholder_becomes_text_with_lowercase_booleans():
    raw = _raw(
        parameters={
            "n": {"type": "integer", "default": 3},
            "flag": {"type": "boolean", "default": False},
        }
    )
    raw["scenario"]["name"] = "Lauf ${n} (${flag})"
    assert expand(raw).expanded["scenario"]["name"] == "Lauf 3 (false)"


def test_fa59_embedded_mapping_or_list_is_an_error_with_hint():
    raw = _raw(parameters={"city": {"type": "mapping", "default": {"id": "l"}}})
    raw["scenario"]["name"] = "Stadt ${city}"
    [(location, message)] = _issues(raw)
    assert location == "scenario -> name"
    assert "${name.key}" in message and "Mapping" in message


def test_fa59_dotted_placeholder_reads_a_mapping_key():
    raw = _raw(parameters={"city": {"type": "mapping", "default": {"id": "leipzig"}}})
    raw["scenario"]["name"] = "Gruen ${city.id}"
    assert expand(raw).expanded["scenario"]["name"] == "Gruen leipzig"
    raw["scenario"]["name"] = "${city.name}"
    [(location, message)] = _issues(raw)
    assert "keinen Schlüssel 'name'" in message and "vorhanden: id" in message


def test_fa59_dollar_escape_yields_a_literal_placeholder():
    raw = _raw(parameters={"n": {"type": "integer", "default": 1}})
    raw["scenario"]["name"] = "Preis $${n} bleibt"
    assert expand(raw).expanded["scenario"]["name"] == "Preis ${n} bleibt"


def test_fa59_scenario_without_expansion_keys_is_passed_through_dict_identically():
    files = sorted(EXAMPLES.rglob("*.yaml"))
    assert len(files) >= 20
    for path in files:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "scenario" not in raw:
            continue
        if needs_expansion(
            raw
        ):  # showcases of FA59-FA62 (W4-04) use expansion on purpose
            continue
        before = copy.deepcopy(raw)
        report = expand(raw)
        assert report.expanded == before, path.name
        assert report.is_trivial()
        assert raw == before  # nothing mutated


def test_fa59_undeclared_placeholder_names_its_position_and_the_known_names():
    raw = _raw(parameters={"radius": {"type": "float", "default": 1.0}})
    raw["steps"][0]["params"] = {"radius_km": "${radus}"}
    [(location, message)] = _issues(raw)
    assert location == "steps -> 0 -> params -> radius_km"
    assert "'radus'" in message and "radius (Parameter)" in message


def test_fa59_parameter_without_default_must_be_overridden():
    raw = _raw(parameters={"radius": {"type": "float"}})
    [(location, message)] = _issues(raw)
    assert location == "parameters -> radius"
    assert "muss überschrieben werden" in message
    assert expand(raw, {"radius": 2}).values["radius"] == 2.0


def test_fa59_override_replaces_the_default_and_is_reported():
    raw = _raw(
        parameters={
            "radius": {"type": "float", "default": 1.0},
            "label": {"type": "string", "default": "x"},
        }
    )
    raw["steps"][0]["params"] = {"radius_km": "${radius}"}
    report = expand(raw, {"radius": 4})
    assert report.expanded["steps"][0]["params"]["radius_km"] == 4.0
    assert report.overridden == ("radius",)
    assert report.values == {"radius": 4.0, "label": "x"}
    assert "parameters" not in report.expanded


def test_fa59_unknown_override_lists_the_declared_parameters():
    raw = _raw(parameters={"radius": {"type": "float", "default": 1.0}})
    [(location, message)] = _issues(raw, {"radiuss": 3})
    assert location == "parameters -> radiuss"
    assert "deklariert: radius" in message


def test_fa59_override_with_the_wrong_type_is_rejected():
    raw = _raw(parameters={"n": {"type": "integer", "default": 1}})
    [(location, message)] = _issues(raw, {"n": "3"})
    assert location == "parameters -> n"
    assert "ganze Zahl" in message


def test_fa59_choices_are_enforced_for_values_and_list_items():
    raw = _raw(
        parameters={
            "mode": {"type": "string", "default": "voll", "choices": ["voll", "kurz"]},
            "tags": {
                "type": "list",
                "items": "string",
                "default": ["park"],
                "choices": ["park", "wald"],
            },
        }
    )
    assert (
        expand(raw, {"mode": "kurz", "tags": ["wald", "park"]}).values["mode"] == "kurz"
    )
    issues = _issues(raw, {"mode": "lang", "tags": ["see"]})
    assert [loc for loc, _ in issues] == ["parameters -> mode", "parameters -> tags"]
    assert all("nicht erlaubt" in message for _, message in issues)


def test_fa59_invalid_parameter_name_and_unknown_spec_field_are_rejected():
    raw = _raw(
        parameters={
            "2x": {"type": "integer", "default": 1},
            "ok": {"type": "integer", "default": 1, "typo": 1},
        }
    )
    issues = dict(_issues(raw))
    assert "kein Bezeichner" in issues["parameters -> 2x"]
    assert "unbekanntes Feld" in issues["parameters -> ok -> typo"]


def test_fa59_default_must_match_the_declared_type():
    raw = _raw(parameters={"n": {"type": "integer", "default": "100000"}})
    [(location, message)] = _issues(raw)
    assert location == "parameters -> n"
    assert "default" in message and "ganze Zahl" in message


def test_fa59_coerce_cli_value_coerces_strings_and_stays_strict_otherwise():
    assert coerce_cli_value(ParameterSpec(type="integer"), "42") == 42
    assert coerce_cli_value(ParameterSpec(type="float"), "2") == 2.0
    assert coerce_cli_value(ParameterSpec(type="boolean"), "false") is False
    assert coerce_cli_value(ParameterSpec(type="string"), "123") == "123"
    assert coerce_cli_value(ParameterSpec(type="list", items="integer"), "[1, 2]") == [
        1,
        2,
    ]
    assert coerce_cli_value(ParameterSpec(type="list"), "a,b") == ["a", "b"]
    assert (
        coerce_cli_value(ParameterSpec(type="integer"), 7) == 7
    )  # schon von yaml gelesen
    with pytest.raises(ValueError, match="keine ganze Zahl"):
        coerce_cli_value(ParameterSpec(type="integer"), "3.5")
    with pytest.raises(ValueError, match="Wahrheitswert"):
        coerce_cli_value(ParameterSpec(type="boolean"), "vielleicht")


def test_fa59_overrides_come_from_the_validation_context():
    raw = _raw(parameters={"radius": {"type": "float", "default": 1.0}})
    raw["steps"][0]["params"] = {"radius_km": "${radius}"}
    context = {
        "registry": api.load_extensions(plugin_paths=[]),
        "parameters": {"radius": 3},
    }
    scenario = Scenario.model_validate(raw, context=context)
    assert scenario.steps[0].params["radius_km"] == 3.0
    assert api.load_scenario(raw).scenario.steps[0].params["radius_km"] == 1.0


def test_fa59_expansion_is_idempotent():
    raw = _raw(parameters={"radius": {"type": "float", "default": 1.0}})
    raw["steps"][0]["params"] = {"radius_km": "${radius}"}
    once = expand(raw).expanded
    assert not needs_expansion(once)
    assert expand(once).expanded == once


def test_fa59_json_schema_lists_parameters_and_modules():
    schema = catalog.scenario_json_schema()
    assert schema["properties"]["parameters"]["additionalProperties"] == {
        "$ref": "#/$defs/ParameterSpec"
    }
    assert schema["properties"]["modules"]["additionalProperties"] == {
        "$ref": "#/$defs/ModuleSpec"
    }
    assert set(schema["$defs"]["ParameterSpec"]["properties"]) >= {
        "type",
        "items",
        "default",
        "description",
        "choices",
    }
    assert "templates" not in schema["properties"]


def _list_doc() -> dict:
    return _raw(
        parameters={
            "years": {"type": "list", "items": "integer", "default": [2020, 2021]},
            "countries": {"type": "list", "items": "string", "default": ["DE"]},
            "codes": {"type": "list", "items": "string", "default": ["01"]},
            "ratios": {"type": "list", "items": "float", "default": [0.5]},
            "flags": {"type": "list", "items": "boolean", "default": [True]},
            "zones": {"type": "list", "items": "mapping", "default": [{"id": "a"}]},
        }
    )


def test_fa59_single_element_list_override_from_cli_and_form():
    """Nachtreview 2: a list parameter given as one number or one YAML keyword."""
    doc = _list_doc()
    assert api.parameter_overrides(doc, ["years=2020"]) == {"years": [2020]}
    assert api.parameter_overrides(doc, ["countries=NO"]) == {"countries": ["NO"]}
    assert api.parameter_overrides(doc, ["codes=[14713000]"]) == {"codes": ["14713000"]}
    assert api.parameter_overrides(doc, ["codes=14713000"]) == {"codes": ["14713000"]}
    assert api.parameter_overrides(doc, ["ratios=1"]) == {"ratios": [1.0]}
    assert api.parameter_overrides(doc, ["flags=false"]) == {"flags": [False]}
    assert api.parameter_overrides(doc, {"years": "2020"}) == {"years": [2020]}
    assert api.parameter_overrides(doc, {"countries": "yes"}) == {"countries": ["yes"]}


def test_fa59_multi_element_list_overrides_keep_working():
    doc = _list_doc()
    assert api.parameter_overrides(doc, ["years=2020,2021"]) == {"years": [2020, 2021]}
    assert api.parameter_overrides(doc, ["years=[2020, 2021]"]) == {
        "years": [2020, 2021]
    }
    assert api.parameter_overrides(doc, ["codes=01234,05"]) == {
        "codes": ["01234", "05"]
    }
    assert api.parameter_overrides(doc, ["countries=[NO, DE]"]) == {
        "countries": ["NO", "DE"]
    }
    assert api.parameter_overrides(doc, ['countries=["NO", "DE"]']) == {
        "countries": ["NO", "DE"]
    }
    assert api.parameter_overrides(doc, ["zones=[{id: b}]"]) == {"zones": [{"id": "b"}]}
    with pytest.raises(ConfigError, match="ganze Zahl"):
        api.parameter_overrides(doc, ["years=zwanzig"])


def test_fa59_substituted_key_must_not_overwrite_an_existing_key():
    """Nachtreview 7: {'${a}': 1, 'b': 2} with a='b' lost the value 1 silently."""
    raw = _raw(parameters={"a": {"type": "string", "default": "b"}})
    raw["scenario"]["description"] = {"${a}": 1, "b": 2}
    [(location, message)] = _issues(raw)
    assert location.startswith("scenario -> description")
    assert "'b'" in message and "doppelt" in message


def test_fa59_whole_value_key_placeholder_becomes_text():
    raw = _raw(
        parameters={
            "n": {"type": "integer", "default": 5},
            "f": {"type": "boolean", "default": True},
        }
    )
    raw["scenario"]["description"] = {"${n}": "five", "${f}": "yes"}
    assert expand(raw).expanded["scenario"]["description"] == {
        "5": "five",
        "true": "yes",
    }
