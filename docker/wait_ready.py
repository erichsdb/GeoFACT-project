"""Implements: FA98 (Start des Backends im Serverpaket erst mit vollständigen Daten).

Waits until the example data are in place (DATENSTAND.json written by data_init.py) and
the OSM import has finished (table osm.geofact_import, written only after the schema swap
in docker/osm/import.sh), then replaces itself with the given command:

    python /app/docker/wait_ready.py geofact-api

Waiting inside the container instead of compose ``depends_on: service_completed_successfully``
keeps ``docker compose up -d`` from blocking for the hours of the first import; meanwhile
the proxy answers with its "wird eingerichtet" page. Every minute it logs what it still
waits for, so a failed import is visible in ``docker compose logs backend``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import psycopg

DATA_DIR = Path(os.environ.get("GEOFACT_COPERNICUS_DIR", "/app/examples/data"))
STATE_FILE = "DATENSTAND.json"
POLL_S = 10
REPORT_S = 60


def data_ready(data_dir: Path = DATA_DIR) -> bool:
    return (data_dir / STATE_FILE).is_file()


def osm_ready(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn:
            row = conn.execute("SELECT to_regclass('osm.geofact_import')").fetchone()
    except psycopg.Error:
        return False
    return row is not None and row[0] is not None


def missing(dsn: str, data_dir: Path = DATA_DIR) -> list[str]:
    out = []
    if not data_ready(data_dir):
        out.append(f"Beispieldaten ({data_dir / STATE_FILE}; Dienst data-init)")
    if not osm_ready(dsn):
        out.append("OSM-Import (Tabelle osm.geofact_import; Dienst osm-import)")
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print("Aufruf: wait_ready.py <Befehl> [Argumente]", file=sys.stderr)
        return 2
    dsn = os.environ.get("GEOFACT_PG_DSN")
    if not dsn:
        print("[wait-ready] FEHLER: GEOFACT_PG_DSN nicht gesetzt", file=sys.stderr)
        return 2
    last_report = 0.0
    while todo := missing(dsn):
        if time.monotonic() - last_report >= REPORT_S:
            print(
                f"[wait-ready] warte auf: {', '.join(todo)}",
                file=sys.stderr,
                flush=True,
            )
            last_report = time.monotonic()
        time.sleep(POLL_S)
    print(
        "[wait-ready] Daten vollständig, starte " + " ".join(argv),
        file=sys.stderr,
        flush=True,
    )
    os.execvp(argv[0], argv)
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
