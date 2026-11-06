// Implements: FA97 (Hilfe zur YAML-Konfiguration am Editor).
//
// Inhalt des (?)-Hilfefensters als reine Daten: eine Kurzfassung von
// docs/konfiguration_schreiben.md. Jeder Ausschnitt ist gültiges YAML und
// nennt nur Felder des Schemas sowie registrierte Quellarten, Operationen und
// Ausgabeformate; die Abschnitte "region", "parameters", "layers", "steps"
// und "output" ergeben zusammen ein Szenario, das `validate` annimmt (nur
// OpenStreetMap, keine lokalen Dateien). Der Test dazu steht in
// tests/unit/test_fa97_yaml_help.py - er prüft Namen und Gültigkeit gegen den Kern.
//
// Kein React und keine Zwischenablage hier: die Darstellung macht
// components/yaml-help.tsx. In Text und Aufzählungen steht `code` in Backticks.
// Die Ausschnitte enden mit einem Zeilenumbruch; "\${...}" ist der Parameter-
// Platzhalter der YAML (im Template-Literal maskiert).

export interface HelpSection {
  id: string;
  title: string;
  /** Ein Absatz; `code` in Backticks wird als Festbreitenschrift gezeigt. */
  text: string;
  bullets?: string[];
  /** Kopierbarer Ausschnitt, gültiges YAML. */
  yaml?: string;
  /** Namen, auf die sich der Text beruft (werden gegen die Registry geprüft). */
  mentions?: { operations?: string[]; sources?: string[]; outputs?: string[] };
}

const GRUNDGERUEST = `scenario:
  name: "Parks in Chemnitz"
  region: "Chemnitz, Deutschland"

layers:
  - id: parks
    source: osm
    tags: { leisure: park }

steps:
  - id: umfeld
    op: buffer
    inputs: { geometry: parks }
    params: { radius_km: 0.3 }

output:
  - type: map
    source: umfeld
`;

const REGION = `scenario:
  name: "Parks in Chemnitz"
  description: "Welche Kilometerzellen haben die meisten Parks?"
  region: "Chemnitz, Deutschland"      # oder BBox West,Süd,Ost,Nord: "12.8,50.75,13.0,50.9"
  crs: auto                            # Arbeits-CRS; auto = UTM-Zone der Region, sonst z. B. "EPSG:25833"
  output_license:                      # optional: Lizenz Ihres Ergebnisses
    name: "CC-BY-4.0"
    attribution: "Eigene Auswertung"
    url: "https://creativecommons.org/licenses/by/4.0/"
`;

const PARAMETERS = `parameters:
  zelle_km:
    type: float
    default: 1.0
    description: "Kantenlänge der Rasterzellen in Kilometern"
  top:
    type: integer
    default: 5
    description: "Anzahl der Zellen in der Rangliste"
`;

const LAYERS = `layers:
  - id: parks
    source: osm
    tags: { leisure: park }            # ein Tag-Paar oder eine Liste davon
  - id: gebiet
    source: region                     # die Grenze der Region als Fläche
`;

const STEPS = `steps:
  - id: raster
    op: hex_grid
    inputs: { region: gebiet }                   # Eingang <- Layer-id
    params: { cell_km: "\${zelle_km}" }           # Wert aus parameters
  - id: parks_je_zelle
    op: aggregate
    inputs: { features: parks, zones: raster }   # zones <- Ergebnis des Schritts "raster"
    params: { statistic: count, predicate: intersects }
  - id: rangliste
    op: top_n
    inputs: { features: parks_je_zelle }
    params: { by: count, n: "\${top}", rank_field: rang }
`;

const OUTPUT = `output:
  - type: map
    source: rangliste
    color_field: count                 # Spalte, nach der die Karte färbt
  - type: geojson
    source: rangliste
    path: "rangliste.geojson"          # Dateiname im Ausgabeordner
  - type: csv
    source: parks_je_zelle
`;

/** Die Abschnitte, deren Ausschnitte (in dieser Reihenfolge) das vollständige Beispiel bilden. */
export const YAML_HELP_EXAMPLE_PARTS = ["region", "parameters", "layers", "steps", "output"] as const;

