"""Implements: FA3 (OSM-Konnektor: Multipolygon-/Grenz-Relationen als (Multi)Polygone).

Contract tests for the relation assembly of the Overpass path
(builtin/sources/osm.py `_relation_to_geometry`) and for the PostGIS path, which
hands the MultiPolygon geometries of `osm_polygons` through unchanged.

Background: `nwr[boundary=administrative]...; out geom;` returns an administrative
boundary (the 39 Stadtteile of Chemnitz, FA46 example) as a RELATION whose
members are ways with geometry. The connector used to drop every relation
silently; a scenario selecting boundaries got an empty layer. Offline: the
Overpass answers are built here (the shape was recorded from the live service,
01.10.2026: members `outer` ways with `geometry`, a `label` node, tags incl.
`type=boundary`).
"""

from __future__ import annotations

import json
import warnings

import geopandas as gpd
import pytest
import shapely
from shapely.geometry import MultiPolygon, Polygon, box

from geofact.builtin.sources import osm
from geofact.builtin.sources.osm import OsmRelationWarning, OverpassBackend
from osm_relations import relation, ring_ways, square, way

CHEMNITZ = box(12.7, 50.7, 13.1, 50.95)


def _region(geometry=CHEMNITZ) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["r"]}, geometry=[geometry], crs="EPSG:4326")


def _parse(elements: list[dict]):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        frame = OverpassBackend()._parse_response({"elements": elements}, {})
    return frame, [w for w in caught if issubclass(w.category, OsmRelationWarning)]


# =====================================================================
# Assembly
# =====================================================================


def test_fa03_boundary_relation_becomes_a_polygon_with_its_tags():
    ring = square(12.8, 50.8, 0.1)
    frame, caught = _parse([relation(1, ring_ways(ring, 3, 100), admin_level="9")])
    assert caught == []
    assert len(frame) == 1 and frame.crs.to_epsg() == 4326
    polygon = frame.geometry.iloc[0]
    assert polygon.geom_type == "Polygon"
    assert (
        polygon.equals(Polygon(ring))
        or polygon.symmetric_difference(Polygon(ring)).area < 1e-12
    )
    assert frame["name"].iloc[0] == "Gebiet 1" and frame["admin_level"].iloc[0] == "9"
    assert frame["boundary"].iloc[0] == "administrative"


def test_fa03_ring_cut_into_unordered_reversed_ways_is_joined():
    ring = square(12.8, 50.8, 0.1)
    members = ring_ways(ring, 5, 100)
    members.reverse()  # also the order of the members is arbitrary
    frame, _ = _parse([relation(1, members)])
    assert frame.geometry.iloc[0].symmetric_difference(Polygon(ring)).area < 1e-12


def test_fa03_member_without_role_counts_as_outer():
    ring = square(12.8, 50.8, 0.1)
    members = ring_ways(ring, 2, 100, role="")
    frame, _ = _parse([relation(1, members)])
    assert frame.geometry.iloc[0].geom_type == "Polygon"


def test_fa03_two_separate_outer_rings_give_a_multipolygon():
    one, two = square(12.8, 50.8, 0.05), square(12.9, 50.8, 0.05)
    members = ring_ways(one, 2, 100) + ring_ways(two, 2, 200)
    frame, caught = _parse([relation(1, members, type="multipolygon")])
    assert caught == []
    assert (
        frame.geometry.iloc[0].geom_type == "MultiPolygon"
        and len(frame.geometry.iloc[0].geoms) == 2
    )


def test_fa03_inner_ring_makes_a_hole():
    outer, inner = square(12.8, 50.8, 0.1), square(12.84, 50.84, 0.02)
    members = ring_ways(outer, 2, 100) + ring_ways(inner, 1, 200, role="inner")
    frame, _ = _parse([relation(1, members, type="multipolygon")])
    polygon = frame.geometry.iloc[0]
    assert polygon.geom_type == "Polygon" and len(polygon.interiors) == 1
    assert polygon.area == pytest.approx(0.1 * 0.1 - 0.02 * 0.02)


