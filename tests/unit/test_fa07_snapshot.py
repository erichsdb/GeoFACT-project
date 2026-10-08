"""Implements: FA7 (Snapshot-Mechanismus).

Contract-Tests für den Snapshot-Mechanismus: OSM-Schlüssel und Wrapper in
src/geofact/builtin/sources/osm.py, Ablage in src/geofact/support/snapshot.py.
"""

import geopandas as gpd
from shapely.geometry import Point, Polygon

from geofact.builtin.sources import osm


def region_gdf(bbox=(13.60, 51.01, 13.94, 51.15)) -> gpd.GeoDataFrame:
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


class CountingBackend:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, tags, region):
        self.calls += 1
        return gpd.GeoDataFrame(
            {"power": [tags.get("power", "?")]},
            geometry=[Point(13.7, 51.05)],
            crs="EPSG:4326",
        )


def test_fa07_second_run_uses_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    backend = CountingBackend()
    tags = {"power": "substation"}
    region = region_gdf()

    first = osm.fetch_with_snapshot(tags, region, backend)
    second = osm.fetch_with_snapshot(tags, region, backend)

    assert backend.calls == 1
    assert len(first) == len(second) == 1


def test_fa07_snapshot_hash_deterministic():
    tags_a = {"power": "substation", "voltage": "110000"}
    tags_b = {"voltage": "110000", "power": "substation"}  # andere Reihenfolge
    region = region_gdf()

    key_a = osm.snapshot_key(tags_a, region)
    key_b = osm.snapshot_key(tags_b, region)
    assert key_a == key_b

    different_region = region_gdf(bbox=(0, 0, 1, 1))
    key_c = osm.snapshot_key(tags_a, different_region)
    assert key_a != key_c


def test_fa07_snapshot_hash_deterministic_for_tag_alternatives():
    region = region_gdf()
    alternatives_a = [{"leisure": "park"}, {"leisure": "garden"}]
    alternatives_b = [{"leisure": "garden"}, {"leisure": "park"}]  # andere Reihenfolge

    key_a = osm.snapshot_key(alternatives_a, region)
    key_b = osm.snapshot_key(alternatives_b, region)
    assert key_a == key_b

    # ein einzelnes dict und eine Liste mit genau diesem einen Tag-Set
    # sind dieselbe Abfrage - müssen denselben Snapshot treffen.
    single_dict_key = osm.snapshot_key({"leisure": "park"}, region)
    single_list_key = osm.snapshot_key([{"leisure": "park"}], region)
    assert single_dict_key == single_list_key
    assert single_dict_key != key_a


