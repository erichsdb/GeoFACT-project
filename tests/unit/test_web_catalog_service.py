"""Tests für backend/geofact_web/catalog_service.py:

- copernicus_catalog(): fehlendes Verzeichnis meldet sich sichtbar
  (warnings.warn) statt kommentarlos eine leere Liste zu liefern
  (Goldene Regel 7); /api/meta unterscheidet zusätzlich "fehlt" von
  "existiert, ist aber leer" über copernicus_dir_exists.
- source_schema_hint(): ein defekter/unlesbarer Quelldatensatz meldet sich
  über warnings.warn statt den Hinweis stillschweigend zu None zu machen.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from geofact_web import catalog_service  # noqa: E402
from geofact_web.models import CatalogSource  # noqa: E402
from geofact_web.settings import Settings  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _settings_with_copernicus_dir(path: Path) -> Settings:
    return Settings(copernicus_dir=path)


def test_copernicus_catalog_warns_when_directory_missing(tmp_path):
    """Vorbedingung: GEOFACT_COPERNICUS_DIR zeigt auf ein nicht
    existierendes Verzeichnis (z. B. frischer Clone ohne Copernicus-Daten).
    Nachbedingung: leere Liste wie bisher, aber mit sichtbarer Warnung -
    kein stilles Verschwinden (Regel 7)."""
    missing = tmp_path / "does_not_exist"
    settings = _settings_with_copernicus_dir(missing)

    with pytest.warns(UserWarning, match="existiert nicht"):
        sources = catalog_service.copernicus_catalog(settings)

    assert sources == []


def test_copernicus_catalog_empty_directory_is_silent(tmp_path):
    """Randfall: Verzeichnis existiert, enthält aber keine Raster - das
    ist ein legitimer leerer Katalog, keine Fehlkonfiguration, daher keine
    Warnung nötig (Abgrenzung zum fehlenden Verzeichnis oben)."""
    empty_dir = tmp_path / "rasters"
    empty_dir.mkdir()
    settings = _settings_with_copernicus_dir(empty_dir)

    with warnings_none():
        sources = catalog_service.copernicus_catalog(settings)

    assert sources == []


def warnings_none():
    """Kontextmanager: schlägt fehl, falls im Block überhaupt gewarnt wird."""
    import warnings as _warnings

    class _NoWarnings:
        def __enter__(self):
            self._cm = _warnings.catch_warnings(record=True)
            self._caught = self._cm.__enter__()
            _warnings.simplefilter("always")
            return self

        def __exit__(self, *exc):
            self._cm.__exit__(*exc)
            assert self._caught == [], f"Unerwartete Warnung(en): {self._caught}"

    return _NoWarnings()


def test_meta_endpoint_distinguishes_missing_from_empty_copernicus_dir(
    tmp_path, monkeypatch
):
    """Nachbedingung: /api/meta liefert copernicus_dir_exists, damit
    Frontend/Operator 'Verzeichnis fehlt' von 'Verzeichnis ist leer'
    unterscheiden können (vorher beides identisch: leerer Katalog)."""
    from fastapi.testclient import TestClient

    from geofact_web.main import create_app
    from geofact_web.settings import get_settings

    get_settings.cache_clear()
    missing = tmp_path / "does_not_exist"
    monkeypatch.setenv("GEOFACT_COPERNICUS_DIR", str(missing))
    try:
        client = TestClient(create_app())
        meta = client.get("/api/meta").json()
        assert meta["copernicus_dir_exists"] is False
    finally:
        get_settings.cache_clear()


def _osm_source() -> CatalogSource:
    return CatalogSource(
        id="osm.test",
        kind="osm_preset",
        label="Test",
        data_type="vector",
        layer_template={"id": "test", "source": "osm", "tags": {"power": "substation"}},
    )


def test_source_schema_hint_osm_lists_tag_keys():
    hint = catalog_service.source_schema_hint(_osm_source())
    assert hint is not None
    assert "power" in hint


def test_list_uploads_filters_by_owner(tmp_path):
    """FA28 Happy Path: list_uploads() liefert nur Uploads von 'owner'."""
    settings = Settings(uploads_dir=tmp_path)
    alice_dir = tmp_path / "alice-upload"
    alice_dir.mkdir()
    (alice_dir / "alice.geojson").write_text("{}", encoding="utf-8")
    catalog_service.write_upload_owner(alice_dir, "alice")

    bob_dir = tmp_path / "bob-upload"
    bob_dir.mkdir()
    (bob_dir / "bob.geojson").write_text("{}", encoding="utf-8")
    catalog_service.write_upload_owner(bob_dir, "bob")

    alice_uploads = catalog_service.list_uploads(settings, owner="alice")
    assert [s.label for s in alice_uploads] == ["alice.geojson"]


def test_list_uploads_without_owner_sidecar_falls_back_to_anonymous(tmp_path):
    """Randfall: ein Upload-Verzeichnis ohne Sidecar (vor FA28 angelegt)
    gehört dem Pseudo-Nutzer 'anonymous' - demselben Default wie
    auth.py::require_user ohne konfigurierte Nutzer (Regel 7: derselbe
    explizite Default, kein stiller Sonderfall)."""
    settings = Settings(uploads_dir=tmp_path)
    legacy_dir = tmp_path / "legacy-upload"
    legacy_dir.mkdir()
    (legacy_dir / "legacy.geojson").write_text("{}", encoding="utf-8")

    assert catalog_service.list_uploads(settings, owner="anonymous") != []
    assert catalog_service.list_uploads(settings, owner="alice") == []


def test_delete_upload_removes_directory_and_reports_success(tmp_path):
    """FA28 Happy Path: delete_upload() entfernt Datei + Verzeichnis und
    meldet True; ein zweiter Versuch auf dieselbe source_id meldet False
    (Randfall: bereits gelöscht)."""
    settings = Settings(uploads_dir=tmp_path)
    upload_dir = tmp_path / "to-delete"
    upload_dir.mkdir()
    (upload_dir / "data.geojson").write_text("{}", encoding="utf-8")
    catalog_service.write_upload_owner(upload_dir, "alice")
    source_id = f"upload.{upload_dir.name}.data.geojson"

    assert catalog_service.delete_upload(settings, source_id, owner="alice") is True
    assert not upload_dir.exists()
    assert catalog_service.delete_upload(settings, source_id, owner="alice") is False


def test_delete_upload_by_wrong_owner_is_a_noop(tmp_path):
    """Typfehler-Fall: delete_upload() unter dem Namen eines anderen
    Nutzers löscht nichts und meldet False - identisch zum Fall
    'existiert nicht', damit der Router beides als 404 melden kann,
    ohne die Existenz fremder Uploads zu bestätigen."""
    settings = Settings(uploads_dir=tmp_path)
    upload_dir = tmp_path / "alice-only"
    upload_dir.mkdir()
    (upload_dir / "data.geojson").write_text("{}", encoding="utf-8")
    catalog_service.write_upload_owner(upload_dir, "alice")
    source_id = f"upload.{upload_dir.name}.data.geojson"

    assert catalog_service.delete_upload(settings, source_id, owner="bob") is False
    assert upload_dir.exists()


def test_source_schema_hint_warns_on_unreadable_file(tmp_path):
    """Vorbedingung: eine 'file'-Quelle zeigt auf ein kaputtes/unlesbares
    GeoJSON. Nachbedingung: Rückgabewert bleibt None (Hinweis ist
    optional), aber es wird sichtbar gewarnt statt still zu scheitern."""
    broken = tmp_path / "broken.geojson"
    broken.write_text("{not valid json", encoding="utf-8")
    source = CatalogSource(
        id="upload.broken",
        kind="upload",
        label="broken.geojson",
        data_type="vector",
        layer_template={
            "id": "broken",
            "source": "file",
            "path": str(broken),
            "format": "geojson",
        },
    )

    with pytest.warns(UserWarning, match="Schema-Hinweis"):
        hint = catalog_service.source_schema_hint(source)

    assert hint is None
