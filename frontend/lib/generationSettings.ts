// FA81-FA83: Einstellungen, die der Nutzer im Browser setzt - Fristen und
// Probelauf der Erzeugung (FA81), OSM-Quelle (FA82), Sprachmodell (FA83). Die
// Defaults sind die Umgebungsvariablen des Servers (/api/meta); gespeichert und
// gesendet wird nur, was der Nutzer davon abweichend gewählt hat.

/** Überschreibungen - ein fehlendes Feld heißt "Wert des Servers". */
export interface GenerationOptions {
  llm_deadline_s?: number;
  trial_run?: boolean;
  trial_run_s?: number;
  osm_backend?: string;
  llm_choice?: string;
}

/** Die Werte des Servers (Umgebungsvariablen). */
export interface GenerationDefaults {
  llm_deadline_s: number;
  trial_run: boolean;
  trial_run_s: number;
  osm_backend: string;
  llm_choice: string;
}

/** Obergrenzen der Fristen in Sekunden. */
export interface GenerationLimits {
  llm_deadline_s: number;
  trial_run_s: number;
}

export interface LlmChoice {
  id: string;
  model: string;
  providers: string[];
}

/** Was der Server zur Wahl stellt. Mit nur einem Eintrag gibt es nichts zu wählen. */
export interface GenerationChoices {
  osm_backends: string[];
  llm_choices: LlmChoice[];
}

export interface ServerSettings {
  defaults: GenerationDefaults;
  limits: GenerationLimits;
  choices: GenerationChoices;
}

/** Der Ausschnitt von /api/meta, aus dem die Einstellungen entstehen. */
export interface SettingsMeta {
  osm_backend: string;
  llm_model: string;
  generation_defaults?: { llm_deadline_s: number; trial_run: boolean; trial_run_s: number };
  generation_limits?: GenerationLimits;
  osm_backends?: { default: string; available: string[] };
  llm_choices?: LlmChoice[];
}

export const GENERATION_SETTINGS_STORAGE_KEY = "geofact:generationSettings";

/** Werte, Grenzen und Auswahl des Servers; null, solange /api/meta fehlt oder
 * von einem Backend ohne diese Angaben stammt. Fehlt nur die Auswahl (älteres
 * Backend), gibt es genau den einen Wert des Servers. */
export function serverSettings(meta: SettingsMeta | null | undefined): ServerSettings | null {
  if (!meta?.generation_defaults || !meta.generation_limits) return null;
  const osm = meta.osm_backends ?? { default: meta.osm_backend, available: [meta.osm_backend] };
  const llm =
    meta.llm_choices && meta.llm_choices.length > 0
      ? meta.llm_choices
      : [{ id: meta.llm_model, model: meta.llm_model, providers: [] }];
  return {
    defaults: { ...meta.generation_defaults, osm_backend: osm.default, llm_choice: llm[0].id },
    limits: meta.generation_limits,
    choices: { osm_backends: osm.available, llm_choices: llm },
  };
}

function validSeconds(value: unknown, max: number | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 && (max === undefined || value <= max);
}

function validChoice(value: unknown, allowed: string[] | undefined): value is string {
  return typeof value === "string" && value !== "" && (allowed === undefined || allowed.includes(value));
}

/** Gespeicherte Überschreibungen lesen. Unlesbares, Werte außerhalb der
 * Grenzen und eine Wahl, die der Server nicht (mehr) anbietet, fallen weg
 * (dann gilt der Wert des Servers) - nie ein Wert, den das Backend ablehnen
 * würde. Ohne `server` (Meta noch nicht geladen) bleibt vorerst alles stehen,
 * was die richtige Form hat. */
export function parseStoredOptions(raw: string | null, server?: ServerSettings | null): GenerationOptions {
  if (!raw) return {};
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return {};
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return {};
  const source = data as Record<string, unknown>;
  const limits = server?.limits;
  const choices = server?.choices;
  const result: GenerationOptions = {};
  if (validSeconds(source.llm_deadline_s, limits?.llm_deadline_s)) result.llm_deadline_s = source.llm_deadline_s;
  if (typeof source.trial_run === "boolean") result.trial_run = source.trial_run;
  if (validSeconds(source.trial_run_s, limits?.trial_run_s)) result.trial_run_s = source.trial_run_s;
  if (validChoice(source.osm_backend, choices?.osm_backends)) result.osm_backend = source.osm_backend;
  if (validChoice(source.llm_choice, choices?.llm_choices.map((c) => c.id))) result.llm_choice = source.llm_choice;
  return result;
}

