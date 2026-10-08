"""Implements: FA3 (OSM-Konnektor).

Contract-Tests für src/geofact/builtin/sources/osm.py. Reine Query-
Builder-Tests, kein Netz/DB-Zugriff.
"""

import re

import geopandas as gpd
import pytest
import requests
from sqlalchemy import text
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


def _bbox_of(query: osm.PostgisQuery) -> tuple[float, float, float, float]:
    """BBox aus den gebundenen Parametern des ST_MakeEnvelope-Aufrufs, in der
    Reihenfolge der Platzhalter im SQL (min_lon, min_lat, max_lon, max_lat)."""
    match = re.search(
        r"ST_MakeEnvelope\((:\w+), (:\w+), (:\w+), (:\w+), 4326\)", query.sql
    )
    assert match, query.sql
    return tuple(query.params[name[1:]] for name in match.groups())


def test_fa03_overpass_query_builder_from_tags():
    backend = osm.OverpassBackend()
    query = backend.build_query({"power": "substation"}, region_gdf())
    assert '["power"="substation"]' in query
    assert "nwr" in query
    assert "51.01,13.6,51.15,13.94" in query


def test_fa03_overpass_query_timeout_matches_client_timeout():
    """Query-Timeout ([timeout:N]) und HTTP-Client-Timeout (self.timeout)
    dürfen nicht auseinanderlaufen."""
    backend = osm.OverpassBackend(timeout=15)
    query = backend.build_query({"power": "substation"}, region_gdf())
    assert "[timeout:15]" in query
    assert backend.timeout == 15


def test_fa03_overpass_url_defaults_to_public_instance(monkeypatch):
    monkeypatch.delenv("GEOFACT_OVERPASS_URL", raising=False)
    backend = osm.OverpassBackend()
    assert backend.url == osm.OVERPASS_URL


def test_fa03_overpass_url_overridable_via_env(monkeypatch):
    monkeypatch.setenv(
        "GEOFACT_OVERPASS_URL", "https://overpass.example.org/api/interpreter"
    )
    backend = osm.OverpassBackend()
    assert backend.url == "https://overpass.example.org/api/interpreter"


def test_fa03_overpass_url_explicit_param_wins_over_env(monkeypatch):
    monkeypatch.setenv(
        "GEOFACT_OVERPASS_URL", "https://overpass.example.org/api/interpreter"
    )
    backend = osm.OverpassBackend(url="https://explicit.example.org/api/interpreter")
    assert backend.url == "https://explicit.example.org/api/interpreter"


def test_fa03_postgis_sql_builder_from_tags(monkeypatch):
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    backend = osm.PostgisBackend()
    queries = backend.build_queries({"power": "substation"}, region_gdf())
    assert set(queries) == {"osm_points", "osm_lines", "osm_polygons"}
    for query in queries.values():
        assert "tags @> CAST(:tags_0 AS jsonb)" in query.sql
        assert query.params["tags_0"] == '{"power": "substation"}'


def test_fa03_postgis_sql_builder_includes_bbox(monkeypatch):
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    backend = osm.PostgisBackend()
    queries = backend.build_queries({"power": "substation"}, region_gdf())
    for query in queries.values():
        assert _bbox_of(query) == (13.6, 51.01, 13.94, 51.15)
        assert "geom && ST_MakeEnvelope(" in query.sql


def test_fa03_backend_selection_via_env(monkeypatch):
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "overpass")
    assert isinstance(osm.get_backend(), osm.OverpassBackend)

    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "postgis")
    assert isinstance(osm.get_backend(), osm.PostgisBackend)


def test_fa03_backend_default_is_overpass(monkeypatch):
    monkeypatch.delenv("GEOFACT_OSM_BACKEND", raising=False)
    assert isinstance(osm.get_backend(), osm.OverpassBackend)


def test_fa03_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "carrier_pigeon")
    with pytest.raises(ValueError, match="carrier_pigeon"):
        osm.get_backend()


