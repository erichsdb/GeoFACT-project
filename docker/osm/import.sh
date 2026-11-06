#!/bin/sh
# Implements: FA98 (OSM-Import des Serverpakets)
#
# Lädt einen Geofabrik-Extrakt (Standard: Deutschland) und importiert ihn mit osm2pgsql
# flex (/styles/pipeline.lua) in das Schema "osm", das für die Datenbank im search_path
# vor "public" steht - der Kern fragt osm_points/osm_lines/osm_polygons ohne Schemanamen ab.
#
#   geofact-osm-import ensure   importiert nur, wenn es noch keinen Stand gibt (Standard)
#   geofact-osm-import update   lädt den Extrakt neu und importiert ihn, auch im Betrieb
#   geofact-osm-import status   nennt Quelle und Zeitpunkt des Stands
#
# Ein Import schreibt in das Schema osm_import und tauscht es erst nach Erfolg in EINER
# Transaktion gegen osm aus: Abfragen sehen immer einen vollständigen Stand, ein
# fehlgeschlagener Import lässt den bisherigen unberührt und endet mit Fehler.
#
# Umgebung: PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE (libpq), dazu
#   GEOFACT_OSM_PBF_URL       Extrakt (Standard: Geofabrik Deutschland)
#   GEOFACT_OSM_PBF_MD5_URL   MD5-Datei dazu (Standard: <URL>.md5; "none" prüft nicht)
#   GEOFACT_OSM_CACHE_MB      osm2pgsql --cache (Standard 4000)
#   GEOFACT_OSM_PROCESSES     osm2pgsql --number-processes (Standard: Anzahl Kerne)
#   GEOFACT_OSM_WORK_DIR      Download-Verzeichnis (Standard /work, ein Volume)
set -eu

LIVE=osm
STAGE=osm_import
OLD=osm_previous
STYLE=/styles/pipeline.lua
URL="${GEOFACT_OSM_PBF_URL:-https://download.geofabrik.de/europe/germany-latest.osm.pbf}"
MD5_URL="${GEOFACT_OSM_PBF_MD5_URL:-$URL.md5}"
CACHE_MB="${GEOFACT_OSM_CACHE_MB:-4000}"
PROCESSES="${GEOFACT_OSM_PROCESSES:-$(nproc)}"
WORK="${GEOFACT_OSM_WORK_DIR:-/work}"
PBF="$WORK/extract.osm.pbf"

# Meldungen auf stderr: stdout von download() ist die MD5-Summe.
log() { printf '[osm-import] %s\n' "$*" >&2; }
die() { printf '[osm-import] FEHLER: %s\n' "$*" >&2; exit 1; }

sql() { psql -v ON_ERROR_STOP=1 -X -q -tA -c "$1"; }

wait_for_db() {
    i=0
    until pg_isready -q; do
        i=$((i + 1))
        [ "$i" -le 150 ] || die "Datenbank $PGHOST:${PGPORT:-5432} nach 5 Minuten nicht erreichbar"
        sleep 2
    done
}

has_stand() {
    [ "$(sql "SELECT count(*) FROM information_schema.tables
              WHERE table_schema = '$LIVE' AND table_name = 'geofact_import'")" = 1 ]
}

prepare_db() {
    sql "CREATE EXTENSION IF NOT EXISTS postgis"
    # Für neue Verbindungen: osm vor public, damit der Kern ohne Schemanamen abfragt.
    sql "ALTER DATABASE \"$PGDATABASE\" SET search_path = $LIVE, public"
}

status() {
    if has_stand; then
        sql "SELECT 'Quelle:        ' || source_url || E'\n' ||
                    'OSM-Datenstand: ' || coalesce(osm_data_timestamp, 'unbekannt') || E'\n' ||
                    'Importiert:    ' || to_char(imported_at, 'YYYY-MM-DD HH24:MI TZ') || E'\n' ||
                    'MD5 Extrakt:   ' || pbf_md5 || E'\n' ||
                    'osm2pgsql:     ' || osm2pgsql_version
             FROM $LIVE.geofact_import"
        for t in osm_points osm_lines osm_polygons; do
            printf '%-14s ~%s Zeilen\n' "$t:" \
                "$(sql "SELECT reltuples::bigint FROM pg_class c JOIN pg_namespace n
                        ON n.oid = c.relnamespace WHERE n.nspname = '$LIVE' AND c.relname = '$t'")"
        done
    else
        log "noch kein Stand importiert"
        return 1
    fi
}

