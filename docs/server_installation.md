# Serverpaket: GeoFACT mit OSM-Datenbank Deutschland

Ein Befehl richtet GeoFACT vollständig ein: die aktuelle Version des Werkzeugs, eine
PostGIS-Datenbank mit den OSM-Daten für ganz Deutschland (gleiches Schema wie die Instanz
der Arbeit, beschrieben in `docker/osm/pipeline.lua`) und die Beispieldaten. Die Daten werden beim ersten
Start geladen, nicht mit den Images ausgeliefert, und lassen sich später im laufenden
Betrieb aktualisieren (FA98).

Wer nur ausprobieren will, ohne Datenbank und in fünf Minuten: `docs/schnellstart.md`.

## Voraussetzungen

| | |
|---|---|
| Software | Docker Engine oder Docker Desktop mit Compose 2.23 oder neuer (`docker compose version`) |
| Arbeitsspeicher | 16 GB empfohlen (der Import nutzt rund 4 GB Cache plus PostgreSQL) |
| Speicherplatz | rund 250 GB frei: Extrakt 4,5 GB, Zwischentabellen beim Import, Datenbank, GHS-POP-Raster 6,5 GB. Beim Aktualisieren liegen alter und neuer Stand kurz nebeneinander. |
| Netz | beim ersten Start rund 10 GB Download (Geofabrik, JRC, Zenodo) |
| Dauer | der erste Import je nach Rechner 2 bis 8 Stunden; danach startet alles in Sekunden |

Intel/AMD und ARM (Apple Silicon) werden beide unterstützt.

## Installieren

```bash
mkdir geofact && cd geofact
curl -fsSLO https://github.com/erichsdb/GeoFACT-project/releases/latest/download/docker-compose.server.yml
docker compose -f docker-compose.server.yml up -d
```

Der Befehl kehrt sofort zurück. Danach:

- **Oberfläche:** `http://localhost/` bzw. `http://<IP des Rechners>/` aus dem LAN
- **API-Dokumentation:** `http://localhost/api/docs`

Bis der erste Import fertig ist, zeigt die Adresse die Seite „GeoFACT wird eingerichtet“
und lädt sich alle 30 Sekunden neu. Den Fortschritt zeigt:

```bash
docker compose -f docker-compose.server.yml logs -f osm-import data-init
```

`docker compose -f docker-compose.server.yml ps -a` zeigt den Stand aller Dienste;
`osm-import` und `data-init` enden nach getaner Arbeit mit `Exited (0)`. Endet einer mit
einem anderen Code, nennt sein Log den Grund (etwa eine falsche MD5-Summe nach einem
gestörten Download). `up -d` startet ihn erneut, ein abgebrochener Download wird dabei
fortgesetzt.

Tipp zum Ausprobieren: Mit `GEOFACT_OSM_PBF_URL=https://download.geofabrik.de/europe/germany/sachsen-latest.osm.pbf`
in der `.env` dauert der Import nur Minuten. Die Sachsen- und Leipzig-Beispiele laufen damit,
die deutschlandweiten nicht.

## Einstellungen

Alle Einstellungen sind optional. Sie stehen in einer Datei `.env` neben der Compose-Datei.
Die Vorlage mit allen Werten ist `.env.server.example`; sie liegt ebenfalls am Release.

```bash
curl -fsSL -o .env https://github.com/erichsdb/GeoFACT-project/releases/latest/download/.env.server.example
```

| Ziel | Einstellung |
|---|---|
| nur auf diesem Rechner | `GEOFACT_BIND=127.0.0.1` |
| im LAN (Standard) | nichts; Port 80 auf allen Schnittstellen |
| Port 80 ist belegt | `GEOFACT_HTTP_PORT=8080` → `http://<Rechner>:8080/` |
| von außen mit HTTPS | `GEOFACT_SITE_ADDRESS=geofact.example.org`; der DNS-Eintrag zeigt auf den Rechner, Ports 80 und 443 sind offen. Caddy holt das Zertifikat selbst. |
| Anmeldung | `GEOFACT_WEB_USERS` und `GEOFACT_WEB_SECRET_KEY`, siehe unten |
| feste Version | `GEOFACT_VERSION=v1.0-abgabe` |

Ohne öffentliche IP-Adresse geht der Zugang von außen auch über einen Tunnel, etwa
Tailscale Funnel oder Cloudflare Tunnel, der auf `http://localhost:80` zeigt. Das Paket
richtet keinen Tunnel ein.

**Anmeldung einschalten.** Das ist empfohlen, sobald die Adresse von außen erreichbar ist.
Ohne Anmeldung kann jeder, der die Adresse kennt, Läufe starten.

```bash
docker compose -f docker-compose.server.yml run --rm --no-deps backend python scripts/add_web_user.py gast
# fragt das Passwort ab und gibt "gast:<salt>.<hash>" aus
```

