// Hilfsfunktionen, um ein NodeResult (Vektor/Graph/Raster) in flache
// Zeilen für Tabelle/Diagramm/Statistik umzuwandeln. Vektor-Ergebnisse
// liefern GeoJSON-Feature-Properties, Graph-Ergebnisse die Knotenattribute
// (inkl. lon/lat); Raster hat keine Zeilen (eigene Stats-Ansicht in
// StatsView über describe.stats).

import type { NodeResult } from "./types";

export type Row = Record<string, unknown>;

export function extractRows(result: NodeResult | null): Row[] {
  if (!result) return [];
  const kind = result.describe.kind;
  if (kind === "vector" && result.geojson) {
    return result.geojson.features.map((f) => ({ ...(f.properties ?? {}) }));
  }
  if (kind === "graph" && result.graph) {
    return result.graph.nodes.map((n) => ({ ...n }));
  }
  return [];
}

export function columnsOf(rows: Row[]): string[] {
  const cols = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) cols.add(key);
  }
  return Array.from(cols).sort();
}

export function numericFields(rows: Row[]): string[] {
  const cols = columnsOf(rows);
  return cols.filter((c) => rows.some((r) => typeof r[c] === "number" && Number.isFinite(r[c] as number)));
}

/** Bestes Feld für eine Label-Spalte (Tabellenbeschriftung in ChartView). */
export function labelField(rows: Row[]): string {
  const cols = columnsOf(rows);
  const preferred = ["name", "label", "id"];
  for (const p of preferred) {
    if (cols.includes(p)) return p;
  }
  // Erste nicht-numerische Spalte als Fallback.
  const nonNumeric = cols.find((c) => rows.some((r) => typeof r[c] === "string"));
  return nonNumeric ?? cols[0] ?? "id";
}

export interface HistogramBin {
  x0: number;
  x1: number;
  count: number;
}

export function histogram(rows: Row[], field: string, binCount: number): HistogramBin[] {
  const values = rows
    .map((r) => r[field])
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v));

  if (!values.length) {
    return Array.from({ length: binCount }, (_, i) => ({ x0: i, x1: i + 1, count: 0 }));
  }

  // Schleife statt Math.min(...values)/Math.max(...values): der Spread
  // übergibt jeden Wert als eigenes Argument und sprengt bei großen
  // Ergebnissen (seit "alle laden" realistisch 100k+) den Argument-Stack
  // der Engine. Gleiches Muster wie minMax() in map-view.tsx.
  let min = Infinity;
  let max = -Infinity;
  for (const v of values) {
    if (v < min) min = v;
    if (v > max) max = v;
  }
  const span = max - min || 1;
  const bins: HistogramBin[] = Array.from({ length: binCount }, (_, i) => ({
    x0: min + (span * i) / binCount,
    x1: min + (span * (i + 1)) / binCount,
    count: 0,
  }));

  for (const v of values) {
    let idx = Math.floor(((v - min) / span) * binCount);
    if (idx >= binCount) idx = binCount - 1;
    if (idx < 0) idx = 0;
    bins[idx].count += 1;
  }
  return bins;
}

export interface RankedEntry {
  label: string;
  value: number;
}

export function topN(rows: Row[], field: string, label: string, n: number): RankedEntry[] {
  const entries: RankedEntry[] = rows
    .filter((r) => typeof r[field] === "number" && Number.isFinite(r[field] as number))
    .map((r, i) => ({
      label: r[label] != null ? String(r[label]) : `#${i + 1}`,
      value: r[field] as number,
    }));
  entries.sort((a, b) => b.value - a.value);
  return entries.slice(0, n);
}

export interface FieldStats {
  count: number;
  min: number;
  max: number;
  mean: number;
  median: number;
  stddev: number;
}

export function computeStats(rows: Row[], field: string): FieldStats | null {
  const values = rows
    .map((r) => r[field])
    .filter((v): v is number => typeof v === "number" && Number.isFinite(v))
    .sort((a, b) => a - b);

  if (!values.length) return null;

  const count = values.length;
  const min = values[0];
  const max = values[count - 1];
  const mean = values.reduce((sum, v) => sum + v, 0) / count;
  const mid = Math.floor(count / 2);
  const median = count % 2 === 0 ? (values[mid - 1] + values[mid]) / 2 : values[mid];
  const variance = values.reduce((sum, v) => sum + (v - mean) ** 2, 0) / count;
  const stddev = Math.sqrt(variance);

  return { count, min, max, mean, median, stddev };
}
