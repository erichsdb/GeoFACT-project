// Implements: FA96 - runs the pure helpers of frontend/lib/wizardSteps.ts under
// node (type stripping) and prints the results as JSON for the pytest module.
import { pathToFileURL } from "node:url";

const wizard = await import(pathToFileURL(process.argv[2]).href);
const {
  stepsForMode,
  firstStep,
  parseWizardStep,
  initialStep,
  stepForMode,
  reachableSteps,
  completedSteps,
  needsRunStatus,
  runEndOpensResult,
  backLabel,
  unreachableReason,
} = wizard;

const MODES = ["generate", "load", "write"];
const ALL = ["input", "config", "execution", "result"];
const yaml = "scenario:\n  name: a\n";

const out = {};
out.steps = Object.fromEntries(MODES.map((m) => [m, stepsForMode(m).map((s) => ({ id: s.id, label: s.label, hint: s.hint }))]));
out.first = Object.fromEntries(MODES.map((m) => [m, firstStep(m)]));
out.parse = Object.fromEntries(["input", "config", "execution", "result", "prompt", ""].map((raw) => [raw, parseWizardStep(raw)]));
out.parse_null = parseWizardStep(null);

out.initial = {
  // ohne Konfiguration: erster Schritt des Einstiegs, egal was gemerkt ist
  generate_no_config: initialStep("generate", "", "result", false),
  load_no_config: initialStep("load", "  \n", "execution", false),
  write_no_config: initialStep("write", "", "result", false),
  // mit Konfiguration: der gemerkte Schritt, wenn er zum Einstieg gehört
  generate_result: initialStep("generate", yaml, "result", false),
  load_execution: initialStep("load", yaml, "execution", false),
  generate_config: initialStep("generate", yaml, "config", false),
  generate_input: initialStep("generate", yaml, "input", false),
  write_input_stored: initialStep("write", yaml, "input", false),
  write_config: initialStep("write", yaml, "config", false),
  // fremde oder fehlende Werte (auch das frühere "prompt")
  generate_old_prompt: initialStep("generate", yaml, "prompt", false),
  write_old_prompt: initialStep("write", yaml, "prompt", false),
  generate_null: initialStep("generate", yaml, null, false),
  generate_garbage: initialStep("generate", yaml, "nonsense", false),
  // geöffneter Link: Schritt 1, auch gegen alles Gemerkte
  link_generate: initialStep("generate", yaml, "result", true),
  link_load: initialStep("load", yaml, "execution", true),
  link_write: initialStep("write", yaml, "result", true),
  link_no_mode: initialStep(null, yaml, "result", true),
  // Frage offen: Schritt 1, der gemerkte Wert zählt nicht
  no_mode: initialStep(null, yaml, "result", false),
  no_mode_empty: initialStep(null, "", null, false),
};

const base = { hasConfig: true, valid: true, hasRun: true, runDone: true, staleRun: false };
const variants = {
  all: base,
  nothing: { hasConfig: false, valid: false, hasRun: false, runDone: false, staleRun: false },
  text_only: { hasConfig: true, valid: false, hasRun: false, runDone: false, staleRun: false },
  valid_no_run: { hasConfig: true, valid: true, hasRun: false, runDone: false, staleRun: false },
  invalid_with_run: { hasConfig: true, valid: false, hasRun: true, runDone: true, staleRun: true },
  no_config_with_run: { hasConfig: false, valid: true, hasRun: true, runDone: true, staleRun: false },
  running: { hasConfig: true, valid: true, hasRun: true, runDone: false, staleRun: false },
  stale: { ...base, staleRun: true },
};
out.reachable = Object.fromEntries(
  MODES.map((m) => [m, Object.fromEntries(Object.entries(variants).map(([k, f]) => [k, reachableSteps(m, f)]))])
);
// der Text entstand auf dem Weg des Einstiegs (originKind = Einstieg)
out.completed = Object.fromEntries(
  MODES.map((m) => [
    m,
    Object.fromEntries(Object.entries(variants).map(([k, f]) => [k, completedSteps(m, { ...f, originKind: m })])),
  ])
);
// der Text entstand auf einem anderen Weg oder seine Herkunft ist unbekannt
out.completed_foreign = {
  generate_after_loading: completedSteps("generate", { ...base, originKind: "load" }),
  load_after_generating: completedSteps("load", { ...base, originKind: "generate" }),
  generate_after_writing: completedSteps("generate", { ...base, originKind: "write" }),
  load_unknown_origin: completedSteps("load", { ...base, originKind: null }),
};

// Stand des gemerkten Laufs holen: (Schritt, Lauf gemerkt, Stand da, wird angelegt)
out.needs_run_status = Object.fromEntries(
  ALL.map((s) => [
    s,
    {
      remembered: needsRunStatus(s, true, false, false),
      no_run: needsRunStatus(s, false, false, false),
      has_status: needsRunStatus(s, true, true, false),
      starting: needsRunStatus(s, true, false, true),
    },
  ])
);
out.run_end_opens_result = Object.fromEntries(ALL.map((s) => [s, runEndOpensResult(s)]));
out.back_labels = Object.fromEntries(MODES.map((m) => [m, Object.fromEntries(ALL.map((s) => [s, backLabel(m, s)]))]));
out.reasons = Object.fromEntries(MODES.map((m) => [m, Object.fromEntries(ALL.map((s) => [s, unreachableReason(m, s)]))]));

out.step_for_mode = {
  first_generate: stepForMode("generate", {}, yaml, "generate"),
  first_load: stepForMode("load", {}, "", null),
  last_config_with_text: stepForMode("load", { load: "config" }, yaml, "load"),
  last_config_without_text: stepForMode("load", { load: "config" }, "  \n", "load"),
  last_input_with_text: stepForMode("generate", { generate: "input" }, yaml, "generate"),
  last_execution_with_text: stepForMode("generate", { generate: "execution" }, yaml, "generate"),
  last_result_with_text: stepForMode("load", { load: "result" }, yaml, "load"),
  load_after_generating: stepForMode("load", { load: "config" }, yaml, "generate"),
  generate_after_loading: stepForMode("generate", { generate: "config" }, yaml, "load"),
  load_unknown_origin: stepForMode("load", { load: "config" }, yaml, null),
  write_empty: stepForMode("write", {}, "", null),
  write_with_text_from_generation: stepForMode("write", { write: "input" }, yaml, "generate"),
};

console.log(JSON.stringify(out));
