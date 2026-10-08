// Implements: FA88, FA89 - runs the pure helpers of frontend/lib/runFiles.ts under
// node (type stripping) and prints the results as JSON for the pytest modules.
import { pathToFileURL } from "node:url";

const [helpersPath] = process.argv.slice(2);
const h = await import(pathToFileURL(helpersPath).href);

const out = {};

// --- FA88: Dateien ---------------------------------------------------------------
out.bytes = {
  zero: h.formatBytes(0),
  small: h.formatBytes(812),
  edge: h.formatBytes(999),
  kilo: h.formatBytes(1000),
  kilo_fraction: h.formatBytes(12345),
  mega: h.formatBytes(4_500_000),
  giga: h.formatBytes(1_230_000_000),
  negative: h.formatBytes(-1),
  nan: h.formatBytes(Number.NaN),
};
out.caption = {
  output: h.fileCaption({ name: "a.geojson", role: "output", size_bytes: 1, type: "geojson", source: "catchment" }),
  output_without_source: h.fileCaption({ name: "a.geojson", role: "output", size_bytes: 1, type: "geojson", source: null }),
  output_unmatched: h.fileCaption({ name: "a.geojson", role: "output", size_bytes: 1, type: null, source: null }),
  companion: h.fileCaption({ name: "scenario.yaml", role: "companion", size_bytes: 1, type: "geojson", source: "x" }),
};
const listing = {
  run_id: "r1",
  status: "done",
  files: [
    { name: "b.csv", role: "output", size_bytes: 2 },
    { name: "scenario.yaml", role: "companion", size_bytes: 3 },
    { name: "a.geojson", role: "output", size_bytes: 1 },
    { name: "ATTRIBUTION.txt", role: "companion", size_bytes: 4 },
  ],
  skipped: [],
};
const split = h.splitFiles(listing);
out.split = {
  outputs: split.outputs.map((f) => f.name),
  companions: split.companions.map((f) => f.name),
  none: h.splitFiles(null),
};
out.zip = Object.fromEntries(
  ["pending", "running", "done", "error", "cancelled"].map((s) => [s, h.canDownloadZip(s)])
);

// --- FA89: Liste der Läufe -------------------------------------------------------
out.status = Object.fromEntries(
  ["pending", "running", "done", "error", "cancelled"].map((s) => [s, h.runStatusLabel(s)])
);
const run = (step_count, output_count, skipped_count) => ({
  run_id: "r", status: "done", scenario_name: "x", created_at: "", step_count, output_count, skipped_count,
});
out.counts = {
  singular: h.runCounts(run(1, 1, 0)),
  plural: h.runCounts(run(3, 2, 0)),
  none: h.runCounts(run(0, 0, 0)),
  skipped: h.runCounts(run(3, 2, 1)),
};
// Lokale Zeit auf beiden Seiten: das Ergebnis hängt nicht von der Zeitzone ab.
const now = new Date(2026, 9, 8, 15, 0);
out.time = {
  today: h.formatRunTime(new Date(2026, 9, 8, 14, 32).toISOString(), now),
  just_after_midnight: h.formatRunTime(new Date(2026, 9, 8, 0, 5).toISOString(), now),
  yesterday: h.formatRunTime(new Date(2026, 9, 7, 23, 55).toISOString(), now),
  older: h.formatRunTime(new Date(2026, 9, 6, 18, 0).toISOString(), now),
  garbage: h.formatRunTime("gestern irgendwann", now),
  empty: h.formatRunTime("", now),
  null: h.formatRunTime(null, now),
};
out.note = {
  with_limit: h.historyNote(20),
  unknown: h.historyNote(null),
};

console.log(JSON.stringify(out));