def test_fa03_island_in_a_lake_inside_a_hole_is_kept():
    outer = square(12.8, 50.8, 0.2)
    lake = square(12.85, 50.85, 0.1)
    island = square(12.88, 50.88, 0.04)
    members = (
        ring_ways(outer, 2, 100)
        + ring_ways(lake, 1, 200, role="inner")
        + ring_ways(island, 1, 300, role="outer")
    )
    frame, _ = _parse([relation(1, members, type="multipolygon")])
    polygon = frame.geometry.iloc[0]
    assert polygon.geom_type == "MultiPolygon"
    assert polygon.area == pytest.approx(0.2 * 0.2 - 0.1 * 0.1 + 0.04 * 0.04)
    assert polygon.contains(box(12.89, 50.89, 12.91, 50.91))  # the island
    assert not polygon.contains(box(12.86, 50.86, 12.87, 50.87))  # the lake


def test_fa03_neighbouring_relations_tile_without_overlap():
    left, right = square(12.8, 50.8, 0.1), square(12.9, 50.8, 0.1)
    frame, _ = _parse(
        [
            relation(1, ring_ways(left, 4, 100), name="West"),
            relation(2, ring_ways(right, 4, 200), name="Ost"),
        ]
    )
    assert list(frame["name"]) == ["West", "Ost"]
    assert frame.geometry.iloc[0].intersection(frame.geometry.iloc[1]).area == 0
    assert frame.union_all().area == pytest.approx(0.02)


def test_fa03_member_nodes_such_as_a_label_do_not_disturb():
    ring = square(12.8, 50.8, 0.1)
    members = [
        {"type": "node", "ref": 7, "role": "label", "lat": 50.85, "lon": 12.85},
        *ring_ways(ring, 2, 100),
    ]
    frame, caught = _parse([relation(1, members)])
    assert len(frame) == 1 and caught == []


def test_fa03_nodes_and_ways_are_parsed_as_before_next_to_relations():
    elements = [
        {"type": "node", "id": 1, "lat": 50.8, "lon": 12.9, "tags": {"name": "n"}},
        {
            "type": "way",
            "id": 2,
            "tags": {"name": "w"},
            "geometry": [{"lat": 50.8, "lon": 12.9}, {"lat": 50.81, "lon": 12.91}],
        },
        {
            "type": "way",
            "id": 3,
            "tags": {"name": "closed"},
            "geometry": [
                {"lat": 50.8, "lon": 12.9},
                {"lat": 50.8, "lon": 12.91},
                {"lat": 50.81, "lon": 12.91},
                {"lat": 50.8, "lon": 12.9},
            ],
        },
        relation(4, ring_ways(square(12.8, 50.8, 0.1), 2, 100), name="rel"),
    ]
    frame, caught = _parse(elements)
    assert list(frame.geometry.geom_type) == [
        "Point",
        "LineString",
        "Polygon",
        "Polygon",
    ]
    assert list(frame["name"]) == ["n", "w", "closed", "rel"] and caught == []


# =====================================================================
# What cannot be assembled is reported once, never dropped silently
# =====================================================================


def test_fa03_relation_without_closed_ring_is_skipped_with_one_aggregated_warning():
    broken = [way(1, [(12.8, 50.8), (12.9, 50.8), (12.9, 50.9)])]  # open way: no area
    good = ring_ways(square(12.8, 50.8, 0.1), 2, 100)
    frame, caught = _parse(
        [
            relation(11, broken, name="kaputt"),
            relation(12, good, name="gut"),
            relation(13, broken, name="kaputt2"),
        ]
    )
    assert list(frame["name"]) == ["gut"]
    assert len(caught) == 1
    message = str(caught[0].message)
    assert (
        "2 Multipolygon-/Grenz-Relation(en) nicht als Fläche zusammensetzbar" in message
    )
    assert "[11, 13]" in message


