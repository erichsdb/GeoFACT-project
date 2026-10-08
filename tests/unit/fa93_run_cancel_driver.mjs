// Implements: FA93 - runs frontend/lib/runCancel.ts under node (type stripping)
// and prints the results as JSON for the pytest module.
import { pathToFileURL } from "node:url";

const { cancelRunStatus } = await import(pathToFileURL(process.argv[2]).href);

const step = (id, status) => ({ id, status, warnings: [] });
const running = {
  run_id: "r1",
  status: "running",
  order: ["a", "b", "c", "d"],
  steps: { a: step("a", "done"), b: step("b", "loading"), c: step("c", "running"), d: step("d", "pending") },
};
const before = JSON.stringify(running);
const once = cancelRunStatus(running);
const twice = cancelRunStatus(once);

console.log(
  JSON.stringify({
    status: once.status,
    steps: Object.fromEntries(Object.entries(once.steps).map(([id, s]) => [id, s.status])),
    idempotent: JSON.stringify(once) === JSON.stringify(twice),
    input_untouched: JSON.stringify(running) === before,
    other_fields_kept: once.run_id === "r1" && once.order.length === 4,
  })
);
