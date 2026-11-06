// Implements: FA80 - runs the pure helpers of frontend/lib/entryMode.ts under
// node (type stripping) and prints the results as JSON for the pytest module.
import { pathToFileURL } from "node:url";

const mod = await import(pathToFileURL(process.argv[2]).href);
const { ENTRY_MODES, ENTRY_GUIDE, parseEntryMode, initialEntryMode, canResume } = mod;

const out = {};
out.modes = ENTRY_MODES.map((m) => ({ id: m.id, label: m.label }));
out.mode_texts_complete = ENTRY_MODES.every((m) => m.description.length > 0 && m.hint.length > 0);
out.guide = ENTRY_GUIDE;
out.parse = Object.fromEntries(
  ["generate", "load", "write", "", "prompt", "Generate"].map((raw) => [raw, parseEntryMode(raw)])
);
out.parse_null = parseEntryMode(null);
out.initial_mode = {
  new_session: initialEntryMode(null, false),
  session_choice: initialEntryMode("generate", false),
  session_unreadable: initialEntryMode("prompt", false),
  link_new_session: initialEntryMode(null, true),
  link_beats_session: initialEntryMode("write", true),
};
out.resume = { empty: canResume(""), blank: canResume("  \n"), text: canResume("scenario:\n") };
console.log(JSON.stringify(out));
