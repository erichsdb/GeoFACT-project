# Eine Konfiguration selbst schreiben

Eine GeoFACT-Analyse ist eine YAML-Datei. Sie sagt, **welche Daten** gebraucht werden, **welche
Schritte** darauf laufen und **welche Ergebnisse** entstehen sollen. Die Reihenfolge der Schritte
leitet GeoFACT selbst ab; geladen wird nur, was ein Schritt oder eine Ausgabe braucht.

Diese Anleitung führt von der leeren Vorlage zu einem lauffähigen Szenario. Sie gilt für den
Editor der Web-Oberfläche (Einstieg „Szenario selbst schreiben“) und für die Kommandozeile.

## 1. Mit der Vorlage beginnen

In der Web-Oberfläche: Einstieg **„Szenario selbst schreiben“** wählen und über dem leeren Editor
auf **„Vorlage“** klicken. Die Vorlage enthält alle vier Abschnitte mit Platzhaltern in
`<spitzen Klammern>`. Solange Platzhalter stehen, meldet die Prüfung unter dem Editor, was noch
fehlt. Die Vorlage ersetzt nie vorhandenen Text.

```yaml
scenario:
  name: "<Titel der Analyse>"
  description: "<Was wird untersucht?>"
  region: "<Ortsname oder BBox west,süd,ost,nord>"
  output_license:                 # optional
    name: "<z. B. CC-BY-4.0>"
    attribution: "<Namensnennung>"
    url: "<https://... Link zum Lizenztext>"

layers:
  - id: <layer_name>
    source: osm
    tags: { <schlüssel>: <wert> }

steps:
  - id: <schritt_name>
    op: <operation>
    inputs: { <eingang>: <layer_name> }
    params: { <parameter>: <wert> }

output:
  - type: map
    source: <schritt_name>
```

## 2. Die vier Abschnitte

### `scenario`: Titel, Region, Lizenz

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `name` | ja | Titel der Analyse |
| `description` | nein | eine Zeile zum Inhalt |
| `region` | ja | Gebiet der Analyse: ein Ortsname (`"Chemnitz, Deutschland"`) oder eine BBox in WGS 84 (`"12.8,50.75,13.0,50.9"`, West, Süd, Ost, Nord) |
| `crs` | nein | Arbeits-Koordinatensystem; Standard `auto` (UTM-Zone der Region). Alle Daten werden dorthin umgerechnet |
| `output_license` | nein | Lizenz, unter die Sie Ihre Ergebnisse stellen: `name`, `attribution`, `url` |

Die Lizenzen der **Quellen** führt GeoFACT selbst mit: jede Ausgabe nennt die Daten, aus denen sie
entstanden ist (Datei `ATTRIBUTION.txt` in jedem Lauf). `output_license` ist Ihre eigene Angabe für
das abgeleitete Ergebnis. Ob sie zu den Lizenzen der Quellen passt, zeigt die Oberfläche unter dem
Editor („Lizenzen prüfen“) und auf der Kommandozeile `geofact licenses`.

### `layers`: die Daten

Jeder Eintrag ist eine Quelle mit einer eindeutigen `id`. Die Quellart steht in `source`:

| `source` | Wofür | Wichtigste Felder |
|---|---|---|
| `osm` | OpenStreetMap-Objekte der Region | `tags` (ein Tag-Paar oder eine Liste davon) |
| `region` | die Grenze der Region als Fläche | – |
| `file` | lokale Datei (GeoJSON, GeoPackage, Shapefile, GML, GeoTIFF) | `path`, `data_type: vector` oder `raster` |
| `table` | Tabelle ohne Geometrie (CSV, XLSX, …) | `path` |
| `wfs`, `ckan`, `rest`, `stac`, `gtfs`, `geocode`, `points` | Webdienste, Satellitenbilder, Fahrpläne, Adressen | siehe Reiter „Quellen“ und „APIs“ |

Relative Pfade gelten gegenüber dem Ordner der YAML-Datei. In der Web-Oberfläche fügt ein Klick im
Reiter **„Quellen“** oder **„APIs“** den passenden Eintrag ein; hochgeladene Dateien erscheinen dort
ebenfalls.

Optional je Layer:

```yaml
  - id: umspannwerke
    source: osm
    tags: { power: substation }
    validation:                    # Datenqualität prüfen
      required_fields: [voltage]
      on_violation: drop           # verletzende Objekte entfernen; warn = nur melden
    license:                       # nur nötig, wenn die Quellart keine Lizenz kennt
      name: ODbL-1.0
      attribution: "© OpenStreetMap contributors"
```

### `steps`: die Analyse

