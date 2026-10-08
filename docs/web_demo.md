# GeoFACT Web-Demo (Backend + Frontend)

Interaktive Oberfläche, um GeoFACT-Pipelines zu demonstrieren:
Datenquellen waehlen/hochladen, Konfiguration per Prompt erzeugen, im
Editor anpassen, ausführen und dabei den Ausführungsgraphen **live**
verfolgen – pro Knoten das Ergebnis als Karte, am Ende Download.

```
┌──────────────┐    HTTP/SSE     ┌───────────────────────────┐
│  Next.js      │  ◀──────────▶  │  FastAPI (geofact_web)     │
│  (Frontend)   │                │  ─ ruft geofact in-process │
└──────────────┘                 │  ─ OSM: lokaler PostGIS-   │
                                  │    Dump · Copernicus: lokal│
                                  └────────────┬──────────────┘
                                               │
                                     ┌─────────▼─────────┐
                                     │  geofact (Kern)   │
                                     └───────────────────┘
```

Das Backend ruft den **Framework-Kern in-process** auf – kein Subprozess,
keine Serialisierung dazwischen. Änderungen am Framework schlagen so
direkt durch; die HTTP-Schicht ist bewusst dünn (Adapter-Muster).

**Das Backend kennt den Kern nur über `geofact.api`** (FA44/FA45; geprüft von
`tests/unit/test_architecture_layers.py`, Regel
`backend-imports-only-api-plugin-api-support`; Ausnahme `geofact.support.http`
für die Endpunkt-Proben des API-Katalogs). Laden, Prüfen, Planen, Ausführen
und das Schreiben der Ausgaben sind derselbe Anwendungsfall (`api.load_scenario`
/ `api.validate` / `api.plan` / `api.run`), den auch `geofact run` nutzt - siehe
Abschnitt „Ausführung eines Laufs“.

## Was das Framework dafür zusätzlich bietet

Die Web-Demo brauchte Fähigkeiten, die der Kern vorher nicht hatte –
sie sind als reguläre Framework-Erweiterungen umgesetzt:

| Modul | Zweck |
|---|---|
| `geofact/engine/events.py` + Executor-Hook | Live-Ereignisse (plan → layer → step → **step_warning** → run) für den DAG-Zustand; ohne Callback läuft der Executor still. Warnungen der Operationen (z. B. getrennte Netzkomponenten) und beim Laden eines Layers werden eingefangen statt auf stderr verloren (`step_warning`, `layer_warning`); ein Ladefehler kommt als `layer_error`, ein Abbruch als `run_cancelled` (additive Typen, das Frontend ignoriert unbekannte). Abbruch läuft über das `CancelToken` des Laufs, das der Executor an jeder Layer-/Schrittgrenze prüft |
| `backend/geofact_web/presentation/serialize.py` | In-Memory-Serialisierung je Ergebnistyp (Vektor→GeoJSON, Graph→Node-Link, Raster→Bounds + Wertestatistik) für Per-Node-Ansichten (seit FA44/FA45 im Backend statt im Kern; `view_columns.py` daneben wählt die angezeigten Attributspalten) |
| `geofact/engine/catalog.py` | Operations-Katalog inkl. **vollständiger Parameter-Schemata** (Typ/Pflicht/Default/Auswahl/Grenze), **abgeleitet aus `Params.model_json_schema()`**, + Scenario-JSON-Schema (Schritte samt `Params` der Operation) – Grounding für Editor und LLM |
| `geofact/core/contracts.py` (`Step.Params`) | Jede Operation deklariert ihre Parameter als verschachtelte `class Params(ParamsBase)` (Typen, Grenzen, Defaults, Querprüfungen); es gibt keine zweite, handgepflegte Beschreibung |
| `geofact/engine/outputs.py` | Führt die deklarierten `output:`-Specs aus; `api.run(..., out_dir=...)` ruft es am Ende des Laufs (FA44) |
| `geofact/engine/run.py` (`geofact.api`) | Der Anwendungsfall (FA44): `ScenarioDocument`, `validate` mit deutschen Meldungen samt Position, `run` mit Abbruch, Store, Beobachter und Ausgabeverzeichnis |

## Voraussetzungen

