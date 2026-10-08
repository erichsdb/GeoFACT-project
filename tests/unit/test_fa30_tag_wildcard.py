"""Implements: FA30 (Wildcard-Tagwert als Existenzprüfung).

Contract-Tests für den Wildcard-Tagwert '*' in OSM-Tag-Filtern. Reine
Query-Builder-Tests, kein Netz/DB-Zugriff.

Motivierender Fall (Bug): das Szenario "Straßennamen in Chemnitz" mit
tags: {name: '*'} lieferte 0 Objekte, weil beide Backends '*'
wortwörtlich als Tagwert verglichen haben.
"""

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from geofact.builtin.sources import osm
from geofact.builtin.sources.osm import OsmLayer


def region_gdf(bbox=(12.80, 50.77, 12.99, 50.89)) -> gpd.GeoDataFrame:
    """BBox um Chemnitz (Szenario aus dem Bugreport)."""
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
    return gpd.GeoDataFrame({"name": ["test"]}, geometry=[polygon], crs="EPSG:4326")


# =====================================================================
# Nachbedingung Overpass: '*' wird zum Existenzfilter ["key"],
# NICHT zum Wertvergleich ["key"="*"]
# =====================================================================


def test_fa30_overpass_wildcard_becomes_existence_filter():
    backend = osm.OverpassBackend()
    query = backend.build_query({"name": "*"}, region_gdf())
    assert '["name"]' in query
    # Der eigentliche Bug: wortwörtlicher Vergleich gegen den String "*".
    assert '["name"="*"]' not in query


def test_fa30_overpass_wildcard_mixed_with_exact_tag():
    """AND-Semantik innerhalb eines Tag-Sets bleibt erhalten: exakter
    Wert bleibt Wertvergleich, Wildcard wird Existenzfilter."""
    backend = osm.OverpassBackend()
    query = backend.build_query({"highway": "residential", "name": "*"}, region_gdf())
    assert '["highway"="residential"]' in query
    assert '["name"]' in query
    assert '["name"="*"]' not in query
    # Beide Filter am selben nwr-Statement (AND), nicht zwei Statements.
    assert query.count("nwr") == 1


def test_fa30_overpass_exact_tags_unchanged_by_wildcard_support():
    """Randfall/Regression: ohne Wildcard bleibt die Query identisch zum
    bisherigen Verhalten (FA3)."""
    backend = osm.OverpassBackend()
    query = backend.build_query({"power": "substation"}, region_gdf())
    assert '["power"="substation"]' in query
    assert '["power"]' not in query.replace('["power"="substation"]', "")


# =====================================================================
# Nachbedingung PostGIS: '*' wird zur jsonb-Existenzprüfung,
# NICHT zur Containment-Prüfung gegen den Wert "*"
# =====================================================================


def test_fa30_postgis_wildcard_becomes_jsonb_existence(monkeypatch):
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    backend = osm.PostgisBackend()
    queries = backend.build_queries({"name": "*"}, region_gdf())
    for query in queries.values():
        # seit INT-16 (FA3) als gebundener Parameter: der Schlüssel steht
        # in params, nicht im SQL-Text
        assert "jsonb_exists(tags, :key_0)" in query.sql
        assert query.params["key_0"] == "name"
        assert "@>" not in query.sql
        assert all('"name": "*"' not in str(v) for v in query.params.values())


def test_fa30_postgis_wildcard_mixed_with_exact_tag(monkeypatch):
    """Exakte Paare bleiben eine einzige Containment-Prüfung (@>,
    GIN-indexfreundlich), die Wildcard kommt per AND dazu."""
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    backend = osm.PostgisBackend()
    queries = backend.build_queries(
        {"highway": "residential", "name": "*"}, region_gdf()
    )
    for query in queries.values():
        assert "tags @> CAST(:tags_0 AS jsonb)" in query.sql
        assert query.params["tags_0"] == '{"highway": "residential"}'
        assert "jsonb_exists(tags, :key_1)" in query.sql
        assert query.params["key_1"] == "name"
        assert " AND " in query.sql
        assert all('"name": "*"' not in str(v) for v in query.params.values())


def test_fa30_postgis_wildcard_in_tag_alternatives(monkeypatch):
    """OR-Semantik über Tag-Sets (FA3) bleibt erhalten; ein Set mit
    zusammengesetzter Bedingung wird geklammert, damit AND nicht über
    die OR-Grenze bindet."""
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    backend = osm.PostgisBackend()
    queries = backend.build_queries(
        [{"leisure": "park"}, {"highway": "residential", "name": "*"}], region_gdf()
    )
    for query in queries.values():
        assert " OR " in query.sql
        assert (
            "(tags @> CAST(:tags_1 AS jsonb) AND jsonb_exists(tags, :key_2))"
            in query.sql
        )
        assert query.params["tags_1"] == '{"highway": "residential"}'


def test_fa30_postgis_exact_tags_unchanged_by_wildcard_support(monkeypatch):
    """Regression: ohne Wildcard bleibt es bei der einen Containment-Prüfung
    wie in FA3 (jetzt mit gebundenem Parameter statt eingesetztem Literal)."""
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    backend = osm.PostgisBackend()
    queries = backend.build_queries({"power": "substation"}, region_gdf())
    for query in queries.values():
        assert "tags @> CAST(:tags_0 AS jsonb)" in query.sql
        assert query.params["tags_0"] == '{"power": "substation"}'
        assert "jsonb_exists" not in query.sql


# =====================================================================
# Vorbedingung: Wildcard ist ein regulärer Tagwert im Schema (FA1)
# =====================================================================


def test_fa30_wildcard_tag_value_validates_in_schema():
    layer = OsmLayer(id="osm_streets", tags={"name": "*"})
    assert layer.tags == {"name": "*"}


def test_fa30_wildcard_only_tag_set_is_not_empty():
    """Randfall: ein Tag-Set, das nur aus einer Wildcard besteht, ist ein
    gültiger Filter (alle Objekte MIT diesem Tag) - kein leerer Filter."""
    layer = OsmLayer(id="named", tags=[{"name": "*"}])
    assert layer.tags == [{"name": "*"}]


def test_fa30_empty_tag_set_still_rejected():
    """Abgrenzung: die Wildcard-Unterstützung weicht die FA3-Vorbedingung
    'mindestens ein Tag-Filter' nicht auf."""
    with pytest.raises(ValueError, match="mindestens einen Tag-Filter"):
        OsmLayer(id="broken", tags={})