def test_fa03_relation_with_one_closed_and_one_open_ring_keeps_the_closed_one_and_warns():
    closed = ring_ways(square(12.8, 50.8, 0.05), 2, 100)
    open_way = [way(200, [(12.9, 50.8), (12.95, 50.8), (12.95, 50.85)])]
    frame, caught = _parse([relation(5, closed + open_way, type="multipolygon")])
    assert len(frame) == 1 and frame.geometry.iloc[0].geom_type == "Polygon"
    assert len(caught) == 1 and "nicht geschlossenen Ringen" in str(caught[0].message)
    assert "[5]" in str(caught[0].message)


def test_fa03_relation_without_member_geometry_is_reported():
    members = [{"type": "way", "ref": 1, "role": "outer"}]
    frame, caught = _parse([relation(9, members)])
    assert len(frame) == 0 and len(caught) == 1 and "[9]" in str(caught[0].message)


def test_fa03_relation_without_members_is_reported():
    frame, caught = _parse([relation(9, [])])
    assert len(frame) == 0 and len(caught) == 1


def test_fa03_non_area_relations_are_counted_in_one_warning():
    route = {
        "type": "relation",
        "id": 1,
        "tags": {"type": "route", "route": "tram"},
        "members": [way(1, [(12.8, 50.8), (12.9, 50.8)], role="")],
    }
    site = {"type": "relation", "id": 2, "tags": {"type": "site"}, "members": []}
    notype = {"type": "relation", "id": 3, "tags": {"name": "x"}, "members": []}
    frame, caught = _parse([route, route, site, notype])
    assert len(frame) == 0 and len(caught) == 1
    message = str(caught[0].message)
    assert "route: 2" in message and "site: 1" in message and "ohne type: 1" in message


def test_fa03_empty_answer_gives_an_empty_frame_without_warning():
    """Edge case 'empty layer'."""
    frame, caught = _parse([])
    assert len(frame) == 0 and frame.crs.to_epsg() == 4326 and caught == []


def test_fa03_all_causes_share_one_warning():
    broken = [way(1, [(12.8, 50.8), (12.9, 50.8)])]
    site = {"type": "relation", "id": 2, "tags": {"type": "site"}, "members": []}
    _, caught = _parse([relation(3, broken), site])
    assert len(caught) == 1
    assert "nicht als Fläche" in str(caught[0].message) and "site: 1" in str(
        caught[0].message
    )


# =====================================================================
# End to end through fetch(): region refinement and the Stadtteil selector
# =====================================================================


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


STADTTEILE = [
    {"boundary": "administrative", "admin_level": "9", "name:prefix": "Stadtteil"},
    {"boundary": "administrative", "admin_level": "10", "name:prefix": "Stadtteil"},
]


def test_fa03_stadtteil_selector_builds_one_overpass_statement_per_level():
    query = OverpassBackend().build_query(STADTTEILE, _region())
    assert (
        'nwr["boundary"="administrative"]["admin_level"="9"]["name:prefix"="Stadtteil"]'
        "(50.7,12.7,50.95,13.1);"
    ) in query
    assert (
        'nwr["boundary"="administrative"]["admin_level"="10"]["name:prefix"="Stadtteil"]'
        in query
    )
    assert query.rstrip().endswith("out geom;")


def test_fa03_fetch_returns_the_boundaries_of_the_region_only(monkeypatch):
    inside = relation(
        1, ring_ways(square(12.8, 50.8, 0.1), 3, 100), name="Innen", admin_level="10"
    )
    far_away = relation(
        2, ring_ways(square(14.0, 51.0, 0.1), 3, 200), name="Fern", admin_level="9"
    )
    payload = {"elements": [inside, far_away]}
    monkeypatch.setattr(osm.requests, "post", lambda *a, **k: _Response(payload))

    frame = OverpassBackend().fetch(STADTTEILE, _region())
    assert list(frame["name"]) == ["Innen"]
    assert frame.geometry.iloc[0].geom_type == "Polygon"