- Python ≥ 3.11, [`uv`](https://docs.astral.sh/uv/)
- Node ≥ 18, npm
- Für OSM live: lokale PostGIS-Instanz mit importiertem OSM-Dump
  (osm2pgsql flex output: `osm_points`/`osm_lines`/`osm_polygons`, Spalte `geom`, EPSG:4326)
- Für Copernicus/Raster: lokale GeoTIFFs in einem Verzeichnis

## Backend starten

```bash
# Abhängigkeiten (Kern + Web-Extra)
uv sync --extra web

# Konfiguration (Beispiel)
export GEOFACT_OSM_BACKEND=postgis                 # lokaler OSM-Dump
export GEOFACT_PG_DSN=postgresql://user:pass@localhost:5432/osm
export GEOFACT_COPERNICUS_DIR=/data/copernicus     # lokale GeoTIFFs
export OPENAI_BASE_URL=https://openrouter.ai/api/v1
export OPENAI_API_KEY=sk-or-...                    # OpenRouter-Key
export GEOFACT_LLM_MODEL=anthropic/claude-3.5-sonnet

# Starten (http://127.0.0.1:8000)
uv run --extra web geofact-api
# oder mit Auto-Reload:
GEOFACT_WEB_RELOAD=1 uv run --extra web geofact-api
```

**Ein Backend je Datenverzeichnis und Port.** Läufe liegen im Arbeitsspeicher genau eines
Backend-Prozesses (siehe „Lauf nicht mehr vorhanden“ unten). Ein zweites Backend, das dieselbe
Portnummer an einer anderen Adresse belegt (unter Windows möglich: das erste lauscht an
`0.0.0.0:8000`, das zweite an `127.0.0.1:8000`), teilt diese Läufe nicht - Anfragen landen dann mal
bei dem einen, mal bei dem anderen Prozess. `geofact-api` warnt beim Start, wenn auf dem Port schon
ein Prozess lauscht (Verbindungsversuch auf Loopback und die konfigurierte Adresse; nur ein Hinweis,
kein Abbruch). Dann das alte Backend beenden oder `GEOFACT_WEB_PORT` ändern.

**Auto-Reload beobachtet nur den Quelltext des Backends:** mit `GEOFACT_WEB_RELOAD` startet uvicorn
das Backend nur neu, wenn sich eine `.py`-Datei unter `src/geofact` oder `backend/geofact_web`
ändert. Änderungen an `tests/`, `examples/`, `docs/` oder am Frontend lösen keinen Neustart aus -
jeder Neustart verwirft alle Läufe im Arbeitsspeicher.

Health-Check: `curl http://127.0.0.1:8000/api/health` → `{"status":"ok","checks":{...}}` (`"degraded"`, wenn das aktive PostGIS-Backend nicht erreichbar ist).
API-Spezifikation (FA85, OpenAPI 3.1) bei laufendem Backend:

| Adresse | Inhalt |
|---|---|
| `http://127.0.0.1:8000/docs` | Swagger UI: jede Operation ausprobieren („Try it out“); bei aktivierter Anmeldung zuerst `POST /api/auth/login` aufrufen und das Token unter „Authorize“ eintragen |
| `http://127.0.0.1:8000/redoc` | ReDoc: dieselbe Spezifikation zum Lesen |
| `http://127.0.0.1:8000/openapi.json` | die Spezifikation als JSON (z. B. zum Import in Bruno oder Postman) |

`/docs` und `/redoc` laden ihre Oberfläche von einem CDN (Internet nötig), `/openapi.json` nicht. Alle
drei sind ohne Token erreichbar. Dieselbe Spezifikation liegt eingecheckt in `docs/openapi.json` - sie
wird erzeugt, nicht von Hand bearbeitet. Nach jeder Änderung an einer Route oder einem Antwortmodell:

```bash
uv run --extra web python scripts/export_openapi.py          # docs/openapi.json neu schreiben
uv run --extra web python scripts/export_openapi.py --check  # nur vergleichen (Exit 1 bei Abweichung)
```

Der Test `test_fa81_committed_spec_matches_the_code` schlägt fehl, solange die Datei veraltet ist.

### Wichtige Umgebungsvariablen

| Variable | Default | Bedeutung |
|---|---|---|
| `GEOFACT_OSM_BACKEND` | `overpass` | `postgis` für den lokalen Dump; im Einstellungsdialog je Browser abweichend wählbar (FA82) |
| `GEOFACT_PG_DSN` | – | DSN der PostGIS-Instanz |
| `GEOFACT_PG_QUERY_TIMEOUT_S` | `120` | Hartes Limit (statement_timeout) je PostGIS-Abfrage - verhindert, dass eine grosse/ungefilterte Abfrage (z. B. deutschlandweite Tabelle ohne enge Region) den Serverprozess unbeendbar macht (NFA2) |
| `GEOFACT_COPERNICUS_DIR` | `examples/data` | Verzeichnis mit lokalen Rastern |
| `GEOFACT_SAMPLE_SOURCES` | Paketdatei `data/sample_sources.yaml` | Beschreibung der mitgelieferten Beispieldaten (FA90); die Dateien selbst liegen unter `examples/` |
| `GEOFACT_API_CATALOG` | mitgelieferte Paketdatei `geofact_web/data/api_catalog.yaml` | FA36: Pfad des kuratierten API-Katalogs; der Standard liegt im Paket (mit `importlib.resources` gelesen, also auch im Wheel und im Docker-Image) |
| `GEOFACT_WEB_BASE_DIR` | Repo-Root | FA4/FA44: Verzeichnis, gegen das relative Pfade in einem Szenario-**Text ohne Ursprung** gelten (Editor, LLM-Ausgabe, gespeicherte Szenarien, `config` als JSON). Mitgelieferte Beispiele tragen ihren Ursprung selbst (s. u.) |
| `GEOFACT_PLUGIN_PATH` | `examples/plugins` | FA94: ohne Angabe lädt das Backend die mitgelieferten Beispiel-Plugins (Fach-Plugin `power_grid_osm` für `sachsen_netz_resilienz`); eine gesetzte Variable gilt unverändert. FA40: zusätzliche Plugin-Ordner (`os.pathsep`-getrennt: `;` unter Windows, `:` sonst); jede `*.py`-Datei und jedes Paket (Ordner mit `__init__.py`) darin wird beim Start erkannt (`api.load_extensions()` im FastAPI-`lifespan`) und erscheint in Katalog, Editor und LLM-Prompt; ein defektes Plugin steht in `GET /api/meta` unter `failed_plugins` |
| `GEOFACT_SNAPSHOT_DIR` | `.geofact/snapshots` (relativ zum Arbeitsverzeichnis des Servers) | FA7: Ablage der Snapshots (OSM, Region, WFS, STAC, ...); gilt für CLI und Web gleich |
| `GEOFACT_REST_*` | – | FA41-FA43: Zugangsdaten für REST-Endpunkte in Szenarien (`${GEOFACT_REST_TOKEN}` in url/headers/query/body). Andere Variablen sind in Szenarien bewusst nicht einsetzbar |
| `GEOFACT_WEB_DATA_DIR` | `.geofact_web` | Datenverzeichnis: gespeicherte Szenarien (`scenarios/`) und die Laufverzeichnisse der Läufe (`runs/`, FA44); `runs/` und `uploads/` stehen in `.gitignore`, `scenarios/` ist eingecheckt |
| `GEOFACT_WEB_UPLOADS_DIR` | `.geofact_web/uploads` | Ablage für Uploads |
| `GEOFACT_WEB_MAX_UPLOAD` | `209715200` (200 MiB) | Obergrenze einer Upload-Datei in Bytes |
| `GEOFACT_WEB_MAX_RETAINED_RUNS` | `20` | Anzahl abgeschlossener Läufe (done/error/cancelled), die im Prozessspeicher bleiben; der älteste wird samt Laufverzeichnis entfernt, laufende Läufe nie |
| `GEOFACT_WEB_RUN_DIR_MAX_AGE_HOURS` | `6` | FA54: Alter in Stunden (Zahl > 0, sonst bricht der Start mit einer Meldung ab), ab dem ein Laufverzeichnis unter `runs/`, das zu keinem Lauf dieses Prozesses gehört, beim Start als Rest eines beendeten Prozesses entfernt wird. Frischere Verzeichnisse (z. B. eines zweiten, noch laufenden Backends) bleiben stehen. Muss über der längsten Laufzeit eines Laufs liegen |
| `GEOFACT_WEB_RELOAD` | – (aus) | Auto-Reload für die Entwicklung (beliebiger nicht-leerer Wert, auch `0`, schaltet ihn ein); beobachtet nur `src/geofact` und `backend/geofact_web` (FA54). Jeder Neustart verwirft alle Läufe |
| `OPENAI_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-kompatibler Endpunkt |
| `OPENAI_API_KEY` / `OPENROUTER_API_KEY` | – | LLM-Key (ohne: Generierung deaktiviert) |
| `GEOFACT_LLM_MODEL` | `anthropic/claude-3.5-sonnet` | Modell-ID |
| `GEOFACT_LLM_REASONING` | `true` | FA31: Reasoning-/Thinking-Ausgabe mitstreamen (`thinking`-SSE-Kanal). Nur wirksam bei Modellen, die Reasoning-Tokens liefern; sonst bleibt der Kanal leer |
| `GEOFACT_LLM_MAX_TOKENS` | `8000` | FA31: Obergrenze der Antwortlänge. Ohne Limit hängt sie am Anbieter-Default und die Generierung kann praktisch endlos laufen (NFA2) |
| `GEOFACT_LLM_DEADLINE_S` | `300` | FA31: Gesamtfrist je LLM-Anfrage in Sekunden (httpx' Timeout gilt nur je Leseoperation) |
| `GEOFACT_LLM_PROVIDER` | – | FA31: Anbieterbindung bei OpenRouter, kommagetrennt (`provider.order`, ohne Ausweichen). Leer = der Router wählt je Anfrage; für reproduzierbare Messungen setzen |
| `GEOFACT_LLM_CHOICES` | – | FA83: weitere wählbare Sprachmodelle, kommagetrennt, je `modell` oder `modell@anbieter1+anbieter2`. Mit Einträgen bietet der Einstellungsdialog das Sprachmodell als Auswahl an; das Modell des Servers steht immer dabei. Ein fehlerhafter Eintrag verhindert den Start |
| `GEOFACT_LLM_REPAIR_ROUNDS` | `3` | FA78: Anfragen an das Modell nach der Erzeugung (Reparatur einer Prüfmeldung, Reparatur eines Abbruchs im Probelauf, Durchsicht der Warnungen); `0` = keine |
| `GEOFACT_LLM_TRIAL_RUN` | `true` | FA78: eine gültige erzeugte Konfiguration einmal ohne Ausgaben ausführen; Abbruch und Warnungen gehen in die Reparatur. Der Probelauf bezieht die Quellen wie ein normaler Lauf (OSM-Backend, WFS, Dateien) und zählt nicht zum Lauf-Limit je Nutzer |
| `GEOFACT_LLM_TRIAL_RUN_S` | `60` | FA78: Frist des Probelaufs in Sekunden (Zahl > 0). Danach bleibt die Konfiguration ungeprüft (`trial_run.status: timeout`); der Lauf endet an der nächsten Schrittgrenze |
| `GEOFACT_WEB_CORS` | `http://localhost:3000,http://127.0.0.1:3000` | erlaubte Frontend-Origins |
| `GEOFACT_WEB_HOST` / `GEOFACT_WEB_PORT` | `127.0.0.1` / `8000` | Bind-Adresse |
| `GEOFACT_WEB_USERS` | – (leer = kein Login nötig) | Zugriffskontrolle (FA24): `name:hash`-Paare, kommagetrennt; per `uv run python scripts/add_web_user.py <name>` erzeugen. Leer = unverändertes Single-User-Verhalten ohne Login |
| `GEOFACT_WEB_SECRET_KEY` | zufällig je Prozessstart | Signaturschlüssel für Zugriffstoken; bei mehreren Worker-Prozessen explizit setzen (sonst validiert jeder Worker nur seine eigenen Tokens) |
| `GEOFACT_WEB_TOKEN_TTL_S` | `43200` (12 h) | Gültigkeitsdauer eines Login-Tokens |
| `GEOFACT_WEB_MAX_CONCURRENT_RUNS_PER_USER` | `2` | gleichzeitig laufende Läufe je Nutzer, bevor `POST /api/runs` mit 429 ablehnt (FA24) |

## Ausführung eines Laufs (FA44)

`POST /api/runs` validiert die Konfiguration (`api.validate(text_oder_dict, base_dir=...)`,
Probleme mit Position auf Deutsch, z. B. `steps -> 0 -> params -> radius_km: muss eine Zahl
sein (erhalten: '5km')`), leitet den Plan ab (`api.plan(document)`, damit der DAG sofort
gezeichnet werden kann) und startet in einem Thread `api.run(document, observer=...,
cancel=..., store=..., out_dir=<Laufverzeichnis>)`. Beim Start des Backends ruft der
FastAPI-`lifespan` einmal `api.load_extensions()` auf; danach ist die Registry eingefroren und
von parallelen Requests ohne Sperre lesbar. Details und Rezepte: `docs/architektur.md`.

- **Ereignisse:** `GET /api/runs/{id}/events` ist ein SSE-Strom mit drei Ereignisarten:
  `status` (Lauf-Status, als erstes und am Ende), `progress` (jedes `ProgressEvent` des
  Executors: `plan`, `layer_loading`, `layer_loaded`, `layer_warning`, `layer_error`,
  `step_running`, `step_warning`, `step_done`, `step_error`, `run_done`, `run_error`,
  `run_cancelled`, seit FA55/FA56 auch `region_ready` und `region_warning`) und `end`
  (Endstatus). Spät verbundene Clients bekommen die bisherigen
  Ereignisse nachgeliefert (Replay), danach live.

- **Ausgaben:** die im Szenario deklarierten Ausgaben werden **einmal am Ende des
  Laufs** in ein Laufverzeichnis unter `<GEOFACT_WEB_DATA_DIR>/runs/` geschrieben,
  nicht bei jedem Download. Ein `type: rest`-Ausgang ruft den Dienst damit einmal je
  Lauf, nicht je Abruf. Das Laufverzeichnis wird mit dem Lauf entfernt, wenn er
  verdrängt wird (`GEOFACT_WEB_MAX_RETAINED_RUNS`). Läufe liegen im Prozessspeicher:
  beim Start des Backends entfernt `RunManager.purge_orphaned_run_dirs()` die
  **veralteten** Laufverzeichnisse eines früheren, beendeten Prozesses - Einträge, die zu
  keinem Lauf dieses Prozesses gehören und älter als
  `GEOFACT_WEB_RUN_DIR_MAX_AGE_HOURS` sind (Standard 6 Stunden, FA54). Frische Verzeichnisse
  eines anderen, gleichzeitig laufenden Backends auf demselben Datenverzeichnis bleiben stehen
  (früher räumte der Start jedes fremde Verzeichnis ab); ein Rest, der sich nicht löschen
  lässt, wird als Warnung gemeldet. Die Läufe selbst bleiben prozesslokal: ein Neustart
  (auch durch `GEOFACT_WEB_RELOAD`) verwirft sie, ihre Verzeichnisse bleiben bis zur Altersgrenze
  liegen.
- **Download** (`GET /api/runs/{id}/download`, erst nach Abschluss, sonst 409): ZIP aus den
  geschriebenen Dateien, dem **ausgeführten Szenario-Text** als `scenario.yaml`
  (Original-Text mit Kommentaren, kein Modell-Dump - ein Dump verlor Layerfelder)
  und - falls Ausgaben übersprungen wurden - `SKIPPED_OUTPUTS.txt` mit Grund. Das
  Backend schreibt dann zusätzlich eine Warnung ins Serverlog.
  Seit FA65 liegt `ATTRIBUTION.txt` bei, wenn eine geschriebene Ausgabe eine Lizenz trägt,
  seit FA59 `parameters.yaml` mit den Überschreibungen des Laufs (nur wenn es welche gibt;
  zusammen mit `scenario.yaml` reproduzierbar per `geofact run scenario.yaml --param ...`).
- **Dateien (FA88):** `GET /api/runs/{id}/outputs` nennt dieselben Dateien wie das ZIP als
  Liste: die geschriebenen Ausgaben mit Format und Quellknoten (`role: output`), die
  Begleitdateien `scenario.yaml`, `ATTRIBUTION.txt`, `parameters.yaml` (`role: companion`)
  und unter `skipped` jede übersprungene Ausgabe mit Grund. `GET /api/runs/{id}/outputs/{name}`
  liefert eine davon als Anhang, mit denselben Bytes wie im ZIP; ein Name, der nicht in
  der Liste steht, ist ein 404 (der Name wird nie als Pfad benutzt). Liste, Einzelabruf
  und ZIP lesen aus einer Aufzählung (`routers/runs.py::_run_files`). Vor dem Abschluss
  und nach einem Fehler gibt es nur die Begleitdateien.
- **Liste der Läufe (FA89):** `GET /api/runs` nennt die Läufe des Nutzers, die das Backend
  noch hält, neuester zuerst (`scenario_name`, `created_at`, `finished_at`, Zahl der
  Schritte, der geschriebenen und der übersprungenen Ausgaben, `parameters`), dazu
  `max_retained`. Ohne konfigurierte Anmeldung teilen sich alle den Nutzer `anonymous`.
  Die Liste ist genau der Inhalt des Prozessspeichers: ein verdrängter Lauf fehlt, nach
  einem Neustart ist sie leer.
- **Parameter (FA59):** `POST /api/config/validate` und `POST /api/runs` nehmen
  `parameters: {name: wert}`. Texte aus dem Formular (`"2.5"`) werden nach dem deklarierten Typ
  gelesen (`api.parameter_overrides`); ein unbekannter Name oder ein unpassender Wert ist ein
  Problem an `parameters -> <name>` (validate: `valid: false`, run: 422). Die Validierungsantwort
  nennt `parameters` (je Parameter `type`, `default`, `choices`, `description`, wirksamer `value`,
  `overridden`) auch bei ungültiger Konfiguration, `expansion` (Knotenzahl, `instances` mit
  `id`, `module`, `keys`, `dropped_keys` (FA75), `dropped` mit Position und `when`-Ausdruck und
  `node_origins` je Knoten für die Vorschau) und `attribution` (Lizenzen der Ausgabequellen, ohne Lauf).
- **Laufstatus (FA58/FA75/FA65/FA56):** `GET /api/runs/{id}` trägt zusätzlich `origins`
  (Herkunft je Knoten aus der Expansion, `NodeOrigin.to_dict()`: `section`, `index`, `module`,
  `instance`, `key`, `inner_id`, `location`; `module: null` = Knoten steht so in der YAML), `layer_provenance`
  (CRS-Herkunft je geladenem Layer aus `layer_loaded.detail.provenance`), `region` (Region und
  Arbeits-CRS aus `region_ready`), `attribution` (Plan, bestätigt durch `run_done`), `warnings`
  (Lauf-Ebene: `region_warning` und Layer-Warnungen ohne Schritt) und je Schritt
  `warning_details` (Meldung mit `category` und `severity` `info`/`warning`). Das
  Knoten-Ergebnis nennt `attribution` des Knotens.
- **Zwischenergebnisse (FA66/FA33):** die Web-Demo ruft `api.run` nie mit
  `release_intermediates`; jeder Knoten bleibt abrufbar (`released` im Status bleibt leer).
- **Vorab-Check (FA44):** ein fehlender Dateipfad scheitert vor jedem Ereignis als
  Konfigurationsfehler mit Position (`layers -> i (id) -> path`); der Lauf endet mit `error`
  und genau einem `run_error`.
- **Abbruch:** `POST /api/runs/{id}/cancel` setzt das `CancelToken` des Laufs; der
  Executor prüft es an jeder Layer- und Schrittgrenze und nach dem letzten Schritt (ein
  Abbruch während des letzten Schritts endet als `cancelled`, ohne `run_done`).
- **Fehler:** ein Layer- oder Schrittfehler nennt Layer/Schritt, Quellart bzw.
  Operation und Herkunft des Bausteins; bereits fertige Zwischenergebnisse bleiben
  abrufbar (FA33).

### Lauf nicht mehr vorhanden (FA54)

Läufe leben nur im Arbeitsspeicher **eines** Backend-Prozesses. Jede Anfrage zu einer `run_id`,
die der antwortende Prozess nicht kennt, endet mit **404** und dieser Meldung (für einen fremden
und einen unbekannten Lauf dieselbe, FA24):

> Lauf '51b37dafc8b1' nicht gefunden - Läufe liegen nur im Arbeitsspeicher des Backends; nach einem Neustart bitte erneut ausführen.

Das heißt in der Praxis meist: das Backend wurde seit dem Lauf **neu gestartet** (von Hand, durch
einen Absturz oder durch den Auto-Reload), oder eine **andere Backend-Instanz** hat geantwortet
(zwei Prozesse auf demselben Port). Ein 404 am Knoten-Endpunkt kann aber auch bedeuten, dass der
Knoten eines bekannten Laufs (noch) kein Ergebnis hat (`Knoten '...' hat (noch) kein Ergebnis.`,
FA33) - die Oberfläche unterscheidet beides, indem sie bei einem 404 die Statusabfrage des Laufs
zurückfragt.

Die Oberfläche erkennt den verlorenen Lauf an jedem Weg zum Lauf (Status, Knoten-Ergebnis,
Rasterbild, Ereignisstrom, Download, Neuladen der Seite): sie entfernt den Lauf aus State und
`localStorage`, kehrt aus der Ergebnisansicht zur Ausführung zurück und zeigt den Hinweis „Lauf
nicht mehr vorhanden“ mit der Schaltfläche **Erneut ausführen** (startet die aktuelle
Konfiguration neu; bei ungültiger Konfiguration deaktiviert). Netzfehler und andere Fehler
(5xx, Knoten ohne Ergebnis) bleiben unverändert.

### Pfadregel für relative Pfade (FA4/FA44)

Eine Szenario-YAML verweist auf Dateien relativ zu **ihrem eigenen Verzeichnis**
(wie bei `geofact run`). Der Editor schickt nur Text, deshalb gilt:

- Der Beispiel-Lader (`GET /api/scenarios/examples/{id}`) stellt dem Text eine Kopfzeile
  voran: `# geofact-origin: leipzig/leipzig13_busnetz_dichte`. Sie bleibt beim Bearbeiten
  und Speichern im Editor stehen; das Backend liest sie und nimmt das Verzeichnis
  der Beispiel-Datei als `base_dir`. Ein unbekannter Ursprung ist ein Validierungsfehler
  (`(Ursprung)`), kein stilles Zurückfallen; der Ursprung kann `examples/` nicht verlassen.
- Text **ohne** Ursprung (selbst geschrieben, vom LLM erzeugt, `config` als JSON) gilt
  gegen `GEOFACT_WEB_BASE_DIR` (Standard: Repo-Root). Das ist eine ausdrückliche
  Einstellung, nie das zufällige Arbeitsverzeichnis des Servers.

### Plugins im Betrieb (FA40)

`GET /api/meta` liefert unter `failed_plugins` (Herkunft → Ursache), welche Plugin-Dateien
beim Start nicht geladen werden konnten - ein defektes Plugin ist im Betrieb sichtbar,
nicht nur im Startlog.

## Frontend starten

```bash
cd frontend
npm install
cp .env.local.example .env.local     # NEXT_PUBLIC_API_BASE ggf. anpassen
npm run dev                          # http://localhost:3000
```

## Bedienung

1. **Einstieg wählen** (FA80): Schritt 1 „Szenario" bietet drei Wege und
   zeigt danach nur, was dazu gehört. Der Einstieg lässt sich über die drei Knöpfe in der Kopfzeile
   in jedem Schritt wechseln; der Editortext bleibt dabei erhalten.
   - **Szenario generieren**: Prompt eingeben und „Konfiguration
     generieren"; rechts lassen sich Quellen und APIs für den Prompt
     vormerken (Checkbox) oder eine Datei hochladen. Das LLM ist auf
     die Registry geerdet (Operationen, Quellarten mit Feldern, Formate,
     Ausgabeformate – auch aus Plugins), sieht drei Referenzszenarien als
     Vorlage und je lokalem Raster Zellgröße und Wertebereich (FA79). Die
     Ausgabe wird geprüft, einmal ohne Ausgaben auf den echten Daten
     ausgeführt und bei einer Prüfmeldung, einem Abbruch oder Warnungen
     bis zu dreimal repariert (FA78). Danach erscheint die Konfiguration im
     Editor, mit dem Ergebnis des Probelaufs als Hinweis darüber (auch in
     `trial_run` der Antwort); „Prompt anpassen" führt zurück.
     Braucht die Frage einen Datensatz, den keine Quelle liefert, nennt
     das Modell ihn statt einer Konfiguration (FA87): der Hinweis steht
     unter dem Prompt, der Editor bleibt unverändert. Datensatz unter
     „Daten vormerken" hochladen, dann „Erneut generieren" - oder die Frage
     umformulieren und nennen, woran sie gemessen werden soll.
   - **Szenario laden**: Beispiel oder eigenes gespeichertes Szenario in
     der Liste wählen (YAML-Vorschau rechts) und „Laden".
   - **Szenario selbst schreiben**: Editor mit dem Katalog daneben;
     „Vorlage“ füllt den leeren Editor mit allen Abschnitten und Platzhaltern (FA91,
     Anleitung: `docs/konfiguration_schreiben.md`), „+ Layer" bzw. eine Operation fügt
     einen Baustein in die YAML ein.
   **Herkunft (FA86):** Die Oberfläche merkt, woher die Konfiguration stammt
   (erzeugt aus einem Prompt, geladenes Szenario, selbst geschrieben; mit der
   Marke „bearbeitet“, sobald der Text abweicht). Eine Leiste über Editor,
   Ausführung und Ergebnis bietet den Sprung zurück: „Prompt anpassen“
   (Prompt und Region wieder gefüllt), „Anderes Szenario wählen“,
   „Konfiguration bearbeiten“. Der Einstieg wechselt über den Umschalter
   in der Kopfzeile und zeigt dort die zuletzt gezeigte Ansicht. Prompt, Region und
   Gedankengang bleiben über Schrittwechsel und Neuladen erhalten; eine
   Erzeugung läuft im Hintergrund weiter und meldet sich per Hinweis.
   Wird die Konfiguration nach einem Lauf geändert, bleibt der Lauf in
   Schritt 3 einsehbar, trägt aber den Hinweis „Dieses Ergebnis gehört zu
   einer früheren Fassung“ mit „Erneut ausführen“.
2. **Editor**: YAML anpassen, Live-Validierung zeigt Fehler mit Position.
3. **Ausführen**: der DAG wird gezeichnet und färbt sich live
   (pending → running → done/error); Knoten mit Warnungen tragen ein ⚠.
   Ein laufender Lauf lässt sich **abbrechen**: er gilt sofort als abgebrochen (FA93);
   ein Baustein, der gerade lädt oder rechnet, endet im Hintergrund an der nächsten
   Schrittgrenze, sein Ergebnis wird verworfen.
   **Parameter (FA59):** deklariert das Szenario `parameters:`, steht über
   dem Graphen ein Formular (Text, Zahl, ja/nein, Auswahl bei `choices`;
   Listen und Zuordnungen als YAML-Feld). Gesetzte Werte überschreiben die
   Defaults für Validierung und Lauf, ohne die YAML zu ändern;
   „Default“ setzt einen Wert zurück, ungültige Eingaben bleiben rot
   markiert und werden nicht übernommen.
   **Module und Instanzen (FA75):** Knoten, die eine Instanz aus einem Modul
   erzeugt, werden je Instanz gruppiert. Eine Instanz mit mehreren Elementen
   (`foreach`) erscheint eingeklappt als ein Knoten „stadt ×80“ mit Modul,
   Zahl der Layer und Schritte, zusammengefasstem Status (Fehler vor läuft vor
   fertig) und Fortschritt fertiger Elemente („37/80“); Kanten zu ihr sind je
   Quelle und Ziel zusammengefasst, `collect` hat dann eine eingehende Kante.
   Ein Klick klappt die Instanz auf: je Element eine Gruppenbox
   `stadt[berlin]` (dagre-Layout mit Gruppen), „Einklappen“ im Kopf der Box
   klappt die ganze Instanz wieder zu. Wird ein Knoten einer eingeklappten
   Instanz ausgewählt (Inspector, „Endergebnis anzeigen“, Fehler), klappt sie
   auf. Rahmen, Gruppenboxen und Abzeichen haben eine Farbe je Modul; das
   Abzeichen nennt den Knoten im Modul (`anteil`), der Tooltip die Position
   (`instances -> stadt[berlin] -> steps -> anteil`). Eine Instanz ohne
   `foreach` ist immer eine Gruppenbox. Knoten-ids mit `[`/`]` werden in URLs
   kodiert (`encodeURIComponent`).
4. **Knoten inspizieren**: einen fertigen Knoten anklicken – das Ergebnis
   erscheint in vier Ansichten: **Karte** (mit Legende), **Tabelle**
   (sortierbar), **Diagramm** (Histogramm + Top-N-Rangliste) und
   **Statistik** (Kennzahlen je numerischem Feld bzw. Rasterwerte).
   Das **Ergebnis** (Schritt 3) steht in voller Breite; der **Ausführungsgraph**
   ist ein Vollbild-Umschalter derselben Ansicht (Taste `g`). Ein Knotenklick im
   Vollbild-Graphen wählt den Knoten und kehrt zum Ergebnis zurück,
   „Endergebnis anzeigen“ führt aus jeder Ansicht zum Endknoten (FA67).
   Fehler und Warnungen des Laufs stehen über beiden Ansichten (FA33).
   Warnungen sind nach Schwere getrennt: Hinweise (`info`, z. B. ein
   wiederverwendeter Snapshot) neutral, Warnungen gelb; Regionswarnungen
   (Region breiter als eine UTM-Zone, unerwartete OSM-Klasse) stehen auf
   Lauf-Ebene (FA56).
   **Herkunft (FA58):** Layer-Knoten zeigen `Quell-CRS → Arbeits-CRS`
   („erzwungen“ bei `crs_override`), der Graph nennt oben links das
   Arbeits-CRS (`auto` = UTM-Zone aus dem Regionszentroid, sonst deklariert).
   Im Inspector-Tab „Statistik“ steht der Block „Herkunft“ mit Quell- und
   Arbeits-CRS, Ausdehnung roh/harmonisiert, Region und – bei erzeugten
   Knoten – Modul, Instanz (`stadt[berlin]`), Knoten im Modul und Position (FA75).
   **Lizenzen (FA65):** Die Kopfzeile der Ergebnisansicht nennt Region,
   Arbeits-CRS und die Lizenzen aller Ausgabequellen (Links auf die
   Lizenztexte, Layer ohne Lizenzangabe gelb); der Inspector nennt unten die
   Datenquellen des gewählten Knotens, die Karte führt sie in ihrer
   Attribution.
   **Ausgaben (FA88):** In der Kopfzeile der Ergebnisansicht steht nach einem
   abgeschlossenen Lauf der Knopf „Ausgaben“ mit der Zahl der Dateien (und einem
   Warnzeichen, wenn eine Ausgabe übersprungen wurde). Er öffnet einen Dialog: jede
   geschriebene Datei mit Format, Quellknoten und Größe als eigener Download,
   übersprungene Ausgaben mit Grund, die Begleitdateien und „Alle als ZIP“. Den Knopf „Ergebnisse“ in der Kopfzeile
   der Seite gibt es nicht mehr.
   **Läufe (FA89):** „Läufe“ in der Kopfzeile der Seite listet die Läufe, die das
   Backend noch hält, je Lauf mit seinen Dateien. „Ansehen“ öffnet einen Lauf in
   Schritt 3, ohne den Editor zu ändern; weicht dessen Text ab, steht dort der
   Hinweis „frühere Fassung“ mit „Konfiguration dieses Laufs übernehmen“.
5. **Operationen-Referenz** (Einstieg „Szenario selbst schreiben", Tab „Operationen"): zeigt Ports
   und Parameter jeder Operation; „+ Schritt" fügt ein Skelett ein.
6. **Szenarien**: über der Editor-Leiste speichern, über den Einstieg
   „Szenario laden" öffnen (Dateisystem).
7. **Einstellungen** (Knopf oben rechts), vorbelegt mit den Werten des Servers; Abweichungen merkt
   sich der Browser, „Standardwerte" setzt sie zurück:
   - **OSM-Quelle** (FA82): `overpass` oder `postgis`, für Läufe und den Probelauf. Zur Wahl steht
     `postgis` nur mit gesetztem `GEOFACT_PG_DSN`; Standard ist `GEOFACT_OSM_BACKEND`.
   - **Sprachmodell** (FA83): nur wenn der Server mit `GEOFACT_LLM_CHOICES` eine Vorauswahl vorgibt;
     Standard ist `GEOFACT_LLM_MODEL` mit `GEOFACT_LLM_PROVIDER`.
   - **Frist je Modellanfrage**, **Probelauf** an/aus und **Frist des Probelaufs** (FA81) für
     „Szenario generieren" (`GEOFACT_LLM_DEADLINE_S`, `GEOFACT_LLM_TRIAL_RUN`,
     `GEOFACT_LLM_TRIAL_RUN_S`); Obergrenzen 1800 s bzw. 600 s.
   - **Dunkler Modus** (FA84): Standard ist hell, unabhängig von der Systemvorgabe; den eigenen
     Knopf im Kopf gibt es nicht mehr.