def test_fa03_postgis_backend_needs_dsn(monkeypatch):
    monkeypatch.delenv("GEOFACT_PG_DSN", raising=False)
    with pytest.raises(ValueError, match="DSN"):
        osm.PostgisBackend()


# =====================================================================
# tags als Liste alternativer Tag-Sets (OR-Semantik)
# =====================================================================


def test_fa03_overpass_query_builder_from_tag_alternatives():
    backend = osm.OverpassBackend()
    query = backend.build_query(
        [{"leisure": "park"}, {"leisure": "garden"}, {"landuse": "village_green"}],
        region_gdf(),
    )
    assert '["leisure"="park"]' in query
    assert '["leisure"="garden"]' in query
    assert '["landuse"="village_green"]' in query
    assert query.count("nwr") == 3


def test_fa03_postgis_sql_builder_from_tag_alternatives(monkeypatch):
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    backend = osm.PostgisBackend()
    queries = backend.build_queries(
        [{"leisure": "park"}, {"leisure": "garden"}], region_gdf()
    )
    for query in queries.values():
        assert query.sql.count("tags @> CAST(") == 2
        assert sorted(v for v in query.params.values() if isinstance(v, str)) == [
            '{"leisure": "garden"}',
            '{"leisure": "park"}',
        ]
        assert " OR " in query.sql


# =====================================================================
# Nachbedingung: BBox ist nur Index-Vorfilter, Ergebnis wird auf die
# tatsächliche (nicht-rechteckige) Regionsgeometrie verfeinert
# =====================================================================


def test_fa03_refine_to_region_drops_objects_outside_actual_polygon():
    """Reproduziert den Bug: eine BBox-Abfrage liefert auch Objekte in den
    Rechteck-Ecken außerhalb der eigentlichen (z. B. Landes-)Grenze -
    _refine_to_region muss diese anhand der echten Geometrie aussortieren."""
    # L-förmige Region (z. B. Sachsen-ähnlicher Zuschnitt) - ihre BBox ist
    # ein Rechteck, das eine Ecke abdeckt, die die Region selbst nicht hat.
    region_polygon = Polygon([(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2), (0, 0)])
    region = gpd.GeoDataFrame(
        {"name": ["L-region"]}, geometry=[region_polygon], crs="EPSG:4326"
    )

    inside = Point(0.5, 0.5)  # innerhalb der L-Form
    in_bbox_but_outside = Point(1.5, 1.5)  # in der BBox-Ecke, aber außerhalb der L-Form
    data = gpd.GeoDataFrame(
        {"tags": [{"a": "1"}, {"a": "2"}]},
        geometry=[inside, in_bbox_but_outside],
        crs="EPSG:4326",
    )

    refined = osm._refine_to_region(data, region)

    assert len(refined) == 1
    assert refined.geometry.iloc[0].equals(inside)


def test_fa03_refine_to_region_is_noop_for_rectangular_region():
    """Bei einem BBox-Literal ist die Regionsgeometrie selbst ein Rechteck -
    die Verfeinerung darf dann nichts zusätzlich aussortieren."""
    region = region_gdf()
    data = gpd.GeoDataFrame(
        {"tags": [{"a": "1"}]},
        geometry=[Point(13.8, 51.1)],
        crs="EPSG:4326",
    )
    refined = osm._refine_to_region(data, region)
    assert len(refined) == 1


def test_fa03_refine_to_region_empty_input_returns_empty():
    region = region_gdf()
    empty = gpd.GeoDataFrame({"tags": []}, geometry=[], crs="EPSG:4326")
    refined = osm._refine_to_region(empty, region)
    assert len(refined) == 0


# =====================================================================
# Overpass-Fehler-Oberfläche nach Ausschöpfen aller Retries (kein Netz-
# zugriff - requests.post wird durch einen Fake ersetzt, time.sleep
# abgeschnitten, damit die Tests keinen echten Backoff abwarten).
# =====================================================================


