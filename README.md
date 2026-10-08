# GeoFACT

GeoFACT beantwortet Fragen an offene Geodaten, die mehrere Datenquellen verbinden. Eine Analyse
wird nicht programmiert, sondern in einer YAML-Datei beschrieben: Layer (Datenquellen), Schritte
(Operationen) und Ausgaben. Daraus entsteht ein gerichteter azyklischer Graph (DAG); die
Reihenfolge folgt aus den Verweisen, und Layer werden erst geladen, wenn ein Schritt sie braucht.

Dieses Repository ist eine Demo: der Framework-Kern mit Kommandozeile, eine Web-Oberfläche und
27 Beispiel-Szenarien. **Zum Einstieg: [`docs/leitfaden.md`](docs/leitfaden.md)** führt in rund
einer Viertelstunde durch die Demo und zeigt, wo sich was im Code nachprüfen lässt.

## Schnellstart

Mit Docker, ohne Konto und ohne Datenbank; die mitgelieferten Szenarien laufen ohne Netz:

```bash
docker compose -f docker-compose.demo.yml up
```

Danach die Web-Demo unter <http://localhost:3000> öffnen, „Szenario laden“ wählen und ausführen.
Die Kommandozeile steht im selben Container bereit:

```bash
docker compose -f docker-compose.demo.yml exec backend geofact run examples/sachsen_nachthimmel.yaml
```

Schritt für Schritt, Images ohne Registry, Fehlersuche und der Weg ohne Docker:
**[`docs/schnellstart.md`](docs/schnellstart.md)**.

## Funktionsumfang

- Deklarative Szenarien als DAG, Layer werden erst bei Bedarf geladen, Validierung vor jedem Datenzugriff.
- **Arbeits-CRS** frei deklarierbar (`crs: auto | EPSG:xxxx | equal_area | equal_area_europe`), Quell-CRS je
  Layer, Ziel-CRS je Ausgabe; CRS-Provenienz je Layer und Vergleichsplot `crs_plot`.
- **Parameter, `foreach`, Vorlagen, `when`** (Expansion vor der Validierung, der DAG bleibt statisch),
  `--param` auf der Kommandozeile, `geofact validate --print-expanded`.
- **Lizenzen und Namensnennung** je Quelle bis in jede Ausgabe (`ATTRIBUTION.txt`, Karte, GeoTIFF, RDF).
- Operationen unter anderem `area`, `length`, `calculate_field`, `select_columns`, `deduplicate`,
  `area_share`, `rasterize`, `cluster`, `collect`, `gate`; typisierte Vergleiche ohne lexikografische Zahlen.
- Web-Demo mit Parameterformular, Provenienz und Ergebnis als eigenem Vollbild-Screen.

## Komponenten

- **`src/geofact/`** – der Framework-Kern (CLI: `geofact validate|run|plugins`).
  Aufbau, Schichtenregel und „Baustein hinzufügen“: **[`docs/architektur.md`](docs/architektur.md)**.
- **`backend/geofact_web/`** – FastAPI-Web-API, ruft den Kern in-process über `geofact.api` auf.
- **`frontend/`** – Next.js-Oberfläche zum Bauen, Ausführen und
  Inspizieren von Pipelines (Live-Graph, Per-Node-Karten, Download).

## Installation

