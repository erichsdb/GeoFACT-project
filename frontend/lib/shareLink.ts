// Implements: FA72 (Szenario per URL teilbar).
//
// Reine Hilfsfunktionen für den Link `?scenario=example:<id>` bzw.
// `?scenario=user:<id>`. Der Link enthält bewusst nur die Szenario-Referenz:
// nie ein Token (FA24) und nie eine Lauf-ID - Läufe sind nicht teilbar (FA54).

export const SCENARIO_PARAM = "scenario";

export interface ScenarioRef {
  id: string;
  source: "user" | "example";
}

export type ParsedScenarioParam =
  | { kind: "none" }
  | { kind: "ok"; ref: ScenarioRef }
  | { kind: "invalid"; raw: string };

/** Liest `?scenario=` aus einem Query-String. Ein vorhandener, aber nicht
 * lesbarer Wert (fehlendes Präfix, leere ID) ist `invalid` und wird vom
 * Aufrufer gemeldet - kein stiller Rückfall. */
export function parseScenarioParam(search: string): ParsedScenarioParam {
  const raw = new URLSearchParams(search).get(SCENARIO_PARAM);
  if (raw === null) return { kind: "none" };
  const cut = raw.indexOf(":");
  const prefix = cut >= 0 ? raw.slice(0, cut) : "";
  const id = cut >= 0 ? raw.slice(cut + 1) : "";
  if ((prefix !== "example" && prefix !== "user") || !id) return { kind: "invalid", raw };
  return { kind: "ok", ref: { id, source: prefix } };
}

/** Query-String (mit führendem `?`) für eine Referenz; `:` und `/` bleiben
 * lesbar, alles andere wird je Segment kodiert. */
export function buildScenarioSearch(ref: ScenarioRef): string {
  const id = ref.id.split("/").map(encodeURIComponent).join("/");
  return `?${SCENARIO_PARAM}=${ref.source}:${id}`;
}

/** Absoluter Link zur aktuellen Seite (ohne Hash und fremde Parameter). */
export function buildShareUrl(origin: string, pathname: string, ref: ScenarioRef): string {
  return `${origin}${pathname}${buildScenarioSearch(ref)}`;
}

/** Schlüssel einer Referenz (`example:<id>` / `user:<id>`), wie im Parameter. */
export function refKey(ref: ScenarioRef): string {
  return `${ref.source}:${ref.id}`;
}

/** `search` ohne den Parameter `scenario`, andere Parameter bleiben unverändert
 * (Reihenfolge und Schreibweise); mit `ref` wird der Parameter angehängt.
 * Ergebnis: "" oder ein String mit führendem `?`. */
export function withScenarioParam(search: string, ref: ScenarioRef | null): string {
  const kept = search
    .replace(/^\?/, "")
    .split("&")
    .filter((seg) => {
      if (!seg) return false;
      const key = seg.split("=")[0];
      try {
        return decodeURIComponent(key.replace(/\+/g, " ")) !== SCENARIO_PARAM;
      } catch {
        return true;
      }
    });
  if (ref) kept.push(buildScenarioSearch(ref).slice(1));
  return kept.length ? `?${kept.join("&")}` : "";
}

/** Relative URL für `history.replaceState`: Pfad, fremde Parameter und Hash
 * bleiben erhalten, nur `scenario` wird gesetzt (`ref`) oder entfernt (`null`). */
export function urlWithScenario(
  loc: { pathname: string; search: string; hash: string },
  ref: ScenarioRef | null
): string {
  return `${loc.pathname}${withScenarioParam(loc.search, ref)}${loc.hash}`;
}

/** Wie die Seite mit dem Parameter umgeht:
 * - `none`: kein Parameter;
 * - `invalid`: Parameter vorhanden, aber unlesbar (Fehlermeldung, entfernen);
 * - `restore`: Neuladen des eigenen Tabs - die Adresse hat die App selbst
 *   gesetzt (`ownKey` = zuletzt geschriebene Referenz); gemerktes YAML, Schritt
 *   und Lauf aus localStorage bleiben gültig, nichts wird neu geladen;
 * - `open`: ein geöffneter Link - hat Vorrang vor localStorage, lädt das Szenario. */
export type LinkMode = "none" | "invalid" | "restore" | "open";

export function planLinkOpen(parsed: ParsedScenarioParam, ownKey: string | null): LinkMode {
  if (parsed.kind === "none") return "none";
  if (parsed.kind === "invalid") return "invalid";
  return ownKey !== null && ownKey === refKey(parsed.ref) ? "restore" : "open";
}

/** Nach fehlgeschlagenem Laden: der Parameter fällt nur bei unbekannter ID
 * (404) weg. Bei 401 (Login folgt, der Link muss ihn überleben), Netzwerk- oder
 * Serverfehler bleibt er, die ID ist ja nicht widerlegt. */
export function linkErrorAction(isNotFound: boolean): "remove" | "keep" {
  return isNotFound ? "remove" : "keep";
}
