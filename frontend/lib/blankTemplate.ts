// FA91: Blanko-Vorlage für den Einstieg "Szenario selbst schreiben". Sie zeigt
// die vier Abschnitte einer Konfiguration und die Felder, die sich ausfüllen
// lassen; Platzhalter stehen in <spitzen Klammern>. Die Vorlage ist gültiges
// YAML, aber bewusst kein gültiges Szenario - die Prüfung nennt, was noch fehlt.
// Ausführliche Fassung: docs/konfiguration_schreiben.md.

export const BLANK_TEMPLATE = `# Vorlage: Platzhalter in <...> ersetzen, nicht Benötigtes löschen.
scenario:
  name: "<Titel der Analyse>"
  description: "<Was wird untersucht?>"
  region: "<Ortsname, z. B. Chemnitz, Deutschland, oder BBox west,süd,ost,nord>"
  # crs: auto                     # Arbeits-CRS; Standard: UTM-Zone der Region
  output_license:                 # Lizenz, unter die Sie die Ergebnisse stellen (optional)
    name: "<z. B. CC-BY-4.0>"
    attribution: "<Namensnennung>"
    url: "<https://... Link zum Lizenztext>"

layers:                           # Daten: je Eintrag eine Quelle (Reiter "Quellen")
  - id: <layer_name>
    source: osm
    tags: { <schlüssel>: <wert> }
  # - id: <layer_name>
  #   source: file
  #   path: "<datei.geojson>"
  #   data_type: vector           # vector | raster
  #   license: { name: "<Lizenz>", attribution: "<Namensnennung>" }

steps:                            # Analyse: je Eintrag eine Operation (Reiter "Operationen")
  - id: <schritt_name>
    op: <operation>
    inputs: { <eingang>: <layer_name> }
    params: { <parameter>: <wert> }

output:                           # Ergebnisse: Karte und Dateien
  - type: map
    source: <schritt_name>
  - type: geojson
    source: <schritt_name>
    path: "<ergebnis.geojson>"
`;

/** Die Vorlage ersetzt nie vorhandenen Text: sie gilt nur für einen leeren Editor. */
export function canInsertTemplate(configYaml: string): boolean {
  return configYaml.trim().length === 0;
}
