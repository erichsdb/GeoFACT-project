"""Implements: FA97 (Hilfe zur YAML-Konfiguration am Editor) - Frontend.

Der Inhalt der Hilfe liegt als Daten in `frontend/lib/yamlHelp.ts`; er wird hier
unter node geladen (wie in test_fa91_blank_template.py) und gegen den Kern
geprüft: jeder Ausschnitt ist gültiges YAML, jeder genannte Schlüssel, jede
Quellart, Operation und jedes Ausgabeformat gibt es, und die Teilabschnitte
ergeben zusammen ein Szenario, das `validate` annimmt. Wo der Knopf steht und wie
die Seitenleiste aufgebaut ist, prüft der Quelltext.
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
from geofact.core.expansion import ROOT_EXPANSION_KEYS, ParameterSpec
from geofact.core.scenario import OutputSpec, Scenario, ScenarioMeta

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
HELP_DATA = FRONTEND / "lib" / "yamlHelp.ts"
HELP_COMPONENT = FRONTEND / "components" / "yaml-help.tsx"
CONFIG_EDITOR = FRONTEND / "components" / "config-editor.tsx"
DRIVER = Path(__file__).resolve().parent / "fa97_yaml_help_driver.mjs"
GUIDE = REPO / "docs" / "konfiguration_schreiben.md"

EXPECTED_IDS = [
    "grundgeruest",
    "region",
    "layers",
    "steps",
    "output",
    "parameters",
    "fehler",
    "beispiel",
]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(DRIVER), str(HELP_DATA)],
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
def sections(js) -> dict[str, dict]:
    return {s["id"]: s for s in js["sections"]}


@pytest.fixture(scope="module")
def catalog() -> dict:
    return api.extension_catalog()


def _snippets(js) -> list[tuple[str, dict]]:
    return [(s["id"], yaml.safe_load(s["yaml"])) for s in js["sections"] if s["yaml"]]


# --- Inhalt ---


def test_fa97_help_has_the_seven_topics_and_the_full_example(js, sections):
    assert [s["id"] for s in js["sections"]] == EXPECTED_IDS
    for section in js["sections"]:
        assert section["title"].strip() and section["text"].strip(), section["id"]
    assert sections["beispiel"]["yaml"] == js["example"]


def test_fa97_every_snippet_is_valid_yaml(js):
    snippets = _snippets(js)
    assert [i for i, _ in snippets] == [i for i in EXPECTED_IDS if i != "fehler"]
    for section_id, doc in snippets:
        assert isinstance(doc, dict) and doc, section_id


def test_fa97_snippets_name_only_fields_of_the_schema(js):
    for section_id, doc in _snippets(js):
        assert set(doc) <= set(Scenario.model_fields) | {"parameters"}, section_id
        assert "parameters" in ROOT_EXPANSION_KEYS
        if "scenario" in doc:
            meta = doc["scenario"]
            assert set(meta) <= set(ScenarioMeta.model_fields), section_id
            ScenarioMeta.model_validate(meta)
            if "output_license" in meta:
                assert set(meta["output_license"]) <= set(License.model_fields)
        for spec in doc.get("output", []):
            assert set(spec) <= set(OutputSpec.model_fields) | {"color_field"}, (
                section_id
            )
        for step in doc.get("steps", []):
            assert set(step) == {"id", "op", "inputs", "params"}, section_id
        for name, spec in doc.get("parameters", {}).items():
            assert set(spec) <= set(ParameterSpec.model_fields), (section_id, name)
            ParameterSpec.model_validate(spec)
        for layer in doc.get("layers", []):
            assert {"id", "source"} <= set(layer), section_id


def test_fa97_every_operation_source_and_output_mentioned_exists_in_the_registry(
    js, catalog
):
    operations = {entry["op"] for entry in api.operation_catalog()}
    sources = {entry["name"] for entry in catalog["sources"]}
    outputs = {entry["name"] for entry in catalog["outputs"]}
    assert operations and sources and outputs

    used_ops, used_sources, used_outputs = set(), set(), set()
    declared_params: set[str] = set()
    used_params: set[str] = set()
    for section in js["sections"]:
        mentions = section["mentions"] or {}
        used_ops |= set(mentions.get("operations", []))
        used_sources |= set(mentions.get("sources", []))
        used_outputs |= set(mentions.get("outputs", []))
    for section_id, doc in _snippets(js):
        used_ops |= {step["op"] for step in doc.get("steps", [])}
        used_sources |= {layer["source"] for layer in doc.get("layers", [])}
        used_outputs |= {spec["type"] for spec in doc.get("output", [])}
        declared_params |= set(doc.get("parameters", {}))
    for section in js["sections"]:
        if section["yaml"]:
            used_params |= set(re.findall(r"\$\{(\w+)\}", section["yaml"]))
    assert used_ops <= operations, used_ops - operations
    assert used_sources <= sources, used_sources - sources
    assert used_outputs <= outputs, used_outputs - outputs
    assert used_params <= declared_params, used_params - declared_params


def test_fa97_every_backticked_name_in_the_text_exists(js, catalog):
    """No stale claims: each `name` in a text or bullet is a section, field,
    registered building block, parameter of an operation or a named literal."""
    known: set[str] = (
        set(Scenario.model_fields) | set(ScenarioMeta.model_fields) | {"parameters"}
    )
    known |= set(OutputSpec.model_fields) | {"color_field"}
    known |= set(ParameterSpec.model_fields)
    known |= {"op", "inputs", "params", "id", "source", "tags", "path", "data_type"}
    for kind in ("sources", "outputs"):
        known |= {entry["name"] for entry in catalog[kind]}
    for op in api.operation_catalog():
        known.add(op["op"])
        known |= set(op["input_ports"]) | set(op["params"])
    known |= set(ParameterSpec.model_fields["type"].annotation.__args__)
    literals = {
        "auto",
        "vector",
        "raster",
        "- ",
        "==",
        "!=",
        "and",
        "or",
        "<…>",
        "${name}",
    }

    unknown = []
    for section in js["sections"]:
        for text in [section["text"], *(section["bullets"] or [])]:
            for token in re.findall(r"`([^`]+)`", text):
                head = token.split(":")[0]
                if token in known or token in literals or head in known:
                    continue
                unknown.append((section["id"], token))
    assert not unknown, unknown


# --- Das Beispiel ---


def test_fa97_the_parts_assemble_into_a_scenario_that_validates(js, sections):
    assert js["parts"] == ["region", "parameters", "layers", "steps", "output"]
    assert js["example"] == "\n".join(sections[part]["yaml"] for part in js["parts"])
    report = api.validate(js["example"])
    assert report.valid, report.issues
    assert tuple(report.plan.order) == ("raster", "parks_je_zelle", "rangliste")
    assert api.validate(js["example"], parameters={"top": 3}).valid


def test_fa97_the_skeleton_alone_validates(sections):
    report = api.validate(sections["grundgeruest"]["yaml"])
    assert report.valid, report.issues
    assert tuple(report.plan.order) == ("umfeld",)


def test_fa97_example_needs_no_local_files(js):
    doc = yaml.safe_load(js["example"])
    assert {layer["source"] for layer in doc["layers"]} <= {"osm", "region"}
    assert all("path" not in layer for layer in doc["layers"])
    assert set(doc) == {"scenario", "parameters", "layers", "steps", "output"}


def test_fa97_a_parameter_without_default_is_invalid_as_the_help_says(js):
    doc = yaml.safe_load(js["example"])
    del doc["parameters"]["top"]["default"]
    assert not api.validate(doc).valid
    assert api.validate(doc, parameters={"top": 3}).valid


def test_fa97_step_order_in_the_file_does_not_matter_as_the_help_says(js):
    doc = yaml.safe_load(js["example"])
    doc["steps"].reverse()
    report = api.validate(doc)
    assert report.valid, report.issues
    assert tuple(report.plan.order) == ("raster", "parks_je_zelle", "rangliste")


def test_fa97_a_cycle_is_rejected_as_the_help_says(js):
    doc = yaml.safe_load(js["example"])
    doc["steps"][0]["inputs"] = {"region": "rangliste"}
    report = api.validate(doc)
    assert not report.valid
    assert any("Zyklus" in message for _, message in report.issues)


def test_fa97_common_errors_are_a_list_without_a_snippet(sections):
    errors = sections["fehler"]
    assert len(errors["bullets"]) >= 5
    assert errors["yaml"] is None


def test_fa97_a_raster_in_a_vector_input_is_rejected_as_the_help_says():
    doc = {
        "scenario": {"name": "t", "region": "12.8,50.75,13.0,50.9"},
        "layers": [
            {"id": "dgm", "source": "file", "path": "dgm.tif", "data_type": "raster"}
        ],
        "steps": [
            {
                "id": "f",
                "op": "filter",
                "inputs": {"features": "dgm"},
                "params": {"condition": "x > 1"},
            }
        ],
        "output": [{"type": "geojson", "source": "f"}],
    }
    report = api.validate(doc)
    assert not report.valid
    assert any(
        "erwartet vector" in message and "raster" in message
        for _, message in report.issues
    )


# --- Verweise ---


def test_fa97_pointers_name_catalog_template_guide_and_api_docs(js):
    pointers = js["pointers"]
    assert "Quellen" in pointers["catalogs"] and "Operationen" in pointers["catalogs"]
    assert "Vorlage" in pointers["template"]
    assert "konfiguration_schreiben.md" in pointers["guide"]
    assert (REPO / "docs" / "konfiguration_schreiben.md").exists()
    assert pointers["apiDocs"].strip()
    assert js["api_docs_url"] == "http://127.0.0.1:8000/api/docs"
    assert js["api_docs_url_bare"] == "https://host/api/docs"


def test_fa97_texts_say_where_the_catalog_tabs_and_the_run_button_are(js):
    """Die Hilfe öffnet in allen drei Einstiegen, die Reiter gibt es nur beim Selbstschreiben."""
    texts = [s["text"] for s in js["sections"]]
    texts += [b for s in js["sections"] for b in (s["bullets"] or [])]
    texts += list(js["pointers"].values())
    with_tabs = [t for t in texts if "Reiter" in t]
    assert len(with_tabs) >= 4
    for text in with_tabs:
        assert "„Szenario selbst schreiben“" in text, text
    # „Weiter zur Ausführung“ wechselt nur den Schritt; den Lauf startet „Ausführen“
    example = next(s for s in js["sections"] if s["id"] == "beispiel")["text"]
    assert "„Weiter zur Ausführung“" in example and "„Ausführen“" in example
    assert "„Weiter zur Ausführung“ starten" not in example


def test_fa97_help_mirrors_the_sections_of_the_guide(js):
    guide = GUIDE.read_text(encoding="utf-8")
    for keyword in (
        "scenario",
        "layers",
        "steps",
        "output",
        "parameters",
        "region",
        "crs",
    ):
        assert f"`{keyword}`" in guide, keyword
    titles = " ".join(s["title"] for s in js["sections"])
    for word in ("Region", "Layer", "Schritte", "Ausgaben", "Parameter"):
        assert word in titles, word


# --- Quelltext ---


def test_fa97_help_button_sits_in_the_editor_header():
    source = _text(CONFIG_EDITOR)
    assert 'import YamlHelpButton from "@/components/yaml-help";' in source
    assert source.count("<YamlHelpButton />") == 1
    heading = source.index("<h3")
    button = source.index("<YamlHelpButton />")
    toolbar = source.index("{toolbar &&")
    assert heading < button < toolbar
    # The editor is the only one in the app, so the (?) is there in all three entries.
    shell = _text(FRONTEND / "components" / "app-shell.tsx")
    assert shell.count("<ConfigEditor") == 1


def test_fa97_help_opens_a_sheet_with_copyable_snippets():
    source = _text(HELP_COMPONENT)
    assert source.startswith('"use client";')
    assert "Implements: FA97" in source
    assert 'from "@/components/ui/sheet"' in source
    assert 'data-action="yaml-help"' in source
    assert 'aria-label="Hilfe zur YAML-Konfiguration"' in source
    assert 'data-action="copy-snippet"' in source
    assert "navigator.clipboard.writeText" in source
    assert "notify.error" in source  # a failed copy is reported, never swallowed
    assert "YAML_HELP_SECTIONS" in source
    assert "apiDocsUrl(API_BASE)" in source
    assert 'target="_blank"' in source and 'rel="noreferrer"' in source


def test_fa97_help_content_lives_in_the_lib_module_not_in_the_component():
    component = _text(HELP_COMPONENT)
    data = _text(HELP_DATA)
    assert "scenario:" not in component
    assert "Implements: FA97" in data
    assert not re.search(r"from\s+[\"']react", data)
    assert "clipboard" not in data
