// Implements: FA86 - runs the pure helpers of frontend/lib/configOrigin.ts,
// frontend/lib/runBasis.ts and the mode-switch helper of frontend/lib/wizardSteps.ts
// (FA96) under node (type stripping) and prints the results as JSON for the pytest module.
import { pathToFileURL } from "node:url";

const [originPath, basisPath, wizardPath] = process.argv.slice(2);
const origin = await import(pathToFileURL(originPath).href);
const basis = await import(pathToFileURL(basisPath).href);
const wizard = await import(pathToFileURL(wizardPath).href);

const gen = {
  origin: { kind: "generate", prompt: "Welche Umspannwerke versorgen die meisten Menschen in Dresden?", region: "Dresden, Deutschland", notes: "Hinweis des Modells" },
  yaml: "scenario:\n  name: a\n",
};
const load = { origin: { kind: "load", id: "leipzig/l10", source: "example", name: "Leipzig 10" }, yaml: "scenario:\n  name: b\n" };
const write = { origin: { kind: "write" }, yaml: "scenario:\n" };
const roundtrip = (r) => origin.parseOriginRecord(origin.serializeOriginRecord(r));

const out = {};

// --- Herkunft lesen / schreiben ------------------------------------------------
out.roundtrip = {
  generate: JSON.stringify(roundtrip(gen)) === JSON.stringify(gen),
  load: JSON.stringify(roundtrip(load)) === JSON.stringify(load),
  write: JSON.stringify(roundtrip(write)) === JSON.stringify(write),
};
out.parse_unreadable = {
  null: origin.parseOriginRecord(null),
  empty: origin.parseOriginRecord(""),
  garbage: origin.parseOriginRecord("{nope"),
  array: origin.parseOriginRecord("[]"),
  number: origin.parseOriginRecord("3"),
  no_yaml: origin.parseOriginRecord(JSON.stringify({ origin: { kind: "write" } })),
  unknown_kind: origin.parseOriginRecord(JSON.stringify({ origin: { kind: "dream" }, yaml: "x" })),
  generate_without_prompt: origin.parseOriginRecord(JSON.stringify({ origin: { kind: "generate", prompt: " " }, yaml: "x" })),
  load_bad_source: origin.parseOriginRecord(JSON.stringify({ origin: { kind: "load", id: "a", source: "cloud", name: "A" }, yaml: "x" })),
  load_without_id: origin.parseOriginRecord(JSON.stringify({ origin: { kind: "load", source: "user", name: "A" }, yaml: "x" })),
};
out.parse_tolerant = {
  generate_without_region: origin.parseOriginRecord(
    JSON.stringify({ origin: { kind: "generate", prompt: "p" }, yaml: "x" })
  ),
  load_without_name: origin.parseOriginRecord(
    JSON.stringify({ origin: { kind: "load", id: "a", source: "user" }, yaml: "x" })
  ),
};

// --- bearbeitet = Textvergleich ----------------------------------------------
out.edited = {
  gen_same: origin.isEdited(gen, gen.yaml),
  gen_changed: origin.isEdited(gen, gen.yaml + "x"),
  gen_changed_back: origin.isEdited(gen, gen.yaml + "x") && !origin.isEdited(gen, gen.yaml),
  load_same: origin.isEdited(load, load.yaml),
  load_changed: origin.isEdited(load, "other"),
  write_changed: origin.isEdited(write, "other"),
  unknown: origin.isEdited(null, "anything"),
};