export const YAML_HELP_SECTIONS: HelpSection[] = [
  {
    id: "grundgeruest",
    title: "Grundgerüst",
    text:
      "Eine Konfiguration hat vier Abschnitte: `scenario` (Titel und Region), `layers` (Daten), " +
      "`steps` (Analyse) und `output` (Ergebnisse); `parameters` ist optional. Die Reihenfolge der " +
      "Schritte in der Datei spielt keine Rolle – GeoFACT leitet sie aus den Verweisen ab und lädt " +
      "jeden Layer erst, wenn ein Schritt ihn braucht. Dieses Minimalbeispiel ist bereits lauffähig.",
    yaml: GRUNDGERUEST,
    mentions: { operations: ["buffer"], sources: ["osm"], outputs: ["map"] },
  },
  {
    id: "region",
    title: "Region und Koordinatensystem",
    text:
      "`region` ist ein Ortsname oder eine BBox in WGS 84 (West, Süd, Ost, Nord) und legt das Gebiet " +
      "der Analyse fest. `crs` ist das Arbeits-Koordinatensystem: `auto` (Standard) nimmt die " +
      "UTM-Zone der Region, ein projizierter EPSG-Code in Metern erzwingt eines. `output_license` ist " +
      "die Lizenz, unter die Sie Ihr Ergebnis stellen; die Lizenzen der Quellen führt GeoFACT selbst mit.",
    yaml: REGION,
  },
  {
    id: "layers",
    title: "Layer und Quellen",
    text:
      "Jeder Layer hat eine eindeutige `id` und eine Quellart in `source`: `osm` (Objekte nach Tags), " +
      "`region` (die Grenze der Region als Fläche), `file` (lokale Datei oder Upload, " +
      "`data_type: vector` oder `raster`), `table` (Tabelle ohne Geometrie), dazu `wfs`, `ckan`, " +
      "`rest`, `stac`, `gtfs`, `geocode` und `points`. Im Einstieg „Szenario selbst schreiben“ fügt ein " +
      "Klick im Reiter „Quellen“ oder „APIs“ neben dem Editor einen fertigen Eintrag ein.",
    yaml: LAYERS,
    mentions: { sources: ["osm", "region", "file", "table", "wfs", "ckan", "rest", "stac", "gtfs", "geocode", "points"] },
  },
  {
    id: "steps",
    title: "Schritte: der Analysegraph",
    text:
      "Ein Schritt wendet eine Operation (`op`) an. `inputs` verbindet ihre Eingänge mit der `id` " +
      "eines Layers oder eines anderen Schritts – daraus entsteht der Ausführungsgraph im Schritt " +
      "„Ausführung“. Die Typen müssen passen: ein Vektor-Eingang nimmt kein Raster. Welche Eingänge " +
      "und Parameter eine Operation hat, zeigt im Einstieg „Szenario selbst schreiben“ der Reiter " +
      "„Operationen“ neben dem Editor; ein Klick fügt den Schritt mit allen Feldern ein. Häufig gebraucht: `filter`, `buffer`, `spatial_join`, `aggregate`, " +
      "`zonal_stats`, `classify`, `top_n`, `calculate_field`.",
    yaml: STEPS,
    mentions: {
      operations: [
        "hex_grid",
        "aggregate",
        "top_n",
        "filter",
        "buffer",
        "spatial_join",
        "zonal_stats",
        "classify",
        "calculate_field",
      ],
    },
  },
  {
    id: "output",
    title: "Ausgaben",
    text:
      "Jeder Eintrag schreibt das Ergebnis eines Schritts oder einen Layer: `map` (interaktive Karte; " +
      "`color_field` färbt nach einer Spalte), `geojson`, `csv`, `geotiff` (Raster) oder `rdf` " +
      "(GeoSPARQL/Turtle mit Herkunft). `path` ist der Dateiname im Ausgabeordner, sonst vergibt " +
      "GeoFACT einen. Die Dateien eines abgeschlossenen Laufs stehen im Schritt „Ergebnis“ unter „Ausgaben“.",
    yaml: OUTPUT,
    mentions: { outputs: ["map", "geojson", "csv", "geotiff", "rdf"] },
  },
  {
    id: "parameters",
    title: "Parameter",
    text:
      "Werte, die sich je Lauf ändern sollen, stehen in `parameters` mit Typ (`string`, `integer`, " +
      "`float`, `boolean`, `list`, `mapping`), Standardwert und Beschreibung und werden mit `${name}` " +
      "eingesetzt. Die Oberfläche zeigt sie als Formular in den Schritten „Konfiguration“ und „Ausführung“; ein Parameter ohne " +
      "`default` muss dort gesetzt werden, sonst ist die Konfiguration ungültig.",
    yaml: PARAMETERS,
  },
  {
    id: "fehler",
    title: "Häufige Fehler",
    text: "Die Prüfung unter dem Editor nennt jeden Fehler mit seiner Stelle. Am häufigsten:",
    bullets: [
      "Einrückung: Listen beginnen mit `- `, zwei Leerzeichen je Ebene, keine Tabulatoren.",
      "Verweise: `inputs` und `source` nennen die `id` eines Layers oder Schritts – genau so geschrieben.",
      "Typen: ein Vektor-Eingang (z. B. `features`) nimmt kein Raster; `zonal_stats` erwartet in `values` ein Raster.",
      "Bedingungen in `filter` sind Python-Syntax: `==`, `!=`, `and`, `or` – nicht SQL.",
      "Pflichtparameter fehlen: der Reiter „Operationen“ (Einstieg „Szenario selbst schreiben“) fügt alle Felder einer Operation ein.",
      "Ein Kreis (Schritt A braucht B, B braucht A) ist kein Graph und wird abgelehnt.",
      "Platzhalter `<…>` der Vorlage sind noch nicht ersetzt.",
    ],
    mentions: { operations: ["zonal_stats", "filter"] },
  },
];