download() {
    mkdir -p "$WORK"
    expected=""
    if [ "$MD5_URL" != none ]; then
        expected="$(curl -fsSL "$MD5_URL" | cut -d' ' -f1)" \
            || die "MD5-Datei nicht abrufbar: $MD5_URL (GEOFACT_OSM_PBF_MD5_URL=none schaltet die Prüfung bewusst ab)"
        [ -n "$expected" ] || die "MD5-Datei leer: $MD5_URL"
        if [ -f "$PBF" ] && [ "$(md5sum "$PBF" | cut -d' ' -f1)" = "$expected" ]; then
            log "Extrakt liegt schon vor und passt zur MD5-Summe"
            echo "$expected"
            return
        fi
    fi
    rm -f "$PBF"
    log "lade $URL (ein Abbruch wird beim nächsten Start fortgesetzt)"
    # Fortschritt als Logzeile je Minute statt als Balken (docker compose logs).
    ( while sleep 60; do
          [ -f "$PBF.part" ] && log "geladen: $(du -h "$PBF.part" | cut -f1)"
      done ) &
    watcher=$!
    # -C -: setzt eine abgebrochene .part-Datei fort.
    if ! curl -fL --retry 5 --retry-delay 10 -C - -sS -o "$PBF.part" "$URL"; then
        kill "$watcher" 2>/dev/null || true
        # Ein vollständiges .part (Abbruch vor dem Umbenennen) beantwortet der Server mit 416.
        if [ -z "$expected" ] || [ ! -f "$PBF.part" ]             || [ "$(md5sum "$PBF.part" | cut -d' ' -f1)" != "$expected" ]; then
            die "Download fehlgeschlagen: $URL"
        fi
    fi
    kill "$watcher" 2>/dev/null || true
    actual="$(md5sum "$PBF.part" | cut -d' ' -f1)"
    if [ -n "$expected" ] && [ "$actual" != "$expected" ]; then
        rm -f "$PBF.part"
        die "MD5-Summe passt nicht (erwartet $expected, erhalten $actual) - der Extrakt hat sich während des Downloads geändert oder ist beschädigt; erneut starten"
    fi
    mv "$PBF.part" "$PBF"
    echo "$actual"
}

import() {
    md5="$(download)"
    log "importiere in Schema $STAGE (dauert für Deutschland mehrere Stunden)"
    sql "DROP SCHEMA IF EXISTS $STAGE CASCADE; CREATE SCHEMA $STAGE"
    osm2pgsql --create --slim --drop --output=flex --style="$STYLE" \
        --schema="$STAGE" --cache="$CACHE_MB" --number-processes="$PROCESSES" \
        --log-progress=true --database="$PGDATABASE" "$PBF" \
        || die "osm2pgsql fehlgeschlagen - der bisherige Stand bleibt unverändert"

    version="$(osm2pgsql --version 2>&1 | head -n 1)"
    osm_ts="$(sql "SELECT value FROM $STAGE.osm2pgsql_properties
                   WHERE property = 'import_timestamp'" 2>/dev/null || true)"
    sql "CREATE TABLE $STAGE.geofact_import AS SELECT
             now() AS imported_at,
             '$URL'::text AS source_url,
             '$md5'::text AS pbf_md5,
             nullif('$osm_ts', '')::text AS osm_data_timestamp,
             '$(sha256sum "$STYLE" | cut -d' ' -f1)'::text AS style_sha256,
             '$version'::text AS osm2pgsql_version"

    log "tausche $STAGE gegen $LIVE"
    psql -v ON_ERROR_STOP=1 -X -q <<SQL
BEGIN;
DROP SCHEMA IF EXISTS $OLD CASCADE;
DO \$\$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = '$LIVE') THEN
        ALTER SCHEMA $LIVE RENAME TO $OLD;
    END IF;
END \$\$;
ALTER SCHEMA $STAGE RENAME TO $LIVE;
COMMIT;
DROP SCHEMA IF EXISTS $OLD CASCADE;
SQL
    log "fertig"
    status
}

cmd="${1:-ensure}"
case "$cmd" in
    ensure|update|status) ;;
    *) die "unbekannter Befehl '$cmd' (ensure | update | status)" ;;
esac

wait_for_db
prepare_db

if [ "$cmd" = status ]; then
    status
    exit
fi

# Nur ein Import zur Zeit (etwa "up" und ein paralleles "run osm-import update").
mkdir -p "$WORK"
exec 9>"$WORK/import.lock"
flock -n 9 || die "es läuft schon ein Import (Sperre $WORK/import.lock)"

if [ "$cmd" = ensure ] && has_stand; then
    log "Stand vorhanden, kein Import (Neuladen: run --rm osm-import update)"
    status
    exit
fi
import
