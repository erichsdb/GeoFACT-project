"""Implements: FA59 (Anschluss an api und CLI: --param, --print-expanded), FA62 (validate-Zeile 'uebersprungen durch when').

Contract tests for parameter overrides through ``api.load_scenario`` /
``api.validate`` (``parameters=...``), ``ScenarioDocument.expansion`` and the
CLI flags ``--param NAME=VALUE`` (validate and run) and ``--print-expanded``.
Offline: file fixtures and a bbox region.
"""

from __future__ import annotations

import pytest
import yaml
from _cli_doubles import BBOX_REGION, base_config, geofact, write_config

from geofact import api, plugin_api


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.chdir(tmp_path)


def _parametrised() -> dict:
    config = base_config()
    config["parameters"] = {
        "radius": {"type": "float", "default": 1.0},
        "anzahl": {"type": "integer", "default": 1},
        "karte": {"type": "boolean", "default": False},
    }
    config["steps"][0]["params"] = {"radius_km": "${radius}"}
    config["steps"].append(
        {
            "id": "größte",
            "op": "top_n",
            "inputs": {"features": "einzug"},
            "params": {"n": "${anzahl}", "by": "area_km2"},
        }
    )
    config["output"].append(
        {"type": "map", "source": "einzug", "path": "karte.html", "when": "${karte}"}
    )
    return config


def _points(layer_id: str) -> dict:
    return {
        "id": layer_id,
        "source": "points",
        "features": [{"name": "a", "lon": 13.70, "lat": 51.05}],
    }


def test_fa59_api_load_scenario_applies_parameters_and_keeps_the_expansion_report():
    document = api.load_scenario(_parametrised(), parameters={"radius": 2.5})
    assert document.scenario.steps[0].params["radius_km"] == 2.5
    assert document.parameters == {"radius": 2.5}
    assert isinstance(document.expansion, api.ExpansionReport)
    assert document.expansion.overridden == ("radius",)
    assert document.expansion.values["radius"] == 2.5
    assert document.expansion.dropped  # output -> 1 dropped by when


def test_fa59_api_validate_accepts_parameters_and_reports_bad_overrides():
    report = api.validate(_parametrised(), parameters={"anzahl": 3})
    assert report.valid, report.issues
    assert report.document.expansion.values["anzahl"] == 3

    bad = api.validate(_parametrised(), parameters={"gibtsnicht": 1})
    assert not bad.valid
    location, message = bad.issues[0]
    assert location == "parameters -> gibtsnicht"
    assert "radius" in message and "anzahl" in message


def test_fa59_api_document_without_expansion_keys_has_a_trivial_report():
    document = api.load_scenario(base_config())
    assert document.parameters == {}
    assert document.expansion is not None and document.expansion.is_trivial()


def test_fa59_parameter_types_are_reexported_in_both_facades():
    for facade in (api, plugin_api):
        assert facade.ParameterSpec.__name__ == "ParameterSpec"
        assert facade.ExpansionReport.__name__ == "ExpansionReport"
        assert facade.NodeOrigin.__name__ == "NodeOrigin"
    assert api.RegionInfo.__name__ == "RegionInfo"


def test_fa59_cli_param_flag_on_validate_and_run(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path / "konfig", _parametrised())

    code, stdout, stderr = geofact(
        monkeypatch,
        capsys,
        "validate",
        config,
        "--param",
        "radius=2",
        "--param",
        "karte=true",
    )
    assert code == 0, stderr
    assert "radius=2.0 (überschrieben)" in stdout
    assert "karte=true (überschrieben)" in stdout
    assert "übersprungen durch when" not in stdout

    out = tmp_path / "out"
    code, stdout, stderr = geofact(
        monkeypatch, capsys, "run", config, "--out", out, "--param", "karte=true"
    )
    assert code == 0, stderr
    assert (out / "karte.html").is_file()
    assert "karte=true (überschrieben)" in stdout


def test_fa59_cli_string_override_is_coerced_to_integer(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path / "konfig", _parametrised())
    code, stdout, stderr = geofact(
        monkeypatch, capsys, "validate", config, "--param", "anzahl=3"
    )
    assert code == 0, stderr
    assert "anzahl=3 (überschrieben)" in stdout

    code, _, stderr = geofact(
        monkeypatch, capsys, "validate", config, "--param", "anzahl=drei"
    )
    assert code == 1
    assert "[parameters -> anzahl]" in stderr and "drei" in stderr


def test_fa59_cli_param_without_equals_sign_is_an_error(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path / "konfig", _parametrised())
    code, _, stderr = geofact(
        monkeypatch, capsys, "validate", config, "--param", "radius"
    )
    assert code == 1
    assert "[--param]" in stderr and "NAME=WERT" in stderr


def test_fa59_cli_unknown_param_lists_the_declared_ones(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path / "konfig", _parametrised())
    code, _, stderr = geofact(monkeypatch, capsys, "validate", config, "--param", "x=1")
    assert code == 1
    assert "[parameters -> x]" in stderr and "radius" in stderr


def test_fa62_cli_validate_names_nodes_dropped_by_when(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path / "konfig", _parametrised())
    code, stdout, stderr = geofact(monkeypatch, capsys, "validate", config)
    assert code == 0, stderr
    assert "übersprungen durch when: output -> 1 (${karte})" in stdout


def test_fa59_print_expanded_dumps_flat_yaml(tmp_path, monkeypatch, capsys):
    raw = base_config()
    raw["parameters"] = {
        "cities": {"type": "list", "items": "string", "default": ["a", "b"]}
    }
    raw["modules"] = {"punkt": {"layers": [_points("p")], "exports": ["p"]}}
    raw["instances"] = [
        {"id": "q", "module": "punkt", "foreach": {"var": "c", "in": "${cities}"}}
    ]
    config = write_config(tmp_path / "konfig", raw)
    code, stdout, stderr = geofact(
        monkeypatch, capsys, "validate", config, "--print-expanded"
    )
    assert code == 0, stderr
    dumped = yaml.safe_load(stdout.split("--- ausgedehntes Szenario ---", 1)[1])
    assert "parameters" not in dumped
    assert [layer["id"] for layer in dumped["layers"]] == [
        "q[a].p",
        "q[b].p",
        "umspannwerke",
    ]
    assert not {"instances", "modules"} & set(dumped)
    assert "${" not in yaml.safe_dump(dumped)
    assert dumped["scenario"]["region"] == BBOX_REGION


def test_fa75_pydantic_error_in_generated_node_names_instance_key_and_inner_id():
    raw = base_config()
    raw["parameters"] = {
        "cities": {"type": "list", "items": "string", "default": ["leipzig", "dresden"]}
    }
    raw["modules"] = {
        "stadt": {
            "args": {"name": {"type": "string"}},
            "steps": [
                {
                    "id": "puffer",
                    "op": "buffer",
                    "inputs": {"geometry": "umspannwerke"},
                    "params": {"radius_km": "${name}"},
                }
            ],
            "exports": ["puffer"],
        }
    }
    raw["instances"] = [
        {
            "id": "stadt",
            "module": "stadt",
            "with": {"name": "${city}"},
            "foreach": {"var": "city", "in": "${cities}"},
        }
    ]
    with pytest.raises(api.ConfigError) as info:
        api.load_scenario(raw)
    location, message = info.value.issues[0]
    assert (
        location
        == "instances -> stadt[leipzig] -> steps -> puffer -> params -> radius_km"
    )
    assert "muss eine Zahl sein" in message
