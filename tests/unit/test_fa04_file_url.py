"""Implements: FA4 (Zusatz Datei-URL, Nachtrag Orchestrator Punkt 2).

``path`` darf ``http(s)://`` oder ``/vsicurl/...`` sein: der Konnektor löst
solche Pfade nicht gegen base_dir auf, prüft keine lokale Datei und reicht
sie als GDAL-Pfad ``/vsicurl/<url>`` an das Format weiter. Kein Netz im Test:
die Lesefunktionen werden ersetzt.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest
import rasterio

from geofact.builtin import _vector_io
from geofact.builtin.sources import file as files
from geofact.builtin.sources.file import FileLayer
from geofact.plugin_api import LoadContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fake_vector_reader(monkeypatch) -> list:
    calls: list = []
    real = gpd.read_file

    def fake(source, **kwargs):
        calls.append(source)
        return real(FIXTURES / "substations.geojson")

    monkeypatch.setattr(_vector_io.gpd, "read_file", fake)
    return calls


@pytest.mark.parametrize(
    "path, expected",
    [
        (
            "https://example.org/daten/umspannwerke.geojson",
            "/vsicurl/https://example.org/daten/umspannwerke.geojson",
        ),
        (
            "http://example.org/daten/umspannwerke.geojson?stand=2026",
            "/vsicurl/http://example.org/daten/umspannwerke.geojson?stand=2026",
        ),
        (
            "/vsicurl/https://example.org/daten/umspannwerke.geojson",
            "/vsicurl/https://example.org/daten/umspannwerke.geojson",
        ),
    ],
)
def test_fa04_file_url_is_read_through_vsicurl(monkeypatch, tmp_path, path, expected):
    calls = _fake_vector_reader(monkeypatch)
    layer = FileLayer(id="fern", path=path, data_type="vector")
    result = files.load(layer, LoadContext(base_dir=tmp_path))
    assert calls == [expected]
    assert len(result) == 5


def test_fa04_file_url_raster_is_handed_to_rasterio_as_vsicurl(monkeypatch):
    seen: list = []

    class Stop(Exception):
        pass

    def fake_open(source, *args, **kwargs):
        seen.append(source)
        raise Stop

    monkeypatch.setattr(rasterio, "open", fake_open)
    layer = FileLayer(id="dgm", path="https://example.org/dgm.tif", data_type="raster")
    with pytest.raises(Stop):
        files.load(layer, LoadContext())
    assert seen == ["/vsicurl/https://example.org/dgm.tif"]


def test_fa04_file_url_with_unknown_extension_needs_format():
    with pytest.raises(ValueError, match="unbekanntes Dateiformat"):
        FileLayer(
            id="fern", path="https://example.org/download?id=7", data_type="vector"
        )
    layer = FileLayer(
        id="fern",
        path="https://example.org/download?id=7",
        data_type="vector",
        format="geojson",
    )
    assert layer.format == "geojson"


def test_fa04_file_url_zip_container_is_rejected():
    layer = FileLayer(
        id="fern", path="https://example.org/daten.zip", data_type="vector"
    )
    with pytest.raises(ValueError, match="Zip-Container"):
        files.load(layer, LoadContext())