8. **Download**: nach Abschluss die deklarierten Ausgaben als ZIP
   (inkl. ausgeführter `scenario.yaml`).

**Beispiel für Lizenzen und Namensnennung (FA65):** „Datenquellen und Lizenzen in
Chemnitz“ (`examples/chemnitz_datenquellen_lizenzen.yaml`) verbindet vier Quellen mit
drei Lizenzarten – OSM-Stadtteile (Default ODbL-1.0), Sentinel-2 (Default
CC-BY-SA-3.0-IGO), Messpunkte mit deklarierter `license` (CC0-1.0, „Eigene Erhebung“)
und Bürgerhinweise ohne Lizenzangabe. Jede Ausgabe nennt genau die Lizenzen der
Quellen, aus denen sie entstanden ist: die NDVI-GeoTIFF nur Copernicus, die
Stadtteilkarte Copernicus + OSM, die Punkte am Ende alle drei plus „ohne
Lizenzangabe: hinweise“. Sichtbar in den Karten-HTML (Fußzeile + Leaflet-Attribution),
den GeoTIFF-Tags, der RDF-Ausgabe (`dcterms:license`/`dcterms:rights`) und in
`ATTRIBUTION.txt` im Laufverzeichnis.

## Szenarien per Link teilen (FA72)

Sobald ein Szenario geladen ist (Einstieg „Szenario laden“ oder Link), steht es in der Adresszeile:

