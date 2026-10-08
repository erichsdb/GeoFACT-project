"""Test helper for the example examples/sachsen_nachthimmel.yaml (FA12, web demo): tiny
synthetic stand-ins for its four layers, built in memory (no file, no network). Used by the
acceptance net (tests/e2e/test_examples_acceptance.py) and by the example's own end-to-end
test (tests/e2e/test_sachsen_nachthimmel_e2e.py).

The picture, in the area of the Saxony region fixture (13.6-13.9 E, 50.95-51.15 N):

* 25 viewpoints on a grid of 5 longitudes x 5 latitudes. The two southern rows lie in
  bright light (DN 30), the three northern rows in the dark (DN 2); the light edge at
  51.05 N is more than 3 km from any 2 km surrounding, so every mean is exactly 30 or 2.
* each viewpoint has a parking lot to its east; in the column 13.76 E it is 1.12 km away
  (just beyond the 1000 m of the example), in the others 0.28 to 0.70 km.
* the elevation is a plane that rises to the north-east, so every viewpoint has its own
  height and the expected order can be written down without running the pipeline.

Suitable are therefore 3 northern rows x 4 reachable columns = 12 sites; the example keeps
the ten highest."""

from __future__ import annotations

from typing import Callable

import geopandas as gpd
import numpy as np
from pyproj import CRS
from rasterio.transform import from_origin

from geofact.api import RasterLayer

# Covers the region fixture and the 2 km surroundings of every site.
BOUNDS = (13.5, 50.85, 14.0, 51.25)
LIGHT_CELL = 0.005  # degrees, about 350 x 560 m (the real raster has 1 km)
# The height of a site is read in a circle of 100 m, which has to contain cell centres: the
# elevation raster is as fine as the real one (90 m), not as coarse as the night light.
ELEVATION_CELL = 0.001  # degrees, about 70 x 110 m

LONS = (13.64, 13.70, 13.76, 13.82, 13.88)
BRIGHT_LATS = (50.98, 51.01)
DARK_LATS = (51.08, 51.11, 51.14)
LIGHT_EDGE_LAT = 51.05
BRIGHT, DARK = 30.0, 2.0

# Parking lot east of the viewpoint, in degrees of longitude (1 degree = about 70 km here).
PARKING_OFFSET = {13.64: 0.004, 13.70: 0.004, 13.76: 0.016, 13.82: 0.010, 13.88: 0.004}
UNREACHABLE_LON = 13.76

TOP_N = 10


def elevation_m(lon: float, lat: float) -> float:
    """Height of the synthetic terrain plane in metres."""
    return 200.0 + 1500.0 * (lon - BOUNDS[0]) + 1000.0 * (lat - BOUNDS[1])


def site_id(lon: float, lat: float) -> str:
    return f"{lon:.2f}/{lat:.2f}"


def all_sites() -> list[tuple[float, float]]:
    return [(lon, lat) for lon in LONS for lat in (*BRIGHT_LATS, *DARK_LATS)]


def suitable_sites() -> list[tuple[float, float]]:
    """Dark and with a parking lot within 1000 m, highest first."""
    sites = [(lon, lat) for lon in LONS if lon != UNREACHABLE_LON for lat in DARK_LATS]
    return sorted(sites, key=lambda site: elevation_m(*site), reverse=True)


def expected_ranking() -> list[str]:
    """Ids of the sites the example must return, in rank order."""
    return [site_id(*site) for site in suitable_sites()[:TOP_N]]


def viewpoints() -> gpd.GeoDataFrame:
    sites = all_sites()
    return gpd.GeoDataFrame(
        {
            "id": [site_id(*site) for site in sites],
            "name": [f"Aussicht {number}" for number in range(len(sites))],
            "tourism": "viewpoint",
        },
        geometry=gpd.points_from_xy(
            [lon for lon, _ in sites], [lat for _, lat in sites]
        ),
        crs="EPSG:4326",
    )


def parking_lots() -> gpd.GeoDataFrame:
    sites = all_sites()
    return gpd.GeoDataFrame(
        {"id": [f"p{site_id(*site)}" for site in sites], "amenity": "parking"},
        geometry=gpd.points_from_xy(
            [lon + PARKING_OFFSET[lon] for lon, _ in sites], [lat for _, lat in sites]
        ),
        crs="EPSG:4326",
    )


def _raster(
    value_at: Callable[[np.ndarray, np.ndarray], np.ndarray], cell: float
) -> RasterLayer:
    """A raster over BOUNDS whose cell value is a function of the cell centre (lon, lat)."""
    minx, miny, maxx, maxy = BOUNDS
    cols, rows = round((maxx - minx) / cell), round((maxy - miny) / cell)
    lon = minx + (np.arange(cols) + 0.5) * cell
    lat = maxy - (np.arange(rows) + 0.5) * cell
    lon_grid, lat_grid = np.meshgrid(lon, lat)
    return RasterLayer(
        data=value_at(lon_grid, lat_grid).astype("float32"),
        transform=from_origin(minx, maxy, cell, cell),
        crs=CRS.from_epsg(4326),
        path="synthetic",
    )


def night_light() -> RasterLayer:
    return _raster(
        lambda lon, lat: np.where(lat > LIGHT_EDGE_LAT, DARK, BRIGHT), LIGHT_CELL
    )


def elevation() -> RasterLayer:
    return _raster(elevation_m, ELEVATION_CELL)


def nachthimmel_overrides() -> dict[str, Callable]:
    """Connector overrides for the four layers of the example (layer id -> loader)."""
    return {
        "aussichtspunkte": lambda layer: viewpoints(),
        "parkplaetze": lambda layer: parking_lots(),
        "nachtlicht": lambda layer: night_light(),
        "hoehe": lambda layer: elevation(),
    }
