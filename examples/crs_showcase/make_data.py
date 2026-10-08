"""Implements: FA55, FA57, FA58 (data of the example "Drei CRS, eine Stadt").

One-off, checked-in script. Builds the three small, synthetic data sets of
``drei_crs_eine_stadt.yaml`` deterministically from the MIT test fixture
``tests/fixtures/chemnitz_street_grid.geojson`` (a street grid between Chemnitz
main station and the TU, see ``tests/fixtures/make_street_grid_fixture.py``).
Nothing here comes from OSM or an official data set. Run with:

    uv run python examples/crs_showcase/make_data.py

The three files deliberately use three different coordinate reference systems
for the same few square kilometres of Chemnitz:

- ``chemnitz_strassen_wgs84.geojson``: the street grid unchanged, geographic
  degrees (OGC CRS84 = WGS 84 lon/lat; x ~ 12.93, y ~ 50.83).
- ``chemnitz_haltestellen_utm33.geojson``: 24 synthetic stops in ETRS89 / UTM
  zone 33N metres (EPSG:25833; x ~ 3.4e5, y ~ 5.63e6), written WITHOUT a GeoJSON
  ``crs`` member. A GeoJSON file without ``crs`` is WGS 84 by definition
  (RFC 7946), so the reader reports EPSG:4326 - the example has to declare the
  real CRS with ``crs: EPSG:25833`` and ``crs_override: true`` (FA55/FA57).
  18 stops lie 20 m beside a street, 6 in the middle of a block (about 140 m
  from the nearest street, so outside the 100 m buffer of the example).
- ``chemnitz_stadtteile_gk4.{shp,shx,dbf,prj,cpg}``: 3 x 3 synthetic districts as
  axis-parallel rectangles in DHDN / 3-degree Gauss-Krueger zone 4 metres
  (EPSG:31468; x ~ 4.56e6, y ~ 5.63e6), with a ``.prj`` file.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import box

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GRID = HERE.parents[1] / "tests" / "fixtures" / "chemnitz_street_grid.geojson"

STREETS = DATA / "chemnitz_strassen_wgs84.geojson"
STOPS = DATA / "chemnitz_haltestellen_utm33.geojson"
DISTRICTS = DATA / "chemnitz_stadtteile_gk4.shp"

# Grid of the fixture (lon/lat): 5 columns, 8 rows.
COLUMNS = [12.922, 12.926, 12.930, 12.934, 12.938]
ROWS = [50.810, 50.815, 50.820, 50.825, 50.830, 50.835, 50.840, 50.845]
# Extent of the districts (lon/lat), a little larger than the street grid;
# the region of the example (bbox literal) lies inside it.
DISTRICT_EXTENT = (12.912, 50.802, 12.948, 50.853)
DISTRICT_NAMES = [
    ["Sued-West", "Sued", "Sued-Ost"],
    ["West", "Mitte", "Ost"],
    ["Nord-West", "Nord", "Nord-Ost"],
]
OFFSET_LON = 0.00028  # ~20 m east of a north-south street


def write_streets() -> int:
    """The street grid unchanged (it already is CRS84 GeoJSON)."""
    STREETS.write_text(GRID.read_text(encoding="utf-8"), encoding="utf-8")
    return len(gpd.read_file(STREETS))


def stop_positions() -> list[tuple[str, str, float, float]]:
    """(id, name, lon, lat): 18 stops beside the streets, 6 in block centres."""
    stops: list[tuple[str, str, float, float]] = []
    # Beside the north-south streets, half way between two rows, in a
    # checkerboard pattern over the 5 columns x 7 row gaps (18 positions).
    for i, lon in enumerate(COLUMNS):
        for j, lat in enumerate(ROWS[:-1]):
            if (i + j) % 2 == 0:
                stops.append(
                    (
                        f"h{len(stops) + 1:02d}",
                        f"Haltestelle {len(stops) + 1}",
                        lon + OFFSET_LON,
                        lat + 0.0025,
                    )
                )
    # Block centres: half way between two columns and two rows - about 140 m
    # from the nearest street, so outside a 100 m buffer.
    centres = [(0, 0), (1, 2), (2, 4), (3, 6), (0, 5), (3, 1)]
    for ci, ri in centres:
        lon = (COLUMNS[ci] + COLUMNS[ci + 1]) / 2
        lat = (ROWS[ri] + ROWS[ri + 1]) / 2
        stops.append(
            (
                f"h{len(stops) + 1:02d}",
                f"Haltestelle {len(stops) + 1} (abseits)",
                lon,
                lat,
            )
        )
    return stops


def write_stops() -> int:
    """GeoJSON with UTM 33 metres and no ``crs`` member (written by hand,
    because GDAL would add the member)."""
    to_utm = Transformer.from_crs("EPSG:4326", "EPSG:25833", always_xy=True)
    features = []
    for stop_id, name, lon, lat in stop_positions():
        x, y = to_utm.transform(lon, lat)
        features.append(
            {
                "type": "Feature",
                "properties": {"id": stop_id, "name": name},
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(x, 2), round(y, 2)],
                },
            }
        )
    payload = {
        "type": "FeatureCollection",
        "name": "chemnitz_haltestellen_utm33",
        "features": features,
    }
    STOPS.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    return len(features)


def write_districts() -> int:
    """3 x 3 rectangles, axis-parallel in Gauss-Krueger zone 4."""
    to_gk = Transformer.from_crs("EPSG:4326", "EPSG:31468", always_xy=True)
    minlon, minlat, maxlon, maxlat = DISTRICT_EXTENT
    xs, ys = to_gk.transform(
        [minlon, maxlon, minlon, maxlon], [minlat, minlat, maxlat, maxlat]
    )
    minx, maxx = round(min(xs)), round(max(xs))
    miny, maxy = round(min(ys)), round(max(ys))
    dx, dy = (maxx - minx) / 3, (maxy - miny) / 3
    ids, names, geoms = [], [], []
    for row in range(3):
        for col in range(3):
            ids.append(f"s{row * 3 + col + 1}")
            names.append(DISTRICT_NAMES[row][col])
            geoms.append(
                box(
                    minx + col * dx,
                    miny + row * dy,
                    minx + (col + 1) * dx,
                    miny + (row + 1) * dy,
                )
            )
    gdf = gpd.GeoDataFrame({"id": ids, "name": names}, geometry=geoms, crs="EPSG:31468")
    gdf.to_file(DISTRICTS, driver="ESRI Shapefile", encoding="utf-8")
    return len(gdf)


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    print(f"{write_streets()} Straßen -> {STREETS.name}")
    print(f"{write_stops()} Haltestellen -> {STOPS.name}")
    print(f"{write_districts()} Stadtteile -> {DISTRICTS.name}")


if __name__ == "__main__":
    main()