Die Ausgabe kommt in die `.env` als `GEOFACT_WEB_USERS=gast:…`, mehrere Konten
kommagetrennt. Dazu ein fester Schlüssel, z. B. `GEOFACT_WEB_SECRET_KEY=` mit 64 zufälligen
Hex-Zeichen. Danach `docker compose -f docker-compose.server.yml up -d`.

## Aktualisieren

Die Daten liegen in Docker-Volumes und überstehen jedes Update der Images.

| Was | Befehl | Wirkung |
|---|---|---|
| OSM-Daten | `docker compose -f docker-compose.server.yml run --rm osm-import update` | lädt den aktuellen Extrakt, importiert ihn in ein eigenes Schema und tauscht ihn erst nach Erfolg in einer Transaktion aus. GeoFACT läuft währenddessen mit dem alten Stand weiter; schlägt der Import fehl, bleibt der alte Stand. |
| Beispieldaten | `docker compose -f docker-compose.server.yml run --rm data-init update` | lädt GHS-POP und die Europa-Netzdaten neu und prüft sie |
| Werkzeug | `GEOFACT_VERSION` in der `.env` ändern, dann `docker compose -f docker-compose.server.yml pull` und `… up -d` | neue Images; `data-init` übernimmt beim Start die Raster des neuen Images |

Den aktuellen Stand nennen:

```bash
docker compose -f docker-compose.server.yml run --rm osm-import status   # Quelle, OSM-Datenstand, Zeilen
docker compose -f docker-compose.server.yml run --rm data-init status    # Datei, Quelle, Zeitpunkt
```

## Aufbau

Alle Dienste hängen an einem Docker-Netz `geofact` und sprechen sich mit ihrem Dienstnamen
an. Nach außen offen ist nur der Proxy.

```
Browser ──► proxy (Caddy, :80/:443) ──► /api/*  ──► backend:8000 ──► postgis:5432
                                    └─► sonst   ──► frontend:3000
osm-import ──► postgis      (einmalig, und bei "update")
data-init  ──► Volume examples_data   (einmalig, und bei "update")
```

| Dienst | Image | Aufgabe |
|---|---|---|
| `postgis` | `…-osm` | PostgreSQL 17 mit PostGIS; Datenbank `osm`, Schema `osm` im `search_path` |
| `osm-import` | `…-osm` | `docker/osm/import.sh`: Download mit MD5-Prüfung, Import mit osm2pgsql flex (`docker/osm/pipeline.lua`) in `osm_import`, dann Tausch gegen `osm`. Der Stand steht in `osm.geofact_import`. |
| `data-init` | `…-backend` | `docker/data_init.py`: Raster des Images ins Volume, dazu GHS-POP 2025 global (JRC) und PyPSA-Eur-Netz (Zenodo); Stand in `DATENSTAND.json` |
| `backend` | `…-backend` | Web-Backend mit `GEOFACT_OSM_BACKEND=postgis`; wartet mit `docker/wait_ready.py`, bis Beispieldaten und Import da sind |
| `frontend` | `…-frontend-proxy` | Oberfläche, gebaut mit leerem `NEXT_PUBLIC_API_BASE`: ruft `/api` unter derselben Adresse auf und funktioniert so unter jeder Adresse; startet, sobald das Backend antwortet |
| `proxy` | `caddy:2.8` | einzige veröffentlichte Ports; Ereignisströme der Läufe ungepuffert; Einrichtungsseite, solange Backend oder Frontend fehlen |

Volumes: `pg_data` (Datenbank), `osm_work` (Extrakt), `examples_data` (Beispieldaten),
`web_data` (Uploads, gespeicherte Szenarien), `caddy_data` (Zertifikate).
`docker compose -f docker-compose.server.yml down` lässt sie stehen; `down -v` löscht sie
mit allen Daten.

## Grenzen

- **Klon heißt hier:** gleiches Schema, gleiche Quelle (Geofabrik Deutschland), neu
  importiert. Es ist kein Abzug der Datenbank der Arbeit, der OSM-Datenstand ist also der
  vom Tag der Installation. Den genauen Stand nennt `osm-import status`.
- `docker/osm/pipeline.lua` ist aus dem introspektierten Schema der Instanz der Arbeit nachgebaut. Tabellen,
  Spalten, Typen und Indizes stimmen überein; welche geschlossenen Wege als Fläche gelten,
  kann vom Stil der NAS-Instanz abweichen. Liegt das Original vor, ersetzt es diese Datei.
- Keine minütlichen Updates: `osm2pgsql-replication` bräuchte eine aktualisierbare
  Datenbank (`--slim` ohne `--drop`) und etwa doppelt so viel Platz. Aktualisiert wird mit
  `osm-import update` durch einen vollständigen Neuimport.
- Solange das Repository privat ist, brauchen der Download vom Release und `docker pull`
  ein Konto mit Zugriff (`docker login ghcr.io`). Nach der Veröffentlichung entfällt das.