/** Was für die nächste Anfrage gilt: Server-Werte, überschrieben vom Nutzer. */
export function effectiveOptions(defaults: GenerationDefaults, overrides: GenerationOptions): GenerationDefaults {
  return {
    llm_deadline_s: overrides.llm_deadline_s ?? defaults.llm_deadline_s,
    trial_run: overrides.trial_run ?? defaults.trial_run,
    trial_run_s: overrides.trial_run_s ?? defaults.trial_run_s,
    osm_backend: overrides.osm_backend ?? defaults.osm_backend,
    llm_choice: overrides.llm_choice ?? defaults.llm_choice,
  };
}

/** Nur die Abweichungen vom Server-Wert behalten: ein Feld auf dem Default
 * folgt einer späteren Änderung der Umgebungsvariable. */
export function overridesFrom(values: GenerationDefaults, defaults: GenerationDefaults): GenerationOptions {
  const result: GenerationOptions = {};
  if (values.llm_deadline_s !== defaults.llm_deadline_s) result.llm_deadline_s = values.llm_deadline_s;
  if (values.trial_run !== defaults.trial_run) result.trial_run = values.trial_run;
  if (values.trial_run_s !== defaults.trial_run_s) result.trial_run_s = values.trial_run_s;
  if (values.osm_backend !== defaults.osm_backend) result.osm_backend = values.osm_backend;
  if (values.llm_choice !== defaults.llm_choice) result.llm_choice = values.llm_choice;
  return result;
}

export function hasOverrides(overrides: GenerationOptions): boolean {
  return Object.keys(overrides).length > 0;
}

/** Der Teil der Einstellungen, der einen Lauf betrifft (FA82): die OSM-Quelle. */
export function runOptions(overrides: GenerationOptions): { osm_backend?: string } {
  return overrides.osm_backend !== undefined ? { osm_backend: overrides.osm_backend } : {};
}

/** Anzeige eines Sprachmodells: Modell, dahinter die gebundenen Anbieter. */
export function llmChoiceLabel(choice: LlmChoice): string {
  return choice.providers.length > 0 ? `${choice.model} · ${choice.providers.join(", ")}` : choice.model;
}

/** Eine Frist aus dem Eingabefeld: Zahl > 0 bis zur Obergrenze, sonst eine
 * Meldung für das Feld (Komma als Dezimaltrenner ist erlaubt). */
export function parseSeconds(
  text: string,
  max: number
): { ok: true; value: number } | { ok: false; error: string } {
  const trimmed = text.trim().replace(",", ".");
  if (!trimmed) return { ok: false, error: "Bitte eine Zahl in Sekunden angeben." };
  const value = Number(trimmed);
  if (!Number.isFinite(value)) return { ok: false, error: "Das ist keine Zahl." };
  if (value <= 0) return { ok: false, error: "Die Frist muss größer als 0 sein." };
  if (value > max) return { ok: false, error: `Höchstens ${max} Sekunden.` };
  return { ok: true, value };
}

/** Kurzfassung der Abweichungen für den Knopf im Kopf. */
export function describeOverrides(overrides: GenerationOptions): string {
  const parts: string[] = [];
  if (overrides.osm_backend !== undefined) parts.push(`OSM-Quelle ${overrides.osm_backend}`);
  if (overrides.llm_choice !== undefined) parts.push(`Sprachmodell ${overrides.llm_choice}`);
  if (overrides.llm_deadline_s !== undefined) parts.push(`Frist je Modellanfrage ${overrides.llm_deadline_s} s`);
  if (overrides.trial_run !== undefined) parts.push(overrides.trial_run ? "Probelauf an" : "Probelauf aus");
  if (overrides.trial_run_s !== undefined) parts.push(`Frist des Probelaufs ${overrides.trial_run_s} s`);
  return parts.join(", ");
}
