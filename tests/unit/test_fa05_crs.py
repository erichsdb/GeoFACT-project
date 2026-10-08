"""Implements: FA5 (CRS-Harmonisierer).

Contract-Tests für src/geofact/builtin/transforms/reproject.py (harmonize) und
src/geofact/engine/region.py (utm_zone_epsg).
"""

from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from affine import Affine
from pyproj import CRS
from shapely.geometry import Point

from geofact.builtin.transforms import reproject as crs_module
from geofact.core.types import RasterLayer
from geofact.engine import region as region_module

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def test_fa05_detect_crs():
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(13.7, 51.0)], crs="EPSG:4326")
    target = CRS.from_epsg(32633)
    result = crs_module.harmonize("substations", gdf, target)
    assert result.crs.to_epsg() == 32633


def test_fa05_utm_zone_from_region():
    region = gpd.read_file(FIXTURES_DIR / "sachsen_region.geojson")
    target = region_module.utm_zone_epsg(region)
    assert target.to_epsg() == 32633


def test_fa05_reproject_roundtrip_area_tolerance():
    gdf = gpd.read_file(FIXTURES_DIR / "sachsen_region.geojson")
    original_area_deg = gdf.geometry.iloc[0].area

    target = CRS.from_epsg(32633)
    reprojected = crs_module.harmonize("sachsen_region", gdf, target)
    back = reprojected.to_crs("EPSG:4326")

    roundtrip_area_deg = back.geometry.iloc[0].area
    assert roundtrip_area_deg == pytest.approx(original_area_deg, rel=0.01)


def test_fa05_missing_crs_with_override():
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(13.7, 51.0)])
    assert gdf.crs is None
    target = CRS.from_epsg(32633)
    result = crs_module.harmonize("ohne_crs", gdf, target, override_crs="EPSG:4326")
    assert result.crs.to_epsg() == 32633


def test_fa05_missing_crs_raises():
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(13.7, 51.0)])
    assert gdf.crs is None
    target = CRS.from_epsg(32633)
    with pytest.raises(ValueError, match="ohne_crs"):
        crs_module.harmonize("ohne_crs", gdf, target)


def test_fa05_raster_reprojected_to_target_crs():
    layer = RasterLayer(
        data=np.full((10, 10), 100, dtype="int32"),
        transform=Affine(0.008, 0, 13.70, 0, -0.004, 51.07),
        crs=CRS.from_epsg(4326),
    )
    target = CRS.from_epsg(32633)
    result = crs_module.harmonize("population", layer, target)
    assert result.crs.to_epsg() == 32633
    assert result.data.sum() > 0


def test_fa04_crs_override_fills_missing_crs():
    """FileLayer.crs (FA4) landet als override_crs bei harmonize() -
    füllt ein fehlendes Datei-CRS (z. B. Shapefile ohne .prj)."""
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(13.7, 51.0)])
    assert gdf.crs is None
    target = CRS.from_epsg(32633)
    result = crs_module.harmonize("ohne_prj", gdf, target, override_crs="EPSG:4326")
    assert result.crs.to_epsg() == 32633


def test_fa04_declared_file_crs_wins_over_override():
    """Deklariertes Datei-CRS hat immer Vorrang vor FileLayer.crs (FA5-
    Vertrag bleibt unverändert, s. _resolve_source_crs)."""
    gdf = gpd.GeoDataFrame(
        {"id": [1]}, geometry=[Point(500000, 5650000)], crs="EPSG:32633"
    )
    target = CRS.from_epsg(32633)
    # Absichtlich ein falsches Override-CRS (EPSG:4326) angeben - wird
    # ignoriert, da die Geometrie bereits ein gesetztes CRS hat.
    result = crs_module.harmonize("mit_crs", gdf, target, override_crs="EPSG:4326")
    assert result.geometry.iloc[0].x == pytest.approx(500000)
    assert result.geometry.iloc[0].y == pytest.approx(5650000)


def test_fa05_raster_reproject_preserves_nodata():
    """Ohne src_nodata/dst_nodata an reproject() würde der Sentinelwert
    beim Resampling mit echten Nachbarzellen vermischt (Bugfix) -
    dieser Test verhindert eine Regression."""
    size = 10
    data = np.full((size, size), 100.0)
    data[:, 5:] = -200.0  # rechte Hälfte: Nodata-Sentinel
    layer = RasterLayer(
        data=data,
        transform=Affine(0.008, 0, 13.70, 0, -0.004, 51.07),
        crs=CRS.from_epsg(4326),
        nodata=-200.0,
    )
    target = CRS.from_epsg(32633)
    result = crs_module.harmonize("population_nodata", layer, target)

    assert result.nodata == pytest.approx(-200.0)
    valid = result.data[result.data != -200.0]
    assert valid.size > 0
    # Keine Vermischung: gültige Zellen sind ausschließlich der echte
    # Messwert (100), kein durch Nodata-Nachbarn verfälschter Zwischenwert.
    assert set(np.unique(result.data)) <= {100.0, -200.0}
