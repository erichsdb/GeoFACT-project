// Implements: FA80 - die drei Einstiege. Nach der Anmeldung (bzw. beim Öffnen der App)
// fragt die Oberfläche zuerst, WAS der Nutzer tun will; danach führt sie ihn
// auf genau diesem Weg zu einer Konfiguration. Die Schrittfolge je Einstieg
// steht in wizardSteps.ts (FA96).

export type EntryMode = "generate" | "load" | "write";

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

/** Überschrift und Anweisung je Einstieg und Schritt der Szenario-Erstellung
 * ("input" = Frage bzw. Auswahl, "config" = Konfiguration): sagt, wo der Nutzer
 * steht und was als Nächstes zu tun ist. Die Zählung "Schritt x von y" trägt der
 * Stepper (FA96); "write" kennt nur die Konfiguration. Beim Generieren und Laden
 * hat die Konfiguration keinen festen Text: er folgt der Herkunft des Editortextes
 * (`configGuideText`, FA86). */
export const ENTRY_GUIDE: Record<EntryMode, Partial<Record<"input" | "config", { title: string; text?: string }>>> = {
  generate: {
    input: {
      title: "Frage stellen",
      text: "Beschreiben Sie, was Sie wissen möchten, und klicken Sie auf „Konfiguration generieren“. Die Konfiguration erscheint danach in Schritt 2.",
    },
    config: { title: "Konfiguration prüfen" },
  },
  load: {
    input: {
      title: "Szenario auswählen",
      text: "Wählen Sie links ein Szenario, sehen Sie sich rechts die Konfiguration an und klicken Sie auf „Laden“. Die Konfiguration erscheint danach in Schritt 2.",
    },
    config: { title: "Konfiguration prüfen" },
  },
  write: {
    config: {
      title: "Konfiguration schreiben",
      text: "Beginnen Sie mit „Vorlage“ über dem Editor, ersetzen Sie die Platzhalter und fügen Sie rechts Quellen und Operationen als Bausteine ein. Das (?) neben dem Titel erklärt den Aufbau.",
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
