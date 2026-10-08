"""Implements: FA4 (Zusatz ZIP-Raster, Changelog 83).

Ein Zip-Container darf ein Raster enthalten: der Datei-Konnektor wählt das
Mitglied nach dem data_type des Layers und reicht es als /vsizip/-Pfad (str)
an das Format ``tif`` weiter.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from geofact.builtin.sources import file as files
from geofact.builtin.sources.file import FileLayer
from geofact.core.types import RasterLayer
from geofact.plugin_api import LoadContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_fa04_zip_raster_loads_the_raster_member(tmp_path):
    archive = tmp_path / "bevoelkerung.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(FIXTURES / "population.tif", "daten/population.tif")
        zf.writestr("liesmich.txt", "Quelle: Testdaten")
        zf.write(FIXTURES / "substations.geojson", "substations.geojson")
    layer = FileLayer(id="pop", path=str(archive), data_type="raster")
    result = files.load(layer, LoadContext())
    assert isinstance(result, RasterLayer)
    assert result.path.startswith("/vsizip/")
    assert result.path.endswith("daten/population.tif")
    assert result.data.size > 0


def test_fa04_zip_raster_without_raster_member_lists_members():
    layer = FileLayer(
        id="pop", path=str(FIXTURES / "substations_container.zip"), data_type="raster"
    )
    with pytest.raises(ValueError) as caught:
        files.load(layer, LoadContext())
    message = str(caught.value)
    assert "raster" in message and "Zip-Container" in message
    assert "substations.shp" in message
