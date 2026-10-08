"""Implements: FA4 (Datei-Konnektor).

Contract-Tests für src/geofact/builtin/sources/file.py und builtin/formats/*.
"""

from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

from geofact.builtin.sources import file as files
from geofact.builtin.sources.file import FileLayer
from geofact.core.types import RasterLayer
from geofact.plugin_api import LoadContext

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _ctx(window_bounds=None, base_dir=None) -> LoadContext:
    """LoadContext wie im Lauf: das BBox-Fenster ist die Szenario-Region (EPSG:4326),
    base_dir das Verzeichnis der YAML-Datei (FA4)."""
    region = None
    if window_bounds is not None:
        region = gpd.GeoDataFrame(geometry=[box(*window_bounds)], crs="EPSG:4326")
    return LoadContext(region=region, base_dir=base_dir)


def make_layer(**overrides) -> FileLayer:
    defaults = {
        "id": "substations",
        "path": str(FIXTURES_DIR / "substations.geojson"),
        "data_type": "vector",
    }
    defaults.update(overrides)
    return FileLayer(**defaults)


def test_fa04_load_geojson():
    layer = make_layer(path=str(FIXTURES_DIR / "substations.geojson"))
    result = files.load(layer, _ctx())
    assert isinstance(result, gpd.GeoDataFrame)
    assert result.crs is not None
    assert len(result) == 5


def test_fa04_load_gpkg():
    layer = make_layer(path=str(FIXTURES_DIR / "substations.gpkg"), format="gpkg")
    result = files.load(layer, _ctx())
    assert isinstance(result, gpd.GeoDataFrame)
    assert result.crs is not None
    assert len(result) == 5


def test_fa04_load_shp():
    layer = make_layer(path=str(FIXTURES_DIR / "substations.shp"), format="shp")
    result = files.load(layer, _ctx())
    assert isinstance(result, gpd.GeoDataFrame)
    assert result.crs is not None
    assert len(result) == 5


def test_fa04_load_geotiff():
    layer = make_layer(
        id="population",
        path=str(FIXTURES_DIR / "population.tif"),
        data_type="raster",
        format="tif",
    )
    result = files.load(layer, _ctx())
    assert isinstance(result, RasterLayer)
    assert result.shape == (10, 10)
    assert result.data.sum() == 10000
    assert result.crs is not None


def test_fa04_load_geotiff_windowed_reads_smaller_extent():
    layer = make_layer(
        id="population",
        path=str(FIXTURES_DIR / "population.tif"),
        data_type="raster",
        format="tif",
    )
    # Fixture deckt 13.70-13.78 / 51.03-51.07 ab; Fenster auf die halbe
    # Breite eingrenzen -> weniger Spalten als beim vollen Lesen.
    windowed = files.load(layer, _ctx(window_bounds=(13.70, 51.03, 13.74, 51.07)))
    full = files.load(layer, _ctx())

    assert isinstance(windowed, RasterLayer)
    assert windowed.shape[1] < full.shape[1]
    assert windowed.shape[0] == full.shape[0]


def test_fa04_load_geotiff_window_outside_bounds_is_empty():
    layer = make_layer(
        id="population",
        path=str(FIXTURES_DIR / "population.tif"),
        data_type="raster",
        format="tif",
    )
    result = files.load(layer, _ctx(window_bounds=(0.0, 0.0, 0.01, 0.01)))
    assert isinstance(result, RasterLayer)
    assert result.data.size == 0 or result.data.sum() == 0


def test_fa04_unknown_format_raises():
    """Seit FA40 wird die Endung schon bei der Prüfung des Layers gegen
    die registrierten Dateiformate abgeglichen (FA2), nicht erst beim
    Laden - der Fehler kommt also vor jedem Dateizugriff."""
    with pytest.raises(ValueError, match="unbekanntes Dateiformat 'csv'"):
        make_layer(path=str(FIXTURES_DIR / "netz_wkt.csv"))


def test_fa04_missing_file_raises():
    layer = make_layer(id="ghost", path=str(FIXTURES_DIR / "does_not_exist.geojson"))
    with pytest.raises(FileNotFoundError) as exc_info:
        files.load(layer, _ctx())
    message = str(exc_info.value)
    assert "ghost" in message
    assert "does_not_exist.geojson" in message


