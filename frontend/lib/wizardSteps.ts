// Implements: FA96 - die Schrittfolge je Einstieg. Der Stepper zeigt den
// wirklichen Weg des gewählten Einstiegs: Generieren und Laden in vier Schritten
// (Frage bzw. Auswahl, Konfiguration, Ausführung, Ergebnis), Selbstschreiben in
// dreien. Alles hier ist rein (ohne React) und läuft in den Tests unter node.

import type { EntryMode } from "./entryMode";

export type WizardStep = "input" | "config" | "execution" | "result";
// Die Schritte, die PromptStep zeigt.
export type ScenarioStep = "input" | "config";

export interface WizardStepInfo {
  id: WizardStep;
  label: string;
  hint: string;
}

const CONFIG_STEP: WizardStepInfo = {
  id: "config",
  label: "Konfiguration",
  hint: "YAML ansehen, bei Bedarf bearbeiten",
};
const EXECUTION_STEP: WizardStepInfo = {
  id: "execution",
  label: "Ausführung",
  hint: "Ausführungsgraph, Lauf starten",
};
const RESULT_STEP: WizardStepInfo = { id: "result", label: "Ergebnis", hint: "Karte, Tabelle, Statistik" };

// Dieselbe Regel wie `canResume` (entryMode.ts); hier nicht importiert, damit
// das Modul unter node ohne Auflösung von Importpfaden läuft.
function hasText(configYaml: string): boolean {
  return configYaml.trim().length > 0;
}

/** Die Schritte eines Einstiegs, der Reihe nach (drei bei "write", sonst vier). */
export function stepsForMode(mode: EntryMode): WizardStepInfo[] {
  if (mode === "generate") {
    return [{ id: "input", label: "Frage stellen", hint: "Was möchten Sie wissen?" }, CONFIG_STEP, EXECUTION_STEP, RESULT_STEP];
  }
  if (mode === "load") {
    return [
      { id: "input", label: "Szenario wählen", hint: "Beispiel oder gespeichertes Szenario" },
      CONFIG_STEP,
      EXECUTION_STEP,
      RESULT_STEP,
    ];
  }
  return [{ ...CONFIG_STEP, hint: "YAML schreiben, mit Vorlage und Katalog" }, EXECUTION_STEP, RESULT_STEP];
}

/** Erster Schritt eines Einstiegs: "input" bei generate/load, "config" bei write. */
export function firstStep(mode: EntryMode): WizardStep {
  return stepsForMode(mode)[0].id;
}

/** Ein gemerkter Schrittname; alles andere (auch das frühere "prompt") ist keiner. */
export function parseWizardStep(raw: string | null): WizardStep | null {
  return raw === "input" || raw === "config" || raw === "execution" || raw === "result" ? raw : null;
}

/** Schritt beim Start der Seite.
 * - geöffneter Link: "input" (FA72: die Auswahl wählt den verlinkten Eintrag vor, bis er geladen ist);
 * - ohne Einstieg: "input" (die Frage ist offen, der Wert zählt nicht);
 * - ohne Konfiguration: erster Schritt des Einstiegs (FA86: ohne Konfiguration kein späterer Schritt);
 * - sonst der gemerkte Schritt, wenn er zum Einstieg gehört, andernfalls der erste. */
export function initialStep(
  mode: EntryMode | null,
  configYaml: string,
  storedStep: string | null,
  fromLink: boolean
): WizardStep {
  if (fromLink || mode === null) return "input";
  if (!hasText(configYaml)) return firstStep(mode);
  const stored = parseWizardStep(storedStep);
  if (stored && stepsForMode(mode).some((s) => s.id === stored)) return stored;
  return firstStep(mode);
}

/** FA86 (7): Schritt beim Wechsel des Einstiegs an Ort und Stelle. "write" landet im
 * Editor; sonst in der Konfiguration, wenn dieser Einstieg zuletzt dort stand, es einen
 * Text gibt UND der Text auf diesem Weg entstand - ein erzeugter Text ist beim Laden
 * nicht "das geladene Szenario" -, andernfalls in der Eingabe. */
