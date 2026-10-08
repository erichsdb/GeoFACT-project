# Leitfaden zur GeoFACT-Demo

Dieser Leitfaden führt in rund einer Viertelstunde durch die Demo: was GeoFACT ist, wie man es
startet, was man sich ansehen sollte und wo die gezeigten Eigenschaften im Code und in den Tests
stehen. Wer nur starten will, findet alle Wege und die Fehlersuche in
[`schnellstart.md`](schnellstart.md).

## 1. Was die Demo zeigt

GeoFACT beantwortet Fragen an offene Geodaten, die mehrere Quellen verbinden, zum Beispiel:
„Welche Umspannwerke in Sachsen liegen zentral im Netz und versorgen viele Menschen?“ Eine solche
Analyse wird nicht programmiert, sondern in einer YAML-Datei beschrieben. Sie hat drei Teile:

| Teil | Bedeutung | Beispiel |
|---|---|---|
| `layers` | Datenquellen | OpenStreetMap, WFS-Dienst, Rasterdatei, Satellitenbild, CSV-Tabelle |
| `steps` | Operationen auf Layern und auf den Ergebnissen anderer Schritte | Puffer, Verschneidung, Netzgraph, Zonenstatistik, Rangliste |
| `output` | Ausgaben | Karte (HTML), GeoJSON, CSV, GeoTIFF, RDF |

Aus den Verweisen zwischen Schritten und Layern entsteht ein gerichteter azyklischer Graph. GeoFACT
leitet daraus die Reihenfolge ab, prüft die gesamte Konfiguration vor dem ersten Datenzugriff und
lädt jeden Layer erst dann, wenn ein Schritt ihn braucht.

Die Demo besteht aus drei Teilen, die denselben Kern verwenden:

- **Kern mit Kommandozeile** (`src/geofact/`, Befehl `geofact`),
- **Web-Oberfläche** (`backend/geofact_web/`, `frontend/`) zum Laden, Bearbeiten, Ausführen und
  Ansehen von Szenarien,
