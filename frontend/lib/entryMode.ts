// FA80: die drei Einstiege in Schritt 1. Nach der Anmeldung (bzw. beim Öffnen
// der App) fragt die Oberfläche zuerst, WAS der Nutzer tun will; danach führt
// sie ihn auf genau diesem Weg zu einer Konfiguration.

export type EntryMode = "generate" | "load" | "write";

// Innerhalb von "generate" und "load": erst die Eingabe (Prompt bzw.
// Szenarioauswahl), danach die entstandene Konfiguration. "write" kennt nur
// die Konfiguration.
export type EntryView = "input" | "config";

// Die Wahl gilt je Tab-Sitzung (sessionStorage): ein Neuladen behält sie, eine
// neue Sitzung oder eine neue Anmeldung fragt wieder.
export const ENTRY_MODE_STORAGE_KEY = "geofact:entryMode";

// `short` ist die Beschriftung in der Kopfzeile, wo "Szenario" dreimal zu viel wäre.
export const ENTRY_MODES: { id: EntryMode; label: string; short: string; description: string; hint: string }[] = [
  {
    id: "generate",
    label: "Szenario generieren",
    short: "Generieren",
    description: "Sie stellen eine Frage in eigenen Worten, das Sprachmodell schreibt die Konfiguration.",
    hint: "Ohne YAML-Kenntnisse",
  },
  {
    id: "load",
    label: "Szenario laden",
    short: "Laden",
    description: "Sie öffnen ein mitgeliefertes Beispiel oder ein Szenario, das Sie gespeichert haben.",
    hint: "Schnellster Weg zu einem Ergebnis",
  },
  {
    id: "write",
    label: "Szenario selbst schreiben",
    short: "Selbst schreiben",
    description: "Sie schreiben die YAML-Konfiguration im Editor, mit Quellen- und Operationskatalog.",
    hint: "Volle Kontrolle",
  },
];

/** Überschrift und Anweisung je Einstieg und Ansicht: sagt, wo der Nutzer
 * steht und was als Nächstes zu tun ist. `step`/`of` zählen die Schritte
 * bis zur fertigen Konfiguration. */
export const ENTRY_GUIDE: Record<
  EntryMode,
  Record<EntryView, { step: number; of: number; title: string; text: string }>
> = {
  generate: {
    input: {
      step: 1,
      of: 2,
      title: "Frage stellen",
      text: "Beschreiben Sie, was Sie wissen möchten, und klicken Sie auf „Konfiguration generieren“.",
    },
    config: {
      step: 2,
      of: 2,
      title: "Konfiguration prüfen",
      text: "Das Sprachmodell hat diese Konfiguration geschrieben. Ist sie gültig, geht es mit „Weiter zur Ausführung“ weiter.",
    },
  },
  load: {
    input: {
      step: 1,
      of: 2,
      title: "Szenario auswählen",
      text: "Wählen Sie links ein Szenario, sehen Sie sich rechts die Konfiguration an und klicken Sie auf „Laden“.",
    },
    config: {
      step: 2,
      of: 2,
      title: "Konfiguration prüfen",
      text: "Das Szenario ist geladen. Ist die Konfiguration gültig, geht es mit „Weiter zur Ausführung“ weiter.",
    },
  },
  write: {
    input: {
      step: 1,
      of: 1,
      title: "Konfiguration schreiben",
      text: "Beginnen Sie mit „Vorlage“ über dem Editor, ersetzen Sie die Platzhalter und fügen Sie rechts Quellen und Operationen als Bausteine ein.",
    },
    config: {
      step: 1,
      of: 1,
      title: "Konfiguration schreiben",
      text: "Beginnen Sie mit „Vorlage“ über dem Editor, ersetzen Sie die Platzhalter und fügen Sie rechts Quellen und Operationen als Bausteine ein.",
    },
  },
};

export function parseEntryMode(raw: string | null): EntryMode | null {
  return raw === "generate" || raw === "load" || raw === "write" ? raw : null;
}

/** Einstieg beim Start: ein geöffneter Link (FA72) ist ein geladenes Szenario;
 * sonst die Wahl dieser Tab-Sitzung. Ohne sie (neue Sitzung, neue Anmeldung)
 * gibt es keinen Einstieg - die Oberfläche fragt. */
export function initialEntryMode(sessionValue: string | null, fromLink: boolean): EntryMode | null {
  if (fromLink) return "load";
  return parseEntryMode(sessionValue);
}

/** Welche Ansicht ein Einstieg zuerst zeigt. */
export function initialEntryView(mode: EntryMode | null, configYaml: string): EntryView {
  if (mode === "write") return "config";
  return configYaml.trim() ? "config" : "input";
}

/** FA86: Ansicht beim Wechsel des Einstiegs an Ort und Stelle - die zuletzt
 * gezeigte Ansicht dieses Einstiegs (Konfiguration nur, wenn es einen Text
 * gibt); beim ersten Mal die Eingabe. "write" kennt nur die Konfiguration. */
export function viewForMode(
  mode: EntryMode,
  lastViews: Partial<Record<EntryMode, EntryView>>,
  configYaml: string,
  // Woher der Editortext stammt ("generate" | "load" | "write", null = unbekannt).
  originKind: string | null
): EntryView {
  if (mode === "write") return "config";
  // Die Konfiguration gehört nur dann zu diesem Einstieg, wenn er sie hervorgebracht
  // hat: ein erzeugter Text ist beim Laden nicht "das geladene Szenario" - dort steht
  // dann die Szenarioauswahl (und umgekehrt der Prompt).
  return lastViews[mode] === "config" && canResume(configYaml) && originKind === mode ? "config" : "input";
}

/** Der Editor ist nur im Einstieg "write" oder nach Erzeugen/Laden sichtbar. */
export function showsEditor(mode: EntryMode | null, view: EntryView): boolean {
  return mode === "write" || (mode !== null && view === "config");
}

/** Ein gemerkter Editortext aus einer früheren Sitzung lässt sich fortsetzen. */
export function canResume(configYaml: string): boolean {
  return configYaml.trim().length > 0;
}

/** Nach einer Anmeldung fragt die Oberfläche wieder (kein gemerkter Einstieg). */
export function forgetEntryMode(): void {
  try {
    window.sessionStorage.removeItem(ENTRY_MODE_STORAGE_KEY);
  } catch {
    // sessionStorage nicht verfügbar: dann gab es auch keinen gemerkten Einstieg.
  }
}
