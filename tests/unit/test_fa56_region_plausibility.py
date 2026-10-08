"""Implements: FA56 (Regionsplausibilität: Namensauflösung, Spannweite, PostGIS-Abdeckung).

Contract-Tests für engine/region.py (Nominatim-Metadaten, describe_region)
und builtin/sources/osm.py (PostGIS-Importausdehnung). Kein Netz, keine
Datenbank: Nominatim ist eine Fixture, der PostGIS-Extent gestubbt.
"""

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

from geofact.builtin.sources import osm
from geofact.engine import region as region_module

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
GERMANY_EXTENT = (5.87, 47.27, 15.04, 55.06)


def _fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))


def _region(*bounds: float) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["r"]}, geometry=[box(*bounds)], crs="EPSG:4326")


# ---------------------------------------------------------------------
# Namensauflösung
# ---------------------------------------------------------------------


def test_fa56_name_resolving_to_a_point_is_an_error(tmp_path):
    with pytest.raises(ValueError) as excinfo:
        region_module.resolve_region(
            "Altmarkt Dresden", fetcher=lambda name: _fixture("nominatim_point.json")
        )
    message = str(excinfo.value)
    assert "Point" in message and "keine Fläche" in message
    assert "Altmarkt, Innere Altstadt, Dresden" in message
    assert "highway/bus_stop" in message
    # ein Punkt-Treffer wird nie gecacht
    assert not list(tmp_path.rglob("*.geojson"))


def test_fa56_name_with_unexpected_osm_class_warns_with_display_name():
    answer = _fixture("nominatim_dresden.json")
    answer[0]["class"] = "amenity"
    answer[0]["type"] = "university"
    answer[0]["display_name"] = "Technische Universitaet Dresden, Sachsen"
    region, meta = region_module.resolve_region_with_meta(
        "TU Dresden", fetcher=lambda n: answer
    )
    info = region_module.describe_region(region, "auto", meta)
    assert info.display_name == "Technische Universitaet Dresden, Sachsen"
    assert info.osm_class == "amenity" and info.osm_type == "university"
    assert len(info.warnings) == 1
    warning = info.warnings[0]
    assert "Technische Universitaet Dresden, Sachsen" in warning
    assert "amenity/university" in warning and "km2" in warning


def test_fa56_expected_osm_class_does_not_warn():
    region, meta = region_module.resolve_region_with_meta(
        "Dresden, Deutschland", fetcher=lambda n: _fixture("nominatim_dresden.json")
    )
    assert meta == {
        "display_name": "Dresden, Sachsen, Deutschland",
        "class": "boundary",
        "type": "administrative",
    }
    info = region_module.describe_region(region, "auto", meta)
    assert info.warnings == []
    assert info.to_dict()["display_name"] == "Dresden, Sachsen, Deutschland"


def test_fa56_region_cache_stores_metadata_and_is_reused(tmp_path):
    calls = []

    def fetcher(name):
        calls.append(name)
        return _fixture("nominatim_dresden.json")

    region_module.resolve_region_with_meta("Dresden, Deutschland", fetcher=fetcher)
    cache = next(tmp_path.rglob("*.geojson"))
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert set(payload) == {"geojson", "display_name", "class", "type"}
    _, meta = region_module.resolve_region_with_meta(
        "Dresden, Deutschland", fetcher=fetcher
    )
    assert calls == ["Dresden, Deutschland"]
    assert meta["display_name"] == "Dresden, Sachsen, Deutschland"


def test_fa56_old_region_cache_without_metadata_still_loads():
    geometry = _fixture("nominatim_dresden.json")[0]["geojson"]
    path = region_module._region_cache_path("Dresden, Deutschland")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(geometry), encoding="utf-8"
    )  # alte Form: nackte Geometrie

    def no_fetch(name):
        raise AssertionError("Cache muss getroffen werden")

    region, meta = region_module.resolve_region_with_meta(
        "Dresden, Deutschland", fetcher=no_fetch
    )
    assert meta is None
    assert region.total_bounds == pytest.approx((13.60, 51.01, 13.94, 51.15))
    info = region_module.describe_region(region, "auto", meta)
    assert info.display_name is None and info.warnings == []
    # resolve_region liefert weiter nur die Geometrie (FA3)
    assert (
        len(region_module.resolve_region("Dresden, Deutschland", fetcher=no_fetch)) == 1
    )