- **27 Beispiel-Szenarien** (`examples/`), Übersicht im [README](../README.md#beispiele).

Mitgeliefert sind 11 Quellarten, 10 Datei- und Tabellenformate, 42 Operationen und 7 Ausgabeformate;
`geofact plugins` listet sie mit Herkunft auf.

## 2. Starten

Voraussetzung ist Docker. Ein Konto, eine Datenbank oder eine Netzverbindung zur Laufzeit sind für
die mitgelieferten Szenarien nicht nötig, weil ihre Daten im Image liegen.

```bash
docker compose -f docker-compose.demo.yml up
```

Danach ist die Oberfläche unter <http://localhost:3000> erreichbar. Das Backend braucht nach dem
Start bis zu 30 Sekunden. Meldet Docker `pull access denied`, beschreibt der
[Schnellstart](schnellstart.md#4-images-ohne-registry-laden), wie sich die Images aus einem Archiv
laden oder selbst bauen lassen; dort steht auch der Weg ganz ohne Docker.

## 3. Rundgang durch die Oberfläche

Die Links öffnen das jeweilige Szenario direkt. Jede Station nennt, worauf es dabei ankommt.

### Station 1: Ein Szenario lesen und ausführen (5 Minuten)

[Sterneschauen in Sachsen](http://localhost:3000/?scenario=example:sachsen_nachthimmel) sucht
dunkle, hoch gelegene und mit dem Auto erreichbare Aussichtspunkte.

1. **Konfiguration lesen.** Der Editor zeigt die YAML-Datei. Vier Layer (Aussichtspunkte und
   Parkplätze aus OpenStreetMap, ein Nachtlicht- und ein Höhenraster) und acht Schritte genügen;
   Programmcode kommt nicht vor.
2. **Graph ansehen.** Der Ausführungsgraph ist aus den Verweisen abgeleitet, die Reihenfolge der
   Schritte in der Datei spielt keine Rolle.
3. **Ausführen.** Die Knoten färben sich, während der Lauf fortschreitet. Jeder Layer wird erst
   geladen, wenn der erste Schritt ihn braucht.
4. **Ergebnis ansehen.** Jeder Knoten, auch jedes Zwischenergebnis, lässt sich als Karte, Tabelle,
   Diagramm und Statistik öffnen. Die deklarierten Ausgaben stehen hinter dem Knopf „Ausgaben“ als Dateiliste, einzeln oder
   zusammen als ZIP herunterladbar, darunter die Datei `ATTRIBUTION.txt` mit den Quellen und
   Lizenzen dieses Laufs. „Läufe“ in der Kopfzeile führt zu den früheren Läufen und ihren Dateien,
   solange das Backend läuft.

Zum Vergleich: Mit dem OpenStreetMap-Stand vom 30.09.2026 bleiben von 2 546 Aussichtspunkten 250
geeignete Orte übrig; die zehn höchsten liegen zwischen 771 und 1 018 m. Von wann die Daten des
Datenpakets stammen, steht in `examples/data/LIESMICH.md` im Image.

### Station 2: Fehler werden vor dem Lauf gemeldet (2 Minuten)

Im Editor desselben Szenarios bei einem Schritt den Namen eines Eingangs ändern, etwa auf einen
Layer, den es nicht gibt, oder einen Parameter auf einen unzulässigen Wert setzen. Die Prüfung läuft
beim Tippen mit und nennt den Fehler mit seiner Position in der Datei. Ein Lauf startet erst, wenn
die Konfiguration gültig ist; es werden keine Daten geladen, um einen Tippfehler zu finden.

### Station 3: Netzgraph, Fach-Plugin und Bevölkerung (3 Minuten)

[Resilienz von Energieinfrastruktur](http://localhost:3000/?scenario=example:sachsen_netz_resilienz)
ist Szenario 1 der Arbeit: Aus Leitungen und Umspannwerken entsteht ein Netzgraph, die Zentralität
jedes Umspannwerks wird mit der Bevölkerung in seinem Umkreis zu einer Rangliste verbunden. Zu sehen
ist, wie Vektordaten, ein Graph und ein Raster in einer Konfiguration zusammenkommen. Das Netz baut
die Operation `power_grid_osm`; sie gehört nicht zum Kern, sondern ist ein Fach-Plugin in
`examples/plugins/` und steht in der Konfiguration wie jede andere Operation. Hier verläuft die
Grenze der Arbeit: was eine Leitung mit einem Umspannwerk verbindet, ist Fachwissen.

### Station 4: Drei Koordinatensysteme (2 Minuten)

[Drei CRS, eine Stadt](http://localhost:3000/?scenario=example:crs_showcase/drei_crs_eine_stadt)
lädt drei Dateien in WGS 84, UTM 33 (ohne CRS-Angabe in der Datei) und Gauß-Krüger 4. GeoFACT
harmonisiert sie auf ein Arbeits-CRS und hält je Layer fest, woher sein CRS stammt. Fehlt eine
CRS-Angabe und ist keine deklariert, bricht der Lauf mit einer Meldung ab, statt zu raten.

### Station 5: Weitere Datenarten (nach Interesse)

| Szenario | Zeigt |
|---|---|
| [Route Hauptbahnhof → TU Chemnitz](http://localhost:3000/?scenario=example:chemnitz_route_hbf_tu) | Routing auf dem Straßennetz |
| [Grünflächenanteil aus Sentinel-2](http://localhost:3000/?scenario=example:chemnitz_gruenflaechenanteil_sentinel2) | Satellitenbild, NDVI, Anteil je Stadtteil |
| [Datenquellen und Lizenzen](http://localhost:3000/?scenario=example:chemnitz_datenquellen_lizenzen) | jede Ausgabe nennt genau die Quellen, die in sie eingeflossen sind |
| [Leipzig](../examples/leipzig) | 13 kommunale Fragen: Versorgungslücken, Dichte, Erreichbarkeit im Nahverkehr |

Fünf Szenarien gehören nicht zum Datenpaket, weil sie große Datenmengen oder eine eigene Umgebung
brauchen: die 80 Großstädte (deutschlandweite OSM-Datenbank), das Sentinel-2-Mosaik für drei
Städte, die Grünflächen in Berlin (Bevölkerungsraster außerhalb des Ausschnitts), das
[europäische Übertragungsnetz](../examples/europa/README.md) und die
[Plugin-Demo](../examples/plugin_demo), die nur auf der Kommandozeile läuft und in der Auswahl der
Oberfläche fehlt. Sie lassen sich prüfen (`geofact validate`), aber nicht ohne Weiteres ausführen.

**Szenario generieren** (Konfiguration aus einer Frage in natürlicher Sprache) ist ohne Schlüssel
für ein Sprachmodell abgeschaltet; der Schnellstart beschreibt das Einschalten. Alles andere
funktioniert ohne.

## 4. Dasselbe auf der Kommandozeile

Die Oberfläche ruft denselben Kern auf wie der Befehl `geofact`. Bei laufender Demo:

```bash
docker compose -f docker-compose.demo.yml exec backend geofact validate examples/sachsen_nachthimmel.yaml
docker compose -f docker-compose.demo.yml exec backend geofact run      examples/sachsen_nachthimmel.yaml
docker compose -f docker-compose.demo.yml exec backend geofact plugins
```

- `validate` prüft die Konfiguration und gibt die abgeleitete Ausführungsreihenfolge, die
  benötigten und die ungenutzten Layer sowie die Lizenzen der Quellen aus.
- `run` führt das Szenario aus und schreibt die Ausgaben nach `geofact-output/<Szenario>/` neben
  `docker-compose.demo.yml`.
- `plugins` listet alle Bausteine mit dem Modul, aus dem sie stammen.

Alle Schalter und Exit-Codes stehen im [README](../README.md#cli-kern).

## 5. Wo es im Code steht

Die Tabelle ordnet jeder Eigenschaft die Stelle zu, an der sie umgesetzt und geprüft ist.

| Eigenschaft | Umsetzung | Nachweis |
|---|---|---|
| Analyse als Graph, Reihenfolge per topologischer Sortierung | `src/geofact/core/scenario.py`, `src/geofact/core/plan.py` | `tests/unit/test_fa09_plan.py`, `test_fa09_executor.py` |
| Layer werden erst bei Bedarf geladen, ungenutzte nie | `ExecutionPlan` in `src/geofact/core/plan.py` | `tests/unit/test_fa09_plan.py`; Ausgabe von `geofact validate` |
| Prüfung vor jedem Datenzugriff, Fehler mit Position | [Architektur, Abschnitt 7](architektur.md#7-validierung-fa2) | `tests/unit/test_fa01_02_schema.py`, `test_fa02_strict_contracts.py` |
| Jeder Baustein ist eine Datei mit Vertrag, Funktion und Registrierung | `src/geofact/builtin/` (je Art ein Ordner), `src/geofact/plugin_api.py` | `tests/unit/test_fa40_contract_colocation.py`, `test_fa40_registration_inventory.py` |
| Neuer Baustein ohne Änderung an bestehendem Code | [Architektur, Abschnitt 6](architektur.md#6-baustein-hinzufügen) | `examples/plugin_demo/plugins/flatgeobuf.py` |
| Schichtenregel: Kern kennt keine Bausteine, Oberflächen nutzen nur `geofact.api` | [Architektur, Abschnitt 2](architektur.md#2-pakete-und-schichtenregel-fa45) | `tests/unit/test_architecture_layers.py` schlägt bei verbotenen Importen fehl |
| Keine stillen Fehler (fehlendes CRS, fehlende Attribute) | Meldungen mit Layer, Schritt und Position | `tests/unit/test_fa08_silent_cases_attributes.py` |
| Lizenzen und Herkunft bis in jede Ausgabe | [`provenienz_rdf.md`](provenienz_rdf.md) | `tests/unit/test_fa13_rdf.py`; `ATTRIBUTION.txt` in jedem Lauf |
| Jede Funktion gehört zu einer Anforderung | Anforderungskatalog der Arbeit (FA/NFA mit Vor- und Nachbedingungen) | `Implements: FAx` im Modul-Docstring, Tests heißen `test_faNN_…` |

Die Tests sind nach ihrer Anforderung benannt (`test_fa05_…` prüft FA5). Mit Python ≥ 3.11 und
[`uv`](https://docs.astral.sh/uv/) laufen sie ohne Netz und ohne Docker:

```bash
uv sync --extra web
uv run pytest -q
uv run geofact run examples/crs_showcase/drei_crs_eine_stadt.yaml   # läuft ohne Netz und ohne Zusatzdaten
```

Einen Baustein von außen zeigt die Plugin-Demo: Die Datei `examples/plugin_demo/plugins/flatgeobuf.py`
ergänzt ein Dateiformat, ohne dass eine Zeile im Kern geändert wird.

```bash
GEOFACT_PLUGIN_PATH=examples/plugin_demo/plugins uv run geofact plugins   # flatgeobuf erscheint mit seiner Herkunft
```

## 6. Was die Demo nicht ist

- **Kein gehosteter Dienst.** Die Web-Oberfläche ist für den Betrieb auf dem eigenen Rechner
  gebaut. Läufe liegen im Arbeitsspeicher des Backends und sind nach einem Neustart fort.
- **Kein Ersatz für Fachwissen.** GeoFACT rechnet, was die Konfiguration festlegt. Schwellenwerte,
  Gewichte und die Wahl der Daten bleiben fachliche Entscheidungen; sie stehen sichtbar in der
  YAML-Datei statt im Code.
- **Bewusst ausgeklammert** sind Zeitreihen, Echtzeitdaten und 3D-Analysen.
- **Raster müssen in den Arbeitsspeicher passen.** GeoFACT liest Ausschnitte und reduzierte
  Auflösungen, verarbeitet aber kein Raster, das größer ist als der Speicher.

Die konzeptionellen Grenzen stehen in
[Architektur, Abschnitt 13](architektur.md#13-bewusste-grenzen).

## 7. Wegweiser

| Frage | Dokument |
|---|---|
| Wie starte ich die Demo, und was tue ich bei Fehlern? | [`schnellstart.md`](schnellstart.md) |
| Welche Beispiele gibt es, und was brauchen sie? | [README, Abschnitt „Beispiele“](../README.md#beispiele) |
| Wie schreibe ich eine eigene Konfiguration? | [`konfiguration_schreiben.md`](konfiguration_schreiben.md) |
| Wie ist der Code aufgebaut, und wie kommt ein Baustein hinzu? | [`architektur.md`](architektur.md) |
| Wie funktioniert die Web-Oberfläche, und welche Schnittstellen hat sie? | [`web_demo.md`](web_demo.md), <http://localhost:8000/docs> |