def test_fa04_relative_path_resolves_against_base_dir():
    """base_dir (FA4-Pfadauflösung): ein relativer layer.path wird gegen
    base_dir aufgelöst statt gegen das Arbeitsverzeichnis des Prozesses -
    motivierender Fall: das Web-Backend startet an wechselnden CWDs, ein
    Pfad wie 'substations.geojson' relativ zur YAML-Datei muss trotzdem
    finden, egal von wo der Prozess gestartet wurde."""
    layer = make_layer(path="substations.geojson")
    result = files.load(layer, _ctx(base_dir=FIXTURES_DIR))
    assert len(result) > 0


def test_fa04_relative_path_without_base_dir_uses_process_cwd():
    """Rückwärtskompatibilität: ohne base_dir (Default None) bleibt das
    bisherige Verhalten (relativ zum Prozess-CWD) unverändert - ein
    Aufrufer ohne Datei-Ursprung (z. B. ein Test, der schon im richtigen
    Verzeichnis läuft) ist von der neuen Option unbeeinflusst."""
    layer = make_layer(path=str(FIXTURES_DIR / "substations.geojson"))
    result = files.load(layer, _ctx())  # kein base_dir - absoluter Pfad wie zuvor
    assert len(result) > 0


def test_fa04_absolute_path_ignores_base_dir():
    """Ein absoluter layer.path ist von base_dir unberührt (Path-Semantik:
    base_dir / absoluter_pfad ergibt den absoluten Pfad selbst) - ein
    frei editiertes Web-Szenario mit einem absoluten Pfad (z. B. ein
    Upload-Layer) darf durch base_dir=Repo-Root nicht verfälscht werden."""
    layer = make_layer(path=str(FIXTURES_DIR / "substations.geojson"))
    result = files.load(layer, _ctx(base_dir=Path("/irgendein/anderes/verzeichnis")))
    assert len(result) > 0


def test_fa04_missing_file_with_base_dir_reports_resolved_path():
    """Die Fehlermeldung zeigt den TATSAECHLICH gesuchten (aufgelösten)
    Pfad, nicht nur den rohen (ggf. relativen) layer.path - sonst bleibt
    unklar, ob die Datei fehlt oder nur am falschen Ort gesucht wurde
    (das ursprüngliche, schwer nachvollziehbare Symptom, ein stiller Fehler)."""
    layer = make_layer(id="ghost", path="does_not_exist.geojson")
    with pytest.raises(FileNotFoundError) as exc_info:
        files.load(layer, _ctx(base_dir=FIXTURES_DIR))
    message = str(exc_info.value)
    assert "ghost" in message
    # Der aufgelöste (base_dir-präfixierte) Pfad steht in der Meldung,
    # nicht nur der rohe relative Dateiname.
    assert str(FIXTURES_DIR) in message


# =====================================================================
# Zip-Container (FA4 Teil 1)
# =====================================================================


def test_fa04_zip_single_member_loads():
    layer = make_layer(path=str(FIXTURES_DIR / "substations_container.zip"))
    result = files.load(layer, _ctx())
    assert isinstance(result, gpd.GeoDataFrame)
    assert result.crs is not None
    assert len(result) == 5


def test_fa04_zip_ambiguous_raises_listing_candidates():
    layer = make_layer(path=str(FIXTURES_DIR / "ambiguous_container.zip"))
    with pytest.raises(ValueError) as exc_info:
        files.load(layer, _ctx())
    message = str(exc_info.value)
    assert "substations.shp" in message
    assert "substations.geojson" in message
    assert "member" in message


def test_fa04_zip_empty_raises_listing_members():
    layer = make_layer(path=str(FIXTURES_DIR / "empty_container.zip"))
    with pytest.raises(ValueError) as exc_info:
        files.load(layer, _ctx())
    message = str(exc_info.value)
    assert "readme.txt" in message


def test_fa04_zip_member_param_selects():
    layer = make_layer(
        path=str(FIXTURES_DIR / "ambiguous_container.zip"), member="substations.geojson"
    )
    result = files.load(layer, _ctx())
    assert isinstance(result, gpd.GeoDataFrame)
    assert len(result) == 5


def test_fa04_zip_missing_member_raises():
    layer = make_layer(
        path=str(FIXTURES_DIR / "ambiguous_container.zip"), member="doesnotexist.shp"
    )
    with pytest.raises(ValueError) as exc_info:
        files.load(layer, _ctx())
    message = str(exc_info.value)
    assert "doesnotexist.shp" in message
    assert "substations.shp" in message  # listet vorhandene Mitglieder


