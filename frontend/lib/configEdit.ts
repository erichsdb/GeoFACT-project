// Text-Manipulation der Szenario-YAML im Editor (page.tsx hält configYaml
// als reinen String, siehe ConfigEditor.tsx). insertLayer/insertStep fügen
// jeweils ein neues Element in die layers:- bzw. steps:-Liste ein, ohne den
// Rest der Konfiguration zu zerstören (Kommentare etc.), indem nur die
// betroffene Liste per js-yaml geparst/neu geschrieben wird.
//
// YAML-Struktur (siehe examples/*.yaml, src/geofact/core/scenario.py):
//   scenario: { name, description, region }
//   layers: [{ id, source: osm|file|table, ... }]
//   steps:  [{ id, op, inputs: {port: layerOrStepId}, params: {...} }]
//   output: [...]

import { load as loadYaml, dump as dumpYaml } from "js-yaml";
import type { CatalogSource, OperationEntry } from "./types";

/** Findet einen bislang ungenutzten Bezeichner "<base>", "<base>_2", ... */
function uniqueId(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base;
  let i = 2;
  while (taken.has(`${base}_${i}`)) i++;
  return `${base}_${i}`;
}

function collectIds(doc: Record<string, unknown> | null | undefined, key: string): Set<string> {
  const ids = new Set<string>();
  const list = doc?.[key];
  if (Array.isArray(list)) {
    for (const entry of list) {
      if (entry && typeof entry === "object" && typeof (entry as { id?: unknown }).id === "string") {
        ids.add((entry as { id: string }).id);
      }
    }
  }
  return ids;
}

/** Parst die Konfiguration tolerant; leerer/ungültiger Text -> leeres Objekt. */
function parseDoc(yamlText: string): Record<string, unknown> {
  if (!yamlText.trim()) return {};
  try {
    const parsed = loadYaml(yamlText);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

/** Hängt einen neuen Eintrag an die YAML-Liste unter `key` an (Text-Ebene,
 * damit vorhandene Formatierung/Kommentare erhalten bleiben) oder legt den
 * Schlüssel neu an, falls er fehlt. */
function appendListEntry(yamlText: string, key: string, entry: Record<string, unknown>): string {
  const entryYaml = dumpYaml([entry], { lineWidth: -1 })
    .trimEnd()
    .split("\n")
    .map((line) => (line.startsWith("- ") ? line : `  ${line}`))
    .join("\n");

  const listHeaderRe = new RegExp(`^${key}:\\s*$`, "m");
  const inlineEmptyRe = new RegExp(`^${key}:\\s*(\\[\\s*\\]|null)?\\s*$`, "m");

  if (listHeaderRe.test(yamlText)) {
    // Schlüssel existiert bereits als Blockliste: nach dem letzten Element
    // (bzw. direkt nach dem Header, falls leer) einfügen.
    const lines = yamlText.split("\n");
    const headerIdx = lines.findIndex((l) => listHeaderRe.test(l));
    let insertAt = headerIdx + 1;
    for (let i = headerIdx + 1; i < lines.length; i++) {
      const line = lines[i];
      if (line.trim() === "") continue;
      // Ende der Liste: eine Zeile ohne Einrückung, die einen neuen
      // Top-Level-Schlüssel beginnt.
      if (/^\S/.test(line)) break;
      insertAt = i + 1;
    }
    lines.splice(insertAt, 0, entryYaml);
    return lines.join("\n");
  }

  if (inlineEmptyRe.test(yamlText)) {
    // Schlüssel existiert, aber leer/inline (z. B. "layers:" oder "layers: []").
    return yamlText.replace(inlineEmptyRe, `${key}:\n${entryYaml}`);
  }

  // Schlüssel fehlt komplett: am Ende anhängen.
  const sep = yamlText.trim() ? (yamlText.endsWith("\n") ? "" : "\n") + "\n" : "";
  return `${yamlText}${sep}${key}:\n${entryYaml}\n`;
}

/** Fügt eine Datenquelle als neuen Eintrag in die layers-Liste ein. */
export function insertLayer(yamlText: string, source: CatalogSource): string {
  const doc = parseDoc(yamlText);
  const takenLayerIds = collectIds(doc, "layers");
  const takenStepIds = collectIds(doc, "steps");

  const template = source.layer_template ?? {};
  const baseId =
    typeof template.id === "string" && template.id.trim() ? template.id.trim() : source.id;
  const id = uniqueId(baseId, new Set([...takenLayerIds, ...takenStepIds]));

  const layer = { ...template, id };
  return appendListEntry(yamlText, "layers", layer);
}

/** Fügt ein Schritt-Skelett für die gegebene Operation in die steps-Liste
 * ein. Eingaenge/Parameter werden als Platzhalter gesetzt, die der Nutzer
 * im Editor ausfüllt. */
export function insertStep(yamlText: string, op: OperationEntry): string {
  const doc = parseDoc(yamlText);
  const takenLayerIds = collectIds(doc, "layers");
  const takenStepIds = collectIds(doc, "steps");
  const taken = new Set([...takenLayerIds, ...takenStepIds]);

  const id = uniqueId(op.op, taken);

  const inputs: Record<string, string> = {};
  for (const port of Object.keys(op.input_ports)) {
    inputs[port] = "TODO";
  }

  const params: Record<string, unknown> = {};
  for (const [name, spec] of Object.entries(op.params)) {
    if (spec.required) {
      params[name] = spec.default !== undefined ? spec.default : defaultForType(spec.type, spec.choices);
    }
  }

  const step: Record<string, unknown> = { id, op: op.op, inputs };
  if (Object.keys(params).length > 0) step.params = params;

  return appendListEntry(yamlText, "steps", step);
}

function defaultForType(type: string, choices?: string[]): unknown {
  switch (type) {
    case "number":
    case "integer":
      return 0;
    case "boolean":
      return false;
    case "enum":
      return choices?.[0] ?? "TODO";
    case "array":
      return [];
    case "object":
      return {};
    default:
      return "TODO";
  }
}
