"""Test helper for the example examples/deutschland_gruenste_grossstadt_osm.yaml (FA59-FA62,
FA64, FA70): tiny synthetic stand-ins for its two layers per city, built in memory (no file,
no network). Used by the acceptance net (tests/e2e/test_examples_acceptance.py) and by the
example's own end-to-end test (tests/e2e/test_deutschland_gruenste_grossstadt_e2e.py).

The picture:

* every city of the parameter ``cities`` (read from the example, so the doubles follow the
  list) is a small rectangle of 0.10 x 0.06 degrees on a grid over Germany; the region
  fetcher answers "<Stadt>, Deutschland" with that rectangle and "Deutschland" with a box
  around all of them;
* the OSM stand-in for ``gruen_<stadt>`` gives two or three polygons inside the city of that
  layer's OWN region (FA64): a western block whose width sets the green share, a small block
  overlapping it (counted once by ``area_share``, ``dissolve: true``) and for every third
  city a block that sticks out of the city (cut at the border). The share of city k is
  ``share_of(k)``; all shares differ, so the ranking is fixed without running the pipeline.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Callable

import geopandas as gpd
import yaml
from shapely.geometry import box, mapping

EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "deutschland_gruenste_grossstadt_osm.yaml"
)

CITY_WIDTH = 0.10
CITY_HEIGHT = 0.06
GERMANY = (5.8, 47.2, 15.1, 55.1)


@lru_cache(maxsize=1)
def default_cities() -> tuple[tuple[str, str], ...]:
    """(id, region) of the default parameter list of the example."""
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    return tuple((c["id"], c["region"]) for c in raw["parameters"]["cities"]["default"])


def _index(region: str) -> int:
    regions = [r for _, r in default_cities()]
    return regions.index(region)


def city_box(region: str) -> tuple[float, float, float, float]:
    """The rectangle that stands in for the boundary of a city (WGS84)."""
    k = _index(region)
    minx = 6.0 + (k % 10) * 0.9
    miny = 47.6 + (k // 10) * 0.9
    return (minx, miny, minx + CITY_WIDTH, miny + CITY_HEIGHT)


def share_of(k: int) -> float:
    """Green share of city k: distinct for every k, between 0.05 and 0.85 (approximately,
    in degrees; the planar share in UTM differs by far less than the gaps)."""
    return round(0.05 + 0.8 * ((k * 0.6180339887) % 1), 4)


def region_fetcher(name: str) -> list[dict]:
    """Nominatim stand-in: the 80 cities and Germany as rectangles."""
    if name == "Deutschland":
        return [
            {
                "geojson": mapping(box(*GERMANY)),
                "display_name": "Deutschland",
                "class": "boundary",
                "type": "administrative",
            }
        ]
    if name in {r for _, r in default_cities()}:
        return [
            {
                "geojson": mapping(box(*city_box(name))),
                "display_name": name,
                "class": "boundary",
                "type": "administrative",
            }
        ]
    return []


def green_polygons(region: str) -> gpd.GeoDataFrame:
    """The OSM stand-in for the green areas of one city (polygons only, as with
    ``geometry: polygon``)."""
    k = _index(region)
    minx, miny, maxx, maxy = city_box(region)
    width = (maxx - minx) * share_of(k)
    geoms = [
        box(minx, miny, minx + width, maxy),  # sets the share
        box(
            minx + width * 0.25, miny + 0.01, minx + width * 0.75, miny + 0.03
        ),  # overlaps
    ]
    names = ["Stadtwald", "Park im Wald"]
    if k % 3 == 0:  # sticks out to the west: only the part inside the city counts
        geoms.append(box(minx - 0.05, miny, minx + width * 0.5, maxy))
        names.append("Wald am Stadtrand")
    return gpd.GeoDataFrame(
        {
            "name": names,
            "leisure": [None, "park", None][: len(geoms)],
            "landuse": ["forest", None, "forest"][: len(geoms)],
            "natural": [None] * len(geoms),
        },
        geometry=geoms,
        crs="EPSG:4326",
    )


def _green_loader(layer) -> gpd.GeoDataFrame:
    assert layer.region, f"layer '{layer.id}' has no region of its own (FA64)"
    return green_polygons(layer.region)


def overrides(cities: tuple[tuple[str, str], ...] | None = None) -> dict[str, Callable]:
    """Connector overrides for the OSM layers ``stadt[<stadt>].gruen`` (FA75 ids); the
    boundaries (``source: region``) load for real through ``region_fetcher``."""
    return {
        f"stadt[{city_id}].gruen": _green_loader
        for city_id, _ in (cities or default_cities())
    }