const byId = (id: string): HelpSection => {
  const section = YAML_HELP_SECTIONS.find((s) => s.id === id);
  if (!section?.yaml) throw new Error(`yamlHelp: Abschnitt ${id} ohne Ausschnitt`);
  return section;
};

/** Die Ausschnitte der Teilabschnitte, zu einem Szenario zusammengesetzt. */
export const YAML_HELP_EXAMPLE: string = YAML_HELP_EXAMPLE_PARTS.map((id) => byId(id).yaml).join("\n");

YAML_HELP_SECTIONS.push({
  id: "beispiel",
  title: "Alles zusammen",
  text:
    "Die Abschnitte oben ergeben zusammen dieses lauffähige Szenario (nur OpenStreetMap, keine " +
    "lokalen Dateien). Kopieren, im Einstieg „Szenario selbst schreiben“ in den leeren Editor " +
    "einfügen, mit „Weiter zur Ausführung“ zum Ausführungsgraphen wechseln und dort „Ausführen“ wählen.",
  yaml: YAML_HELP_EXAMPLE,
  mentions: { operations: ["hex_grid", "aggregate", "top_n"], sources: ["osm", "region"], outputs: ["map", "geojson", "csv"] },
});

export const YAML_HELP_POINTERS = {
  catalogs:
    "Die Reiter „Quellen“ und „APIs“ fügen einen Layer ein, „Operationen“ einen Schritt mit allen Feldern – im Einstieg „Szenario selbst schreiben“ rechts neben dem Editor.",
  template: "„Vorlage“ über dem Editor füllt einen leeren Editor mit allen Abschnitten und Platzhaltern.",
  guide: "Ausführliche Anleitung: docs/konfiguration_schreiben.md im Repository.",
  apiDocs: "API-Dokumentation (OpenAPI) des Backends",
};

/** Adresse der API-Dokumentation des Backends (FA95). */
export function apiDocsUrl(apiBase: string): string {
  return `${apiBase.replace(/\/$/, "")}/api/docs`;
}
