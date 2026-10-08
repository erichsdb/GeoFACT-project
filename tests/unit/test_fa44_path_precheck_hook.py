"""Implements: FA44 (Vorab-Check der Layer-Pfade, Hook-Ebene), FA4.

``check_before_run(base_dir)`` an FileLayer und TableLayer: eine fehlende
lokale Datei wird als (feld, meldung) gemeldet, URLs und GDAL-Pfade (/vsi...)
werden übersprungen, relative Pfade gelten gegen base_dir. Den Aufruf in
``run()`` ergänzt W2-02.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from geofact.builtin.sources.file import FileLayer
from geofact.builtin.sources.table import TableLayer
from geofact.plugin_api import LayerBase

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_fa44_precheck_missing_file_is_reported_with_field():
    layer = FileLayer(id="fehlt", path="gibt_es_nicht.geojson", data_type="vector")
    problems = layer.check_before_run(FIXTURES)
    assert len(problems) == 1
    field, message = problems[0]
    assert field == "path"
    assert "nicht gefunden" in message
    assert str(FIXTURES / "gibt_es_nicht.geojson") in message


def test_fa44_precheck_relative_path_resolves_against_base_dir():
    layer = FileLayer(id="ok", path="substations.geojson", data_type="vector")
    assert layer.check_before_run(FIXTURES) == []
    assert layer.check_before_run(FIXTURES / "nirgends") != []


@pytest.mark.parametrize(
    "path",
    [
        "https://example.org/daten/umspannwerke.geojson",
        "http://example.org/daten/raster.tif",
        "/vsicurl/https://example.org/daten/umspannwerke.geojson",
        "/vsizip/irgendwo/archiv.zip/datei.shp",
    ],
)
def test_fa44_precheck_skips_urls_and_gdal_paths(path):
    data_type = "raster" if path.endswith(".tif") else "vector"
    layer = FileLayer(id="fern", path=path, data_type=data_type)
    assert layer.check_before_run(None) == []


def test_fa44_precheck_zip_checks_the_container_not_the_member():
    layer = FileLayer(
        id="zip",
        path="substations_container.zip",
        data_type="vector",
        member="gibt_es_nicht.shp",
    )
    assert layer.check_before_run(FIXTURES) == []


def test_fa44_precheck_table_layer():
    missing = TableLayer(id="t", path="fehlt.csv")
    assert missing.check_before_run(FIXTURES)[0][0] == "path"
    assert TableLayer(id="t", path="bip_mini.csv").check_before_run(FIXTURES) == []
    assert (
        TableLayer(id="t", path="https://example.org/t.csv").check_before_run(None)
        == []
    )


def test_fa44_precheck_plugin_layer_without_hook_returns_empty():
    if not hasattr(LayerBase, "check_before_run"):
        pytest.skip("LayerBase.check_before_run kommt mit W1-01 (core/contracts.py)")

    class PluginLayer(LayerBase):
        source: str = "demo"

    layer = PluginLayer(id="p", source="demo", data_type="vector")
    assert layer.check_before_run(FIXTURES) == []