def test_fa07_refresh_forces_refetch(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    backend = CountingBackend()
    tags = {"power": "substation"}
    region = region_gdf()

    osm.fetch_with_snapshot(tags, region, backend)
    osm.fetch_with_snapshot(tags, region, backend, refresh=True)

    assert backend.calls == 2


class OddColumnBackend:
    """Simuliert reale OSM-Tag-Spalten, die als GeoPackage-Feldnamen
    ungültig sind (führende Ziffer, Doppelpunkt) - z. B. '3dr:type',
    das den Snapshot-Schreibvorgang gegen die echte PostGIS-Instanz
    zum Absturz gebracht hat."""

    def fetch(self, tags, region):
        return gpd.GeoDataFrame(
            {"power": ["substation"], "3dr:type": ["mast"], "FIXME": ["check this"]},
            geometry=[Point(13.7, 51.05)],
            crs="EPSG:4326",
        )


def test_fa07_snapshot_survives_invalid_gpkg_column_names(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    backend = OddColumnBackend()
    tags = {"power": "substation"}
    region = region_gdf()

    first = osm.fetch_with_snapshot(tags, region, backend)
    assert "3dr:type" in first.columns
    assert first["3dr:type"].iloc[0] == "mast"

    second = osm.fetch_with_snapshot(tags, region, backend)
    assert "3dr:type" in second.columns
    assert second["3dr:type"].iloc[0] == "mast"
    assert second["FIXME"].iloc[0] == "check this"


class CaseCollidingColumnBackend:
    """Simuliert real vorkommende OSM-Tags, die sich nur in Groß-/
    Kleinschreibung unterscheiden (z. B. 'FIXME' und 'fixme' auf
    verschiedenen Objekten in Sachsen) - GeoPackage/SQLite vergleicht
    Spaltennamen case-insensitiv, beide für sich sind aber gültige
    Spaltennamen und wurden vom ursprünglichen Sanitizer nicht erkannt."""

    def fetch(self, tags, region):
        return gpd.GeoDataFrame(
            {"power": ["substation"], "FIXME": ["check this"], "fixme": ["also this"]},
            geometry=[Point(13.7, 51.05)],
            crs="EPSG:4326",
        )


def test_fa07_snapshot_survives_case_colliding_column_names(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    backend = CaseCollidingColumnBackend()
    tags = {"power": "substation"}
    region = region_gdf()

    first = osm.fetch_with_snapshot(tags, region, backend)
    assert first["FIXME"].iloc[0] == "check this"
    assert first["fixme"].iloc[0] == "also this"

    second = osm.fetch_with_snapshot(tags, region, backend)
    assert second["FIXME"].iloc[0] == "check this"
    assert second["fixme"].iloc[0] == "also this"


# ---------------------------------------------------------------------
# Nachtreview 02.10. (Runde 2): reservierte GPKG-Namen, verschachtelte Werte
# ---------------------------------------------------------------------

import pytest  # noqa: E402

from geofact.support import snapshot as snapshot_module  # noqa: E402


@pytest.mark.parametrize("column", ["fid", "FID", "geom"])
def test_fa07_snapshot_survives_reserved_gpkg_column_names(
    tmp_path, monkeypatch, column
):
    """Eine Spalte 'fid'/'FID'/'geom' (häufig in QGIS-/GPKG-Exporten) brach
    das Schreiben des Snapshots nach erfolgreichem Bezug ab."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    data = gpd.GeoDataFrame(
        {column: ["x1", "x2"]},
        geometry=[Point(13.7, 51.05), Point(13.8, 51.06)],
        crs="EPSG:4326",
    )
    key = snapshot_module.payload_key({"kind": "test", "column": column})

    first = snapshot_module.fetch_with_snapshot_key(key, lambda: data)
    second = snapshot_module.fetch_with_snapshot_key(
        key, lambda: pytest.fail("kein Neubezug")
    )

    assert list(first[column]) == list(second[column]) == ["x1", "x2"]


def test_fa07_snapshot_round_trip_keeps_list_and_dict_values(tmp_path, monkeypatch):
    """Verschachtelte Properties (OGC API Features) kamen aus dem Snapshot als
    Text zurück - frischer Lauf und Snapshot-Lauf unterschieden sich (FA7)."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    data = gpd.GeoDataFrame.from_features(
        [
            {
                "type": "Feature",
                "properties": {"l": [1, 2], "d": {"a": 1}, "s": "text"},
                "geometry": {"type": "Point", "coordinates": [13.7, 51.05]},
            },
            {
                "type": "Feature",
                "properties": {"l": None, "d": {"b": [2]}, "s": "[1]"},
                "geometry": {"type": "Point", "coordinates": [13.8, 51.06]},
            },
        ],
        crs="EPSG:4326",
    )
    key = snapshot_module.payload_key({"kind": "test", "nested": True})

    first = snapshot_module.fetch_with_snapshot_key(key, lambda: data)
    second = snapshot_module.fetch_with_snapshot_key(
        key, lambda: pytest.fail("kein Neubezug")
    )

    assert second["l"].iloc[0] == first["l"].iloc[0] == [1, 2]
    assert second["d"].iloc[0] == {"a": 1} and second["d"].iloc[1] == {"b": [2]}
    assert second["l"].isna().iloc[1]
    assert list(second["s"]) == ["text", "[1]"]