| Link | Szenario |
|---|---|
| `http://localhost:3000/?scenario=example:deutschland_gruenste_grossstadt_osm` | mitgeliefertes Beispiel (ID = Pfad relativ zu `examples/` ohne Endung) |
| `http://localhost:3000/?scenario=example:leipzig/leipzig10_erreichbarkeit` | Beispiel in einem Unterordner |
| `http://localhost:3000/?scenario=user:lonely-villages` | gespeichertes Nutzerszenario (ID = Name) |

„Link kopieren“ (neben „Speichern“) legt die absolute Adresse in die Zwischenablage.

- **Öffnen:** Der Link lädt das Szenario in den Editor, zeigt Schritt 1, meldet den Erfolg mit dem Anzeigenamen des Szenarios (Rückfall: die ID) und hat Vorrang vor dem im
  Browser gemerkten YAML, Schritt und Lauf (eine gemerkte `runId` wird nicht wiederverwendet). Ist
  die Anmeldung aktiv (FA24), erscheint zuerst der Login; der Parameter bleibt dabei erhalten.
- **Fehler:** Eine unbekannte ID (404) oder ein Parameter ohne Präfix `example:`/`user:` meldet sich
  als Fehlermeldung und der Parameter wird entfernt; das Frontend fällt nie still auf ein anderes
  Szenario zurück. Andere Ladefehler (abgelaufene Anmeldung 401, Netzwerk, Serverfehler) melden sich
  ebenfalls, lassen den Parameter aber stehen: nach dem Login bzw. einem erneuten Öffnen wird er geladen.