export function stepForMode(
  mode: EntryMode,
  lastSteps: Partial<Record<EntryMode, WizardStep>>,
  configYaml: string,
  // Woher der Editortext stammt ("generate" | "load" | "write", null = unbekannt).
  originKind: string | null
): ScenarioStep {
  if (mode === "write") return "config";
  const last = lastSteps[mode];
  return last !== undefined && last !== "input" && hasText(configYaml) && originKind === mode ? "config" : "input";
}

export interface WizardFacts {
  hasConfig: boolean; // Editortext nicht leer
  valid: boolean; // Validierung gültig
  hasRun: boolean; // es gibt einen (gemerkten) Lauf
  runDone: boolean; // der Lauf ist fertig
  staleRun: boolean; // der Lauf gehört zu einer früheren Fassung (FA86)
  // Woher der Editortext stammt ("generate" | "load" | "write", null = unbekannt).
  originKind: string | null;
}

/** Welche Schritte anklickbar sind. Der aktuelle Schritt wird dadurch nie entzogen
 * (das regelt der Stepper); die Tabelle gilt nur für Klicks auf andere Schritte.
 * - input: immer, wo es ihn gibt; - config: beim Schreiben immer, sonst mit Text;
 * - execution: Text und gültig; - result: Text und Lauf (FA86 (12): ein Lauf bleibt
 *   mit ungültiger Konfiguration einsehbar). */
export function reachableSteps(mode: EntryMode, f: WizardFacts): Record<WizardStep, boolean> {
  return {
    input: mode !== "write",
    config: mode === "write" || f.hasConfig,
    execution: f.hasConfig && f.valid,
    result: f.hasConfig && f.hasRun,
  };
}

/** Haken im Stepper: Schritt 1, sobald ein Text da ist, der auf DIESEM Weg entstand
 * (eine Frage gilt nicht als gestellt, weil ein geladenes Szenario im Editor steht),
 * Konfiguration wenn gültig, Ausführung bei fertigem, nicht veraltetem Lauf
 * (FA86 (10)); das Ergebnis nie. */
export function completedSteps(mode: EntryMode, f: WizardFacts): Partial<Record<WizardStep, boolean>> {
  return {
    input: f.hasConfig && f.originKind === mode,
    config: f.hasConfig && f.valid,
    execution: f.runDone && !f.staleRun,
    result: false,
  };
}

/** Ob der Stand des gemerkten Laufs vom Backend zu holen ist: Ausführung und Ergebnis
 * zeigen ihn, nach einem Neuladen gibt es aber nur noch die gemerkte Lauf-ID - egal, in
 * welchem Schritt die Seite geladen wurde. Nicht, solange ein Lauf gerade angelegt wird
 * (sein Stand kommt über den Ereignisstrom). */
export function needsRunStatus(step: WizardStep, hasRun: boolean, hasStatus: boolean, starting: boolean): boolean {
  return (step === "execution" || step === "result") && hasRun && !hasStatus && !starting;
}

/** Ob das Ende eines Laufs die Ansicht ins Ergebnis führt: nur aus der Ausführung.
 * In jedem anderen Schritt (der Nutzer bearbeitet gerade etwas) bleibt die Ansicht. */
export function runEndOpensResult(step: WizardStep): boolean {
  return step === "execution";
}

/** Text des "Zurück"-Knopfes eines Schritts; null beim ersten Schritt. */
export function backLabel(mode: EntryMode, step: WizardStep): string | null {
  if (step === "config") {
    if (mode === "generate") return "Zurück zur Frage";
    if (mode === "load") return "Zurück zur Auswahl";
    return null;
  }
  if (step === "execution") return "Zurück zur Konfiguration";
  if (step === "result") return "Zurück zur Ausführung";
  return null;
}

/** Warum ein Schritt gesperrt ist (title eines gesperrten Stepper-Knopfes). */
export function unreachableReason(mode: EntryMode, step: WizardStep): string | null {
  if (step === "config") {
    if (mode === "generate") return "Erst eine Frage stellen und eine Konfiguration erzeugen";
    if (mode === "load") return "Erst ein Szenario laden";
    return null;
  }
  if (step === "execution") return "Erst eine gültige Konfiguration";
  if (step === "result") return "Erst einen Lauf starten";
  return null;
}