def test_fa03_boundaries_survive_the_snapshot(monkeypatch, tmp_path):
    """FA7: Polygon and MultiPolygon rows with colon tag names (`name:prefix`) read back unchanged."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    one = relation(
        1,
        ring_ways(square(12.8, 50.8, 0.05), 2, 100),
        name="Eins",
        **{"name:prefix": "Stadtteil"},
    )
    two = relation(
        2,
        ring_ways(square(12.9, 50.8, 0.03), 2, 200)
        + ring_ways(square(12.95, 50.8, 0.03), 2, 300),
        name="Zwei",
        **{"name:prefix": "Stadtteil"},
    )
    monkeypatch.setattr(
        osm.requests, "post", lambda *a, **k: _Response({"elements": [one, two]})
    )
    first = osm.fetch_with_snapshot(STADTTEILE, _region(), OverpassBackend())

    def forbidden(*args, **kwargs):
        raise AssertionError("network access although a snapshot exists")

    monkeypatch.setattr(osm.requests, "post", forbidden)
    second = osm.fetch_with_snapshot(STADTTEILE, _region(), OverpassBackend())
    assert list(second["name"]) == ["Eins", "Zwei"] and "name:prefix" in second.columns
    # a GeoPackage stores the layer with one polygonal type: areas and shapes are unchanged
    assert set(second.geometry.geom_type) <= {"Polygon", "MultiPolygon"}
    for before, after in zip(first.geometry, second.geometry):
        assert after.symmetric_difference(before).area == pytest.approx(0, abs=1e-12)


# =====================================================================
# PostGIS path: osm_polygons holds (Multi)Polygons, resolved from relations by osm2pgsql
# =====================================================================


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)


class _PgConnection:
    def __init__(self, rows_by_table, log):
        self.rows_by_table, self.log = rows_by_table, log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.log.append((sql, params))
        table = sql.split("FROM ")[1].split("\n")[0].strip()
        return _Rows(self.rows_by_table.get(table, []))


class _PgEngine:
    def __init__(self, rows_by_table, log):
        self.rows_by_table, self.log = rows_by_table, log

    def connect(self):
        return _PgConnection(self.rows_by_table, self.log)

    def dispose(self):
        pass


def test_fa03_postgis_returns_boundary_multipolygons_from_osm_polygons_unchanged(
    monkeypatch,
):
    """The geometry column of `osm_polygons` is MULTIPOLYGON (osm2pgsql
    flex output resolved the relation): the backend passes every (Multi)Polygon through."""
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    stadtteil = box(12.8, 50.8, 12.9, 50.9)
    # the backend reads WKB (ST_AsBinary, FA3 addition); psycopg hands over bytes
    multi = shapely.to_wkb(MultiPolygon([stadtteil]))
    rows = {
        "osm_polygons": [
            (
                {
                    "boundary": "administrative",
                    "admin_level": "9",
                    "name": "Mittelbach",
                    "name:prefix": "Stadtteil",
                },
                multi,
            ),
        ]
    }
    log: list = []
    monkeypatch.setattr(
        osm, "create_engine", lambda dsn, **kwargs: _PgEngine(rows, log)
    )

    frame = osm.PostgisBackend().fetch(STADTTEILE, _region())

    assert len(frame) == 1 and frame.geometry.iloc[0].geom_type == "MultiPolygon"
    assert (
        frame.geometry.iloc[0].equals(stadtteil)
        or frame.geometry.iloc[0].symmetric_difference(stadtteil).is_empty
    )
    assert frame["name"].iloc[0] == "Mittelbach" and frame.crs.to_epsg() == 4326
    polygon_sql = next(sql for sql, _ in log if "FROM osm_polygons" in sql)
    # both alternatives (level 9 OR level 10) are queried, values only as bound parameters
    assert polygon_sql.count(" OR ") == 1 and "Stadtteil" not in polygon_sql


def test_fa03_postgis_stadtteil_query_binds_the_colon_tag_and_both_levels(monkeypatch):
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    query = osm.PostgisBackend().build_queries(STADTTEILE, _region())["osm_polygons"]
    containment = [
        json.loads(v) for k, v in query.params.items() if k.startswith("tags_")
    ]
    assert {
        "boundary": "administrative",
        "admin_level": "9",
        "name:prefix": "Stadtteil",
    } in containment
    assert {
        "boundary": "administrative",
        "admin_level": "10",
        "name:prefix": "Stadtteil",
    } in containment