- **Neuladen:** Hat die App den Parameter selbst gesetzt (Szenario über „Szenario laden“), gilt ein Neuladen
  desselben Tabs nicht als geöffneter Link: gemerktes YAML, Schritt und laufender Lauf bleiben erhalten.
  Andere URL-Parameter und der Hash bleiben beim Setzen und Entfernen unverändert.
- **Gültigkeit:** Der Parameter steht nur, solange der Editortext genau dem geladenen Szenario
  entspricht. LLM-Erzeugung, eingefügte Ebenen/Operationen oder Handänderungen entfernen ihn, und
  „Link kopieren“ ist dann deaktiviert. Der Link enthält nur die Referenz, nicht den Text.
- **Grenzen:** Der Link enthält nie ein Token (das `?token=` bleibt auf Ereignisstrom, Rasterbild und
  Download beschränkt). Läufe sind nicht teilbar: sie liegen nur im Arbeitsspeicher und gehören
  einem Nutzer (FA54). Gespeicherte Szenarien sind ein gemeinsamer Pool, ein `user:`-Link gilt
  deshalb nur innerhalb derselben Instanz und desselben Datenverzeichnisses.

## API-Überblick

Maßgeblich ist die Spezifikation (`/docs` bzw. `docs/openapi.json`, FA85) mit Schemas, Medientypen und Fehlercodes; die Tabelle ordnet die Endpunkte den Anforderungen zu.

