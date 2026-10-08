# Schnellstart: GeoFACT ausprobieren

Diese Seite führt in wenigen Minuten zu einer laufenden GeoFACT-Instanz. Der empfohlene Weg ist
Docker: zwei fertige Images, ein Befehl, kein Konto, keine Datenbank. Die mitgelieferten Szenarien
laufen dabei ohne Netz, weil ihre Daten im Image liegen.

| Weg | Voraussetzung | Dauer bis zum ersten Ergebnis |
|---|---|---|
| [Web-Demo mit Docker](#1-web-demo-mit-docker) | Docker Desktop oder Docker Engine mit Compose | rund 5 Minuten, davon fast alles Download (etwa 1,5 GB) |
| [Kommandozeile im Container](#3-kommandozeile-im-container) | wie oben | Sekunden je Szenario |
| [Ohne Docker](#6-ohne-docker) | Python ≥ 3.11, `uv`, für die Oberfläche Node ≥ 18 | rund 10 Minuten |

## 1. Web-Demo mit Docker

```bash
git clone https://github.com/erichsdb/GeoFACT-project.git
cd GeoFACT-project
docker compose -f docker-compose.demo.yml up
```

Sobald beide Container laufen:

- **Oberfläche:** <http://localhost:3000>
- API-Dokumentation: <http://localhost:8000/docs>

Beenden mit `Strg+C`, aufräumen mit `docker compose -f docker-compose.demo.yml down`.

Das Repository wird nur für die Datei `docker-compose.demo.yml` gebraucht; sie allein genügt.
Die Images gibt es für Intel/AMD (64 Bit, auch Intel-Macs) und für ARM (Apple Silicon); Docker wählt
das passende selbst. Rechner mit 32-Bit-System werden nicht unterstützt.

## 2. Ein Szenario ausführen

1. In Schritt 1 „Szenario“ den Einstieg **Szenario laden** wählen, ein Beispiel aus der Liste
   anklicken und **Laden**. Ein Link öffnet ein Beispiel direkt, zum Beispiel
   <http://localhost:3000/?scenario=example:sachsen_nachthimmel>.
2. Der Editor zeigt die YAML-Konfiguration: Layer (Datenquellen), Schritte (Operationen) und
   Ausgaben. Die Prüfung läuft beim Tippen mit und nennt Fehler mit Position.
3. **Ausführen:** Der Ausführungsgraph färbt sich je Knoten, während der Lauf fortschreitet.
4. **Ergebnis:** Jeder Knoten lässt sich als Karte, Tabelle, Diagramm und Statistik ansehen; die
   deklarierten Ausgaben gibt es am Ende als ZIP.

Gute erste Beispiele:

| Beispiel | Zeigt | Link |
|---|---|---|
| Sterneschauen in Sachsen | Vektor- und Rasterdaten verbinden, filtern, Rangliste | [`sachsen_nachthimmel`](http://localhost:3000/?scenario=example:sachsen_nachthimmel) |
| Resilienz von Energieinfrastruktur | Netzgraph aus OSM-Rohdaten (Fach-Plugin), Zentralität, Bevölkerung im Umkreis (Szenario 1 der Arbeit) | [`sachsen_netz_resilienz`](http://localhost:3000/?scenario=example:sachsen_netz_resilienz) |
| Route Hauptbahnhof → TU Chemnitz | Routing auf dem Straßennetz | [`chemnitz_route_hbf_tu`](http://localhost:3000/?scenario=example:chemnitz_route_hbf_tu) |
| Grünflächenanteil aus Sentinel-2 | Satellitenbild, NDVI, Anteil je Stadtteil | [`chemnitz_gruenflaechenanteil_sentinel2`](http://localhost:3000/?scenario=example:chemnitz_gruenflaechenanteil_sentinel2) |
| Drei CRS, eine Stadt | Harmonisierung dreier Koordinatensysteme | [`crs_showcase/drei_crs_eine_stadt`](http://localhost:3000/?scenario=example:crs_showcase/drei_crs_eine_stadt) |

**Welche Szenarien ohne Netz laufen** und von wann ihre Daten stammen, steht im Image in
`examples/data/LIESMICH.md`:

```bash
docker compose -f docker-compose.demo.yml exec backend cat examples/data/LIESMICH.md
```

Ein Lauf auf mitgelieferten Daten meldet je Quelle als Hinweis, dass er einen Snapshot verwendet,
und nennt dessen Bezugszeitpunkt. Eigene oder geänderte Abfragen (andere Region, andere OSM-Tags)
bezieht GeoFACT aus dem Netz (Overpass, Nominatim, WFS); das dauert länger und hängt von der
Erreichbarkeit dieser Dienste ab.

**Szenario generieren** (Konfiguration aus einer Frage in natürlicher Sprache) braucht einen
Schlüssel für ein Sprachmodell und ist ohne ihn abgeschaltet; alles andere funktioniert ohne.
Zum Einschalten eine Datei `.env` neben `docker-compose.demo.yml` anlegen und neu starten:

```
OPENAI_API_KEY=sk-or-...
# optional: OPENAI_BASE_URL=https://openrouter.ai/api/v1, GEOFACT_LLM_MODEL=...
```

## 3. Kommandozeile im Container

Die Web-Demo ruft denselben Kern auf wie die Kommandozeile `geofact`. Bei laufender Demo:

```bash
docker compose -f docker-compose.demo.yml exec backend geofact validate examples/sachsen_nachthimmel.yaml
docker compose -f docker-compose.demo.yml exec backend geofact run      examples/sachsen_nachthimmel.yaml
docker compose -f docker-compose.demo.yml exec backend geofact plugins
```

`validate` prüft die Konfiguration und zeigt die abgeleitete Ausführungsreihenfolge, `run` führt
sie aus, `plugins` listet alle Bausteine mit Herkunft. Die Ausgaben von `run` liegen danach auf
dem eigenen Rechner im Ordner `geofact-output/<Szenario>/` neben `docker-compose.demo.yml`
(Karte als HTML, GeoJSON, CSV, RDF, je nach Szenario).

Ohne laufende Demo genügt das Backend-Image:

```bash
docker run --rm -v "$PWD/geofact-output:/app/output" ghcr.io/erichsdb/geofact-project-backend \
  geofact run examples/sachsen_nachthimmel.yaml
```

Alle Befehle, Schalter und Exit-Codes: [README, Abschnitt „CLI“](../README.md#cli-kern).

## 4. Images ohne Registry laden

Jedes Release enthält die Images zusätzlich als Archiv, je eines pro Prozessorarchitektur. Damit
braucht der Rechner keinen Zugriff auf die Registry:

| Rechner | Archiv |
|---|---|
| Intel oder AMD (Windows, Linux, Intel-Mac) | `geofact-demo-images-amd64.tar.gz` |
| Apple Silicon (M1 und neuer), andere ARM-Rechner | `geofact-demo-images-arm64.tar.gz` |

```bash
docker load -i geofact-demo-images-amd64.tar.gz
GEOFACT_VERSION=v1.0-abgabe docker compose -f docker-compose.demo.yml up
```

`GEOFACT_VERSION` ist das Tag des Releases (unter PowerShell: `$env:GEOFACT_VERSION = "v1.0-abgabe"`).

## 5. Fehlersuche

| Beobachtung | Ursache und Abhilfe |
|---|---|
| `port is already allocated` | Port 3000 oder 8000 ist belegt; das andere Programm beenden. Der Backend-Port 8000 ist in das Frontend-Image eingebaut und lässt sich nicht umlegen. |
| Oberfläche lädt, meldet aber „Backend nicht erreichbar“ | Das Backend startet noch (bis zu 30 Sekunden); `docker compose -f docker-compose.demo.yml ps` zeigt `healthy`, sobald es bereit ist. |
| Ein Lauf endet mit einem Netz- oder Zeitfehler | Das Szenario gehört nicht zum Datenpaket oder wurde geändert und fragt einen externen Dienst. Erneut versuchen oder ein Szenario aus `LIESMICH.md` wählen. |
| „Lauf nicht mehr vorhanden“ | Läufe liegen im Arbeitsspeicher des Backends; nach einem Neustart erneut ausführen. |
| `pull access denied` | Die Images sind (noch) nicht öffentlich; Abschnitt 4 verwenden oder selbst bauen (Abschnitt 7). |

## 6. Ohne Docker

```bash
uv sync --extra web
uv run pytest -q                                   # Tests, offline
uv run geofact run examples/crs_showcase/drei_crs_eine_stadt.yaml   # läuft ohne Netz und ohne Zusatzdaten
uv run --extra web geofact-api                     # Backend, http://127.0.0.1:8000
cd frontend && npm install && npm run dev          # Oberfläche, http://localhost:3000
```

Ohne das Datenpaket beziehen die Szenarien ihre Daten aus dem Netz; drei brauchen lokale Raster
(siehe README, Abschnitt „Beispiele“). Das Datenpaket `geofact-demo-data.tar.gz` (Release
`demo-data`) lässt sich auch hier nutzen: `snapshots/` nach `.geofact/snapshots/` und
`examples-data/` nach `examples/data/` entpacken.

Das Beispiel `sachsen_netz_resilienz` braucht auf der Kommandozeile den mitgelieferten Plugin-Ordner:
`GEOFACT_PLUGIN_PATH=examples/plugins uv run geofact run examples/sachsen_netz_resilienz.yaml`
(PowerShell: `$env:GEOFACT_PLUGIN_PATH = "examples/plugins"`). Das Backend der Web-Demo und die Images
setzen ihn selbst.

## 7. Images und Datenpaket selbst erzeugen

Aus dem Quelltext bauen statt aus der Registry laden:

```bash
docker compose -f docker-compose.demo.yml -f docker-compose.demo.build.yml up --build
```

Das Datenpaket entsteht auf einem Rechner, der die Quellraster unter `examples/data/` hat:

```bash
uv run python scripts/build_demo_data.py                        # füllt demo-data/, schreibt MANIFEST.md und das Archiv
docker build -f Dockerfile.backend --target demo -t geofact-backend:demo .
uv run python scripts/build_demo_data.py --verify geofact-backend:demo   # jedes Szenario im Image ohne Netz
```

Das Skript führt jedes Szenario einmal aus, merkt sich, welche Snapshots es liest, kopiert genau
diese nach `demo-data/snapshots/` und schneidet die Raster auf Sachsen zu (gleiches Raster, gleiche
Zellwerte). Ausgenommene Szenarien und der Grund stehen im Manifest.

Veröffentlichen: das Archiv als Anhang des Releases `demo-data` hochladen, dann ein Tag setzen. Der
Workflow `.github/workflows/images.yml` baut daraus beide Images für amd64 und arm64, prüft das
Backend-Image mit einem Offline-Szenario und dem Health-Check und legt sie in der GitHub Container
Registry ab.

```bash
gh release create demo-data demo-data/geofact-demo-data.tar.gz --title "Demo-Datenpaket" --notes-file demo-data/MANIFEST.md
git tag v1.0-abgabe && git push origin v1.0-abgabe
```
