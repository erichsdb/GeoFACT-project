// Implements: FA81, FA82, FA83 - runs the pure helpers of
// frontend/lib/generationSettings.ts under node (type stripping) and prints the
// results as JSON for the pytest modules.
import { pathToFileURL } from "node:url";

const mod = await import(pathToFileURL(process.argv[2]).href);
const {
  parseStoredOptions, effectiveOptions, overridesFrom, hasOverrides, parseSeconds, describeOverrides,
  serverSettings, runOptions, llmChoiceLabel,
} = mod;

const meta = {
  osm_backend: "postgis",
  llm_model: "a/model",
  generation_defaults: { llm_deadline_s: 300, trial_run: true, trial_run_s: 60 },
  generation_limits: { llm_deadline_s: 1800, trial_run_s: 600 },
  osm_backends: { default: "postgis", available: ["postgis", "overpass"] },
  llm_choices: [
    { id: "a/model@Relace", model: "a/model", providers: ["Relace"] },
    { id: "b/other", model: "b/other", providers: [] },
  ],
};
const server = serverSettings(meta);
const defaults = server.defaults;

const out = {};
out.server = {
  full: server,
  no_meta: serverSettings(null),
  old_backend: serverSettings({ osm_backend: "overpass", llm_model: "a/model" }),
  no_choices: serverSettings({
    osm_backend: "overpass", llm_model: "a/model",
    generation_defaults: meta.generation_defaults, generation_limits: meta.generation_limits,
  }),
};
const stored = (value, withServer = true) => parseStoredOptions(JSON.stringify(value), withServer ? server : null);
out.stored = {
  none: parseStoredOptions(null, server),
  empty: parseStoredOptions("", server),
  broken: parseStoredOptions("{not json", server),
  array: parseStoredOptions("[1,2]", server),
  full: stored({ llm_deadline_s: 120, trial_run: false, trial_run_s: 30 }),
  wrong_types: stored({ llm_deadline_s: "120", trial_run: "no", trial_run_s: null, osm_backend: 7, llm_choice: "" }),
  out_of_range: stored({ llm_deadline_s: 5000, trial_run_s: 0 }),
  unknown_field: stored({ trial_run: false, repair_rounds: 9 }),
  no_limits: stored({ llm_deadline_s: 5000 }, false),
  choices: stored({ osm_backend: "overpass", llm_choice: "b/other" }),
  choices_not_offered: stored({ osm_backend: "planet", llm_choice: "c/unlisted" }),
  choices_unchecked: stored({ osm_backend: "planet", llm_choice: "c/unlisted" }, false),
};
out.effective = {
  none: effectiveOptions(defaults, {}),
  some: effectiveOptions(defaults, { trial_run: false, llm_deadline_s: 120, osm_backend: "overpass" }),
};
out.overrides = {
  same: overridesFrom(defaults, defaults),
  changed: overridesFrom({ ...defaults, llm_deadline_s: 120, trial_run: false }, defaults),
  choices: overridesFrom({ ...defaults, osm_backend: "overpass", llm_choice: "b/other" }, defaults),
};
out.has = { empty: hasOverrides({}), some: hasOverrides({ trial_run: false }) };
out.run = {
  none: runOptions({}),
  other_only: runOptions({ trial_run: false, llm_choice: "b/other" }),
  osm: runOptions({ osm_backend: "overpass", trial_run: false }),
};
out.labels = meta.llm_choices.map(llmChoiceLabel);
out.seconds = Object.fromEntries(
  ["120", " 45,5 ", "0.5", "", "abc", "0", "-3", "1800", "1800.1", "Infinity"].map((t) => [t, parseSeconds(t, 1800)])
);
out.describe = {
  empty: describeOverrides({}),
  all: describeOverrides({
    llm_deadline_s: 120, trial_run: false, trial_run_s: 30, osm_backend: "overpass", llm_choice: "b/other",
  }),
};
console.log(JSON.stringify(out));
