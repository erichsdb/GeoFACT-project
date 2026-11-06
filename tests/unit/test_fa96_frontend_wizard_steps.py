"""Implements: FA96 (Schrittfolge je Einstieg im Stepper) - Frontend.

Es gibt kein Frontend-Testwerkzeug im Repo (siehe test_fa54_frontend_lost_run.py).
Die Regeln der Schritte (welche es gibt, was erreichbar ist, wo der Start liegt)
stehen deshalb in reinen Helfern (`frontend/lib/wizardSteps.ts`), die hier unter node
ausgeführt werden; die Nachbedingungen an die Ansicht (Stepper zeigt die Schritte des
Einstiegs, „Weiter“/„Zurück“ in jedem Schritt, keine Sprungknöpfe mehr) prüft der
Quelltext.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
WIZARD = FRONTEND / "lib" / "wizardSteps.ts"
ENTRY = FRONTEND / "lib" / "entryMode.ts"
DRIVER = Path(__file__).resolve().parent / "fa96_wizard_steps_driver.mjs"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
STEPPER = FRONTEND / "components" / "stepper.tsx"
ORIGIN_BAR = FRONTEND / "components" / "origin-bar.tsx"
RUN_OUTPUTS = FRONTEND / "components" / "run-outputs.tsx"
PROMPT_PANEL = FRONTEND / "components" / "prompt-panel.tsx"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _between(source: str, begin: str, end: str) -> str:
    start = source.index(begin)
    return source[start : source.index(end, start)]


def _prompt_step() -> str:
    return _between(_text(APP_SHELL), "function PromptStep(", "function ExecutionStep(")


def _execution_step() -> str:
    return _between(_text(APP_SHELL), "function ExecutionStep(", "function ResultStep(")


def _result_step() -> str:
    shell = _text(APP_SHELL)
    return shell[shell.index("function ResultStep(") :]


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(DRIVER), str(WIZARD)],
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


# --- Die Schritte je Einstieg (unter node ausgeführt) ---------------------------


def test_fa96_generate_and_load_have_four_steps_write_has_three(js: dict) -> None:
    steps = js["steps"]

    def shape(mode: str) -> list[tuple[str, str]]:
        return [(s["id"], s["label"]) for s in steps[mode]]

    assert shape("generate") == [
        ("input", "Frage stellen"),
        ("config", "Konfiguration"),
        ("execution", "Ausführung"),
        ("result", "Ergebnis"),
    ]
    assert shape("load") == [
        ("input", "Szenario wählen"),
        ("config", "Konfiguration"),
        ("execution", "Ausführung"),
        ("result", "Ergebnis"),
    ]
    assert shape("write") == [
        ("config", "Konfiguration"),
        ("execution", "Ausführung"),
        ("result", "Ergebnis"),
    ]
    assert steps["generate"][0]["hint"] == "Was möchten Sie wissen?"
    assert steps["load"][0]["hint"] == "Beispiel oder gespeichertes Szenario"
    assert steps["write"][0]["hint"] == "YAML schreiben, mit Vorlage und Katalog"
    for mode in ("generate", "load"):
        assert steps[mode][1]["hint"] == "YAML ansehen, bei Bedarf bearbeiten"
        assert steps[mode][2]["hint"] == "Ausführungsgraph, Lauf starten"
        assert steps[mode][3]["hint"] == "Karte, Tabelle, Statistik"
    assert all(s["hint"] for mode in steps.values() for s in mode)


def test_fa96_first_step_is_input_for_generate_and_load_and_config_for_write(
    js: dict,
) -> None:
    assert js["first"] == {"generate": "input", "load": "input", "write": "config"}


def test_fa96_the_old_step_name_prompt_is_not_a_step(js: dict) -> None:
    parse = js["parse"]
    assert parse["prompt"] is None and parse[""] is None and js["parse_null"] is None
    assert [parse[s] for s in ("input", "config", "execution", "result")] == [
        "input",
        "config",
        "execution",
        "result",
    ]


def test_fa96_initial_step_without_config_is_the_first_step_of_the_mode(
    js: dict,
) -> None:
    """FA86 (12): ohne Konfiguration gibt es keinen späteren Schritt - auch nicht aus dem Gedächtnis."""
    i = js["initial"]
    assert i["generate_no_config"] == "input" and i["load_no_config"] == "input"
    assert i["write_no_config"] == "config"


def test_fa96_initial_step_restores_a_stored_step_that_belongs_to_the_mode(
    js: dict,
) -> None:
    i = js["initial"]
    assert i["generate_result"] == "result" and i["load_execution"] == "execution"
    assert i["generate_config"] == "config" and i["generate_input"] == "input"
    # "write" kennt keinen Schritt "input": der gemerkte Wert gehört nicht zum Einstieg
    assert i["write_input_stored"] == "config" and i["write_config"] == "config"
    # fremde Werte, auch das frühere "prompt", führen zu Schritt 1
    assert i["generate_old_prompt"] == "input" and i["write_old_prompt"] == "config"
    assert i["generate_null"] == "input" and i["generate_garbage"] == "input"


def test_fa96_an_opened_link_starts_in_the_input_step(js: dict) -> None:
    i = js["initial"]
    assert {i["link_generate"], i["link_load"], i["link_write"], i["link_no_mode"]} == {
        "input"
    }


def test_fa96_an_open_question_starts_in_step_one(js: dict) -> None:
    assert (
        js["initial"]["no_mode"] == "input"
        and js["initial"]["no_mode_empty"] == "input"
    )


def test_fa96_config_step_is_reachable_with_any_text_and_always_when_writing(
    js: dict,
) -> None:
    for name in ("all", "text_only", "valid_no_run", "invalid_with_run", "stale"):
        assert js["reachable"]["generate"][name]["config"] is True, name
        assert js["reachable"]["load"][name]["config"] is True, name
    # Randfall: leerer Editor
    assert js["reachable"]["generate"]["nothing"]["config"] is False
    assert js["reachable"]["load"]["nothing"]["config"] is False
    for name, reachable in js["reachable"]["write"].items():
        assert reachable["config"] is True, name
    # Schritt 1 gibt es nur, wo er existiert - und dort ist er immer erreichbar
    for mode in ("generate", "load"):
        assert all(r["input"] is True for r in js["reachable"][mode].values())
    assert all(r["input"] is False for r in js["reachable"]["write"].values())


def test_fa96_execution_needs_a_valid_config_and_result_needs_a_run(js: dict) -> None:
    for mode in ("generate", "load", "write"):
        r = js["reachable"][mode]
        assert r["all"]["execution"] is True and r["all"]["result"] is True
        assert (
            r["text_only"]["execution"] is False and r["text_only"]["result"] is False
        )
        assert r["valid_no_run"]["execution"] is True
        assert r["valid_no_run"]["result"] is False
        # FA86 (12): ein Lauf bleibt mit ungültiger Konfiguration einsehbar
        assert r["invalid_with_run"]["execution"] is False
        assert r["invalid_with_run"]["result"] is True
        # ohne Konfiguration ist auch ein gemerkter Lauf nicht erreichbar
        assert r["no_config_with_run"]["execution"] is False
        assert r["no_config_with_run"]["result"] is False
        assert r["nothing"]["execution"] is False and r["nothing"]["result"] is False


def test_fa96_completed_marks_follow_config_validity_and_a_fresh_run(js: dict) -> None:
    for mode in ("generate", "load", "write"):
        c = js["completed"][mode]
        assert c["all"]["input"] is True and c["all"]["config"] is True
        assert c["all"]["execution"] is True
        # Text ohne gültige Konfiguration: Schritt 1 abgehakt, die Konfiguration nicht
        assert c["text_only"]["input"] is True and c["text_only"]["config"] is False
        assert c["nothing"]["input"] is False and c["nothing"]["config"] is False
        # ein veralteter oder laufender Lauf ist nicht "ausgeführt"
        assert c["stale"]["execution"] is False
        assert c["running"]["execution"] is False
        assert c["invalid_with_run"]["execution"] is False
        # das Ergebnis ist der letzte Schritt und wird nie abgehakt
        assert not any(v["result"] for v in c.values())


def test_fa96_step_one_is_only_checked_for_a_text_of_this_path(js: dict) -> None:
    """Eine Frage gilt nicht als gestellt, weil ein geladenes Szenario im Editor steht."""
    foreign = js["completed_foreign"]
    for name, marks in foreign.items():
        assert marks["input"] is False, name
        # die übrigen Haken hängen nicht an der Herkunft
        assert marks["config"] is True and marks["execution"] is True, name
    # Gegenprobe: derselbe Stand mit einem Text dieses Wegs ist abgehakt
    assert js["completed"]["generate"]["all"]["input"] is True
    assert js["completed"]["load"]["all"]["input"] is True
    shell = _text(APP_SHELL)
    assert "originKind: originRecord?.origin.kind ?? null," in shell


def test_fa96_run_status_is_fetched_when_execution_or_result_is_entered(
    js: dict,
) -> None:
    """Der Stand des gemerkten Laufs kommt beim Betreten des Schritts, egal wo die Seite lud."""
    need = js["needs_run_status"]
    for step in ("execution", "result"):
        assert need[step]["remembered"] is True
        # Randfälle: kein Lauf, Stand schon da, Lauf wird gerade angelegt
        assert need[step]["no_run"] is False
        assert need[step]["has_status"] is False
        assert need[step]["starting"] is False
    for step in ("input", "config"):
        assert not any(need[step].values()), step
    shell = _text(APP_SHELL)
    effect = _between(shell, "const statusRequestedRef", "}, [step, runId]);")
    assert (
        "if (!needsRunStatus(step, true, status !== null, startingRef.current)) return;"
        in effect
    )
    # je Lauf eine Anfrage; eine Antwort zu einem inzwischen ersetzten Lauf verfällt
    assert "if (statusRequestedRef.current === runId) return;" in effect
    assert effect.count("if (runIdRef.current !== runId) return;") == 2
    assert ".runStatus(runId)" in effect and "handleRunGone(runId)" in effect


def test_fa96_a_finished_run_opens_the_result_only_from_the_execution_step(
    js: dict,
) -> None:
    assert js["run_end_opens_result"] == {
        "input": False,
        "config": False,
        "execution": True,
        "result": False,
    }
    shell = _text(APP_SHELL)
    end = _between(shell, 'es.addEventListener("end"', "es.onerror")
    assert "const opensResult = runEndOpensResult(stepRef.current);" in end
    assert 'setStep("result");' not in end
    assert (
        'if (opensResult) setStep((cur) => (runEndOpensResult(cur) ? "result" : cur));'
        in end
    )
    # in einem anderen Schritt wird das Ende gemeldet (nicht im Ergebnis selbst)
    assert 'if (!opensResult && stepRef.current !== "result") {' in end
    assert "notify.info(" in end and "Schritt „Ergebnis“" in end


def test_fa96_back_labels_name_the_previous_step(js: dict) -> None:
    b = js["back_labels"]
    assert b["generate"]["config"] == "Zurück zur Frage"
    assert b["load"]["config"] == "Zurück zur Auswahl"
    for mode in ("generate", "load", "write"):
        assert b[mode]["execution"] == "Zurück zur Konfiguration"
        assert b[mode]["result"] == "Zurück zur Ausführung"
    # der erste Schritt hat kein „Zurück“
    assert b["generate"]["input"] is None and b["load"]["input"] is None
    assert b["write"]["config"] is None


def test_fa96_locked_steps_name_their_reason(js: dict) -> None:
    r = js["reasons"]
    assert (
        r["generate"]["config"]
        == "Erst eine Frage stellen und eine Konfiguration erzeugen"
    )
    assert r["load"]["config"] == "Erst ein Szenario laden"
    for mode in ("generate", "load", "write"):
        assert r[mode]["execution"] == "Erst eine gültige Konfiguration"
        assert r[mode]["result"] == "Erst einen Lauf starten"
        assert r[mode]["input"] is None
    assert r["write"]["config"] is None


def test_fa96_mode_switch_lands_on_config_only_for_a_text_of_this_path(
    js: dict,
) -> None:
    v = js["step_for_mode"]
    assert v["first_generate"] == "input" and v["first_load"] == "input"
    assert v["last_config_with_text"] == "config"
    assert v["last_input_with_text"] == "input"
    # ein zuletzt gezeigtes späteres Schritt führt zur Konfiguration des Einstiegs
    assert v["last_execution_with_text"] == "config"
    assert v["last_result_with_text"] == "config"
    # Randfall: leer geworden
    assert v["last_config_without_text"] == "input"
    # der Text entstand auf einem anderen Weg: die Eingabe dieses Einstiegs
    assert v["load_after_generating"] == "input"
    assert v["generate_after_loading"] == "input"
    assert v["load_unknown_origin"] == "input"
    assert v["write_empty"] == "config"
    assert v["write_with_text_from_generation"] == "config"


# --- Der Stepper (Quelltextverträge) ------------------------------------------------


def test_fa96_stepper_renders_the_steps_of_the_mode() -> None:
    stepper = _text(STEPPER)
    assert "const STEPS" not in stepper
    assert "steps.map((step, i) =>" in stepper
    assert "data-step={step.id}" in stepper
    assert 'aria-current={active ? "step" : undefined}' in stepper
    # nummeriert wird nach Position, der aktuelle Schritt wird nie entzogen
    assert "i + 1" in stepper
    assert "disabled={!enabled && !active}" in stepper
    shell = _text(APP_SHELL)
    assert "steps={stepsForMode(wizardMode)}" in shell
    assert "reasons={{" in shell and 'unreachableReason(wizardMode, "result")' in shell
    # der Stepper erscheint erst, wenn der Einstieg gewählt ist
    assert "{!entryQuestionOpen && (\n        <Stepper" in shell


def test_fa96_step_one_is_clickable_from_every_step_and_the_jump_buttons_are_gone() -> (
    None
):
    shell = _text(APP_SHELL)
    for name in ("jumpAdjustPrompt", "jumpPickScenario", "jumpEditConfig"):
        assert name not in shell, name
    assert "<Button" not in _text(ORIGIN_BAR)
    assert "generatingRef" not in shell and "onGeneratingChange" not in shell
    assert "promptValues" not in _text(FRONTEND / "lib" / "configOrigin.ts")
    assert 'input: mode !== "write",' in _text(WIZARD)
    assert "reachableSteps(wizardMode, facts)" in shell
    assert "onSelect={setStep}" in shell


def test_fa96_every_step_has_one_primary_next_action_and_a_back_button() -> None:
    body = _prompt_step()
    # Schritt 1: „Weiter zur Konfiguration“, sobald es einen Text gibt
    assert (
        "const continueButton = (promptVisible || pickerVisible) && hasConfig && ("
        in body
    )
    assert (
        "Weiter zur Konfiguration" in body
        and 'data-action="continue-to-config"' in body
    )
    assert "extra={continueButton}" in body
    # Schritt 2: „Weiter zur Ausführung“ nur bei gültiger Konfiguration, „Zurück“ im Hinweis
    assert 'runLabel={validation?.valid ? "Weiter zur Ausführung" : undefined}' in body
    assert 'back={\n            step === "config" && entryMode !== "write"' in body
    assert "onClick: onBack" in body
    shell = _text(APP_SHELL)
    assert 'onBack={() => setStep("input")}' in shell
    # Schritt 3: „Zurück“ (Text aus backLabel) und die Aktionen der Ausführung
    execution = _execution_step()
    assert "onClick={onBack}" in execution and "{backLabel}" in execution
    assert 'onBack={() => setStep("config")}' in shell
    assert 'backLabel={backLabel(wizardMode, "execution")}' in shell
    for label in ("Ausführen", "Erneut ausführen", "Ergebnis ansehen"):
        assert label in execution, label
    # Schritt 4: „Zurück“ (Text aus backLabel)
    result = _result_step()
    assert "{backLabel}" in result and "onClick={onBackToExecution}" in result
    assert 'backLabel={backLabel(wizardMode, "result")}' in shell
    # die Texte stehen nur in wizardSteps.ts, nicht noch einmal in der Ansicht
    assert "Zurück zur Konfiguration" not in shell
    assert "Zurück zur Ausführung" not in shell
    # der Hinweis zeigt „Zurück“ vor der Weiter-Aktion
    guide = _between(shell, "function EntryGuide(", "function PromptStep(")
    assert "back?: { label: string; onClick: () => void };" in guide
    assert guide.index("{back && (") < guide.index("{extra}")


def test_fa96_entry_view_no_longer_exists() -> None:
    entry = _text(ENTRY)
    shell = _text(APP_SHELL)
    for name in ("EntryView", "initialEntryView", "showsEditor", "viewForMode"):
        assert name not in entry, name
        assert name not in shell, name
    assert "setEntryView" not in shell and "entryViewRef" not in shell
    assert "lastViewsRef" not in shell
    # die Hinweise kennen keine Zählung mehr: das trägt der Stepper
    assert "step: 1" not in entry and "of: 2" not in entry


def test_fa96_choosing_loading_generating_and_links_set_the_step() -> None:
    shell = _text(APP_SHELL)
    choose = _between(shell, "const chooseEntryMode", "}, []);")
    assert "setStep(firstStep(mode));" in choose
    change = _between(shell, "const changeEntryMode", "}, []);")
    assert "setStep(stepForMode(" in change
    load = _between(shell, "const loadScenario = async", "const setUrlScenario")
    assert 'setStep("config");' in load
    link = _between(
        shell, "const openLink = async", "useEffect(() => {\n    if (linkMode"
    )
    assert 'setEntryMode("load");\n      setStep("config");' in link
    generated = _between(
        shell, "const handleGenerated = (", "// FA96: Fakten für den Stepper"
    )
    assert 'setStep("config");' in generated


def test_fa96_a_run_opened_without_an_entry_takes_the_write_path() -> None:
    shell = _text(APP_SHELL)
    open_run = _between(shell, "const openRun = async", "const adoptRunConfig")
    assert 'setEntryMode((m) => m ?? "write");' in open_run
    assert open_run.index('setEntryMode((m) => m ?? "write");') < open_run.index(
        'setStep("result");'
    )


def test_fa96_step_is_persisted_under_the_same_key() -> None:
    shell = _text(APP_SHELL)
    assert 'const STEP_STORAGE_KEY = "geofact:step";' in shell
    assert "window.localStorage.setItem(STEP_STORAGE_KEY, step);" in shell
    # der gemerkte Wert geht durch initialStep (fremde Werte führen zu Schritt 1)
    assert "readLocalStorage(STEP_STORAGE_KEY)" in _between(
        shell, "const [step, setStep] = useState<WizardStep>(", ");"
    )


def test_fa96_the_step_belongs_to_the_prompt_step_view_only_for_input_and_config() -> (
    None
):
    """Schritt 1 und 2 zeigt PromptStep (bleibt eingehängt); Ausführung und Ergebnis haben eigene Ansichten."""
    body = _prompt_step()
    assert 'const promptVisible = entryMode === "generate" && step === "input";' in body
    assert 'const pickerVisible = entryMode === "load" && step === "input";' in body
    assert 'const editorVisible = step === "config";' in body
    shell = _text(APP_SHELL)
    assert 'className={cn(step !== "input" && step !== "config" && "hidden")}' in shell
    assert 'goneRunId && (step === "execution" || step === "result")' in shell


def test_fa96_new_text_from_step_one_keeps_later_steps_and_marks_the_run_stale() -> (
    None
):
    """Nachbedingung (10): nichts wird versteckt; der Lauf gilt über staleRun als veraltet (FA86)."""
    shell = _text(APP_SHELL)
    handler = _between(
        shell, "const handleGenerated = (", "// FA96: Fakten für den Stepper"
    )
    assert "setRunId(" not in handler and "setStatus(" not in handler
    assert (
        "const staleRun = isStaleRun(runBasis, runId, configYaml, effectiveParams);"
        in shell
    )


def test_fa96_mount_effect_never_leaves_the_user_on_a_result_without_a_run() -> None:
    shell = _text(APP_SHELL)
    mount = _between(
        shell,
        'if (step === "input" || step === "config") return;',
        "}, [step, runId]);",
    )
    assert 'if (step === "result") setStep("config");' in mount


def test_fa96_texts_outside_the_stepper_name_steps_instead_of_numbering_them() -> None:
    """Die Nummer eines Schritts hängt vom Einstieg ab (Ausführung: 3, beim Schreiben 2)."""
    outputs = _text(RUN_OUTPUTS)
    assert "im Schritt „Ausführung“ ausführen" in outputs
    assert "im Schritt „Ergebnis“ ansehen" in outputs
    assert "in Schritt 2" not in outputs and "in Schritt 3" not in outputs
    # der Läufe-Dialog steht in der Kopfzeile, auch solange es keinen Stepper gibt
    assert "<RunHistoryDialog" in _text(APP_SHELL)


def test_fa96_stepper_keeps_the_current_step_in_view() -> None:
    """Vier Schritte sind auf einem schmalen Bildschirm breiter als die Leiste."""
    stepper = _text(STEPPER)
    assert "overflow-x-auto" in stepper and "ref={navRef}" in stepper
    scroll = _between(stepper, "useEffect(() => {", "}, [current, steps.length]);")
    assert "nav?.querySelector<HTMLElement>('[aria-current=\"step\"]')" in scroll
    # nur waagerecht und nur in der Leiste; passt alles, wird nichts verschoben
    assert "nav.scrollWidth <= nav.clientWidth" in scroll
    assert "nav.scrollTo({ left:" in scroll
    assert "scrollIntoView(" not in scroll


def test_fa96_leftovers_of_the_jump_buttons_are_gone() -> None:
    wizard = _text(WIZARD)
    assert "export function nextStep" not in wizard
    assert "export function prevStep" not in wizard
    assert "onBusyChange" not in _text(PROMPT_PANEL)
    assert "runId={runId}\n            parameters" not in _text(APP_SHELL)
    # die Konfiguration hat beim Generieren und Laden keinen eigenen, nie gezeigten Text
    guide = _between(
        _text(ENTRY), "export const ENTRY_GUIDE", "export function parseEntryMode"
    )
    assert guide.count('config: { title: "Konfiguration prüfen" },') == 2