| Methode | Pfad | Zweck |
|---|---|---|
| POST | `/api/auth/login` | Anmeldung (FA24) - Nutzername+Passwort -> Zugriffstoken; nur relevant wenn `GEOFACT_WEB_USERS` gesetzt ist |
| GET | `/api/health` | Health-Check: PostGIS erreichbar?, Raster-Verzeichnis, LLM konfiguriert? (`status`: `ok` oder `degraded`) |
| GET | `/api/meta` | Server-Status (OSM-Backend, LLM konfiguriert?, nicht geladene Plugins `failed_plugins`) |
| GET | `/api/operations` | Operations-Katalog |
| GET | `/api/schema` | Scenario-JSON-Schema |
| GET | `/api/catalog` | OSM-Presets + Copernicus + eigene Uploads (FA28: nach Eigentümer gefiltert) + mitgelieferte Beispieldaten (`samples`, FA90) |
| GET | `/api/apis` | Kuratierter API-Katalog (FA36): behördliche Schnittstellen mit Endpunkten, Attributen, Lizenz, Layer-Fragment |
| GET | `/api/apis/{id}/spec` | Maschinenlesbare Spezifikation einer Schnittstelle (GetCapabilities/OpenAPI), auf Anfrage geladen (FA36) |
| POST | `/api/uploads` | Datei hochladen → Layer-Template (Eigentümer = anfragender Nutzer, FA28) |
| DELETE | `/api/uploads/{id}` | Eigenen Upload endgültig löschen (FA28); fremde/unbekannte `id` -> 404 |
| POST | `/api/config/validate` | Konfiguration validieren (optional `parameters`; Antwort mit `parameters`, `expansion`, `attribution`, `output_licenses` (FA73, ungeprüft), `planned_steps` (FA75: id, op und Eingänge der Schritte des ausgedehnten Szenarios für den Graphen vor dem Lauf)) |
| POST | `/api/config/licenses` | FA73: Lizenzen je Ausgabe (Quellen + Ausgabelizenz) gegen die DALICC-API prüfen – fragt einen externen Dienst, nur auf Knopfdruck („Lizenzen prüfen (DALICC)“ im Validierungsbereich), angemeldet (FA24); Antwort `valid`, `issues`, `status`, `checks[]` |
| POST | `/api/config/generate` | Prompt → Konfiguration (LLM); optional `options` (`llm_deadline_s`, `trial_run`, `trial_run_s`, FA81 – überschreibt die Werte des Servers für diese Anfrage, auch im Strom-Endpunkt); Antwort mit `validation`, `notes` und `trial_run` (`status: ok\|failed\|timeout\|skipped`, `error`, `warnings`, `repair_rounds`, FA78) und `missing_data` (`[{name, reason, hint, alternative}]`, FA87: nicht leer = dem Modell fehlen Daten, `config_yaml` ist dann leer) |
| POST | `/api/config/generate/stream` | **SSE**: Prompt → Konfiguration mit Live-Feedback. Ereignisse: `thinking` (Reasoning-Text, FA31), `token` (YAML-Deltas), `phase` (`validating`/`repairing`, FA31; `trial_run`/`reviewing`, FA78; nach `repairing` und `reviewing` folgen wieder `thinking` und `token` - sie gehören zur korrigierten Fassung), `done` (Endergebnis mit `trial_run` und `missing_data`, FA87), `error` |
| POST | `/api/runs` | Lauf starten (optional `parameters`, FA59) |
| GET | `/api/runs` | Läufe des Nutzers, die das Backend noch hält, neuester zuerst (FA89) |
| GET | `/api/runs/{id}` | Lauf-Status (inkl. Warnungen je Schritt und Lauf, `origins`, `layer_provenance`, `region`, `attribution`) |
| POST | `/api/runs/{id}/cancel` | Lauf abbrechen |
| GET | `/api/runs/{id}/events` | **SSE**: Live-Fortschritt |
| GET | `/api/runs/{id}/nodes/{node}` | Knoten-Ergebnis (GeoJSON/Graph/Raster-Meta) |
| GET | `/api/runs/{id}/nodes/{node}/raster.png` | Raster-Vorschau (PNG) |
| GET | `/api/runs/{id}/outputs` | Dateien des Laufs: Ausgaben, Begleitdateien, übersprungene Ausgaben (FA88) |
| GET | `/api/runs/{id}/outputs/{name}` | Eine Datei der Liste als Anhang (FA88; Token auch als `?token=`) |
| GET | `/api/runs/{id}/download` | Ergebnis-ZIP: geschriebene Ausgaben + `scenario.yaml` (+ `ATTRIBUTION.txt`, `parameters.yaml`, `SKIPPED_OUTPUTS.txt`), erst nach Abschluss (FA44) |
| GET/POST | `/api/scenarios` | Szenarien auflisten (eigene + die mitgelieferten Beispiele, je Gruppe nach Namen sortiert, auch aus Unterordnern wie `crs_showcase/` und `leipzig/`; id = Pfad ohne Endung) / speichern |
| GET | `/api/scenarios/examples/{id}` | Mitgeliefertes Beispiel laden; dem Text ist die Ursprungs-Kopfzeile `# geofact-origin: <id>` vorangestellt (Pfadregel) |
| GET/DELETE | `/api/scenarios/{name}` | Szenario laden / löschen |