def test_fa04_zip_raster_unsupported_raises():
    # Seit Changelog 83 darf ein Zip-Container ein Raster enthalten
    # (tests/unit/test_fa04_zip_raster.py); ein Archiv ohne Raster-Mitglied
    # bleibt für einen Raster-Layer ein Fehler.
    layer = make_layer(
        id="raster_in_zip",
        path=str(FIXTURES_DIR / "substations_container.zip"),
        data_type="raster",
    )
    with pytest.raises(
        ValueError, match="kein Kandidat für data_type 'raster' im Zip-Container"
    ):
        files.load(layer, _ctx())


# =====================================================================
# Mehrschichtiges GeoPackage (FA4 Teil 2)
# =====================================================================


def test_fa04_gpkg_multilayer_without_layer_raises():
    layer = make_layer(path=str(FIXTURES_DIR / "multilayer.gpkg"), format="gpkg")
    with pytest.raises(ValueError) as exc_info:
        files.load(layer, _ctx())
    message = str(exc_info.value)
    assert "substations" in message
    assert "power_lines" in message


def test_fa04_gpkg_layer_param_selects():
    layer = make_layer(
        path=str(FIXTURES_DIR / "multilayer.gpkg"), format="gpkg", layer="power_lines"
    )
    result = files.load(layer, _ctx())
    assert isinstance(result, gpd.GeoDataFrame)
    assert len(result) == 4  # 4 Kanten im Ring, s. make_fixtures.py write_power_lines
    assert "geometry" in result.columns


# =====================================================================
# GML (FA4 Teil 3)
# =====================================================================


def test_fa04_gml_loads():
    layer = make_layer(path=str(FIXTURES_DIR / "minimal.gml"))
    result = files.load(layer, _ctx())
    assert isinstance(result, gpd.GeoDataFrame)
    assert result.crs is not None
    assert len(result) == 2


# =====================================================================
# CRS-Override (FA4 Teil 4)
# =====================================================================


def test_fa04_crs_override_field_defaults_to_none():
    layer = make_layer(path=str(FIXTURES_DIR / "substations.geojson"))
    assert layer.crs is None


def test_fa04_crs_override_field_accepts_epsg_string():
    layer = make_layer(path=str(FIXTURES_DIR / "substations.geojson"), crs="EPSG:25833")
    assert layer.crs == "EPSG:25833"


# =====================================================================
# Raster-Nodata (FA4 Teil 5)
# =====================================================================


def test_fa04_raster_nodata_passed_through():
    layer = make_layer(
        id="population",
        path=str(FIXTURES_DIR / "population_nodata.tif"),
        data_type="raster",
        format="tif",
    )
    result = files.load(layer, _ctx())
    assert isinstance(result, RasterLayer)
    assert result.nodata == pytest.approx(-200.0)


def test_fa04_raster_nodata_passed_through_windowed():
    layer = make_layer(
        id="population",
        path=str(FIXTURES_DIR / "population_nodata.tif"),
        data_type="raster",
        format="tif",
    )
    result = files.load(layer, _ctx(window_bounds=(13.70, 51.03, 13.74, 51.07)))
    assert isinstance(result, RasterLayer)
    assert result.nodata == pytest.approx(-200.0)


def test_fa04_raster_without_nodata_is_none():
    layer = make_layer(
        id="population",
        path=str(FIXTURES_DIR / "population.tif"),
        data_type="raster",
        format="tif",
    )
    result = files.load(layer, _ctx())
    assert isinstance(result, RasterLayer)
    assert result.nodata is None


# =====================================================================
# Lesefenster an der Datei beschnitten, Quell-CRS aus der Konfiguration
# (Nachtreview 02.10.2026)
# =====================================================================


def _write_tif(
    path: Path, crs, west: float, north: float, cell: float, size: int = 100
) -> Path:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    data = np.arange(size * size, dtype="float32").reshape(size, size)
    profile = {
        "driver": "GTiff",
        "height": size,
        "width": size,
        "count": 1,
        "dtype": "float32",
        "transform": from_origin(west, north, cell, cell),
    }
    if crs is not None:
        profile["crs"] = crs
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


def _bounds(raster: RasterLayer) -> tuple[float, float, float, float]:
    from rasterio.transform import array_bounds

    south_west_north_east = array_bounds(
        raster.data.shape[-2], raster.data.shape[-1], raster.transform
    )
    west, south, east, north = south_west_north_east
    return west, south, east, north


