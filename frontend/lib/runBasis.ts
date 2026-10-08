// Implements: FA86 - veraltetes Ergebnis erkennen.
//
// Reine Helfer ohne React: womit ein Lauf gestartet wurde (YAML + wirksame
// Parameter) und ob die aktuelle Konfiguration davon abweicht. Ist die Basis
// unbekannt (nichts gemerkt, anderer Lauf, unlesbar), gilt der Lauf NICHT als
// veraltet - lieber nichts behaupten als zu Unrecht.

export interface RunBasis {
  runId: string;
  yaml: string;
  params: Record<string, unknown>;
}

export const RUN_BASIS_STORAGE_KEY = "geofact:runBasis";

/** Parameter als Text, unabhängig von der Schlüsselreihenfolge (rekursiv sortiert). */
export function canonicalParams(value: unknown): string {
  const sort = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(sort);
    if (v && typeof v === "object") {
      return Object.fromEntries(
        Object.entries(v as Record<string, unknown>)
          .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
          .map(([k, x]) => [k, sort(x)])
      );
    }
    return v;
  };
  return JSON.stringify(sort(value ?? {}));
}

export function serializeRunBasis(basis: RunBasis): string {
  return JSON.stringify(basis);
}

export function parseRunBasis(raw: string | null): RunBasis | null {
  if (!raw) return null;
  try {
    const d = JSON.parse(raw) as Partial<RunBasis> | null;
    if (!d || typeof d !== "object") return null;
    if (typeof d.runId !== "string" || !d.runId || typeof d.yaml !== "string") return null;
    const params = d.params && typeof d.params === "object" && !Array.isArray(d.params) ? d.params : null;
    if (!params) return null;
    return { runId: d.runId, yaml: d.yaml, params: params as Record<string, unknown> };
  } catch {
    return null;
  }
}

/** Gehört der Lauf `runId` zu einer früheren Fassung der Konfiguration? */
export function isStaleRun(
  basis: RunBasis | null,
  runId: string | null,
  yaml: string,
  params: Record<string, unknown>
): boolean {
  if (!basis || !runId || basis.runId !== runId) return false;
  return basis.yaml !== yaml || canonicalParams(basis.params) !== canonicalParams(params);
}
