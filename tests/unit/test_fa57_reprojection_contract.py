"""Implements: FA57 (Nachbedingung der Reprojektion), FA55 (crs_override, densify_km im Harmonisierer).

Contract-Tests für builtin/transforms/reproject.py::harmonize und die
Transformation 'reproject'. Synthetische Layer, kein Netz.
"""

import warnings

import geopandas as gpd
import numpy as np
import pytest
from pyproj import CRS
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, Polygon, box

from geofact.builtin.transforms import reproject as reproject_module
from geofact.builtin.transforms.reproject import (
    ReprojectionWarning,
    SourceCrsWarning,
    harmonize,
    reproject_to_target,
)
from geofact.plugin_api import LoadContext, RasterLayer

UTM33 = CRS.from_epsg(32633)


def _points(*coords, crs="EPSG:4326") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"v": range(len(coords))}, geometry=[Point(c) for c in coords], crs=crs
    )


# ---------------------------------------------------------------------
# (1) Quell-CRS: Datei, Konfiguration, crs_override
# ---------------------------------------------------------------------


def test_fa57_crs_override_replaces_file_crs_with_warning():
    # GK4-Koordinaten (EPSG:31468), die Datei behauptet fälschlich EPSG:25833
    data = _points((4_590_000, 5_655_000), crs="EPSG:25833")
    with pytest.warns(
        SourceCrsWarning, match="Datei deklariert EPSG:25833.*erzwingt EPSG:31468"
    ):
        result = harmonize("gk", data, UTM33, override_crs="EPSG:31468", force=True)
    lon, lat = result.to_crs(4326).geometry.iloc[0].coords[0]
    assert 13.0 < lon < 14.0 and 50.5 < lat < 51.5  # Sachsen, nicht Kasachstan


def test_fa57_declared_crs_without_override_keeps_file_crs_and_warns():
    data = _points((13.7, 51.05))
    with pytest.warns(SourceCrsWarning, match="die Datei gewinnt"):
        result = harmonize("f", data, UTM33, override_crs="EPSG:3035")
    expected = data.to_crs(UTM33)
    assert result.geometry.iloc[0].equals_exact(expected.geometry.iloc[0], 1e-6)


def test_fa57_declared_crs_equal_to_file_crs_is_silent():
    data = _points((13.7, 51.05))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = harmonize("f", data, UTM33, override_crs="EPSG:4326", force=True)
    assert result.crs == UTM33


def test_fa57_layer_without_crs_needs_a_declared_crs():
    data = gpd.GeoDataFrame(geometry=[Point(13.7, 51.05)])
    with pytest.raises(ValueError, match="kein CRS erkennbar"):
        harmonize("ohne", data, UTM33)
    assert harmonize("ohne", data, UTM33, override_crs="EPSG:4326").crs == UTM33


# ---------------------------------------------------------------------
# (2) Plausibilität der Rohkoordinaten
# ---------------------------------------------------------------------


def test_fa57_metric_coordinates_under_geographic_crs_are_an_error():
    data = _points(
        (390_000, 5_650_000), (400_000, 5_660_000)
    )  # UTM-Meter unter EPSG:4326
    with pytest.raises(ValueError) as excinfo:
        harmonize("meter", data, UTM33)
    message = str(excinfo.value)
    assert (
        "Layer 'meter'" in message
        and "Koordinaten passen nicht zu EPSG:4326" in message
    )
    assert "crs_override" in message


def test_fa57_projected_coordinates_outside_area_of_use_warn():
    # GK4-Rechtswerte (4.59e6) unter UTM 33 gelesen: weit außerhalb der Zone
    data = _points((4_590_000, 5_655_000), crs="EPSG:25833")
    with pytest.warns(
        SourceCrsWarning, match="außerhalb des Gültigkeitsbereichs von EPSG:25833"
    ):
        harmonize("gk", data, CRS.from_epsg(3035))


# ---------------------------------------------------------------------
# (3) densify_km
# ---------------------------------------------------------------------


def test_fa57_densify_km_segments_lines_and_polygons_but_not_points():
    data = gpd.GeoDataFrame(
        {"kind": ["line", "polygon", "point"]},
        geometry=[
            LineString([(12.0, 51.0), (14.0, 51.0)]),  # ca. 140 km
            box(12.0, 50.0, 13.0, 50.5),
            Point(13.0, 51.0),
        ],
        crs="EPSG:4326",
    )
    plain = harmonize("d", data, UTM33)
    dense = harmonize("d", data, UTM33, densify_km=10.0)
    assert len(plain.geometry.iloc[0].coords) == 2
    assert len(dense.geometry.iloc[0].coords) >= 15
    assert len(dense.geometry.iloc[1].exterior.coords) > len(
        plain.geometry.iloc[1].exterior.coords
    )
    assert dense.geometry.iloc[2].equals(plain.geometry.iloc[2])
    assert len(data.geometry.iloc[0].coords) == 2  # Eingabe unverändert


# ---------------------------------------------------------------------
# (5) Nachbedingung: endliche Stützpunkte, Gültigkeit
# ---------------------------------------------------------------------


def test_fa57_nonfinite_coordinates_after_reprojection_are_an_error():
    # Antipode des LAEA-Zentrums (10 E, 52 N) ist in EPSG:3035 nicht abbildbar
    data = _points((13.7, 51.05), (-170.0, -52.0), (-170.0, -52.0))
    with pytest.raises(ValueError) as excinfo:
        harmonize("welt", data, CRS.from_epsg(3035))
    message = str(excinfo.value)
    assert "Layer 'welt'" in message
    assert "2 nicht endliche Stützpunkte in 2 Objekt(en)" in message
    assert "[1, 2]" in message and "scenario.crs: equal_area" in message


