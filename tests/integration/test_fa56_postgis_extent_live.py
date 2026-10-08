"""Implements: FA56 (PostGIS-Importausdehnung).

Integrationstests gegen die lokale PostGIS-Instanz (Deutschland-Import).
Ohne GEOFACT_PG_DSN automatisch übersprungen (siehe
tests/conftest.py).
"""

import geopandas as gpd
import pytest
from shapely.geometry import box

from geofact.builtin.sources import osm

pytestmark = pytest.mark.integration


def _region(*bounds: float) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["r"]}, geometry=[box(*bounds)], crs="EPSG:4326")


def test_fa56_postgis_import_extent_covers_germany_live():
    extent = osm.PostgisBackend().import_extent()
    if extent is None:
        pytest.skip("keine Tabellenstatistik für osm_polygons (ANALYZE fehlt)")
    min_lon, min_lat, max_lon, max_lat = extent
    assert min_lon < 13.6 and max_lon > 13.9 and min_lat < 51.0 and max_lat > 51.2


def test_fa56_postgis_region_outside_import_is_an_error_live():
    backend = osm.PostgisBackend()
    if backend.import_extent() is None:
        pytest.skip("keine Tabellenstatistik für osm_polygons (ANALYZE fehlt)")
    with pytest.raises(ValueError, match="ganz außerhalb des PostGIS-Imports"):
        backend.fetch({"power": "substation"}, _region(-74.05, 40.68, -73.90, 40.82))
