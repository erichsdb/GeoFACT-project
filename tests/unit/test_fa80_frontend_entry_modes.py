"""Implements: FA80 (Frage nach der Anmeldung, drei geführte Einstiege) - Frontend.

Es gibt kein Frontend-Testwerkzeug im Repo (siehe test_fa54_frontend_lost_run.py).
Die Logik der Einstiege liegt deshalb in reinen Helfern (`frontend/lib/entryMode.ts`),
die hier unter node ausgeführt werden; die Nachbedingungen an die Ansicht
(die Frage steht allein, nie Prompt und Editor zugleich, Auswahl statt Dialog,
Katalog zum Einfügen nur beim Schreiben) prüft der Quelltext. Die Schritte je
Einstieg (Stepper) gehören seit FA96 zu `frontend/lib/wizardSteps.ts`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
ENTRY = FRONTEND / "lib" / "entryMode.ts"
DRIVER = Path(__file__).resolve().parent / "fa80_entry_mode_driver.mjs"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
COMPONENTS = FRONTEND / "components"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _between(source: str, begin: str, end: str) -> str:
    start = source.index(begin)
    return source[start : source.index(end, start)]


def _prompt_step() -> str:
    return _between(_text(APP_SHELL), "function PromptStep(", "function ExecutionStep(")


def _block(body: str, marker: str) -> str:
    """Der JSX-Block ab `marker` bis zum nächsten Einstiegs-Block."""
    begin = body.index(marker)
    following = [
        body.find(m, begin + len(marker))
        for m in ('data-entry="', "{pickerVisible && (")
    ]
    ends = [i for i in following if i != -1]
    return body[begin : min(ends) if ends else len(body)]


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(DRIVER), str(ENTRY)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    # Nur ein node ohne Typ-Stripping ist ein Grund zum Überspringen; jeder andere
    # Fehler (Syntax, fehlender Export, Laufzeitfehler im Treiber) lässt die Tests scheitern.
    if proc.returncode != 0 and "ERR_UNKNOWN_FILE_EXTENSION" in proc.stderr:
        pytest.skip("node without TypeScript type stripping")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --- Logik der Einstiege (unter node ausgeführt) ----------------------------


def test_fa80_there_are_exactly_three_entry_modes_in_order(js: dict) -> None:
    assert js["modes"] == [
        {"id": "generate", "label": "Szenario generieren"},
        {"id": "load", "label": "Szenario laden"},
        {"id": "write", "label": "Szenario selbst schreiben"},
    ]
    assert js["mode_texts_complete"] is True


def test_fa80_a_new_session_has_no_mode_so_the_question_is_asked(js: dict) -> None:
    assert js["initial_mode"]["new_session"] is None


def test_fa80_the_choice_of_the_session_is_restored(js: dict) -> None:
    assert js["initial_mode"]["session_choice"] == "generate"


def test_fa80_unreadable_session_value_is_no_choice(js: dict) -> None:
    assert js["parse"] == {
        "generate": "generate",
        "load": "load",
        "write": "write",
        "": None,
        "prompt": None,
        "Generate": None,
    }
    assert js["parse_null"] is None
    assert js["initial_mode"]["session_unreadable"] is None


def test_fa80_an_opened_link_is_a_loaded_scenario(js: dict) -> None:
    assert js["initial_mode"]["link_new_session"] == "load"
    assert js["initial_mode"]["link_beats_session"] == "load"


def test_fa80_every_scenario_step_has_a_title_and_an_instruction(js: dict) -> None:
    """FA96: die Zählung der Schritte trägt der Stepper, der Hinweis nennt sie nicht mehr."""
    guide = js["guide"]
    assert set(guide) == {"generate", "load", "write"}
    for mode in ("generate", "load"):
        assert set(guide[mode]) == {"input", "config"}, mode
    assert set(guide["write"]) == {"config"}
    for mode, steps in guide.items():
        for step, entry in steps.items():
            assert entry["title"], (mode, step)
            # Die Konfiguration hat beim Generieren und Laden keinen festen Text: er
            # folgt der Herkunft des Editortextes (configGuideText, FA86).
            if step == "config" and mode != "write":
                assert set(entry) == {"title"}, (mode, step)
            else:
                assert entry["text"], (mode, step)
                assert set(entry) == {"title", "text"}, (mode, step)  # kein step/of


def test_fa80_only_a_non_blank_editor_text_can_be_resumed(js: dict) -> None:
    """Randfall: leerer oder nur aus Leerraum bestehender Text ist nichts zum Fortsetzen."""
    assert js["resume"] == {"empty": False, "blank": False, "text": True}


# --- Die Frage nach der Anmeldung (Quelltextverträge) -----------------------


def test_fa80_the_choice_lives_in_the_tab_session_not_across_sessions() -> None:
    shell = _text(APP_SHELL)
    assert "window.sessionStorage.getItem(ENTRY_MODE_STORAGE_KEY)" in shell
    assert "window.sessionStorage.setItem(ENTRY_MODE_STORAGE_KEY, entryMode)" in shell
    assert "localStorage.setItem(ENTRY_MODE_STORAGE_KEY" not in shell
    assert 'initialEntryMode(readSessionEntryMode(), linkMode === "open")' in shell
    assert 'ENTRY_MODE_STORAGE_KEY = "geofact:entryMode"' in _text(ENTRY)


def test_fa80_a_login_and_a_logout_forget_the_choice() -> None:
    gate = _text(COMPONENTS / "auth-gate.tsx")
    success = _between(gate, "onSuccess={() => {", "}}")
    assert "forgetEntryMode();" in success and 'setState("authenticated");' in success
    shell = _text(APP_SHELL)
    logout = _between(shell, "clearAuth();", "window.location.reload();")
    assert "forgetEntryMode();" in logout
    assert "sessionStorage.removeItem(ENTRY_MODE_STORAGE_KEY)" in _text(ENTRY)


def test_fa80_without_a_choice_the_question_comes_before_any_remembered_step() -> None:
    shell = _text(APP_SHELL)
    init = _between(shell, "const [step, setStep] = useState<WizardStep>(() =>", ");")
    # FA96: initialStep fragt zuerst nach dem Einstieg (ohne ihn: "input"), dann nach dem gemerkten Schritt
    assert (
        'initialStep(entryMode, readLocalStorage(CONFIG_YAML_STORAGE_KEY) ?? "", '
        'readLocalStorage(STEP_STORAGE_KEY), linkMode === "open")' in init
    )


def test_fa80_the_open_question_stands_alone() -> None:
    shell = _text(APP_SHELL)
    assert "const entryQuestionOpen = entryMode === null;" in shell
    assert "{!entryQuestionOpen && (\n        <Stepper" in shell
    body = _prompt_step()
    assert "{entryMode === null ? (" in body and "<EntryChooser" in body
    # Prompt, Auswahl und Editor hängen alle an einem gewählten Einstieg
    assert "Was möchten Sie tun?" in _between(
        shell, "function EntryChooser(", "function EntryGuide("
    )


def test_fa80_question_offers_the_three_ways_and_resuming_earlier_work() -> None:
    shell = _text(APP_SHELL)
    chooser = _between(shell, "function EntryChooser(", "function EntryGuide(")
    assert "ENTRY_MODES.map" in chooser and "onClick={() => onChoose(m.id)}" in chooser
    assert "{resumable && (" in chooser and "onClick={onResume}" in chooser
    # ohne Sprachmodell ist "generieren" gesperrt und nennt den Grund
    assert (
        'const unavailable = m.id === "generate" && llmConfigured === false;' in chooser
    )
    assert (
        "disabled={unavailable}" in chooser
        and "kein Sprachmodell konfiguriert" in chooser
    )
    body = _prompt_step()
    assert "resumable={canResume(configYaml)}" in body
    assert 'onResume={() => onChooseEntryMode("write")}' in body


# --- Der geführte Weg (Quelltextverträge) ----------------------------------


def test_fa80_guide_names_way_step_and_next_action() -> None:
    shell = _text(APP_SHELL)
    guide = _between(shell, "function EntryGuide(", "function PromptStep(")
    assert "const guide = ENTRY_GUIDE[mode][step]" in guide
    assert "{guide.title}" in guide and "{text ?? guide.text}" in guide
    # FA96: gezählt wird im Stepper
    assert "Schritt ${" not in guide
    # der Wechsel des Einstiegs steht in der Kopfzeile, nicht mehr hier
    assert "onReset" not in guide and "Anderen Weg wählen" not in guide
    assert "<EntryGuide" in _prompt_step()


def test_fa80_the_way_can_be_switched_from_the_header_in_every_step() -> None:
    shell = _text(APP_SHELL)
    header = shell[shell.index("<header") : shell.index("</header>")]
    # nicht an einen Schritt gebunden; nur solange die Frage offen ist, gibt es nichts zu wechseln
    assert "{entryMode !== null && (\n          <EntrySwitch" in header
    assert "onSwitch={switchEntryMode}" in header
    # alle drei Wege stehen sichtbar nebeneinander, kein Aufklappmenü
    assert "Dropdown" not in shell
    switch = _between(shell, "const switchEntryMode = ", "};")
    # FA86/FA96: ein anderer Einstieg zeigt seinen zuletzt gezeigten Schritt (stepForMode),
    # der gewählte führt zu seinem ersten Schritt zurück
    assert "if (mode !== entryMode) changeEntryMode(mode);" in switch
    assert "else setStep(firstStep(mode));" in switch
    # der Wechsel fasst den Editortext nicht an
    assert "setConfigYaml" not in switch
    menu = _between(shell, "function EntrySwitch(", "function EntryGuide(")
    assert "ENTRY_MODES.map" in menu and "onClick={() => onSwitch(m.id)}" in menu
    assert "aria-pressed={active}" in menu and "{m.short}" in menu
    assert 'const unavailable = m.id === "generate" && llmConfigured === false;' in menu
    assert "disabled={unavailable}" in menu


def test_fa80_prompt_picker_and_editor_have_disjoint_conditions() -> None:
    body = _prompt_step()
    assert 'const promptVisible = entryMode === "generate" && step === "input";' in body
    assert 'const pickerVisible = entryMode === "load" && step === "input";' in body
    assert 'const editorVisible = step === "config";' in body
    assert body.count("<PromptPanel") == 1
    assert body.count("<ScenarioPicker") == 1
    assert body.count("<ConfigEditor") == 1
    # der Prompt-Block ist ausgeblendet, sobald der Prompt nicht dran ist
    assert '!promptVisible && "hidden"' in _block(body, 'data-entry="generate"')
    assert "{pickerVisible && (" in body and "{editorVisible && (" in body
    assert "<ConfigEditor" in _block(body, 'data-entry="config"')
    assert "<ConfigEditor" not in _block(body, 'data-entry="generate"')
    assert "<PromptPanel" not in _block(body, 'data-entry="config"')


def test_fa80_choosing_a_mode_shows_its_input_first() -> None:
    shell = _text(APP_SHELL)
    handler = _between(shell, "const chooseEntryMode", "}, []);")
    assert "setEntryMode(mode);" in handler
    assert "setStep(firstStep(mode));" in handler


def test_fa80_generating_and_loading_lead_to_the_config() -> None:
    shell = _text(APP_SHELL)
    # FA86/FA96: ein fertiges Ergebnis öffnet die Konfiguration, wenn der Nutzer in der Frage
    # des Generierens steht; sonst wird nichts umgeschaltet, sondern gemeldet.
    generated = _between(
        shell, "const handleGenerated = (", "// FA96: Fakten für den Stepper"
    )
    assert (
        'stepRef.current === "input" && entryModeRef.current === "generate"'
        in generated
        and 'setStep("config");' in generated
    )
    assert "notify.success(" in generated
    load = _between(shell, "const loadScenario = async", "const setUrlScenario")
    assert 'setStep("config");' in load
    link = _between(
        shell, "const openLink = async", "useEffect(() => {\n    if (linkMode"
    )
    assert 'setEntryMode("load");' in link and 'setStep("config");' in link


def test_fa80_scenarios_are_chosen_inline_not_in_a_dialog() -> None:
    assert not (COMPONENTS / "scenario-picker-dialog.tsx").exists()
    picker = _text(COMPONENTS / "scenario-picker.tsx")
    assert "<Dialog" not in picker and "ui/dialog" not in picker
    shell = _text(APP_SHELL)
    assert "loadModalOpen" not in shell and "Laden…" not in shell


def test_fa80_insert_catalog_only_while_writing() -> None:
    body = _prompt_step()
    config = _block(body, 'data-entry="config"')
    assert '{entryMode === "write" && (' in config
    assert "onInsertLayer={onInsertLayer}" in config and "<OperationsPanel" in config
    # beim Schreiben gibt es nichts vorzumerken: keine Checkboxen
    assert "onToggle" not in config
    # beim Generieren dient der Katalog nur der Vorauswahl: kein Einfügen
    generate = _block(body, 'data-entry="generate"')
    assert "<SourcePanel" in generate and "<ApiPanel" in generate
    assert "onInsertLayer" not in generate and "<OperationsPanel" not in generate
    assert generate.count("onToggle={onToggle}") == 2
    assert "onUploaded={onUploadedForPrompt}" in generate
    source_panel = _text(COMPONENTS / "source-panel.tsx")
    assert "{onInsert && (" in source_panel
    assert re.search(r"\{onToggle && \(\s*<Checkbox", source_panel)
    api_panel = _text(COMPONENTS / "api-panel.tsx")
    assert "{entry.layer_template && onInsertLayer && (" in api_panel
    assert re.search(r"\{onToggle && \(\s*<Checkbox", api_panel)


def test_fa80_config_step_leads_back_to_its_input_through_the_guide() -> None:
    """FA96: der Weg zurück läuft über den Stepper und den „Zurück“-Knopf des Hinweises;
    die Herkunftsleiste nennt nur noch die Herkunft. Den Katalog erreicht man über den
    Umschalter (Einstieg "Schreiben")."""
    body = _prompt_step()
    assert (
        'back={\n            step === "config" && entryMode !== "write"' in body
        and 'label: backLabel(entryMode, "config")!, onClick: onBack' in body
    )
    config = _block(body, 'data-entry="config"')
    assert "<OriginBar" in config and "onAdjustPrompt" not in config
    bar = _text(COMPONENTS / "origin-bar.tsx")
    assert "Prompt anpassen" not in bar and "Anderes Szenario wählen" not in bar
    shell = _text(APP_SHELL)
    assert "jumpAdjustPrompt" not in shell and "jumpPickScenario" not in shell
    assert "Mit Katalog weiterbearbeiten" not in shell
