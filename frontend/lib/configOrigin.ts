// Implements: FA86 - Herkunft der Konfiguration (Variante B "Herkunft & Direktsprünge").
//
// Reine Helfer ohne React: woher der Text im Editor stammt (Prompt, geladenes
// Szenario, selbst geschrieben), ob er seither geändert wurde und was die
// Oberfläche darüber sagt. "Geändert" ist ein Textvergleich mit der Fassung
// von damals (derselbe Gedanke wie `shareRef` in FA72), keine Liste von
// Änderungswegen.

export type ConfigOrigin =
  | { kind: "generate"; prompt: string; region: string; notes: string | null }
  | { kind: "load"; id: string; source: "user" | "example"; name: string }
  | { kind: "write" };

/** Herkunft samt dem YAML-Text, wie er im Moment der Herkunft war. */
export interface OriginRecord {
  origin: ConfigOrigin;
  yaml: string;
}

// Im Browser gemerkt (localStorage), damit die Herkunft ein Neuladen überlebt.
export const CONFIG_ORIGIN_STORAGE_KEY = "geofact:configOrigin";

export function serializeOriginRecord(record: OriginRecord): string {
  return JSON.stringify(record);
}

/** Gemerkte Herkunft lesen. Unlesbar, unvollständig oder fremd geformt =
 * unbekannte Herkunft (null) - nie eine geratene. */
export function parseOriginRecord(raw: string | null): OriginRecord | null {
  if (!raw) return null;
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!data || typeof data !== "object") return null;
  const rec = data as { origin?: unknown; yaml?: unknown };
  if (typeof rec.yaml !== "string" || !rec.origin || typeof rec.origin !== "object") return null;
  const o = rec.origin as Record<string, unknown>;
  if (o.kind === "write") return { origin: { kind: "write" }, yaml: rec.yaml };
  if (o.kind === "generate") {
    if (typeof o.prompt !== "string" || !o.prompt.trim()) return null;
    return {
      origin: {
        kind: "generate",
        prompt: o.prompt,
        region: typeof o.region === "string" ? o.region : "",
        notes: typeof o.notes === "string" && o.notes ? o.notes : null,
      },
      yaml: rec.yaml,
    };
  }
  if (o.kind === "load") {
    if (typeof o.id !== "string" || !o.id) return null;
    if (o.source !== "user" && o.source !== "example") return null;
    return {
      origin: { kind: "load", id: o.id, source: o.source, name: typeof o.name === "string" && o.name ? o.name : o.id },
      yaml: rec.yaml,
    };
  }
  return null;
}

/** Wurde der Text seit der Herkunft verändert? Für "selbst geschrieben" ohne
 * Bedeutung (false): dort ist der Text von Anfang an der eigene. */
export function isEdited(record: OriginRecord | null, yaml: string): boolean {
  if (!record || record.origin.kind === "write") return false;
  return record.yaml !== yaml;
}

/** Prompt für die Anzeige kürzen (an einer Wortgrenze, mit Auslassung). */
export function truncatePrompt(prompt: string, max = 90): string {
  const flat = prompt.replace(/\s+/g, " ").trim();
  if (flat.length <= max) return flat;
  const cut = flat.slice(0, max);
  const space = cut.lastIndexOf(" ");
  return `${(space > max * 0.6 ? cut.slice(0, space) : cut).trimEnd()} …`;
}

export interface OriginView {
  kind: "generate" | "load" | "write" | "unknown";
  /** Vorspann ("Erzeugt aus:"), danach der Gegenstand. */
  prefix: string;
  /** Gegenstand der Herkunft (Prompt gekürzt, Szenarioname); null bei write/unknown. */
  subject: string | null;
  /** Voller Wortlaut für den Tooltip. */
  full: string | null;
  edited: boolean;
}

export function describeOrigin(record: OriginRecord | null, yaml: string): OriginView {
  const edited = isEdited(record, yaml);
  const o = record?.origin;
  if (!o) return { kind: "unknown", prefix: "Herkunft unbekannt", subject: null, full: null, edited: false };
  if (o.kind === "generate") {
    return {
      kind: "generate",
      prefix: "Erzeugt aus:",
      subject: `„${truncatePrompt(o.prompt)}“`,
      full: o.prompt,
      edited,
    };
  }
  if (o.kind === "load") {
    return { kind: "load", prefix: "Geladen:", subject: o.name, full: o.name, edited };
  }
  return { kind: "write", prefix: "Selbst geschrieben", subject: null, full: null, edited: false };
}

/** Anweisungstext der Konfigurationsansicht gemäß der HERKUNFT des Textes -
 * nicht gemäß dem gerade gewählten Einstieg. */
export function configGuideText(record: OriginRecord | null, yaml: string): string {
  const next = "Ist die Konfiguration gültig, geht es mit „Weiter zur Ausführung“ weiter.";
  const o = record?.origin;
  const edited = isEdited(record, yaml);
  if (!o) return `Die Herkunft dieser Konfiguration ist nicht bekannt. ${next}`;
  if (o.kind === "generate") {
    return edited
      ? `Das Sprachmodell hat diese Konfiguration geschrieben, Sie haben sie seither bearbeitet. ${next}`
      : `Das Sprachmodell hat diese Konfiguration geschrieben. ${next}`;
  }
  if (o.kind === "load") {
    return edited
      ? `Das Szenario ist geladen, Sie haben es seither bearbeitet. ${next}`
      : `Das Szenario ist geladen. ${next}`;
  }
  return `Sie haben diese Konfiguration selbst geschrieben. ${next}`;
}

/** Anmerkungen des Sprachmodells - nur, solange die Herkunft eine Erzeugung ist. */
export function originNotes(record: OriginRecord | null): string | null {
  return record?.origin.kind === "generate" ? record.origin.notes : null;
}

/** Werte, mit denen "Prompt anpassen" Eingabe und Region füllt; null ohne Erzeugung. */
export function promptValues(record: OriginRecord | null): { prompt: string; region: string } | null {
  return record?.origin.kind === "generate"
    ? { prompt: record.origin.prompt, region: record.origin.region }
    : null;
}