def test_fa57_newly_invalid_geometries_after_reprojection_warn(monkeypatch):
    data = gpd.GeoDataFrame(
        geometry=[box(13.0, 51.0, 13.1, 51.1), box(13.2, 51.0, 13.3, 51.1)],
        crs="EPSG:4326",
    )
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])
    original = gpd.GeoDataFrame.to_crs

    def folding_to_crs(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        result.loc[result.index[0], "geometry"] = bowtie
        return result

    monkeypatch.setattr(gpd.GeoDataFrame, "to_crs", folding_to_crs)
    with pytest.warns(
        ReprojectionWarning, match="1 Geometrie\\(n\\) nach der Reprojektion"
    ):
        harmonize("faltung", data, UTM33)


def test_fa57_empty_layer_passes_all_checks():
    empty = gpd.GeoDataFrame({"v": []}, geometry=[], crs="EPSG:4326")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = harmonize(
            "leer",
            empty,
            CRS.from_epsg(3035),
            override_crs="EPSG:4326",
            force=True,
            densify_km=1.0,
        )
    assert result.empty and result.crs.to_epsg() == 3035


# ---------------------------------------------------------------------
# Raster
# ---------------------------------------------------------------------


def _raster(crs, origin=(13.0, 51.2), cell=0.01, nodata=-1.0) -> RasterLayer:
    data = np.arange(100, dtype="float32").reshape(10, 10)
    return RasterLayer(
        data=data, transform=from_origin(*origin, cell, cell), crs=crs, nodata=nodata
    )


def test_fa57_crs_override_applies_to_rasters():
    raster = _raster(
        CRS.from_epsg(3857)
    )  # Gradwerte, fälschlich als Web-Mercator markiert
    with pytest.warns(SourceCrsWarning, match="erzwingt EPSG:4326"):
        result = harmonize("r", raster, UTM33, override_crs="EPSG:4326", force=True)
    west, north = result.transform.c, result.transform.f
    assert 300_000 < west < 400_000 and 5_670_000 < north < 5_690_000


def test_fa57_metric_raster_under_geographic_crs_is_an_error():
    raster = _raster(CRS.from_epsg(4326), origin=(390_000, 5_660_000), cell=100)
    with pytest.raises(ValueError, match="Koordinaten passen nicht zu EPSG:4326"):
        harmonize("r", raster, UTM33)


def test_fa57_raster_reprojected_to_all_nodata_warns(monkeypatch):
    monkeypatch.setattr(reproject_module, "reproject", lambda **kwargs: None)
    with pytest.warns(ReprojectionWarning, match="komplett Nodata"):
        harmonize("r", _raster(CRS.from_epsg(4326)), UTM33)


def test_fa57_valid_raster_reprojection_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = harmonize("r", _raster(CRS.from_epsg(4326)), UTM33)
    assert (result.data != -1.0).any()


# ---------------------------------------------------------------------
# Transformation 'reproject' reicht crs_override und densify_km durch
# ---------------------------------------------------------------------


class _Layer:
    id = "datei"
    crs = "EPSG:31468"
    crs_override = True


def test_fa57_reproject_transform_passes_crs_override_and_densify_km():
    data = gpd.GeoDataFrame(
        geometry=[LineString([(4_590_000, 5_655_000), (4_610_000, 5_655_000)])],
        crs="EPSG:25833",
    )
    ctx = LoadContext(target_crs=UTM33, densify_km=1.0)
    with pytest.warns(SourceCrsWarning, match="erzwingt EPSG:31468"):
        result = reproject_to_target(_Layer(), data, ctx)
    line = result.geometry.iloc[0]
    assert len(line.coords) >= 20  # 20 km in Segmenten <= 1 km
    lon, lat = (
        gpd.GeoSeries([Point(line.coords[0])], crs=UTM33).to_crs(4326).iloc[0].coords[0]
    )
    assert 13.0 < lon < 14.0 and 50.5 < lat < 51.5


def test_fa57_float_raster_without_nodata_gets_nan_outside_the_source():
    """Nachtreview 11: cells outside the source footprint were filled with 0 and kept
    nodata=None, so a constant 5.0 raster averaged 4.68 after reprojection."""
    raster = RasterLayer(
        data=np.full((100, 100), 5.0, dtype="float32"),
        transform=from_origin(13.0, 51.2, 0.01, 0.01),
        crs=CRS.from_epsg(4326),
        nodata=None,
    )
    result = harmonize("r", raster, UTM33)
    assert result.nodata is not None and np.isnan(result.nodata)
    valid = result.data[~np.isnan(result.data)]
    assert valid.size > 0 and np.all(valid == 5.0)
    assert np.isnan(result.data).any()


def test_fa57_integer_raster_without_nodata_warns_about_zero_padding():
    raster = RasterLayer(
        data=np.full((100, 100), 5, dtype="int16"),
        transform=from_origin(13.0, 51.2, 0.01, 0.01),
        crs=CRS.from_epsg(4326),
        nodata=None,
    )
    with pytest.warns(ReprojectionWarning, match="außerhalb der Quelle"):
        result = harmonize("r", raster, UTM33)
    assert result.nodata is None
