"""Implements: FA86 (Herkunft der Konfiguration, Direktsprünge, veraltetes Ergebnis) - Frontend.

Es gibt kein Frontend-Testwerkzeug im Repo (siehe test_fa54_frontend_lost_run.py).
Die Logik liegt deshalb in reinen Helfern (`frontend/lib/configOrigin.ts`,
`frontend/lib/runBasis.ts`, `viewForMode` in `frontend/lib/entryMode.ts`), die hier
unter node ausgeführt werden; die Nachbedingungen an die Ansicht (Schritt 1 bleibt
eingehängt, Herkunftsleiste mit Sprüngen, Umschalter, Hinweis auf ein veraltetes
Ergebnis) prüft der Quelltext.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
ORIGIN = FRONTEND / "lib" / "configOrigin.ts"
BASIS = FRONTEND / "lib" / "runBasis.ts"
ENTRY = FRONTEND / "lib" / "entryMode.ts"
DRIVER = Path(__file__).resolve().parent / "fa86_origin_driver.mjs"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
PROMPT_PANEL = FRONTEND / "components" / "prompt-panel.tsx"
ORIGIN_BAR = FRONTEND / "components" / "origin-bar.tsx"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _between(source: str, begin: str, end: str) -> str:
    start = source.index(begin)
    return source[start : source.index(end, start)]


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(DRIVER), str(ORIGIN), str(BASIS), str(ENTRY)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"node cannot run the TypeScript helpers: {proc.stderr[-300:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --- Herkunft lesen und schreiben (unter node ausgeführt) --------------------


def test_fa86_origin_survives_a_roundtrip_through_storage(js: dict) -> None:
    assert js["roundtrip"] == {"generate": True, "load": True, "write": True}


def test_fa86_unreadable_or_foreign_stored_origin_is_unknown_not_guessed(
    js: dict,
) -> None:
    assert all(v is None for v in js["parse_unreadable"].values()), js[
        "parse_unreadable"
    ]


def test_fa86_stored_origin_with_missing_optional_fields_is_still_read(
    js: dict,
) -> None:
    tolerant = js["parse_tolerant"]
    assert tolerant["generate_without_region"]["origin"]["region"] == ""
    assert tolerant["generate_without_region"]["origin"]["notes"] is None
    assert tolerant["load_without_name"]["origin"]["name"] == "a"


# --- bearbeitet = Textvergleich -------------------------------------------------


def test_fa86_edited_is_a_text_comparison_with_the_text_of_the_origin(js: dict) -> None:
    edited = js["edited"]
    assert edited["gen_same"] is False and edited["load_same"] is False
    assert edited["gen_changed"] is True and edited["load_changed"] is True
    # wer zur Ursprungsfassung zurückkehrt, hat nichts mehr "bearbeitet"
    assert edited["gen_changed_back"] is True


def test_fa86_self_written_and_unknown_origins_are_never_edited(js: dict) -> None:
    assert js["edited"]["write_changed"] is False
    assert js["edited"]["unknown"] is False


# --- Herkunftsleiste: Anzeige -----------------------------------------------------


def test_fa86_origin_bar_names_prompt_scenario_or_self_written(js: dict) -> None:
    d = js["describe"]
    assert d["generate"]["prefix"] == "Erzeugt aus:" and d["generate"][
        "subject"
    ].startswith("„Welche Umspannwerke")
    assert d["generate"]["full"].startswith(
        "Welche Umspannwerke"
    )  # voller Wortlaut für den Tooltip
    assert d["load"]["prefix"] == "Geladen:" and d["load"]["subject"] == "Leipzig 10"
    assert (
        d["write"]["prefix"] == "Selbst geschrieben" and d["write"]["subject"] is None
    )


def test_fa86_origin_bar_marks_edited_only_for_generated_and_loaded_text(
    js: dict,
) -> None:
    d = js["describe"]
    assert d["generate"]["edited"] is False and d["generate_edited"]["edited"] is True
    assert d["load"]["edited"] is False and d["load_edited"]["edited"] is True
    assert d["write"]["edited"] is False


def test_fa86_unknown_origin_says_so_instead_of_claiming_one(js: dict) -> None:
    assert js["describe"]["unknown"] == {
        "kind": "unknown",
        "prefix": "Herkunft unbekannt",
        "subject": None,
        "full": None,
        "edited": False,
    }


def test_fa86_long_prompt_is_shortened_with_an_ellipsis(js: dict) -> None:
    t = js["truncate"]
    assert t["short"] == "Kurz gefragt"  # Leerraum zusammengezogen, nicht gekürzt
    assert t["long_ellipsis"] is True and t["long_length"] <= 95


def test_fa86_guide_text_follows_the_origin_of_the_text(js: dict) -> None:
    g = js["guide_text"]
    assert g["generate"].startswith(
        "Das Sprachmodell hat diese Konfiguration geschrieben."
    )
    assert "bearbeitet" in g["generate_edited"]
    assert g["load"].startswith("Das Szenario ist geladen.")
    assert "bearbeitet" in g["load_edited"]
    assert g["write"].startswith("Sie haben diese Konfiguration selbst geschrieben.")
    assert g["unknown"].startswith(
        "Die Herkunft dieser Konfiguration ist nicht bekannt."
    )
    assert all("Weiter zur Ausführung" in text for text in g.values())


def test_fa86_notes_belong_to_a_generated_origin_only(js: dict) -> None:
    n = js["notes"]
    assert n["generate"] == "Hinweis des Modells"
    assert n["load"] is None and n["write"] is None and n["unknown"] is None
    assert n["generate_without_notes"] is None


def test_fa86_adjust_prompt_restores_prompt_and_region_of_a_generation_only(
    js: dict,
) -> None:
    p = js["prompt_values"]
    assert p["generate"]["prompt"].startswith("Welche Umspannwerke")
    assert p["generate"]["region"] == "Dresden, Deutschland"
    assert p["load"] is None and p["unknown"] is None


# --- Einstieg an Ort und Stelle wechseln ------------------------------------------


def test_fa86_mode_switch_shows_the_last_view_of_that_mode(js: dict) -> None:
    v = js["view_for_mode"]
    assert v["last_config_with_text"] == "config"
    assert v["last_input_with_text"] == "input"


def test_fa86_first_visit_of_a_mode_shows_its_input(js: dict) -> None:
    v = js["view_for_mode"]
    assert v["first_generate"] == "input" and v["first_load"] == "input"


def test_fa86_config_view_needs_a_config_text(js: dict) -> None:
    """Randfall: die zuletzt gezeigte Konfiguration ist leer geworden -> Eingabe."""
    assert js["view_for_mode"]["last_config_without_text"] == "input"


def test_fa86_a_text_from_another_way_shows_the_input_of_the_chosen_way(
    js: dict,
) -> None:
    """Erzeugen, dann zu "Laden" wechseln: dort steht die Szenarioauswahl, nicht der erzeugte Text."""
    v = js["view_for_mode"]
    assert v["load_after_generating"] == "input"
    assert v["generate_after_loading"] == "input"
    assert v["load_unknown_origin"] == "input"


def test_fa86_the_input_of_the_chosen_way_stays_reachable_from_the_config_view() -> (
    None
):
    bar = _text(ORIGIN_BAR)
    assert (
        '(view.kind === "load" || mode === "load") && (' in bar
        and "Szenario wählen" in bar
    )
    assert (
        '(view.kind === "generate" || mode === "generate") && (' in bar
        and "Zum Prompt" in bar
    )
    body = _between(_text(APP_SHELL), "function PromptStep(", "function ExecutionStep(")
    assert "mode={entryMode}" in body


def test_fa86_writing_always_shows_the_editor(js: dict) -> None:
    v = js["view_for_mode"]
    assert v["write_empty"] == "config" and v["write_remembers_input"] == "config"


# --- veraltetes Ergebnis (unter node ausgeführt) ---------------------------------


def test_fa86_unchanged_run_is_not_stale(js: dict) -> None:
    s = js["stale"]
    assert s["fresh"] is False and s["restored_text"] is False


def test_fa86_changed_yaml_or_parameters_make_the_run_stale(js: dict) -> None:
    s = js["stale"]
    assert s["yaml_changed"] is True
    assert s["param_changed"] is True and s["param_added"] is True


def test_fa86_parameter_key_order_alone_is_not_a_change(js: dict) -> None:
    assert js["stale"]["param_order_only"] is False


def test_fa86_unknown_basis_never_claims_staleness(js: dict) -> None:
    """Nichts gemerkt (z. B. Lauf aus einer früheren Version), anderer Lauf, kein Lauf."""
    s = js["stale"]
    assert s["no_basis"] is False and s["other_run"] is False and s["no_run"] is False


def test_fa86_run_basis_survives_storage_and_unreadable_basis_is_unknown(
    js: dict,
) -> None:
    assert js["basis_roundtrip"] is True
    assert all(v is None for v in js["basis_unreadable"].values()), js[
        "basis_unreadable"
    ]


# --- Schritt 1 bleibt eingehängt, Prompt gehört der Seite -------------------------


def test_fa86_prompt_and_region_live_in_the_app_shell_and_are_persisted() -> None:
    shell = _text(APP_SHELL)
    assert 'const PROMPT_STORAGE_KEY = "geofact:prompt";' in shell
    assert 'const REGION_STORAGE_KEY = "geofact:promptRegion";' in shell
    assert 'useState(() => readLocalStorage(PROMPT_STORAGE_KEY) ?? "")' in shell
    assert "window.localStorage.setItem(PROMPT_STORAGE_KEY, prompt)" in shell
    assert "window.localStorage.setItem(REGION_STORAGE_KEY, region)" in shell
    panel = _text(PROMPT_PANEL)
    assert (
        'useState("")' not in panel.split("const [phase")[0]
    )  # kein eigener Prompt-/Regionszustand mehr
    assert "onChange={(e) => onPromptChange(e.target.value)}" in panel
    assert "onChange={(e) => onRegionChange(e.target.value)}" in panel


def test_fa86_step_one_stays_mounted_while_other_steps_show() -> None:
    shell = _text(APP_SHELL)
    assert 'className={cn(step !== "prompt" && "hidden")}' in shell
    assert '{step === "prompt" && (\n          <PromptStep' not in shell
    assert shell.count("<PromptStep") == 1
    # Ausführung und Ergebnis NICHT: Karte und Graph messen sich verborgen falsch
    assert '{step === "execution" && (' in shell and '{step === "result" && (' in shell


def test_fa86_a_generation_finishing_elsewhere_applies_the_config_and_notifies() -> (
    None
):
    shell = _text(APP_SHELL)
    handler = _between(shell, "const handleGenerated = (", "// FA86: Direktsprünge")
    assert "applyConfigYaml(yaml);" in handler
    assert 'kind: "generate", prompt: used.prompt, region: used.region' in handler
    # nur wer in der Eingabe der Erzeugung steht, wird zur Konfiguration geleitet
    assert (
        'stepRef.current === "prompt"' in handler
        and 'entryViewRef.current === "input"' in handler
    )
    assert "notify.success(" in handler
    assert "setStep(" not in handler  # der Schritt wird nie umgeschaltet
    panel = _text(PROMPT_PANEL)
    assert (
        "const used = { prompt, region };" in panel
        and "onGenerated(res.config_yaml, res.notes, used);" in panel
    )


# --- Herkunft setzen ----------------------------------------------------------------


def test_fa86_origin_is_set_on_generation_load_link_and_empty_editor_start() -> None:
    shell = _text(APP_SHELL)
    assert 'kind: "generate", prompt: used.prompt' in shell
    load = _between(shell, "const loadScenario = async", "const setUrlScenario")
    assert (
        'kind: "load", id: scenario.id, source: scenario.source, name: scenario.name'
        in load
    )
    link = _between(
        shell, "const openLink = async", "useEffect(() => {\n    if (linkMode"
    )
    assert (
        'setOriginRecord({ origin: { kind: "load", ...ref, name: displayName }, yaml: config_yaml });'
        in link
    )
    apply = _between(
        shell, "const applyConfigYaml = useCallback", "// FA59: deklarierte Parameter"
    )
    assert (
        'if (!prevText.trim() && text.trim()) setOriginRecord({ origin: { kind: "write" }, yaml: text });'
        in apply
    )


def test_fa86_origin_is_persisted_and_read_tolerantly() -> None:
    shell = _text(APP_SHELL)
    assert "parseOriginRecord(readLocalStorage(CONFIG_ORIGIN_STORAGE_KEY))" in shell
    assert "serializeOriginRecord(originRecord)" in shell
    assert 'CONFIG_ORIGIN_STORAGE_KEY = "geofact:configOrigin"' in _text(ORIGIN)


# --- Herkunftsleiste mit Direktsprüngen ----------------------------------------------


def test_fa86_origin_bar_is_shown_on_steps_two_and_three_and_above_the_editor() -> None:
    shell = _text(APP_SHELL)
    outer = _between(
        shell,
        '{step !== "prompt" && (\n          <OriginBar',
        '{step === "result" && staleRun',
    )
    assert "onAdjustPrompt={jumpAdjustPrompt}" in outer
    assert (
        "onPickScenario={jumpPickScenario}" in outer
        and "onEditConfig={jumpEditConfig}" in outer
    )
    inner = _between(
        shell,
        "<OriginBar\n              record={originRecord}",
        "originNotes(originRecord) && (",
    )
    assert (
        "hideEdit" in inner
    )  # im Editor selbst ist "Konfiguration bearbeiten" überflüssig
    bar = _text(ORIGIN_BAR)
    assert "if (!yaml.trim()) return null;" in bar  # ohne Konfiguration keine Leiste


def test_fa86_origin_bar_actions_depend_on_the_origin() -> None:
    bar = _text(ORIGIN_BAR)
    assert (
        '(view.kind === "generate" || mode === "generate") && (' in bar
        and "Prompt anpassen" in bar
    )
    assert (
        '(view.kind === "load" || mode === "load") && (' in bar
        and "Anderes Szenario wählen" in bar
    )
    assert "Konfiguration bearbeiten" in bar and "{!hideEdit && (" in bar
    assert "bearbeitet" in bar and "{view.edited && (" in bar


def test_fa86_adjust_prompt_jumps_to_the_prompt_with_the_values_of_the_origin() -> None:
    shell = _text(APP_SHELL)
    jump = _between(shell, "const jumpAdjustPrompt", "const jumpPickScenario")
    assert "promptValues(originRecord)" in jump
    assert (
        "values && !generatingRef.current" in jump
    )  # eine laufende Erzeugung behält ihren Prompt
    assert "setPrompt(values.prompt);" in jump and "setRegion(values.region);" in jump
    assert 'setEntryMode("generate");' in jump and 'setEntryView("input");' in jump
    assert 'setStep("prompt");' in jump


def test_fa86_edit_config_jumps_to_the_config_view_of_step_one() -> None:
    shell = _text(APP_SHELL)
    jump = shell[shell.index("const jumpEditConfig") :]
    jump = jump[: jump.index("};")]
    assert 'setEntryView("config");' in jump and 'setStep("prompt");' in jump


def test_fa86_guide_text_and_notes_follow_the_origin_not_the_mode() -> None:
    body = _between(_text(APP_SHELL), "function PromptStep(", "function ExecutionStep(")
    assert (
        'entryView === "config" && entryMode !== "write" ? configGuideText(originRecord, configYaml)'
        in body
    )
    assert "originNotes(originRecord)" in body
    assert 'entryMode === "generate" && notes' not in body


# --- Umschalter ---------------------------------------------------------------------------


def test_fa86_mode_switch_keeps_the_editor_text_and_uses_the_last_view() -> None:
    """Die Kopfzeile (FA80) wechselt den Einstieg; FA86 setzt dabei die zuletzt gezeigte Ansicht."""
    shell = _text(APP_SHELL)
    change = _between(shell, "const changeEntryMode", "}, []);")
    assert "setEntryMode(mode);" in change
    assert (
        "viewForMode(mode, lastViewsRef.current, configYamlRef.current, originKindRef.current)"
        in change
    )
    assert "setConfigYaml" not in change and "applyConfigYaml" not in change
    assert "lastViewsRef.current[entryMode] = entryView;" in shell
    switch = _between(shell, "const switchEntryMode = ", "};")
    assert (
        "if (mode !== entryMode) changeEntryMode(mode);" in switch
        and 'setStep("prompt");' in switch
    )


def test_fa86_the_question_is_only_for_the_first_choice_of_a_session() -> None:
    shell = _text(APP_SHELL)
    assert "<EntryChooser" in shell and "onChooseEntryMode" in shell
    choose = _between(shell, "const chooseEntryMode", "}, []);")
    assert 'setEntryView(mode === "write" ? "config" : "input");' in choose


def test_fa86_guide_has_no_second_mode_switch_next_to_the_header_one() -> None:
    shell = _text(APP_SHELL)
    guide = _between(shell, "function EntryGuide(", "function PromptStep(")
    assert "TabsList" not in guide and "onSwitch" not in guide
    assert shell.count("<EntrySwitch") == 1


# --- veraltetes Ergebnis (Quelltext) -------------------------------------------------------------


def test_fa86_an_edit_keeps_the_finished_run_instead_of_dropping_it() -> None:
    apply = _between(
        _text(APP_SHELL),
        "const applyConfigYaml = useCallback",
        "// FA59: deklarierte Parameter",
    )
    assert "setStatus(" not in apply and "setSelectedNode(" not in apply


def test_fa86_a_run_remembers_the_yaml_and_parameters_it_started_with() -> None:
    shell = _text(APP_SHELL)
    start = _between(shell, "const startRun = async", "const finishStarting")
    assert (
        "const startYaml = configYaml;" in start
        and "const startParams = effectiveParams;" in start
    )
    assert (
        "setRunBasis({ runId: run_id, yaml: startYaml, params: startParams });" in start
    )
    assert (
        "serializeRunBasis(runBasis)" in shell
        and "parseRunBasis(readLocalStorage(RUN_BASIS_STORAGE_KEY))" in shell
    )


def test_fa86_step_three_shows_a_banner_with_rerun_for_a_stale_run() -> None:
    shell = _text(APP_SHELL)
    assert (
        "const staleRun = isStaleRun(runBasis, runId, configYaml, effectiveParams);"
        in shell
    )
    assert '{step === "result" && staleRun && (' in shell
    notice = _between(
        shell, "function StaleResultNotice(", "// --- Hinweis: Lauf nicht mehr bekannt"
    )
    assert (
        "Dieses Ergebnis gehört zu einer früheren Fassung der Konfiguration" in notice
    )
    assert "Erneut ausführen" in notice and "disabled={!canRerun}" in notice
    assert "onRerun={rerunAfterLoss}" in shell
    # kein Neustart während ein Lauf noch läuft, keiner mit ungültiger Konfiguration
    assert "canRerun={!!validation?.valid && !isRunning}" in shell


def test_fa86_step_two_shows_the_plan_of_the_current_config_for_a_stale_run() -> None:
    shell = _text(APP_SHELL)
    assert "const showRunInExecution = !staleRun || isRunning;" in shell
    assert (
        "const executionGraphStatus = showRunInExecution ? graphStatus : staticStatus;"
        in shell
    )
    step = _between(shell, '{step === "execution" && (', '{step === "result" && (')
    assert "graphStatus={executionGraphStatus}" in step
    assert "status={showRunInExecution ? status : null}" in step
    # Schritt 3 behält den Lauf selbst
    result = shell[shell.index('{step === "result" && (\n          <ResultStep') :]
    assert "status={status}" in result and "graphStatus={graphStatus}" in result


def test_fa86_a_stale_run_is_not_marked_as_completed_in_the_stepper() -> None:
    assert "execution: !!isDone && !staleRun," in _text(APP_SHELL)


def test_fa86_a_lost_run_clears_its_basis_and_a_link_open_does_too() -> None:
    shell = _text(APP_SHELL)
    gone = _between(
        shell, "const handleRunGone = useCallback", "useEffect(() => {\n    api.meta()"
    )
    assert "setRunBasis(null);" in gone
    link = _between(
        shell, "const openLink = async", "useEffect(() => {\n    if (linkMode"
    )
    assert "setRunBasis(null);" in link


# --- ohne Konfiguration kein Ergebnis ------------------------------------------------------------


def test_fa86_without_a_config_neither_execution_nor_result_is_reachable() -> None:
    """Nachbedingung (12): ein gemerkter Lauf allein öffnet Schritt 3 nicht."""
    shell = _text(APP_SHELL)
    assert "const hasConfig = configYaml.trim().length > 0;" in shell
    reachable = _between(
        shell, "const reachable: Record<WizardStep, boolean> = {", "};"
    )
    assert "execution: !!validation?.valid," in reachable
    assert "result: !!runId && hasConfig," in reachable


def test_fa86_reload_without_a_config_starts_on_step_one() -> None:
    shell = _text(APP_SHELL)
    initial = _between(
        shell, "const [step, setStep] = useState<WizardStep>(() => {", "});"
    )
    assert (
        'if (!(readLocalStorage(CONFIG_YAML_STORAGE_KEY) ?? "").trim()) return "prompt";'
        in initial
    )
