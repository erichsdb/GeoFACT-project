"""Implements: FA55 (Arbeits-CRS aus scenario.crs), FA5-Zusatz (working_crs ersetzt utm_zone_epsg).

Contract-Tests für engine/region.py::working_crs/describe_region. Kein Netz:
Regionen sind BBox-Literale.
"""

import pytest
from pyproj import CRS

from geofact.engine import region as region_module

DRESDEN = "13.60,51.01,13.94,51.15"
WORLD = "-180,-90,180,90"


def test_fa55_default_crs_auto_equals_fa05_utm_zone():
    region = region_module.resolve_region(DRESDEN)
    crs, warnings = region_module.working_crs(region)
    assert crs == region_module.utm_zone_epsg(region)
    assert crs.to_epsg() == 32633
    assert warnings == []
    info = region_module.describe_region(region, "auto")
    assert info.crs == crs and info.crs_mode == "auto" and info.crs_spec == "auto"


def test_fa55_preset_equal_area_resolves_to_epsg_6933():
    region = region_module.resolve_region(DRESDEN)
    crs, warnings = region_module.working_crs(region, "equal_area")
    assert crs.to_epsg() == 6933
    assert warnings == []
    info = region_module.describe_region(region, "equal_area")
    assert info.crs_mode == "declared"
    assert info.to_dict()["crs"] == "EPSG:6933"


def test_fa55_explicit_epsg_is_used():
    region = region_module.resolve_region(DRESDEN)
    crs, _ = region_module.working_crs(region, "EPSG:25833")
    assert crs == CRS.from_epsg(25833)
    info = region_module.describe_region(region, "EPSG:25833")
    data = info.to_dict()
    assert data["crs"] == "EPSG:25833"
    assert data["crs_label"].startswith("EPSG:25833 (")
    assert data["bbox"] == pytest.approx([13.60, 51.01, 13.94, 51.15])
    # Dresden-BBox: ca. 23.8 km x 15.6 km
    assert 300 < data["area_km2"] < 450


def test_fa55_geographic_working_crs_is_rejected():
    region = region_module.resolve_region(DRESDEN)
    with pytest.raises(ValueError, match="geographisches CRS"):
        region_module.working_crs(region, "EPSG:4326")


def test_fa55_world_bbox_with_auto_crs_is_not_mappable():
    region = region_module.resolve_region(WORLD)
    with pytest.raises(ValueError, match="nicht abbildbar") as excinfo:
        region_module.describe_region(region, "auto")
    assert "equal_area" in str(excinfo.value)


def test_fa55_world_bbox_with_equal_area_describes_area_gt_zero():
    region = region_module.resolve_region(WORLD)
    info = region_module.describe_region(region, "equal_area")
    assert info.area_km2 > 4.0e8  # Erdoberfläche ca. 5.1e8 km2
    assert info.warnings == []


def test_fa55_polar_region_under_auto_is_an_error():
    region = region_module.resolve_region("10,85,20,89")
    with pytest.raises(ValueError, match="84 Grad"):
        region_module.working_crs(region, "auto")
