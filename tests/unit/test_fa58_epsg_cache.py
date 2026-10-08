"""Implements: FA58 (CRS-Provenienz je Layer), FA57 (CRS-Beschriftung in Meldungen).

The EPSG lookup behind provenance and labels is cached per CRS definition:
pyproj searches the PROJ database on every ``to_epsg()`` call for a CRS
without an EPSG code (about 60 ms for the Mollweide GHS rasters), and a layer
asks for the same CRS several times. Found by a regression run:
S1 Kommune 0.3 s -> 0.6 s."""

from __future__ import annotations

import pytest
from pyproj import CRS

from geofact.core import crs as crs_module
from geofact.core.crs import crs_label, epsg_code

MOLLWEIDE_WKT = CRS.from_proj4(
    "+proj=moll +lon_0=0 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
).to_wkt()


@pytest.fixture(autouse=True)
def _empty_cache():
    crs_module._EPSG_CACHE.clear()
    yield
    crs_module._EPSG_CACHE.clear()


@pytest.fixture
def count_lookups(monkeypatch):
    calls: list[str] = []
    original = CRS.to_epsg

    def counting(self, *args, **kwargs):
        calls.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CRS, "to_epsg", counting)
    return calls


def test_fa58_epsg_code_matches_pyproj():
    assert epsg_code(CRS.from_user_input("EPSG:25833")) == 25833
    assert epsg_code(CRS.from_user_input("EPSG:4326")) == 4326
    assert epsg_code(CRS.from_wkt(MOLLWEIDE_WKT)) is None


def test_fa58_epsg_lookup_runs_once_per_crs_definition(count_lookups):
    mollweide = CRS.from_wkt(MOLLWEIDE_WKT)
    for _ in range(5):
        crs_label(mollweide)
        epsg_code(CRS.from_wkt(MOLLWEIDE_WKT))  # new object, same definition
    assert len(count_lookups) == 1
    assert crs_label(mollweide) == mollweide.name


def test_fa58_epsg_cache_keeps_distinct_crs_apart(count_lookups):
    assert crs_label(CRS.from_user_input("EPSG:25832")).startswith("EPSG:25832 ")
    assert crs_label(CRS.from_user_input("EPSG:25833")).startswith("EPSG:25833 ")
    assert crs_label(CRS.from_user_input("EPSG:25832")).startswith("EPSG:25832 ")
    assert len(count_lookups) == 2


def test_fa58_epsg_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(crs_module, "_EPSG_CACHE_MAX", 2)
    for code in (25832, 25833, 32633):
        assert epsg_code(CRS.from_epsg(code)) == code
    assert len(crs_module._EPSG_CACHE) <= 2
