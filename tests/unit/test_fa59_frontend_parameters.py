"""Implements: FA59 (Szenario-Parameter, Web-Frontend-Teil).

Quelltextverträge (kein Frontend-Testwerkzeug im Repo, siehe
test_fa54_frontend_lost_run.py): Das Parameterformular entsteht aus den
deklarierten ``ParameterSpec``-Feldern (type, items, default, description,
choices), die Werte gehen in ``validate`` und ``run``. Die Gruppierung
erzeugter Knoten im Graphen (FA75) prüft test_fa75_web_graph_contract.py.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TYPES = FRONTEND / "lib" / "types.ts"
API = FRONTEND / "lib" / "api.ts"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
RUN_GRAPH = FRONTEND / "components" / "run-graph.tsx"
PANEL = FRONTEND / "components" / "parameter-panel.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block(source: str, start_marker: str) -> str:
    begin = source.index(start_marker)
    return source[begin : source.index("\n}\n", begin)]


def test_fa59_frontend_types_mirror_parameter_spec_and_expansion():
    types = _read(TYPES)
    info = _block(types, "export interface ParameterInfo")
    for field in ("name", "type", "items", "default", "description", "choices"):
        assert re.search(rf"\b{field}\??:", info), f"ParameterInfo ohne Feld {field}"
    validate = _block(types, "export interface ValidateResponse")
    assert re.search(r"parameters\?: ParameterInfo\[\]", validate)
    assert re.search(r"expansion\?: ExpansionInfo \| null", validate)
    status = _block(types, "export interface RunStatus")
    assert re.search(r"origins\?: Record<string, NodeOriginOut>", status)


def test_fa59_frontend_api_sends_parameters_to_validate_and_run():
    api = _read(API)
    validate = api[api.index("validate: (") :]
    validate = validate[: validate.index("),\n")]
    assert "parameters" in validate
    create = api[api.index("createRun: (") :]
    create = create[: create.index("),\n")]
    assert "parameters" in create


def test_fa59_frontend_parameter_form_covers_every_parameter_type():
    panel = _read(PANEL)
    assert panel.startswith('"use client"')
    # string -> Textfeld, integer/float -> Zahlenfeld, boolean -> Schalter,
    # choices -> Auswahl, list/mapping -> YAML-Textfeld
    assert 'type="number"' in panel
    assert 'type="checkbox"' in panel
    assert "choices" in panel and "<Select" in panel
    assert "<textarea" in panel or "<Textarea" in panel
    assert "loadYaml" in panel, "Listen/Mappings werden nicht als YAML gelesen"
    for kind in ('"integer"', '"float"', '"boolean"', '"list"', '"mapping"'):
        assert kind in panel, f"Parametertyp {kind} wird im Formular nicht behandelt"
    # Fehler bei ungültiger Eingabe sichtbar, nicht still verworfen (Regel 7)
    assert "kein gültiges YAML" in panel or "keine gültige Zahl" in panel


def test_fa59_frontend_parameter_values_flow_into_validate_and_run():
    shell = _read(APP_SHELL)
    assert "ParameterPanel" in shell
    assert re.search(r"api\.validate\(configYaml, \w+\)", shell), (
        "validate bekommt die Werte nicht"
    )
    assert re.search(r"api\.createRun\(configYaml, false, \w+(, \w+)?\)", shell), (
        "run bekommt die Werte nicht"
    )


def test_fa59_frontend_parameter_form_falls_back_to_the_yaml_declaration():
    # Solange das Backend (noch) keine ParameterInfo liefert, entsteht das
    # Formular aus dem `parameters:`-Block der YAML - Defaults laufen ohnehin.
    shell = _read(APP_SHELL)
    assert "declaredParameters" in shell


def test_fa59_parameter_form_is_reachable_while_the_config_is_invalid():
    """Nachtreview 02.10. (Runde 2): ein Parameter ohne Default macht die
    Konfiguration ungültig; das Formular stand nur in der Ausführung, die
    erst bei gültiger Konfiguration erreichbar ist - der Lauf war blockiert.
    Jetzt steht es auch im Konfigurationsschritt (PromptStep)."""
    shell = _read(APP_SHELL)
    prompt_step = shell[
        shell.index("function PromptStep(") : shell.index("function ExecutionStep(")
    ]
    assert "<ParameterPanel" in prompt_step
    assert "onChange={onParametersChange}" in prompt_step
    usage = shell[shell.index("<PromptStep") : shell.index('{step === "execution"')]
    assert "onParametersChange={setParamOverrides}" in usage
    assert "parameterValues={effectiveParams}" in usage