## Tests

```bash
uv run --extra web pytest tests/unit/test_web_framework.py tests/unit/test_web_backend.py tests/unit/test_fa44_web_run.py tests/unit/test_fa24_auth.py tests/unit/test_fa28_upload_isolation.py tests/unit/test_fa54_lost_run.py tests/unit/test_fa54_frontend_lost_run.py tests/unit/test_fa81_openapi_spec.py -q
cd frontend && npm run lint && npm run build     # next build prüft auch die Typen
```

Die Web-Tests laufen ohne Netz (lokale Fixtures, BBox-Region). Die
Die LLM-Generierung wird ohne echten Anbieter getestet: Prompt-Bau, Anfrage, Reparaturschleife und
Probelauf mit festen Modellantworten (`test_fa31_*`, `test_fa78_trial_run_repair.py`,
`test_fa79_prompt_grounding.py`). Die Qualität der Antworten eines Modells misst die LLM-Vorstudie der
Arbeit, nicht die Testsuite.
Ohne das Web-Extra (`uv sync` ohne `--extra web`) überspringen sich die Web-Tests
(`pytest.importorskip`), alle übrigen laufen; mit `uv run --extra web pytest` laufen alle.

## Grenzen (bewusst, Deployment-Ziel „lokale Einzelnutzer-Demo" bzw. „kleine bekannte Gruppe")

