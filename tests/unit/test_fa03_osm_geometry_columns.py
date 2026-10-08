"""Implements: FA3 (OSM-Konnektor: geometry-Typwahl, columns-Whitelist, WKB, remark).

Contract-Tests für die FA3-Zusätze in src/geofact/builtin/sources/osm.py.
Kein Netz, keine Datenbank: Overpass-Antworten und PostGIS-Verbindung sind Fakes.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pytest
import shapely
from pydantic import ValidationError
from shapely.geometry import LineString, Point, Polygon

from geofact.builtin.sources import osm


def region_gdf(bbox=(13.60, 51.01, 13.94, 51.15)) -> gpd.GeoDataFrame:
    min_lon, min_lat, max_lon, max_lat = bbox
    polygon = Polygon(
        [(min_lon, min_lat), (max_lon, min_lat), (max_lon, max_lat), (min_lon, max_lat)]
    )
    return gpd.GeoDataFrame({"name": ["test"]}, geometry=[polygon], crs="EPSG:4326")


# =====================================================================
# Layer-Modell
# =====================================================================


def test_fa03_osm_layer_accepts_geometry_and_columns():
    layer = osm.OsmLayer(
        id="parks", tags={"leisure": "park"}, geometry="polygon", columns=["name"]
    )
    assert layer.geometry_kinds() == ("polygon",)
    assert layer.columns == ["name"]
    both = osm.OsmLayer(id="x", tags={"a": "b"}, geometry=["line", "point", "line"])
    assert both.geometry_kinds() == ("point", "line")


def test_fa03_osm_layer_defaults_keep_todays_behaviour():
    layer = osm.OsmLayer(id="x", tags={"power": "substation"})
    assert layer.geometry is None and layer.columns is None
    assert layer.geometry_kinds() is None


@pytest.mark.parametrize(
    "fields",
    [
        {"geometry": "area"},
        {"geometry": []},
        {"columns": []},
        {"columns": ["geometry"]},
        {"columns": [""]},
    ],
)
def test_fa03_osm_layer_rejects_invalid_geometry_or_columns(fields):
    with pytest.raises(ValidationError):
        osm.OsmLayer(id="x", tags={"a": "b"}, **fields)


# =====================================================================
# Overpass
# =====================================================================


def test_fa03_overpass_query_without_new_fields_is_byte_identical():
    query = osm.OverpassBackend().build_query({"power": "substation"}, region_gdf())
    assert query == (
        "[out:json][timeout:60];\n(\n"
        '  nwr["power"="substation"](51.01,13.6,51.15,13.94);\n'
        ");\nout geom;"
    )
    explicit_none = osm.OverpassBackend().build_query(
        {"power": "substation"}, region_gdf(), geometry=None
    )
    assert explicit_none == query


def test_fa03_overpass_geometry_polygon_requests_way_and_rel():
    query = osm.OverpassBackend().build_query(
        {"leisure": "park"}, region_gdf(), geometry=("polygon",)
    )
    assert "nwr" not in query and "node" not in query
    assert 'way["leisure"="park"](51.01,13.6,51.15,13.94);' in query
    assert 'rel["leisure"="park"](51.01,13.6,51.15,13.94);' in query


def test_fa03_overpass_geometry_point_and_line_request_node_and_way():
    query = osm.OverpassBackend().build_query(
        [{"amenity": "bench"}, {"highway": "*"}],
        region_gdf(),
        geometry=("point", "line"),
    )
    lines = [line.strip() for line in query.splitlines() if line.startswith("  ")]
    assert lines == [
        'node["amenity"="bench"](51.01,13.6,51.15,13.94);',
        'way["amenity"="bench"](51.01,13.6,51.15,13.94);',
        'node["highway"](51.01,13.6,51.15,13.94);',
        'way["highway"](51.01,13.6,51.15,13.94);',
    ]


def _payload(elements, remark=None):
    body = {
        "osm3s": {"timestamp_osm_base": "2026-10-01T12:00:00Z"},
        "elements": elements,
    }
    if remark is not None:
        body["remark"] = remark
    return body


NODE = {
    "type": "node",
    "id": 1,
    "lon": 13.7,
    "lat": 51.05,
    "tags": {"leisure": "park", "name": "Punkt", "note": "x"},
}
CLOSED_WAY = {
    "type": "way",
    "id": 2,
    "tags": {"leisure": "park", "name": "Fläche"},
    "geometry": [
        {"lon": 13.70, "lat": 51.05},
        {"lon": 13.71, "lat": 51.05},
        {"lon": 13.71, "lat": 51.06},
        {"lon": 13.70, "lat": 51.05},
    ],
}
OPEN_WAY = {
    "type": "way",
    "id": 3,
    "tags": {"leisure": "park", "surface": "grass"},
    "geometry": [{"lon": 13.70, "lat": 51.05}, {"lon": 13.72, "lat": 51.07}],
}


def test_fa03_overpass_parse_filters_geometry_kinds():
    data = osm.OverpassBackend()._parse_response(
        _payload([NODE, CLOSED_WAY, OPEN_WAY]),
        {"leisure": "park"},
        geometry=("polygon",),
    )
    assert list(data.geom_type) == ["Polygon"]
    assert data["name"].tolist() == ["Fläche"]


def test_fa03_columns_whitelist_keeps_filter_keys_and_geometry():
    data = osm.OverpassBackend()._parse_response(
        _payload([NODE, CLOSED_WAY, OPEN_WAY]),
        {"leisure": "park"},
        columns=["name", "ref"],
    )
    assert list(data.columns) == ["leisure", "name", "ref", "geometry"]
    assert data.geometry.name == "geometry" and len(data) == 3
    assert data["ref"].isna().all()  # gelistet, aber nirgends gesetzt -> Spalte bleibt


def test_fa03_columns_filter_rows_before_dataframe(monkeypatch):
    """Die Tag-Dicts werden VOR dem GeoDataFrame-Bau gekürzt: der Konstruktor
    sieht keine Schlüssel außerhalb der Whitelist."""
    seen: list = []
    original = osm.gpd.GeoDataFrame

    def spy(data=None, *args, **kwargs):
        if isinstance(data, list):
            seen.extend(data)
        return original(data, *args, **kwargs)

    monkeypatch.setattr(osm.gpd, "GeoDataFrame", spy)
    osm.OverpassBackend()._parse_response(
        _payload([NODE, OPEN_WAY]), {"leisure": "park"}, columns=["name"]
    )
    assert seen and all(set(row) <= {"leisure", "name"} for row in seen)


def test_fa03_columns_whitelist_on_empty_result_keeps_schema():
    data = osm.OverpassBackend()._parse_response(
        _payload([]), {"leisure": "park"}, columns=["name"]
    )
    assert data.empty and list(data.columns) == ["leisure", "name", "geometry"]
    assert data.crs == "EPSG:4326"


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


def test_fa03_overpass_remark_runtime_error_is_retried_and_never_returned(monkeypatch):
    calls: list = []

    def post(url, **kwargs):
        calls.append(url)
        return _Response(
            _payload([NODE], remark='runtime error: Query timed out in "query"')
        )

    monkeypatch.setattr(osm.requests, "post", post)
    monkeypatch.setattr(osm.time, "sleep", lambda s: None)
    backend = osm.OverpassBackend(url="https://overpass.example/api", max_retries=2)
    with pytest.raises(RuntimeError, match="Teilergebnis") as info:
        backend.fetch({"leisure": "park"}, region_gdf())
    assert len(calls) == 2
    assert isinstance(info.value.__cause__, osm.OverpassPartialResponse)


def test_fa03_overpass_remark_is_never_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(
        osm.requests,
        "post",
        lambda url, **kw: _Response(
            _payload([NODE], remark="runtime error: out of memory")
        ),
    )
    monkeypatch.setattr(osm.time, "sleep", lambda s: None)
    backend = osm.OverpassBackend(url="https://overpass.example/api", max_retries=1)
    with pytest.raises(RuntimeError):
        osm.fetch_with_snapshot({"leisure": "park"}, region_gdf(), backend)
    assert list(tmp_path.iterdir()) == []


def test_fa03_overpass_records_last_meta(monkeypatch):
    monkeypatch.setattr(
        osm.requests, "post", lambda url, **kw: _Response(_payload([NODE]))
    )
    backend = osm.OverpassBackend(url="https://overpass.example/api")
    backend.fetch({"leisure": "park"}, region_gdf())
    assert backend.last_meta == {
        "backend": "overpass",
        "osm_base": "2026-10-01T12:00:00Z",
        "endpoint": "https://overpass.example/api",
    }


def test_fa03_zero_objects_warn_on_load(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(osm.requests, "post", lambda url, **kw: _Response(_payload([])))
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "overpass")
    layer = osm.OsmLayer(id="leer", tags={"leisure": "parkk"})

    class Ctx:
        region = region_gdf()
        refresh = False
        options: dict = {}

    with pytest.warns(osm.EmptyOsmResultWarning, match="'leer'.*0 Objekte"):
        data = osm.load(layer, Ctx())
    assert data.empty


def test_fa03_get_backend_name(monkeypatch):
    monkeypatch.delenv("GEOFACT_OSM_BACKEND", raising=False)
    assert osm.get_backend_name() == "overpass"
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "postgis")
    assert osm.get_backend_name() == "postgis"
    assert osm.get_backend_name("overpass") == "overpass"


# =====================================================================
# PostGIS
# =====================================================================


@pytest.fixture
def pg_backend(monkeypatch):
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    return osm.PostgisBackend()


def test_fa03_postgis_geometry_selects_tables(pg_backend):
    assert list(pg_backend.build_queries({"a": "b"}, region_gdf())) == [
        "osm_points",
        "osm_lines",
        "osm_polygons",
    ]
    assert list(
        pg_backend.build_queries({"a": "b"}, region_gdf(), geometry=("polygon",))
    ) == [
        "osm_polygons",
    ]
    assert list(
        pg_backend.build_queries({"a": "b"}, region_gdf(), geometry=("line", "point"))
    ) == ["osm_points", "osm_lines"]


def test_fa03_postgis_reads_wkb(pg_backend, monkeypatch):
    rows = {
        "osm_points": [
            ({"leisure": "park", "name": "P"}, shapely.to_wkb(Point(13.7, 51.05)))
        ],
        "osm_lines": [
            (
                {"leisure": "park"},
                memoryview(shapely.to_wkb(LineString([(13.7, 51.05), (13.71, 51.06)]))),
            )
        ],
        "osm_polygons": [],
    }
    log: list = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement, params):
            sql = str(statement)
            log.append((sql, params))
            table = sql.split("FROM ")[1].split("\n")[0].strip()
            return rows[table]

    class Engine:
        def connect(self):
            return Conn()

        def dispose(self):
            pass

    monkeypatch.setattr(osm, "create_engine", lambda dsn, **kw: Engine())
    data = pg_backend.fetch({"leisure": "park"}, region_gdf())
    assert all(
        "ST_AsBinary(geom)" in sql and "ST_AsGeoJSON" not in sql for sql, _ in log
    )
    assert sorted(data.geom_type) == ["LineString", "Point"]
    assert pg_backend.last_meta == {"backend": "postgis", "osm_base": None}


def test_fa03_postgis_columns_whitelist_is_bound(pg_backend):
    queries = pg_backend.build_queries(
        {"leisure": "park"}, region_gdf(), columns=["name"]
    )
    for query in queries.values():
        assert "jsonb_object_agg" in query.sql
        bound_lists = [v for v in query.params.values() if isinstance(v, list)]
        assert bound_lists == [["leisure", "name"]]
        assert "'name'" not in query.sql
    plain = pg_backend.build_queries({"leisure": "park"}, region_gdf())
    assert all("jsonb_object_agg" not in q.sql for q in plain.values())


# =====================================================================
# Snapshot-Schlüssel
# =====================================================================


def test_fa03_snapshot_key_unchanged_without_new_fields():
    region = region_gdf()
    tags = {"amenity": "doctors"}
    legacy = osm.snapshot_key(tags, region)
    assert (
        osm.snapshot_key(tags, region, geometry=None, columns=None, backend="overpass")
        == legacy
    )
    assert osm.snapshot_key(tags, region, geometry=("polygon",)) != legacy
    assert osm.snapshot_key(tags, region, columns=["name"]) != legacy
    assert osm.snapshot_key(tags, region, backend="postgis") != legacy


def test_fa03_fetch_with_snapshot_passes_new_fields_only_when_set(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    received: list = []

    class Backend:
        def fetch(self, tags, region, **kwargs):
            received.append(kwargs)
            return gpd.GeoDataFrame(
                {"a": ["b"]}, geometry=[Point(13.7, 51.05)], crs="EPSG:4326"
            )

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        osm.fetch_with_snapshot({"a": "b"}, region_gdf(), Backend())
        osm.fetch_with_snapshot(
            {"a": "b"}, region_gdf(), Backend(), geometry=("point",), columns=["name"]
        )
    assert received == [{}, {"geometry": ("point",), "columns": ["name"]}]
