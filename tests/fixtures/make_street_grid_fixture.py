"""Implements: FA50, FA51 (Test-Fixture fuer die Routing-Szenarien).

Einmalig ausfuehrbares, eingechecktes Skript. Erzeugt deterministisch das kleine
Strassenraster `chemnitz_street_grid.geojson` (< 10 KB, EPSG:4326) zwischen Hauptbahnhof
und TU Chemnitz, wie der OSM-Konnektor es liefern wuerde: Linien mit `highway` und `name`,
eine gemeinsame Koordinate an jeder Kreuzung, und ein Kreisverkehr als geschlossener Weg,
den der Konnektor als POLYGON liefert. Ausfuehren mit:

    uv run python tests/fixtures/make_street_grid_fixture.py

Aufbau (lon/lat):

- 5 Spalten (lon 12.922 ... 12.938, Abstand 0.004 Grad = ca. 281 m) und 8 Zeilen
  (lat 50.810 ... 50.845, Abstand 0.005 Grad = ca. 556 m); die mittlere Spalte
  (lon 12.930) ist die Hauptstrasse (`primary`), alle anderen `residential`.
- Kreisverkehr bei (12.930, 50.835): ein Viereck aus den Punkten N/E/S/W; die vier
  Arme (Spalte nach Norden und Sueden, Zeile nach Westen und Osten) enden an seinen
  Ecken. Ohne die Umrandung des Rings (polygons: boundary) waere die Hauptstrasse dort
  unterbrochen.
- Hauptbahnhof (50.8396, 12.9306) und TU (50.8158, 12.9295) liegen 61 m bzw. 96 m neben
  Kreuzungen der mittleren Spalte; die kuerzeste Route fuehrt geradeaus durch den
  Kreisverkehr (ca. 2802 m), ohne Ring muesste sie eine Spalte seitwaerts ausweichen
  (ca. 3343 m).
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Polygon

TARGET = Path(__file__).resolve().parent / "chemnitz_street_grid.geojson"

COLUMNS = [12.922, 12.926, 12.930, 12.934, 12.938]
ROWS = [50.810, 50.815, 50.820, 50.825, 50.830, 50.835, 50.840, 50.845]
CENTER = (12.930, 50.835)
RING_N = (12.930, 50.8353)
RING_S = (12.930, 50.8347)
RING_W = (12.9296, 50.835)
RING_E = (12.9304, 50.835)


def build() -> gpd.GeoDataFrame:
    names, highways, geometries = [], [], []

    def add(name: str, highway: str, coords: list[tuple[float, float]]) -> None:
        names.append(name)
        highways.append(highway)
        geometries.append(LineString(coords))

    for lon in COLUMNS:
        if lon == CENTER[0]:
            continue
        add(f"Querstrasse {lon:.3f}", "residential", [(lon, lat) for lat in ROWS])
    # Hauptstrasse: oberer und unterer Arm enden am Kreisverkehr
    add("Hauptstrasse", "primary", [(CENTER[0], 50.845), (CENTER[0], 50.840), RING_N])
    add(
        "Hauptstrasse",
        "primary",
        [RING_S] + [(CENTER[0], lat) for lat in reversed(ROWS[:5])],
    )
    for lat in ROWS:
        if lat == CENTER[1]:
            continue
        add(f"Allee {lat:.3f}", "residential", [(lon, lat) for lon in COLUMNS])
    west = [(lon, CENTER[1]) for lon in COLUMNS[:2]] + [RING_W]
    east = [RING_E] + [(lon, CENTER[1]) for lon in COLUMNS[3:]]
    add("Allee 50.835", "residential", west)
    add("Allee 50.835", "residential", east)

    ring = Polygon([RING_N, RING_E, RING_S, RING_W])
    names.append("Kreisverkehr")
    highways.append("primary")
    geometries.append(ring)
    return gpd.GeoDataFrame(
        {"name": names, "highway": highways}, geometry=geometries, crs="EPSG:4326"
    )


def main() -> None:
    gdf = build()
    gdf.to_file(TARGET, driver="GeoJSON")
    print(f"{len(gdf)} Objekte -> {TARGET} ({TARGET.stat().st_size} Bytes)")


if __name__ == "__main__":
    main()