Voraussetzungen: Python >= 3.11 und [`uv`](https://docs.astral.sh/uv/); für das Frontend Node >= 18.

```bash
uv sync                  # Kern + Entwicklungswerkzeuge (pytest, ruff)
uv sync --extra web      # zusätzlich das Web-Backend (FastAPI)
```

## CLI (Kern)

```bash
uv run geofact validate examples/chemnitz_route_hbf_tu.yaml
uv run geofact run      examples/chemnitz_route_hbf_tu.yaml               # schreibt die Ausgaben nach ./output/chemnitz_route_hbf_tu/
uv run geofact run      examples/chemnitz_route_hbf_tu.yaml --out ergebnis
uv run geofact plugins                                                    # alle erkannten Bausteine mit Herkunft
```

| Befehl | Wirkung |
|---|---|
| `geofact validate CONFIG` | prüft die Konfiguration (Typen, Verweise, Parameter) und zeigt Ausführungsreihenfolge, benötigte und ungenutzte Layer |
| `geofact run CONFIG [--out DIR] [--refresh-snapshots] [--dump-intermediate DIR] [--allow-skipped]` | führt das Szenario aus und **schreibt die deklarierten Ausgaben** (Standard `./output/<Name der YAML>/` relativ zum Arbeitsverzeichnis) |
| `geofact plugins` | alle Quellarten, Formate, Transformationen, Operationen und Ausgabeformate mit Herkunft; nicht geladene Plugins mit Ursache |

**Exit-Codes:** `0` ok · `1` Konfigurations- oder Laufzeitfehler (mit Position bzw.
Layer/Schritt, ohne Traceback) · `2` eine deklarierte Ausgabe wurde übersprungen
(`--allow-skipped` macht daraus `0`). Warnungen und übersprungene Ausgaben gehen nach stderr.
`--refresh-snapshots` bezieht Netzdaten neu (sonst gilt der gespeicherte Snapshot unter
`GEOFACT_SNAPSHOT_DIR`, Standard `.geofact/snapshots`).

Relative Pfade in einer Szenario-YAML gelten gegen das Verzeichnis der YAML-Datei, nicht gegen
das Arbeitsverzeichnis. Plugins (eigene Quellarten, Formate, Operationen, Ausgaben als je eine
Python-Datei) liegen in einem Ordner, den `GEOFACT_PLUGIN_PATH` nennt. Aus Python:
`geofact.api` (`load_scenario`, `validate`, `plan`, `run`).

Konfiguration über Umgebungsvariablen (nie in der YAML): `GEOFACT_OSM_BACKEND` (`overpass` oder
`postgis`), `GEOFACT_PG_DSN`,
`GEOFACT_SNAPSHOT_DIR`, `GEOFACT_PLUGIN_PATH`; die Web-Demo hat weitere
(siehe [`docs/web_demo.md`](docs/web_demo.md)).

## Beispiele

30 Referenz-Szenarien in `examples/` (jedes validiert mit `geofact validate`, die Pfade gelten
relativ zur jeweiligen YAML):

| Szenario | Inhalt | Voraussetzung |
|---|---|---|
| [`berlin_gruenflaechen_erreichbarkeit.yaml`](examples/berlin_gruenflaechen_erreichbarkeit.yaml) | **Szenario 2 der Arbeit:** Anteil der Berliner Bevölkerung in 300 und 500 m Luftlinie um Grünflächen ab 2 ha, je Definition von Grünfläche (Parameter, Modul, `foreach`, `collect`); Tabelle, Karte | OSM; lokales Raster GHS-POP 2025 in `examples/data/` |
| [`chemnitz_datenquellen_lizenzen.yaml`](examples/chemnitz_datenquellen_lizenzen.yaml) | **Lizenzen und Namensnennung:** vier Quellen, drei Lizenzarten; jede Ausgabe nennt genau ihre Quellen | OSM, Earth Search |
| [`chemnitz_gruenanteil_osm.yaml`](examples/chemnitz_gruenanteil_osm.yaml) | **Grünanteil aus Vektordaten:** OSM-Grünflächen je Chemnitzer Stadtteil mit `area`, `area_share`, `calculate_field`, `clip`, `filter`, `dissolve`, `select_columns`, `top_n` | OSM (Overpass oder PostGIS) |
| [`chemnitz_gruenflaechenanteil_sentinel2.yaml`](examples/chemnitz_gruenflaechenanteil_sentinel2.yaml) | **Satellitenbild:** Sentinel-2 (STAC) -> NDVI -> Grünmaske -> Anteil je Stadtteil und für die ganze Stadt; Karte, GeoJSON, CSV, GeoTIFF | Netz (Earth Search, Overpass, Nominatim); der zweite Lauf nutzt den Snapshot |
| [`chemnitz_route_hbf_tu.yaml`](examples/chemnitz_route_hbf_tu.yaml) | **Routing:** Hauptbahnhof -> TU Chemnitz auf dem OSM-Straßennetz (`line_network`, `route`): Länge, Fahrzeit, Umwegfaktor; Karte, GeoJSON | Netz (Overpass, Nominatim) |
| [`crs_showcase/drei_crs_eine_stadt.yaml`](examples/crs_showcase/drei_crs_eine_stadt.yaml) | **Drei CRS, eine Stadt:** WGS 84, UTM 33 ohne CRS-Angabe (`crs_override`) und Gauß-Krüger 4 in einem Szenario; `crs_plot` | offline (eingecheckte Daten) |
| [`deutschland_einkommen_erwerbslosigkeit.yaml`](examples/deutschland_einkommen_erwerbslosigkeit.yaml) | verfügbares Einkommen und Erwerbslosenquote je Bundesland: zwei Eurostat-Tabellen über den NUTS-Code an die BKG-Grenzen gehängt, `ranking` | Netz (WFS) |
| [`deutschland_gruenste_grossstadt_osm.yaml`](examples/deutschland_gruenste_grossstadt_osm.yaml) | **Grünste Großstadt:** 80 Großstädte per Parameter, Vorlage, `foreach`, `collect` und `when`; Rangliste | OSM auf PostGIS (deutschlandweit) |
| [`europa/europa_netz_resilienz.yaml`](examples/europa/europa_netz_resilienz.yaml) | **Europa:** Szenario 1 auf das europäische Übertragungsnetz übertragen: kritische Höchstspannungsknoten (220–750 kV) aus Netzlage (Betweenness) und Bevölkerung im 20-km-Umkreis; fertiges Netzmodell als Kantenliste (`edge_list_network`), Raster in Zielauflösung; Karte, GeoJSON, CSV, RDF | PyPSA-Eur-Netz (`scripts/fetch_europa_data.py`) und das globale Bevölkerungsraster, siehe [`examples/europa/README.md`](examples/europa/README.md); rund 5,5 Minuten, 2,8 GB Speicher |
| [`leipzig/`](examples/leipzig) | 13 kommunale Analysen für Leipzig: Versorgungslücken, Dichte (Hexraster), Abdeckung, Erreichbarkeit im Tram-/S-Bahn-/Bus-Netz, amtliche vs. OSM-Daten | OSM, WFS, GTFS (`leipzig12`/`13` ohne OSM) |
| [`plugin_demo/`](examples/plugin_demo) | Plugin-Mechanismus: FlatGeobuf-Dateiformat als Plugin, REST-Quelle/-Operation/-Ausgabe gegen einen lokalen Demo-Dienst | `GEOFACT_PLUGIN_PATH=examples/plugin_demo/plugins`, `examples/plugin_demo/demo_dienst.py` (siehe dessen Kopf) |
| [`sachsen_bip_je_einwohner_kreise.yaml`](examples/sachsen_bip_je_einwohner_kreise.yaml) | BIP je Einwohner der 13 sächsischen Kreise: Kreisgrenzen (BKG) + Eurostat-Tabelle aller deutschen Kreise, Join über NUTS-Code, Top-N | Netz (WFS) |
| [`sachsen_nachthimmel.yaml`](examples/sachsen_nachthimmel.yaml) | **Sterneschauen:** dunkle, hoch gelegene, erreichbare Aussichtspunkte in Sachsen (OSM + Nachtlicht- und Höhenraster; `nearest_distance`, `zonal_stats`, `filter`, `top_n`): die zehn höchsten Orte; Karte, GeoJSON | OSM; zwei lokale Raster in `examples/data/` (nicht eingecheckt, Bezug unten) |
| [`sachsen_netz_resilienz.yaml`](examples/sachsen_netz_resilienz.yaml) | **Szenario 1 der Arbeit:** kritische Umspannwerke des Hochspannungsnetzes in Sachsen: Netz aus OSM-Rohdaten mit dem Fach-Plugin `power_grid_osm`, Zentralität, Bevölkerung im 20-km-Umkreis, Rangliste; Karte, GeoJSON, RDF | OSM; lokales Raster GHS-POP 2025 in `examples/data/`; `GEOFACT_PLUGIN_PATH=examples/plugins` (die Web-Demo setzt das selbst) |
| [`sachsen_staedte_gruenanteil_sentinel2.yaml`](examples/sachsen_staedte_gruenanteil_sentinel2.yaml) | **Satellitenbild, drei Städte:** Sentinel-2-Mosaik (4 Kacheln, 20-m-Zellen) -> NDVI -> Grünmaske -> Grünanteil je Stadt für Leipzig, Dresden und Chemnitz; Karte, GeoJSON, CSV | Netz (Earth Search, Overpass); rund 1,5 GB Speicher; der zweite Lauf nutzt den Snapshot |
| [`szenario2_gruenflaechen.yaml`](examples/szenario2_gruenflaechen.yaml), [`szenario3_…`](examples/szenario3_gruenflaechen_bevoelkerung.yaml) | Grünflächen-Erreichbarkeit in Dresden, mit Bevölkerung je Stufe | OSM; Szenario 3 zusätzlich das Bevölkerungsraster |
| [`szenario4_bip_bundeslaender.yaml`](examples/szenario4_bip_bundeslaender.yaml) | die fünf größten Bundesländer nach BIP: WFS-Grenzen (BKG) + CSV-Tabelle, Join, Top-N | Netz (WFS) |

```bash
uv run geofact run examples/chemnitz_route_hbf_tu.yaml                  # Route + Karte nach ./output/chemnitz_route_hbf_tu/
uv run geofact run examples/chemnitz_gruenflaechenanteil_sentinel2.yaml # Grünflächenanteil (erster Lauf: Szene beziehen)
uv run geofact run examples/sachsen_nachthimmel.yaml                    # Beobachtungsorte (braucht die zwei Raster, siehe unten)
```

**Rasterdaten für `sachsen_nachthimmel`** (nicht eingecheckt, Ordner `examples/data/` ist gitignored; Quellen,
Lizenzen und Begründung stehen im Kopf der YAML-Datei). Wie das Bevölkerungsraster der Szenarien 1 und 3
liegen sie lokal dort; ohne sie scheitert der Lauf dieses Beispiels, `geofact validate` nicht:

- Nachtlicht `ntl_2024_simviirs.tif`: harmonisierter globaler Datensatz DMSP/VIIRS von Li et al., Jahr 2024
  (simVIIRS, 1 km, CC BY 4.0), unverändert von figshare:
  `curl -L -o examples/data/ntl_2024_simviirs.tif https://ndownloader.figshare.com/files/57065306`
- Höhe `dem_sachsen_glo90.tif`: Copernicus DEM GLO-90 (AWS-Bucket `copernicus-dem-90m`, frei mit Quellenangabe);
  GeoFACT liest je Raster-Layer eine Datei, darum fügt `uv run python scripts/prepare_dem_sachsen.py` die zehn
  Kacheln für Sachsen (N50/N51, E011 bis E015) zusammen (ca. 33 MB, kein Konto nötig).

Mit den OSM-Snapshots (`.geofact/snapshots`) läuft das Szenario in rund zehn Sekunden ohne Netz. Mit dem OSM-Stand
vom 30.09.2026 ergibt es 2 546 Aussichtspunkte, 41 795 Parkplätze und 250 geeignete Orte; die zehn höchsten liegen
zwischen 771 und 1 018 m.

## Web-Demo

Backend + Frontend zur interaktiven Demonstration – Einrichtung, alle
Umgebungsvariablen (lokaler OSM-PostGIS-Dump, Copernicus-Verzeichnis,
LLM/OpenRouter) und Bedienung: **[`docs/web_demo.md`](docs/web_demo.md)**.

```bash
# Backend
uv sync --extra web
uv run --extra web geofact-api            # http://127.0.0.1:8000

# Frontend
cd frontend && npm install && npm run dev  # http://localhost:3000
```

## Tests

```bash
uv run pytest -q                          # Standardlauf, offline (Kern; mit --extra web auch das Web-Backend)
uv run --extra web pytest -q              # alles inklusive der Web-Tests
cd frontend && npm run build              # Frontend-Build/Typecheck
```

Der Standardlauf ist **offline**: Tests, die echte externe Endpunkte abfragen
(Overpass, Nominatim, WFS, Behördenportale), tragen die Marke `netcheck` und
sind per `addopts` in `pyproject.toml` ausgeschlossen. Zusätzlich scheitert
jede Verbindung ins Netz in einem Standardtest laut (`tests/conftest.py`;
Loopback bleibt offen) - ein vergessener Live-Aufruf fällt also sofort auf.
Ein `-m` auf der Kommandozeile ersetzt den Standardausdruck:

```bash
uv run pytest -m netcheck                 # Live-Tests gegen die echten Endpunkte (Netz nötig;
                                          # ein Fehlschlag ist meist ein Dienstproblem, keine Regression)
GEOFACT_PG_DSN=postgresql://user:pw@host:5432/osm uv run pytest -m integration
                                          # PostGIS-Integrationstests (lokale OSM-Instanz);
                                          # ohne GEOFACT_PG_DSN werden sie übersprungen
uv run pytest -m "integration and not netcheck"   # nur PostGIS, ohne die Overpass-Gegenprobe
```

Der Test `test_fa03_backends_equivalent_on_small_bbox` vergleicht PostGIS mit
der öffentlichen Overpass-API und trägt deshalb beide Marken (`integration`
und `netcheck`).

## Dokumentation

Zum Einstieg:

- [`docs/leitfaden.md`](docs/leitfaden.md) – Leitfaden: Rundgang durch die Demo, was sie zeigt und wo es im Code steht
- [`docs/schnellstart.md`](docs/schnellstart.md) – Schnellstart: Web-Demo und CLI mit Docker, Datenpaket, Fehlersuche
- [`docs/konfiguration_schreiben.md`](docs/konfiguration_schreiben.md) – Anleitung: eine Konfiguration selbst schreiben, von der Vorlage zum lauffähigen Szenario

Zum Nachschlagen:

- [`docs/architektur.md`](docs/architektur.md) – Architektur: Pakete und Schichtenregel, Registry, Erkennung, Bausteine hinzufügen, Validierung, Anwendungsfall, Plan
- [`docs/web_demo.md`](docs/web_demo.md) – Web-Backend und Frontend: Aufbau, Umgebungsvariablen, API
- [`docs/provenienz_rdf.md`](docs/provenienz_rdf.md) – Provenienz und Lizenzen im RDF-Export, mit Beispielabfrage

