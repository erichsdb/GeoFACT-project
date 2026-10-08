// FA93: Zustand eines abgebrochenen Laufs, wie ihn das Backend setzt
// (run_manager.cancel): der Lauf und jeder Schritt, der gerade lädt oder
// rechnet, gelten als abgebrochen; fertige Schritte bleiben fertig. Die
// Oberfläche wendet das beim Klick an und noch einmal beim Ereignis
// run_cancelled - zweimal angewandt ändert sich nichts mehr.

import type { RunStatus } from "./types";

export function cancelRunStatus(status: RunStatus): RunStatus {
  const steps = Object.fromEntries(
    Object.entries(status.steps).map(([id, step]) => [
      id,
      step.status === "loading" || step.status === "running" ? { ...step, status: "cancelled" as const } : step,
    ])
  );
  return { ...status, status: "cancelled", steps };
}
