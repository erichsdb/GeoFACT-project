"""Implements: FA91 (Blanko-Vorlage im Einstieg "Szenario selbst schreiben") - Frontend.

Die Vorlage ist eine Konstante in `frontend/lib/blankTemplate.ts`; sie wird hier
unter node geladen (wie in test_fa80_frontend_entry_modes.py) und gegen das
Schema des Kerns geprüft: jeder Schlüssel, den sie nennt, ist ein Feld, das es
gibt. Wo der Knopf steht, prüft der Quelltext.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from geofact import api
from geofact.core.contracts import License
from geofact.core.scenario import OutputSpec, Scenario, ScenarioMeta

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
TEMPLATE = FRONTEND / "lib" / "blankTemplate.ts"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
DRIVER = Path(__file__).resolve().parent / "fa91_blank_template_driver.mjs"
GUIDE = REPO / "docs" / "konfiguration_schreiben.md"


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(DRIVER), str(TEMPLATE)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if proc.returncode != 0 and "ERR_UNKNOWN_FILE_EXTENSION" in proc.stderr:
        pytest.skip("node without TypeScript type stripping")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def template(js) -> dict:
    return yaml.safe_load(js["template"])


def _uncommented(text: str) -> dict:
    """The template with its commented-out alternatives switched on."""
    return yaml.safe_load(re.sub(r"(?m)^(\s+)# ", r"\1", text))


def test_fa91_template_is_yaml_with_the_four_sections(template):
    assert list(template) == ["scenario", "layers", "steps", "output"]
    assert set(template) <= set(Scenario.model_fields)
    assert template["layers"] and template["steps"] and template["output"]


def test_fa91_template_names_only_fields_of_the_schema(js, template):
    assert set(template["scenario"]) <= set(ScenarioMeta.model_fields)
    for spec in template["output"]:
        assert set(spec) <= set(OutputSpec.model_fields)
    for step in template["steps"]:
        assert set(step) == {"id", "op", "inputs", "params"}
    # The commented-out alternatives count too: the working CRS and a file layer.
    full = _uncommented(js["template"])
    assert "crs" in full["scenario"]
    assert set(full["scenario"]) <= set(ScenarioMeta.model_fields)
    file_layer = next(layer for layer in full["layers"] if layer["source"] == "file")
    assert {"id", "source", "path", "data_type", "license"} == set(file_layer)
    assert set(file_layer["license"]) <= set(License.model_fields)


def test_fa91_template_contains_the_output_license(template):
    license_block = template["scenario"]["output_license"]
    assert set(license_block) == {"name", "attribution", "url"}
    assert set(license_block) <= set(License.model_fields)


def test_fa91_template_uses_registered_source_kinds_and_output_formats(js, template):
    catalog = api.extension_catalog()
    sources = {entry["name"] for entry in catalog["sources"]}
    outputs = {entry["name"] for entry in catalog["outputs"]}
    full = _uncommented(js["template"])
    assert {layer["source"] for layer in full["layers"]} <= sources
    assert {spec["type"] for spec in template["output"]} <= outputs


def test_fa91_template_is_a_blank_and_not_a_valid_scenario(js):
    # Placeholders stay visible; the validator names what is missing instead of
    # running a made-up analysis.
    assert js["template"].count("<") >= 10
    with pytest.raises(api.ConfigError):
        api.load_scenario(yaml.safe_load(js["template"]))


def test_fa91_filled_template_is_a_valid_scenario(template):
    filled = {
        "scenario": {
            "name": "Parks in Chemnitz",
            "description": "Einzugsbereich der Parks",
            "region": "12.8,50.75,13.0,50.9",
            "output_license": {
                "name": "CC-BY-4.0",
                "attribution": "Eigene Auswertung",
                "url": "https://creativecommons.org/licenses/by/4.0/",
            },
        },
        "layers": [{"id": "parks", "source": "osm", "tags": {"leisure": "park"}}],
        "steps": [
            {
                "id": "umfeld",
                "op": "buffer",
                "inputs": {"geometry": "parks"},
                "params": {"radius_km": 0.3},
            }
        ],
        "output": [
            {"type": "map", "source": "umfeld"},
            {"type": "geojson", "source": "umfeld", "path": "umfeld.geojson"},
        ],
    }
    # Same keys as the template, section by section.
    assert set(filled["scenario"]) == set(template["scenario"])
    assert set(filled["layers"][0]) == set(template["layers"][0])
    assert set(filled["steps"][0]) == set(template["steps"][0])
    assert [set(spec) for spec in filled["output"]] == [
        set(spec) for spec in template["output"]
    ]
    document = api.load_scenario(filled)
    assert document.scenario.scenario.output_license.name == "CC-BY-4.0"


def test_fa91_template_never_replaces_existing_text(js):
    assert js["insert_into_empty"] is True
    assert js["insert_into_blank_lines"] is True
    assert js["insert_into_text"] is False
    assert js["insert_into_template"] is False


def test_fa91_button_is_offered_only_in_the_write_entry():
    source = APP_SHELL.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert source.count('data-action="insert-template"') == 1
    begin = source.index('{entryMode === "write" && (\n                    <Button')
    block = source[begin : source.index("</Button>", begin)]
    assert 'data-action="insert-template"' in block
    assert "setConfigYaml(BLANK_TEMPLATE)" in block
    assert "disabled={!canInsertTemplate(configYaml)}" in block


def test_fa91_guide_explains_every_section_of_the_template(template):
    text = GUIDE.read_text(encoding="utf-8")
    for section in template:
        assert f"`{section}`" in text, section
    assert "output_license" in text
    assert "Vorlage" in text