def test_fa56_bbox_literal_is_checked_when_resolved():
    with pytest.raises(ValueError, match="Antimeridian"):
        region_module.resolve_region("177,-19,-178,-16")
    with pytest.raises(ValueError, match="keine Ausdehnung"):
        region_module.resolve_region("13,51,13,51")


# ---------------------------------------------------------------------
# Spannweite unter auto
# ---------------------------------------------------------------------


def test_fa56_region_wider_than_one_zone_warns_under_auto_crs():
    germany = region_module.resolve_region("5.87,47.27,15.04,55.06")
    crs, warnings = region_module.working_crs(germany, "auto")
    assert crs.to_epsg() == 32632  # FA5 bleibt: Zone des Zentroids
    assert len(warnings) == 1
    assert "9.2 Grad breit" in warnings[0] and "equal_area" in warnings[0]
    info = region_module.describe_region(germany, "auto")
    assert info.warnings == warnings


def test_fa56_region_wider_than_one_zone_does_not_warn_with_declared_crs():
    germany = region_module.resolve_region("5.87,47.27,15.04,55.06")
    info = region_module.describe_region(germany, "equal_area_europe")
    assert info.crs.to_epsg() == 3035
    assert info.warnings == []


# ---------------------------------------------------------------------
# PostGIS: Abdeckung der Region durch den Import
# ---------------------------------------------------------------------


def _backend(monkeypatch, extent):
    backend = osm.PostgisBackend(dsn="postgresql://user@localhost/osm")
    monkeypatch.setattr(osm.PostgisBackend, "import_extent", lambda self: extent)

    def no_engine(*args, **kwargs):
        raise AssertionError("keine Datenbankabfrage erwartet")

    monkeypatch.setattr(osm, "create_engine", no_engine)
    return backend


def test_fa56_postgis_region_outside_import_extent_is_an_error(monkeypatch):
    backend = _backend(monkeypatch, GERMANY_EXTENT)
    paris = _region(2.22, 48.81, 2.47, 48.90)
    with pytest.raises(ValueError) as excinfo:
        backend.fetch({"power": "substation"}, paris)
    message = str(excinfo.value)
    assert "außerhalb" in message and "PostGIS-Import" in message
    assert "5.87" in message and "2.22" in message


def test_fa56_postgis_region_partly_outside_import_extent_warns(monkeypatch):
    backend = _backend(monkeypatch, GERMANY_EXTENT)
    border = _region(14.9, 50.9, 15.3, 51.1)  # Goerlitz über die Neisse
    with pytest.warns(osm.RegionWarning, match="teilweise außerhalb"):
        backend.check_import_coverage(border)


def test_fa56_postgis_region_inside_import_extent_passes_silently(monkeypatch, recwarn):
    backend = _backend(monkeypatch, GERMANY_EXTENT)
    backend.check_import_coverage(_region(13.60, 51.01, 13.94, 51.15))
    assert not [w for w in recwarn if issubclass(w.category, osm.RegionWarning)]


def test_fa56_postgis_without_extent_statistics_gives_a_notice_once(monkeypatch):
    backend = _backend(monkeypatch, None)
    with pytest.warns(osm.PostgisExtentNotice, match="nicht geprüft"):
        backend.check_import_coverage(_region(2.22, 48.81, 2.47, 48.90))
    import warnings as warnings_module

    with warnings_module.catch_warnings():
        warnings_module.simplefilter("error")
        backend.check_import_coverage(_region(2.22, 48.81, 2.47, 48.90))


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeConnection:
    def __init__(self, extent_row):
        self.extent_row = extent_row
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.sql.append(sql)
        if "ST_EstimatedExtent" in sql:
            return _FakeResult(self.extent_row)
        return _FakeResult((1,))


class _FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection

    def dispose(self):
        pass