def test_fa04_raster_smaller_than_the_region_keeps_its_position(tmp_path):
    """Nachtreview 9: the window was not clipped to the file, so the data of a small
    raster was placed at the region corner (about 20 km west, 55 km north)."""
    path = _write_tif(tmp_path / "small.tif", "EPSG:4326", 12.3, 51.4, 0.001)
    layer = make_layer(id="klein", path=str(path), data_type="raster", format="tif")
    result = files.load(layer, _ctx(window_bounds=(12.0, 51.0, 13.0, 52.0)))
    assert result.data.shape == (100, 100)
    assert _bounds(result) == pytest.approx((12.3, 51.3, 12.4, 51.4))


def test_fa04_raster_partly_overlapping_the_region_keeps_its_position(tmp_path):
    path = _write_tif(tmp_path / "half.tif", "EPSG:4326", 12.3, 51.4, 0.001)
    layer = make_layer(id="halb", path=str(path), data_type="raster", format="tif")
    result = files.load(layer, _ctx(window_bounds=(12.35, 51.0, 13.0, 52.0)))
    west, south, east, north = _bounds(result)
    assert west == pytest.approx(12.35) and east == pytest.approx(12.4)
    assert south == pytest.approx(51.3) and north == pytest.approx(51.4)
    assert result.data[0, 0] == 50.0  # Zeile 0, Spalte 50 der Datei


def test_fa04_raster_without_crs_uses_the_declared_crs_for_the_window(tmp_path):
    """Nachtreview 10a: a GeoTIFF without CRS plus crs: EPSG:25833 failed with
    'CRSError: CRS is invalid: None'."""
    path = _write_tif(tmp_path / "nocrs.tif", None, 310_000, 5_700_000, 100)
    layer = make_layer(
        id="ohne", path=str(path), data_type="raster", format="tif", crs="EPSG:25833"
    )
    result = files.load(layer, _ctx(window_bounds=(12.33, 51.36, 12.40, 51.40)))
    assert 0 < result.data.shape[0] < 100 and 0 < result.data.shape[1] < 100


def test_fa04_raster_crs_override_is_used_for_the_window(tmp_path):
    """Nachtreview 10b: a mislabelled file (EPSG:4326 in the file, UTM metres) with
    crs_override got a 0x0 window computed in degrees."""
    reference = files.load(
        make_layer(
            id="ok",
            path=str(
                _write_tif(tmp_path / "ok.tif", "EPSG:25833", 310_000, 5_700_000, 100)
            ),
            data_type="raster",
            format="tif",
        ),
        _ctx(window_bounds=(12.33, 51.36, 12.40, 51.40)),
    )
    path = _write_tif(tmp_path / "wrong.tif", "EPSG:4326", 310_000, 5_700_000, 100)
    layer = make_layer(
        id="falsch",
        path=str(path),
        data_type="raster",
        format="tif",
        crs="EPSG:25833",
        crs_override=True,
    )
    result = files.load(layer, _ctx(window_bounds=(12.33, 51.36, 12.40, 51.40)))
    assert result.data.shape == reference.data.shape
    assert result.transform == reference.transform


def test_fa04_raster_without_any_crs_is_a_clear_error(tmp_path):
    path = _write_tif(tmp_path / "nocrs.tif", None, 310_000, 5_700_000, 100)
    layer = make_layer(id="ohne", path=str(path), data_type="raster", format="tif")
    with pytest.raises(ValueError, match="ohne.*kein CRS.*crs"):
        files.load(layer, _ctx(window_bounds=(12.33, 51.36, 12.40, 51.40)))


def test_fa04_raster_window_is_rounded_outward_to_whole_cells(tmp_path):
    """Nachtreview 02.10. (Runde 2): ein Fenster mit Bruchteil-Versatz las die
    Zellen ab Spalte 1, georeferenzierte sie aber ab der Fenstergrenze (halbe
    Zelle verschoben) und schnitt den Randstreifen ab."""
    path = _write_tif(tmp_path / "grob.tif", "EPSG:4326", 0.0, 1.0, 0.1, size=10)
    layer = make_layer(id="grob", path=str(path), data_type="raster", format="tif")
    result = files.load(layer, _ctx(window_bounds=(0.05, 0.4, 0.27, 0.6)))
    west, south, east, north = _bounds(result)
    assert (west, south, east, north) == pytest.approx((0.0, 0.4, 0.3, 0.6))
    assert result.data.shape == (2, 3)
    # Zeile 4 der Datei (Breite 0.5..0.6), Spalten 0..2: Werte 40, 41, 42
    assert list(result.data[0]) == [40.0, 41.0, 42.0]
