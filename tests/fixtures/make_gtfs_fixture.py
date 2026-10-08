"""Implements: Grundlage fuer FA20 (Test-Fixture: Mini-GTFS-Zip).

Einmalig ausfuehrbares, eingechecktes Skript (Konvention siehe
make_fixtures.py). Erzeugt deterministisch (feste Koordinaten, keine
Zeitstempel/Zufall) eine winzige, aber strukturell realistische
GTFS-Zip < 100 KB fuer einen Leipzig-Ausschnitt (EPSG:4326). Ausfuehren
mit:

    uv run python tests/fixtures/make_gtfs_fixture.py

Enthaelt:
- stops.txt: 8 Haltestellen
- routes.txt: 3 Routen (1x Tram route_type=0, 2x Bus route_type=3)
- trips.txt: 5 Fahrten (1 Tram-Fahrt, 4 Bus-Fahrten - davon 3 auf
  derselben Buslinie B1 mit ueberlappenden Haltestellenfolgen, um die
  Dedup-Logik zu testen: bus_trip_1/2/3 teilen sich 2 der 3
  aufeinanderfolgenden Haltestellenpaare, bus_trip_1b faehrt eine andere
  Route B2 mit komplett anderen Haltestellen)
- stop_times.txt: Haltestellenfolgen je Fahrt (3-4 Halte je Fahrt)

Die erzeugte Datei wird eingecheckt; das Skript bleibt im Repo, damit
die Fixture bei Bedarf reproduzierbar neu erzeugt werden kann."""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = FIXTURES_DIR / "gtfs_mini.zip"
INCOMPLETE_OUTPUT_PATH = FIXTURES_DIR / "gtfs_mini_missing_stop_times.zip"

# stop_id -> (lat, lon, name) - realistische Leipzig-Koordinaten
STOPS = [
    ("S1", 51.3400, 12.3750, "Hauptbahnhof"),
    ("S2", 51.3420, 12.3800, "Augustusplatz"),
    ("S3", 51.3440, 12.3850, "Wilhelm-Leuschner-Platz"),
    ("S4", 51.3460, 12.3900, "Connewitz"),
    ("S5", 51.3300, 12.3600, "Lindenau"),
    ("S6", 51.3320, 12.3650, "Plagwitz"),
    ("S7", 51.3340, 12.3700, "Schleussig"),
    ("S8", 51.3500, 12.3950, "Stoetteritz"),
]

# route_id -> (route_short_name, route_type)
ROUTES = [
    ("TRAM1", "1", 0),  # Tram
    ("BUS1", "60", 3),  # Bus
    ("BUS2", "70", 3),  # Bus, andere Linie
]

# trip_id -> route_id
TRIPS = [
    ("tram_trip_1", "TRAM1"),
    ("bus_trip_1", "BUS1"),
    ("bus_trip_2", "BUS1"),
    ("bus_trip_3", "BUS1"),
    ("bus_trip_1b", "BUS2"),
]

# trip_id -> ordered list of stop_id (stop_sequence = Index + 1)
# Tram: S1 -> S2 -> S3 -> S4 (3 Kanten, alle eindeutig)
# Bus (BUS1), 3 Fahrten mit ueberlappender Folge:
#   bus_trip_1: S1 -> S2 -> S3   (Kanten S1-S2, S2-S3)
#   bus_trip_2: S1 -> S2 -> S3   (identische Folge -> dedupliziert)
#   bus_trip_3: S2 -> S3 -> S8   (teilt sich Kante S2-S3, neue Kante S3-S8)
#   => eindeutige Kanten fuer BUS1: (S1,S2), (S2,S3), (S3,S8) = 3 Kanten
#      trotz 3 Fahrten mit insgesamt 6 Kanten-Vorkommen (Dedup-Test)
# Bus (BUS2): S5 -> S6 -> S7 (komplett andere Haltestellen)
TRIP_STOPS = {
    "tram_trip_1": ["S1", "S2", "S3", "S4"],
    "bus_trip_1": ["S1", "S2", "S3"],
    "bus_trip_2": ["S1", "S2", "S3"],
    "bus_trip_3": ["S2", "S3", "S8"],
    "bus_trip_1b": ["S5", "S6", "S7"],
}


def _write_csv(
    zf: zipfile.ZipFile, name: str, fieldnames: list[str], rows: list[dict]
) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    zf.writestr(name, buf.getvalue())


def _write_core_files(zf: zipfile.ZipFile) -> None:
    _write_csv(
        zf,
        "stops.txt",
        ["stop_id", "stop_name", "stop_lat", "stop_lon"],
        [
            {"stop_id": sid, "stop_name": name, "stop_lat": lat, "stop_lon": lon}
            for sid, lat, lon, name in STOPS
        ],
    )
    _write_csv(
        zf,
        "routes.txt",
        ["route_id", "route_short_name", "route_type"],
        [
            {"route_id": rid, "route_short_name": short, "route_type": rt}
            for rid, short, rt in ROUTES
        ],
    )
    _write_csv(
        zf,
        "trips.txt",
        ["trip_id", "route_id"],
        [{"trip_id": tid, "route_id": rid} for tid, rid in TRIPS],
    )


def build() -> None:
    with zipfile.ZipFile(OUTPUT_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_core_files(zf)
        stop_time_rows = []
        for trip_id, stop_ids in TRIP_STOPS.items():
            for i, stop_id in enumerate(stop_ids, start=1):
                stop_time_rows.append(
                    {
                        "trip_id": trip_id,
                        "stop_id": stop_id,
                        "stop_sequence": i,
                    }
                )
        _write_csv(
            zf,
            "stop_times.txt",
            ["trip_id", "stop_id", "stop_sequence"],
            stop_time_rows,
        )

    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB)")

    # Zweite, absichtlich unvollstaendige Zip fuer den Pflichtdatei-Test
    # (fehlendes stop_times.txt) - dieselben stops/routes/trips, aber ohne
    # stop_times.txt.
    with zipfile.ZipFile(INCOMPLETE_OUTPUT_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_core_files(zf)

    size_kb2 = INCOMPLETE_OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {INCOMPLETE_OUTPUT_PATH} ({size_kb2:.1f} KB)")


if __name__ == "__main__":
    build()