class _FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} error")
            error.response = self
            raise error

    def json(self) -> dict:
        return {"elements": []}


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(osm.time, "sleep", lambda _seconds: None)


def test_fa03_overpass_exhausted_retries_message_mentions_attempts(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(status_code=500)

    monkeypatch.setattr(osm.requests, "post", fake_post)
    backend = osm.OverpassBackend(max_retries=2)

    with pytest.raises(
        RuntimeError, match="Overpass API nicht erreichbar nach 2 Versuchen"
    ):
        backend.fetch({"power": "substation"}, region_gdf())


def test_fa03_overpass_429_mentions_rate_limit_and_postgis_fallback(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(status_code=429)

    monkeypatch.setattr(osm.requests, "post", fake_post)
    backend = osm.OverpassBackend(max_retries=2)

    with pytest.raises(RuntimeError) as excinfo:
        backend.fetch({"power": "substation"}, region_gdf())

    message = str(excinfo.value)
    assert "429" in message
    assert "postgis" in message.lower()


def test_fa03_overpass_504_mentions_gateway_timeout_and_postgis_fallback(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=None):
        return _FakeResponse(status_code=504)

    monkeypatch.setattr(osm.requests, "post", fake_post)
    backend = osm.OverpassBackend(max_retries=2)

    with pytest.raises(RuntimeError) as excinfo:
        backend.fetch({"power": "substation"}, region_gdf())

    message = str(excinfo.value)
    assert "504" in message
    assert "postgis" in message.lower()


def test_fa03_overpass_succeeds_after_transient_failures(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, data=None, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            return _FakeResponse(status_code=503)
        return _FakeResponse(status_code=200)

    monkeypatch.setattr(osm.requests, "post", fake_post)
    backend = osm.OverpassBackend(max_retries=3)

    result = backend.fetch({"power": "substation"}, region_gdf())
    assert calls["n"] == 2
    assert len(result) == 0


# =====================================================================
# Gebundene Parameter und maskierte Literale (INT-16): Tag-Filter kommen aus
# der Szenario-YAML (im Web aus Editor oder LLM) und sind nicht vertrauenswürdig.
# =====================================================================

INJECTION = "x'); DROP TABLE osm_points; --"


@pytest.fixture
def pg_backend(monkeypatch):
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    return osm.PostgisBackend()


def test_fa03_postgis_tag_values_are_bound_not_interpolated(pg_backend):
    queries = pg_backend.build_queries({"name": INJECTION}, region_gdf())
    for query in queries.values():
        assert "DROP" not in query.sql
        assert "'" not in query.sql
        assert INJECTION in query.params["tags_0"]


def test_fa03_postgis_wildcard_key_is_bound_not_interpolated(pg_backend):
    queries = pg_backend.build_queries({INJECTION: "*"}, region_gdf())
    for query in queries.values():
        assert "jsonb_exists(tags, :key_0)" in query.sql
        assert "DROP" not in query.sql
        assert query.params["key_0"] == INJECTION


def test_fa03_postgis_apostrophe_in_value_stays_one_parameter(pg_backend):
    """Der Anlass (INT-16): 'St. Mary's' beendete das SQL-Literal."""
    queries = pg_backend.build_queries({"name": "St. Mary's"}, region_gdf())
    for query in queries.values():
        assert "Mary" not in query.sql
        assert "Mary's" in query.params["tags_0"]


def test_fa03_postgis_every_placeholder_has_a_parameter_and_vice_versa(pg_backend):
    """Nachbedingung: das SQL enthält nur gebundene Namen und Konstanten.
    SQLAlchemy erkennt dieselben Platzhalter wie die Parameterliste."""
    tags = [{"leisure": "park", "name": "*"}, {"highway": INJECTION}]
    for query in pg_backend.build_queries(tags, region_gdf()).values():
        compiled = text(query.sql).compile()
        assert set(compiled.params) == set(query.params)


def test_fa03_postgis_tag_set_alternatives_get_distinct_parameter_names(pg_backend):
    queries = pg_backend.build_queries(
        [{"leisure": "park"}, {"leisure": "garden"}, {"landuse": "*"}], region_gdf()
    )
    for query in queries.values():
        assert (
            len(query.params) == len(set(query.params)) == 3 + 4
        )  # 2 Mengen + 1 Schlüssel + BBox


@pytest.mark.parametrize(
    "table",
    [
        "osm_points; DROP TABLE x",
        "osm points",
        "1abc",
        "a.b.c",
        "",
        "osm_points--",
        "osm_points\n",
        '"x"',
    ],
)
def test_fa03_postgis_table_name_is_validated(table):
    with pytest.raises(ValueError, match="Tabellenname"):
        osm.build_postgis_query(
            table, {"power": "substation"}, (13.6, 51.0, 13.9, 51.1)
        )


def test_fa03_postgis_schema_qualified_table_is_allowed():
    query = osm.build_postgis_query(
        "public.osm_points", {"power": "substation"}, (13.6, 51.0, 13.9, 51.1)
    )
    assert "FROM public.osm_points" in query.sql


def test_fa03_postgis_non_text_tag_value_is_rejected(pg_backend):
    with pytest.raises(ValueError, match="Texte"):
        pg_backend.build_queries({"height": 12}, region_gdf())


def test_fa03_postgis_empty_tag_set_is_rejected(pg_backend):
    """Randfall: ein leeres Tag-Set ergäbe ein syntaktisch kaputtes WHERE ()."""
    with pytest.raises(ValueError, match="nicht leer"):
        pg_backend.build_queries([{"power": "substation"}, {}], region_gdf())


def test_fa03_postgis_empty_region_is_rejected_with_a_message(pg_backend):
    """Randfall: eine leere Region hat NaN-Grenzen - explizite Meldung statt 'nan' im SQL."""
    empty = gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:4326")
    with pytest.raises(ValueError, match="endliche BBox"):
        pg_backend.build_queries({"power": "substation"}, empty)


class _FakeConnection:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        self.log.append((str(statement), params))
        return []


class _FakeEngine:
    def __init__(self, log):
        self.log = log
        self.disposed = False

    def connect(self):
        return _FakeConnection(self.log)

    def dispose(self):
        self.disposed = True


def test_fa03_postgis_fetch_passes_parameters_to_the_driver(pg_backend, monkeypatch):
    """Vertrag: fetch() übergibt die Werte als Parameter an execute() - nie
    eingesetzt im Statement - und gibt die Verbindung frei."""
    log: list = []
    engine = _FakeEngine(log)
    monkeypatch.setattr(osm, "create_engine", lambda dsn, **kwargs: engine)

    result = pg_backend.fetch({"name": INJECTION}, region_gdf())
    # FA56: die Abdeckungsprüfung liest vorab die Importausdehnung (ohne Parameter)
    log[:] = [
        (stmt, params) for stmt, params in log if "ST_EstimatedExtent" not in stmt
    ]

    assert len(result) == 0
    assert [stmt.split("FROM ")[1].split("\n")[0] for stmt, _ in log] == [
        "osm_points",
        "osm_lines",
        "osm_polygons",
    ]
    for statement, params in log:
        assert "DROP" not in statement
        assert isinstance(params, dict) and INJECTION in params["tags_0"]
    assert engine.disposed


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "12; DROP TABLE x", "1.5", ""])
def test_fa03_postgis_query_timeout_must_be_a_positive_integer(monkeypatch, raw):
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.setenv("GEOFACT_PG_QUERY_TIMEOUT_S", raw)
    with pytest.raises(ValueError, match="GEOFACT_PG_QUERY_TIMEOUT_S"):
        osm.PostgisBackend()


def test_fa03_postgis_query_timeout_default_and_override(monkeypatch):
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user:pw@localhost/db")
    monkeypatch.delenv("GEOFACT_PG_QUERY_TIMEOUT_S", raising=False)
    assert osm.PostgisBackend().query_timeout_s == osm.DEFAULT_PG_QUERY_TIMEOUT_S
    monkeypatch.setenv("GEOFACT_PG_QUERY_TIMEOUT_S", "30")
    assert osm.PostgisBackend().query_timeout_s == 30


# Ein Overpass-Statement ist genau dann unversehrt, wenn es vollständig aus
# Filtern ["schlüssel"] / ["schlüssel"="wert"] mit korrekt maskierten
# Literalen, der BBox und dem Semikolon besteht.
_LITERAL = r'"(?:[^"\\]|\\.)*"'
_STATEMENT = re.compile(rf"nwr(?:\[{_LITERAL}(?:={_LITERAL})?\])+\([-\d.,]+\);")


def _statements(query: str) -> list[str]:
    return [
        line.strip() for line in query.splitlines() if line.strip().startswith("nwr")
    ]


def test_fa03_overpass_normal_query_is_byte_identical():
    """Regression: ohne Sonderzeichen ändert die Maskierung nichts."""
    query = osm.OverpassBackend().build_query({"power": "substation"}, region_gdf())
    assert query == (
        "[out:json][timeout:60];\n(\n"
        '  nwr["power"="substation"](51.01,13.6,51.15,13.94);\n'
        ");\nout geom;"
    )


@pytest.mark.parametrize(
    "value, literal",
    [
        ('St. "Mary"', r'"St. \"Mary\""'),
        ("a\\b", r'"a\\b"'),
        ("zwei\nZeilen", r'"zwei\nZeilen"'),
        ("tab\there", r'"tab\there"'),
        ("Straße Müller's", '"Straße Müller\'s"'),
    ],
)
def test_fa03_overpass_value_literal_is_escaped(value, literal):
    query = osm.OverpassBackend().build_query({"name": value}, region_gdf())
    assert f'["name"={literal}]' in query


def test_fa03_overpass_injection_cannot_leave_the_literal():
    payload = '"]; out; ["x'
    query = osm.OverpassBackend().build_query(
        [{"name": payload}, {payload: "*"}, {payload: payload}], region_gdf()
    )
    statements = _statements(query)
    assert len(statements) == 3
    for statement in statements:
        assert _STATEMENT.fullmatch(statement), statement
    assert query.count("out geom;") == 1  # nur die eine Ausgabeanweisung am Ende


@pytest.mark.parametrize("value", ["a\x00b", "a\rb", "a\x1bb", "a\x7fb"])
def test_fa03_overpass_control_characters_are_rejected(value):
    with pytest.raises(ValueError, match="Steuerzeichen"):
        osm.OverpassBackend().build_query({"name": value}, region_gdf())


def test_fa03_overpass_non_text_tag_value_is_rejected():
    with pytest.raises(ValueError, match="Text"):
        osm.OverpassBackend().build_query({"height": 12}, region_gdf())


def test_fa03_overpass_empty_tag_set_is_rejected():
    """Randfall: ohne Filter lieferte nwr(bbox) ALLE Objekte der BBox."""
    with pytest.raises(ValueError, match="nicht leer"):
        osm.OverpassBackend().build_query([{"power": "substation"}, {}], region_gdf())


def test_fa03_overpass_empty_region_is_rejected_with_a_message():
    empty = gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:4326")
    with pytest.raises(ValueError, match="endliche BBox"):
        osm.OverpassBackend().build_query({"power": "substation"}, empty)


def test_fa03_overpass_fetch_sends_the_escaped_query(monkeypatch):
    sent = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        sent["data"] = data
        return _FakeResponse(status_code=200)

    monkeypatch.setattr(osm.requests, "post", fake_post)
    osm.OverpassBackend().fetch({"name": 'St. "Mary"'}, region_gdf())
    assert r'["name"="St. \"Mary\""]' in sent["data"]["data"]
