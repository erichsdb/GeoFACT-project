"""Implements: FA3 (OSM-Konnektor).

Integrationstests gegen eine echte lokale PostGIS-Instanz. Werden ohne
GEOFACT_PG_DSN automatisch übersprungen (siehe tests/conftest.py).
"""

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from geofact.builtin.sources import osm

pytestmark = pytest.mark.integration


def dresden_region() -> gpd.GeoDataFrame:
    bbox = (13.70, 51.03, 13.78, 51.07)
    min_lon, min_lat, max_lon, max_lat = bbox
    polygon = Polygon(
        [
            (min_lon, min_lat),
            (max_lon, min_lat),
            (max_lon, max_lat),
            (min_lon, max_lat),
            (min_lon, min_lat),
        ]
    )
    return gpd.GeoDataFrame({"name": ["dresden"]}, geometry=[polygon], crs="EPSG:4326")


def test_fa03_postgis_fetch_substations():
    backend = osm.PostgisBackend()
    result = backend.fetch({"power": "substation"}, dresden_region())
    assert isinstance(result, gpd.GeoDataFrame)
    assert result.crs.to_epsg() == 4326
    assert len(result) > 0
    assert all(result["power"] == "substation")


@pytest.mark.netcheck  # fragt zusätzlich die öffentliche Overpass-API ab (Netz, FA45)
def test_fa03_backends_equivalent_on_small_bbox():
    postgis = osm.PostgisBackend()
    overpass = osm.OverpassBackend()
    region = dresden_region()

    postgis_result = postgis.fetch({"power": "substation"}, region)
    overpass_result = overpass.fetch({"power": "substation"}, region)

    n_postgis = len(postgis_result)
    n_overpass = len(overpass_result)
    # Toleranz für Datenstand-Differenzen zwischen der lokalen Instanz
    # und der Live-Overpass-API.
    tolerance = 0.10
    diff_ratio = abs(n_postgis - n_overpass) / max(n_postgis, n_overpass, 1)
    assert diff_ratio <= tolerance, (
        f"PostGIS lieferte {n_postgis}, Overpass {n_overpass} Objekte "
        f"(Abweichung {diff_ratio:.1%} > Toleranz {tolerance:.0%})"
    )


# =====================================================================
# INT-16: Tag-Schlüssel und -Werte werden gebunden, nicht eingesetzt.
# Vor der Härtung brach ein Apostroph im Wert die Abfrage mit einem
# SQL-Syntaxfehler ab (und ein präparierter Wert hätte SQL eingeschleust).
# =====================================================================

HOSTILE_VALUES = [
    "St. Mary's",
    "x'); DROP TABLE osm_points; --",
    'say "hi"',
    "back\\slash",
    "100% :name %(x)s",
]


@pytest.mark.parametrize("value", HOSTILE_VALUES)
def test_fa03_postgis_special_characters_in_tag_value_are_plain_data(value):
    backend = osm.PostgisBackend()
    result = backend.fetch({"name": value}, dresden_region())
    assert isinstance(result, gpd.GeoDataFrame)
    assert all(result["name"] == value) if len(result) else True


@pytest.mark.parametrize("key", HOSTILE_VALUES)
def test_fa03_postgis_special_characters_in_wildcard_key_are_plain_data(key):
    backend = osm.PostgisBackend()
    result = backend.fetch({key: "*"}, dresden_region())
    assert len(result) == 0


def test_fa03_postgis_tables_survive_hostile_values():
    """Nach den feindlichen Abfragen existieren die Tabellen und liefern."""
    backend = osm.PostgisBackend()
    backend.fetch({"name": "x'); DROP TABLE osm_points; --"}, dresden_region())
    assert len(backend.fetch({"power": "substation"}, dresden_region())) > 0
