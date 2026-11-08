# GeoFACT auf Linux installieren

**1. Docker installieren und starten** (entfällt, wenn `docker compose version` schon antwortet):

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
```

**2. GeoFACT herunterladen und starten:**

```bash
mkdir ~/geofact && cd ~/geofact
curl -fsSLO https://github.com/erichsdb/GeoFACT-project/releases/latest/download/docker-compose.server.yml
sudo docker compose -f docker-compose.server.yml up -d
```

**3. Öffnen:** `http://<IP des Servers>/`

Beim ersten Start werden die OSM-Daten für Deutschland geladen und importiert. Das dauert
einige Stunden, bis dahin zeigt die Seite „GeoFACT wird eingerichtet“. Fortschritt:

```bash
sudo docker compose -f docker-compose.server.yml logs -f osm-import
```

Der Server braucht dafür etwa 16 GB RAM und 250 GB freien Platz.

## Schneller Test mit Sachsen

Statt Deutschland nur Sachsen importieren (Minuten statt Stunden, wenige GB). Vor dem
ersten `up -d` im Ordner `~/geofact` ausführen:

```bash
cat > .env <<'EOF'
GEOFACT_OSM_PBF_URL=https://download.geofabrik.de/europe/germany/sachsen-latest.osm.pbf
GEOFACT_GHS_POP_URL=none
EOF
sudo docker compose -f docker-compose.server.yml up -d
```

Die Sachsen- und Leipzig-Beispiele laufen damit, die deutschlandweiten nicht.

## Einstellungen ändern

Einstellungen stehen in der Datei `.env` neben `docker-compose.server.yml`, eine Zeile je
Wert (`NAME=wert`). Datei bearbeiten, dann übernehmen:

```bash
nano .env
sudo docker compose -f docker-compose.server.yml up -d
```

`up -d` startet nur die Dienste neu, deren Einstellung sich geändert hat.

| Zeile | Wirkung |
|---|---|
| `GEOFACT_SITE_ADDRESS=geofact.example.org` | Domain mit HTTPS (DNS zeigt auf den Server, Ports 80 und 443 offen) |
| `GEOFACT_HTTP_PORT=8080` | anderer Port, wenn 80 belegt ist |
| `GEOFACT_BIND=127.0.0.1` | nur auf dem Server selbst erreichbar |
| `GEOFACT_WEB_USERS=name:hash` | Anmeldung einschalten; den Wert erzeugt `sudo docker compose -f docker-compose.server.yml run --rm --no-deps backend python scripts/add_web_user.py name` |
| `GEOFACT_OSM_CACHE_MB=2000` | für Server mit 8 GB RAM |

Alle Werte mit Erklärung: `env.server.example` am Release.

**Vom Sachsen-Test zu Deutschland wechseln:** Die OSM-Daten werden nur beim ersten Start
importiert. Nach dem Ändern der Quelle also neu importieren:

```bash
nano .env        # Zeile GEOFACT_OSM_PBF_URL löschen (Standard = Deutschland), ebenso GEOFACT_GHS_POP_URL
sudo docker compose -f docker-compose.server.yml up -d
sudo docker compose -f docker-compose.server.yml run --rm osm-import update
sudo docker compose -f docker-compose.server.yml run --rm data-init update
```

GeoFACT läuft währenddessen mit den Sachsen-Daten weiter.

Mehr zu Aufbau und Aktualisierung: `docs/server_installation.md`.
