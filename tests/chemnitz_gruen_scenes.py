"""Test helper for the example examples/chemnitz_gruenanteil_osm.yaml (FA68-FA70): tiny
synthetic stand-ins for its two OSM layers, built in memory (no file, no network). Used by
the acceptance net (tests/e2e/test_examples_acceptance.py) and by the example's own
end-to-end test (tests/e2e/test_chemnitz_gruenanteil_osm_e2e.py).

The picture, inside the Nominatim fixture of Chemnitz (12.73-13.05 E, 50.74-50.90 N):

* 3 x 3 rectangular districts over 12.75-13.03 E, 50.76-50.88 N (ids z0..z8, column-wise
  from the south-west, the same construction as the other Chemnitz stand-ins).
* ten green areas: 'Stadtpark' twice (a large relation and a small way inside it - the
  typical OSM duplicate), 'Kuechwald' across the border of z2 and z5 with an unnamed grass
  area lying completely inside it (counted once by area_share), 'Zeisigwald' in two
  separate pieces (both count), 'Struth' reaching far beyond the east edge of the districts
  (only its part inside counts), 'Schlossteich' and two unnamed woods. Districts z1, z3 and
  z7 have no green at all."""

from __future__ import annotations

from typing import Callable

import geopandas as gpd
from shapely.geometry import box

DISTRICT_BOUNDS = (12.75, 50.76, 13.03, 50.88)
EMPTY_DISTRICTS = ("z1", "z3", "z7")

# (name, leisure, landuse, natural, (minx, miny, maxx, maxy))
GREEN_AREAS: list[tuple[str | None, str | None, str | None, str | None, tuple]] = [
    ("Stadtpark", "park", None, None, (12.86, 50.81, 12.90, 50.83)),
    ("Stadtpark", "park", None, None, (12.87, 50.815, 12.88, 50.82)),
    ("Küchwald", None, "forest", None, (12.82, 50.845, 12.87, 50.865)),
    (None, None, "grass", None, (12.84, 50.85, 12.86, 50.86)),
    ("Zeisigwald", None, None, "wood", (12.95, 50.77, 12.99, 50.79)),
    ("Zeisigwald", None, None, "wood", (12.995, 50.765, 13.0, 50.77)),
    ("Struth", None, "forest", None, (13.02, 50.762, 13.04, 50.768)),
    (None, None, None, "wood", (12.76, 50.77, 12.77, 50.775)),
    ("Schlossteich", "park", None, None, (12.91, 50.845, 12.92, 50.85)),
    (None, None, "forest", None, (13.00, 50.85, 13.02, 50.87)),
]


def districts() -> gpd.GeoDataFrame:
    """3 x 3 districts with 'id' and 'name' (stand-in for the OSM boundary relations)."""
    minx, miny, maxx, maxy = DISTRICT_BOUNDS
    dx, dy = (maxx - minx) / 3, (maxy - miny) / 3
    geoms, ids, names = [], [], []
    for i in range(3):
        for j in range(3):
            geoms.append(
                box(
                    minx + i * dx,
                    miny + j * dy,
                    minx + (i + 1) * dx,
                    miny + (j + 1) * dy,
                )
            )
            ids.append(f"z{i * 3 + j}")
            names.append(f"Ortsteil {i * 3 + j + 1}")
    return gpd.GeoDataFrame({"id": ids, "name": names}, geometry=geoms, crs="EPSG:4326")


def green_areas() -> gpd.GeoDataFrame:
    """The ten green areas with the whitelisted OSM columns of the example."""
    return gpd.GeoDataFrame(
        {
            "name": [row[0] for row in GREEN_AREAS],
            "leisure": [row[1] for row in GREEN_AREAS],
            "landuse": [row[2] for row in GREEN_AREAS],
            "natural": [row[3] for row in GREEN_AREAS],
        },
        geometry=[box(*row[4]) for row in GREEN_AREAS],
        crs="EPSG:4326",
    )


def gruen_overrides() -> dict[str, Callable[[object], gpd.GeoDataFrame]]:
    """Connector overrides (layer id -> loader) for the example."""
    return {
        "stadtteile": lambda layer: districts(),
        "gruen": lambda layer: green_areas(),
    }
