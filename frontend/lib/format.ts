// Zahlen in der Oberfläche einheitlich deutsch formatieren (R3, Browsercheck
// 03.10.): vorher stand "381.435 km²" (Tausenderpunkt aus toLocaleString)
// neben "0.295" (Dezimalpunkt aus toFixed) auf demselben Bildschirm.
//
// Ganze Zahlen ohne Tausendertrennung, wenn sie Kennungen sein können
// (osm_id, Tabellenzellen) - "123.456.789" wäre dort falsch.

const LOCALE = "de-DE";

/** Kommazahl mit höchstens `digits` Nachkommastellen, deutsch gruppiert. */
export function formatDecimal(value: number, digits: number): string {
  if (!Number.isFinite(value)) return "–";
  return value.toLocaleString(LOCALE, { maximumFractionDigits: digits });
}

/** Zaehler/Menge (ganze Zahl), deutsch gruppiert: 12.345. */
export function formatCount(value: number): string {
  if (!Number.isFinite(value)) return "–";
  return Math.round(value).toLocaleString(LOCALE);
}

/** Wert einer Attributzelle: ganze Zahlen roh (Kennungen), sonst wie formatDecimal. */
export function formatCell(value: number, digits: number): string {
  if (!Number.isFinite(value)) return "–";
  return Number.isInteger(value) ? String(value) : formatDecimal(value, digits);
}