def test_fa56_postgis_extent_in_backend_status(monkeypatch):
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "postgis")
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://user@localhost/osm")
    connection = _FakeConnection(GERMANY_EXTENT)
    # backend_status importiert create_engine beim Aufruf (Health-Endpunkt)
    monkeypatch.setattr(
        "sqlalchemy.create_engine", lambda *a, **k: _FakeEngine(connection)
    )
    status = osm.backend_status()
    assert status["reachable"] is True
    assert status["extent"] == list(GERMANY_EXTENT)
    extent_sql = next(sql for sql in connection.sql if "ST_EstimatedExtent" in sql)
    assert "'osm_polygons'" in extent_sql and "'geom'" in extent_sql


def test_fa56_backend_status_extent_is_none_without_postgis(monkeypatch):
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "overpass")
    assert osm.backend_status()["extent"] is None


def test_fa56_postgis_import_extent_reads_estimated_extent(monkeypatch):
    connection = _FakeConnection(GERMANY_EXTENT)
    monkeypatch.setattr(osm, "create_engine", lambda *a, **k: _FakeEngine(connection))
    backend = osm.PostgisBackend(dsn="postgresql://user@localhost/osm")
    assert backend.import_extent() == GERMANY_EXTENT
    assert backend.import_extent() == GERMANY_EXTENT
    assert len(connection.sql) == 1  # einmal je Backend-Instanz


def test_fa56_postgis_import_extent_without_statistics_is_none(monkeypatch):
    connection = _FakeConnection((None, None, None, None))
    monkeypatch.setattr(osm, "create_engine", lambda *a, **k: _FakeEngine(connection))
    backend = osm.PostgisBackend(dsn="postgresql://user@localhost/osm")
    assert backend.import_extent() is None


def _fiji_answer() -> list[dict]:
    """Nominatim splits a region across 180 degrees into parts on both edges."""
    from shapely.geometry import MultiPolygon, mapping

    parts = MultiPolygon([box(176, -18, 180, -16), box(-180, -18, -178, -16)])
    return [
        {
            "geojson": mapping(parts),
            "display_name": "Fidschi",
            "class": "boundary",
            "type": "administrative",
        }
    ]


def test_fa56_named_region_across_the_antimeridian_is_rejected(tmp_path):
    """Nachtreview 12: only bbox literals were checked; a named region across 180
    degrees became a 360-degree bbox under equal_area or a misleading error under auto."""
    with pytest.raises(ValueError, match="Antimeridian"):
        region_module.resolve_region_with_meta(
            "Fidschi", fetcher=lambda name: _fiji_answer()
        )
    assert not list(tmp_path.glob("**/*.json")), "rejected region must not be cached"


def test_fa56_world_band_bbox_literal_stays_allowed():
    region, _ = region_module.resolve_region_with_meta("-180,-10,180,10")
    assert tuple(region.total_bounds) == (-180, -10, 180, 10)


def test_fa56_jsonv2_category_is_read_as_the_osm_class():
    """Nachtreview 02.10. (Runde 2): ``format=jsonv2`` liefert ``category``
    statt ``class``; vorher war die Klasse in echten Läufen immer None und
    die Klassenwarnung feuerte nie."""
    answer = _fixture("nominatim_dresden.json")
    del answer[0]["class"]
    answer[0]["category"] = "amenity"
    answer[0]["type"] = "university"
    region, meta = region_module.resolve_region_with_meta(
        "TU Dresden", fetcher=lambda n: answer
    )
    assert meta["class"] == "amenity"
    info = region_module.describe_region(region, "auto", meta)
    assert len(info.warnings) == 1 and "amenity/university" in info.warnings[0]


def test_fa07_refresh_re_resolves_a_cached_region_name(tmp_path):
    """Nachtreview 02.10. (Runde 2): ``refresh=True`` (``--refresh-snapshots``)
    las bisher die gecachte Region, ein veralteter Treffer ließ sich nur durch
    Löschen der Datei beheben."""
    calls = []

    def fetcher(name):
        calls.append(name)
        return _fixture("nominatim_dresden.json")

    region_module.resolve_region_with_meta("Dresden, Deutschland", fetcher=fetcher)
    region_module.resolve_region_with_meta("Dresden, Deutschland", fetcher=fetcher)
    assert len(calls) == 1
    region_module.resolve_region_with_meta(
        "Dresden, Deutschland", fetcher=fetcher, refresh=True
    )
    assert len(calls) == 2