Ein Schritt wendet eine Operation an. `inputs` verbindet die Eingänge der Operation mit Layern oder
mit den Ergebnissen früherer Schritte; `params` setzt ihre Parameter.

```yaml
steps:
  - id: umfeld
    op: buffer
    inputs: { geometry: parks }
    params: { radius_km: 0.3 }
  - id: einwohner
    op: zonal_stats
    inputs: { zones: umfeld, values: bevoelkerung }
    params: { stat: sum }
```

Welche Eingänge und Parameter eine Operation hat, steht im Reiter **„Operationen“**; ein Klick fügt
den Schritt mit allen Feldern ein. Häufig gebrauchte Operationen:

| Zweck | Operationen |
|---|---|
| Auswählen und Rechnen | `filter`, `calculate_field`, `select_columns`, `top_n`, `ranking`, `classify` |
| Geometrie | `buffer`, `clip`, `overlay`, `dissolve`, `to_points`, `area`, `length` |
| Verknüpfen | `spatial_join`, `attribute_join`, `aggregate`, `area_share`, `nearest_distance` |
| Raster | `zonal_stats`, `raster_calc`, `raster_classify`, `rasterize` |
| Dichte | `hexbin`, `hex_grid`, `cluster` |
| Netze | `line_network`, `build_network`, `centrality`, `route`, `shortest_path`, `isochrone`, `graph_to_vector` |

Die Typen müssen passen: ein Eingang für Vektordaten nimmt kein Raster und keinen Graphen. Die
Prüfung meldet das vor dem Lauf, mit Schritt und Eingang.

### `output`: die Ergebnisse

Jeder Eintrag schreibt das Ergebnis eines Schritts (oder einen Layer) in einem Format:

| `type` | Ergebnis |
|---|---|
| `map` | interaktive Karte (HTML); `color_field` färbt nach einer Spalte |
| `geojson`, `csv` | Vektordaten bzw. Tabelle |
| `geotiff` | Raster |
| `rdf` | GeoSPARQL/Turtle mit Lizenz und Herkunft |

`path` ist der Dateiname im Ausgabeordner; ohne `path` vergibt GeoFACT einen Namen. Eine einzelne
Ausgabe kann mit `license:` von `scenario.output_license` abweichen.

## 3. Ein vollständiges Beispiel

Einzugsbereich von 300 m um die Parks in Chemnitz:

```yaml
scenario:
  name: "Parks in Chemnitz"
  description: "Einzugsbereich von 300 m um die Parks"
  region: "Chemnitz, Deutschland"
  output_license:
    name: "CC-BY-4.0"
    attribution: "Eigene Auswertung"
    url: "https://creativecommons.org/licenses/by/4.0/"

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
  - type: geojson
    source: umfeld
    path: "umfeld.geojson"
```

## 4. Prüfen und ausführen

**Web-Oberfläche:** Die Konfiguration wird beim Tippen geprüft. Fehler stehen unter dem Editor, mit
der Stelle in der Datei. Ist sie gültig, zeigt die Oberfläche die Ausführungsreihenfolge und die
Lizenzen; „Weiter zur Ausführung“ startet den Lauf. „Speichern“ legt das Szenario unter einem Namen
ab, es erscheint danach unter „Szenario laden“.

**Kommandozeile:**

```bash
uv run geofact validate mein_szenario.yaml     # prüfen, ohne Daten zu laden
uv run geofact run mein_szenario.yaml          # ausführen, Ergebnisse nach ./output/mein_szenario/
```

`validate` lädt keine Daten: es prüft Aufbau, Namen, Typen der Verbindungen und Parameter und nennt
die Ausführungsreihenfolge, die benötigten Layer und die Lizenzen.

## 5. Weiter gehen

- **Parameter:** Werte, die sich je Lauf ändern sollen, stehen im Abschnitt `parameters` (Typ,
  Standardwert, Beschreibung) und werden mit `${name}` eingesetzt. Die Oberfläche zeigt sie als
  Eingabefelder; auf der Kommandozeile setzt `--param name=wert` einen Wert.
- **Wiederholungen:** `modules` beschreibt einen Teilgraphen einmal, `instances` wendet ihn auf eine
  Liste an (z. B. je Stadt). Beispiele: `examples/berlin_gruenflaechen_erreichbarkeit.yaml` und
  `examples/deutschland_gruenste_grossstadt_osm.yaml`.
- **Vorlagen zum Abschauen:** die Beispiele unter `examples/` (in der Oberfläche „Szenario laden“);
  die kleinsten liegen in `examples/leipzig/`.
- **Eigene Bausteine:** eine neue Quellart, Operation oder ein Ausgabeformat ist eine Datei, siehe
  [Architektur, Abschnitt 6](architektur.md#6-baustein-hinzufügen).