- Läufe liegen **im Prozessspeicher** (kein DB/Queue) – Neustart
  verwirft sie (die Oberfläche erklärt das und bietet „Erneut ausführen“ an, FA54);
  ein Backend-Prozess je Datenverzeichnis und Port. Für die Demo ausreichend, für echten
  Mehrbenutzerbetrieb (Self-Signup, Rollen, viele Nutzer) wären
  Persistenz und ein Task-Queue zu ergänzen.
- Uploads sind Einzeldateien; Shapefiles brauchen ihre Sidecars
  (als Verzeichnis/GeoPackage bereitstellen).
- **Zugriffskontrolle (FA24, optional):** `GEOFACT_WEB_USERS` schaltet
  eine minimale Token-Auth für eine kleine, fest hinterlegte
  Nutzerliste ein (kein Self-Signup) und isoliert Läufe pro Nutzer
  (`owner`, Concurrency-Limit). Gespeicherte Szenarien bleiben bewusst
  ein gemeinsamer Pool aller Nutzer, nicht isoliert. Ohne gesetztes
  `GEOFACT_WEB_USERS` bleibt das Backend wie zuvor voll offen (kein
  Login) - passend für lokale Einzelnutzer-Nutzung.
- **Upload-Isolation (FA28):** Uploads sind seit FA28 pro Eigentümer
  isoliert (Katalog zeigt nur eigene Uploads) und über
  `DELETE /api/uploads/{id}` durch ihren Ersteller endgültig löschbar.
  Der Eigentümer wird als kleine Sidecar-Datei (`.owner`) im
  Upload-Unterverzeichnis abgelegt, kein DB-Backend. Ohne konfigurierte
  Nutzer laufen alle Uploads unter dem Pseudo-Nutzer `anonymous` -
  unverändertes Single-User-Verhalten. Gespeicherte Szenarien sind davon
  nicht betroffen (siehe FA24-Punkt oben).
