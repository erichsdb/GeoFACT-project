// Implements: FA88 (Dateien eines Laufs), FA89 (Liste der Läufe).
//
// Reine Helfer ohne React: Beschriftungen für die Dateiliste und die Liste der
// Läufe. Was das Backend nicht nennt (Format/Quelle einer Datei, ein lesbarer
// Zeitpunkt), wird nicht erfunden, sondern weggelassen bzw. als unbekannt gezeigt.

import type { RunFile, RunOutputs, RunStatusValue, RunSummary } from "./types";

const LOCALE = "de-DE";

/** Dateigröße, deutsch formatiert: 812 B, 12,3 kB, 4,5 MB. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "–";
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  const units = ["kB", "MB", "GB", "TB"];
  let value = bytes / 1000;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  return `${value.toLocaleString(LOCALE, { maximumFractionDigits: 1 })} ${units[unit]}`;
}

/** „geojson aus catchment“ - nur wenn das Backend Format und Quelle nennt. */
export function fileCaption(file: RunFile): string | null {
  if (file.role !== "output" || !file.type) return null;
  return file.source ? `${file.type} aus ${file.source}` : file.type;
}

/** Ausgaben und Begleitdateien getrennt, jeweils in der Reihenfolge des Backends. */
export function splitFiles(outputs: RunOutputs | null): { outputs: RunFile[]; companions: RunFile[] } {
  const files = outputs?.files ?? [];
  return {
    outputs: files.filter((f) => f.role === "output"),
    companions: files.filter((f) => f.role !== "output"),
  };
}

export function runStatusLabel(status: RunStatusValue): string {
  switch (status) {
    case "done":
      return "fertig";
    case "error":
      return "fehlgeschlagen";
    case "cancelled":
      return "abgebrochen";
    case "running":
      return "läuft";
    default:
      return "wartet";
  }
}

/** Das ZIP gibt es erst nach einem abgeschlossenen Lauf (sonst 409). */
export function canDownloadZip(status: RunStatusValue): boolean {
  return status === "done";
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** „1 Schritt · 2 Dateien · 1 übersprungen“ */
export function runCounts(run: RunSummary): string {
  const parts = [plural(run.step_count, "Schritt", "Schritte"), plural(run.output_count, "Datei", "Dateien")];
  if (run.skipped_count > 0) parts.push(`${run.skipped_count} übersprungen`);
  return parts.join(" · ");
}

/** „heute, 14:32“, „gestern, 09:10“, sonst „06.10.2026, 18:00“. */
export function formatRunTime(iso: string | null | undefined, now: Date = new Date()): string {
  const date = iso ? new Date(iso) : null;
  if (!date || Number.isNaN(date.getTime())) return "Zeitpunkt unbekannt";
  const time = date.toLocaleTimeString(LOCALE, { hour: "2-digit", minute: "2-digit" });
  const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((dayStart(now) - dayStart(date)) / 86_400_000);
  if (days === 0) return `heute, ${time}`;
  if (days === 1) return `gestern, ${time}`;
  const day = date.toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit", year: "numeric" });
  return `${day}, ${time}`;
}

/** Warum die Liste endlich ist: Läufe liegen nur im Arbeitsspeicher des Backends. */
export function historyNote(maxRetained: number | null): string {
  const limit =
    maxRetained !== null && Number.isFinite(maxRetained)
      ? `Das Backend hält die letzten ${maxRetained} abgeschlossenen Läufe`
      : "Das Backend hält nur die letzten abgeschlossenen Läufe";
  return `${limit} im Arbeitsspeicher. Nach einem Neustart des Backends ist die Liste leer – was Sie behalten wollen, bitte herunterladen.`;
}