// --- Anzeige -------------------------------------------------------------------
const long = "x ".repeat(200);
out.truncate = {
  short: origin.truncatePrompt("Kurz  gefragt"),
  long_length: origin.truncatePrompt(long).length,
  long_ellipsis: origin.truncatePrompt(long).endsWith("…"),
};
out.describe = {
  generate: origin.describeOrigin(gen, gen.yaml),
  generate_edited: origin.describeOrigin(gen, gen.yaml + "\n# x"),
  load: origin.describeOrigin(load, load.yaml),
  load_edited: origin.describeOrigin(load, "other"),
  write: origin.describeOrigin(write, "other"),
  unknown: origin.describeOrigin(null, "x"),
};
out.guide_text = {
  generate: origin.configGuideText(gen, gen.yaml),
  generate_edited: origin.configGuideText(gen, "other"),
  load: origin.configGuideText(load, load.yaml),
  load_edited: origin.configGuideText(load, "other"),
  write: origin.configGuideText(write, "other"),
  unknown: origin.configGuideText(null, "x"),
};
out.notes = {
  generate: origin.originNotes(gen),
  load: origin.originNotes(load),
  write: origin.originNotes(write),
  unknown: origin.originNotes(null),
  generate_without_notes: origin.originNotes({ origin: { ...gen.origin, notes: null }, yaml: "x" }),
};
// --- Einstieg an Ort und Stelle wechseln ----------------------------------------
const text = "scenario:\n";
out.step_for_mode = {
  first_generate: wizard.stepForMode("generate", {}, text, "generate"),
  first_load: wizard.stepForMode("load", {}, "", null),
  last_config_with_text: wizard.stepForMode("load", { load: "config" }, text, "load"),
  last_config_without_text: wizard.stepForMode("load", { load: "config" }, "  \n", "load"),
  last_input_with_text: wizard.stepForMode("generate", { generate: "input" }, text, "generate"),
  // FA96: auch ein zuletzt gezeigtes Ergebnis führt zur Konfiguration des Einstiegs
  last_result_with_text: wizard.stepForMode("load", { load: "result" }, text, "load"),
  // der Text entstand auf einem anderen Weg: die Eingabe dieses Einstiegs
  load_after_generating: wizard.stepForMode("load", { load: "config" }, text, "generate"),
  generate_after_loading: wizard.stepForMode("generate", { generate: "config" }, text, "load"),
  load_unknown_origin: wizard.stepForMode("load", { load: "config" }, text, null),
  write_empty: wizard.stepForMode("write", {}, "", null),
  write_remembers_input: wizard.stepForMode("write", { write: "input" }, text, "generate"),
};

// --- Stepper (FA96): Haken, Erreichbarkeit, Start ---------------------------------
const facts = { hasConfig: true, valid: true, hasRun: true, runDone: true, staleRun: false, originKind: "generate" };
out.steps = {
  fresh_run_completed: wizard.completedSteps("generate", facts),
  stale_run_completed: wizard.completedSteps("generate", { ...facts, staleRun: true }),
  no_config_but_run: wizard.reachableSteps("generate", { ...facts, hasConfig: false }),
  reload_generate_no_config_stored_result: wizard.initialStep("generate", "", "result", false),
  reload_write_blank_stored_result: wizard.initialStep("write", "  ", "result", false),
};

// --- veraltetes Ergebnis ----------------------------------------------------------
const b = { runId: "r1", yaml: "scenario:\n", params: { a: 1, b: [2, { y: 1, x: 2 }] } };
const reordered = { b: [2, { x: 2, y: 1 }], a: 1 };
out.stale = {
  fresh: basis.isStaleRun(b, "r1", b.yaml, b.params),
  yaml_changed: basis.isStaleRun(b, "r1", b.yaml + "# x", b.params),
  param_changed: basis.isStaleRun(b, "r1", b.yaml, { a: 2, b: b.params.b }),
  param_added: basis.isStaleRun(b, "r1", b.yaml, { ...b.params, c: 1 }),
  param_order_only: basis.isStaleRun(b, "r1", b.yaml, reordered),
  restored_text: basis.isStaleRun(b, "r1", b.yaml, b.params),
  no_basis: basis.isStaleRun(null, "r1", "anything", {}),
  other_run: basis.isStaleRun(b, "r2", "anything", {}),
  no_run: basis.isStaleRun(b, null, "anything", {}),
};
out.basis_roundtrip = JSON.stringify(basis.parseRunBasis(basis.serializeRunBasis(b))) === JSON.stringify(b);
out.basis_unreadable = {
  null: basis.parseRunBasis(null),
  garbage: basis.parseRunBasis("{nope"),
  no_run_id: basis.parseRunBasis(JSON.stringify({ yaml: "x", params: {} })),
  no_yaml: basis.parseRunBasis(JSON.stringify({ runId: "r", params: {} })),
  no_params: basis.parseRunBasis(JSON.stringify({ runId: "r", yaml: "x" })),
  array_params: basis.parseRunBasis(JSON.stringify({ runId: "r", yaml: "x", params: [] })),
};

console.log(JSON.stringify(out));
