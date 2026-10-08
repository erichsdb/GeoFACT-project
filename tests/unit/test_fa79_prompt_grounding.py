"""Implements: FA79 (Grounding der Generierung aus Registry, Referenzszenarien
und Rasterkennwerten).

Der System-Prompt bezieht Ausgabeformate, Quellfelder und Formate aus der
Registry, trägt ausgewählte Beispiele als Vorlagen und je lokalem Raster die
Kennwerte der Datei. Offline: keine LLM-Anfrage, Raster aus tmp_path.
"""

from __future__ import annotations

import warnings

import numpy as np
import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")
rasterio = pytest.importorskip("rasterio")

from rasterio.transform import from_origin  # noqa: E402

from geofact import api  # noqa: E402
from geofact_web import catalog_service, llm  # noqa: E402
from geofact_web.llm import LLMError, build_system_prompt  # noqa: E402
from geofact_web.models import CatalogSource, GenerateRequest  # noqa: E402
from geofact_web.routers import config as config_router  # noqa: E402
from geofact_web.settings import EXAMPLES_DIR, Settings  # noqa: E402


def _block(text: str, head: str) -> str:
    """Der Abschnitt eines Listeneintrags ``- head`` bis zum nächsten Eintrag."""
    return text.split(f"\n- {head}", 1)[1].split("\n- ", 1)[0]


# =====================================================================
# Registry statt fester Listen
# =====================================================================


def test_fa79_every_registered_output_format_is_in_the_prompt():
    """Nachbedingung (1)/(2): alle Ausgabeformate der Registry mit Datentypen
    und Feldern; die feste Liste von vorher gibt es nicht mehr."""
    prompt = build_system_prompt([], [])
    assert "map | geojson | csv | rdf" not in prompt
    for entry in api.extension_catalog()["outputs"]:
        line = next(
            row
            for row in llm._outputs_text().splitlines()
            if row.startswith(f"- {entry['name']}: ")
        )
        assert line in prompt
        for data_type in entry["accepts"]:
            assert data_type in line
    geotiff = _block("\n" + llm._outputs_text(), "geotiff: ")
    assert "[raster]" in "- geotiff: " + geotiff
    assert "color_field?: string" in _block("\n" + llm._outputs_text(), "map: ")


def test_fa79_a_new_output_format_appears_without_touching_llm(monkeypatch):
    """NFA4: ein per Plugin registriertes Ausgabeformat steht im Prompt, weil
    der Text aus dem Katalog entsteht."""
    real = api.extension_catalog()
    extended = {
        **real,
        "outputs": [
            *real["outputs"],
            {
                "name": "kml",
                "description": "Keyhole Markup (Demo-Plugin)",
                "origin": "plugin.py",
                "extension": "kml",
                "accepts": ["vector"],
                "options": [],
            },
        ],
    }
    monkeypatch.setattr(llm.api, "extension_catalog", lambda: extended)
    assert (
        "- kml: [vector] -> .kml; Keyhole Markup (Demo-Plugin)"
        in build_system_prompt([], [])
    )


def test_fa79_source_types_carry_their_fields():
    """Jede Quellart steht mit Beschreibung und Feldern da: Pflichtfelder ohne,
    optionale mit Fragezeichen; feste Werte (data_type: vector) entfallen."""
    text = "\n" + llm._sources_text()
    for source in api.source_types():
        assert f"\n- {source['source']}: {source['description']}" in text
    wfs = _block(text, "wfs: ")
    assert "    url: string" in wfs and "    typename: string" in wfs
    assert "data_type" not in wfs and "version" not in wfs
    osm = _block(text, "osm: ")
    assert "    tags: object oder array" in osm
    assert "    geometry?: " in osm and "point|line|polygon" in osm
    table = _block(text, "table: ")
    assert "geometry?: object mit x_column, y_column, wkt_column, crs" in table
    assert "Formatfeld (fwf, Pflicht)" in table
    file_block = _block(text, "file: ")
    assert "    data_type: vector|raster|graph" in file_block
    # die allgemeinen Layer-Felder stehen einmal im Prompttext, nicht je Quellart
    assert "    id" not in text and "    when" not in text


def test_fa79_a_new_source_type_appears_with_its_description(monkeypatch):
    extended = [
        *api.source_types(),
        {"source": "demo_dienst", "description": "Demo-Plugin"},
    ]
    monkeypatch.setattr(llm.api, "source_types", lambda: extended)
    assert "\n- demo_dienst: Demo-Plugin" in build_system_prompt([], [])


def test_fa79_file_and_table_formats_come_from_the_registry():
    catalog = api.extension_catalog()
    text = llm._formats_text()
    for entry in (*catalog["file_formats"], *catalog["table_formats"]):
        assert f"- {entry['name']} (" in text
    assert "- tif (source: file, raster; .tif, .tiff): " in text
    assert "- fwf (source: table; nur per format): " in text
    assert text in build_system_prompt([], [])


# =====================================================================
# Referenzszenarien als Vorlagen
# =====================================================================


@pytest.mark.parametrize("example_id", llm.PROMPT_EXAMPLES)
def test_fa79_prompt_examples_exist_and_are_valid(example_id):
    """Vorbedingung/Nachbedingung (3): jede Vorlage ist ein gültiges Beispiel
    aus examples/ und steht - ohne Kopfkommentar, sonst unverändert - im Prompt."""
    path = (EXAMPLES_DIR / example_id).with_suffix(".yaml")
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    report = api.validate(text, base_dir=path.parent)
    assert report.valid, report.issues

    prompt = build_system_prompt([], [])
    assert f"--- Beispiel {example_id} ---" in prompt
    body = text[text.index("scenario:") :].strip()
    assert body in prompt


def test_fa79_header_comment_of_an_example_is_dropped():
    prompt = build_system_prompt([], [])
    assert (
        "doi:10.6084" not in prompt
    )  # Datenbezug aus dem Kopf von sachsen_nachthimmel
    assert "distance: Meter zum" in prompt  # Kommentare an den Schritten bleiben


def test_fa79_missing_example_is_an_explicit_error(monkeypatch):
    monkeypatch.setattr(llm, "PROMPT_EXAMPLES", ("gibt/es_nicht",))
    with pytest.raises(LLMError, match="Vorlage 'gibt/es_nicht'"):
        build_system_prompt([], [])


# =====================================================================
# Rasterkennwerte
# =====================================================================


def _write_raster(path, data, *, nodata=None, crs="EPSG:32633", cell=100.0):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=str(data.dtype),
        crs=crs,
        transform=from_origin(300000, 5700000, cell, cell),
        nodata=nodata,
    ) as dst:
        dst.write(data, 1)


def _source(path, data_type="raster") -> CatalogSource:
    return CatalogSource(
        id="copernicus.licht",
        kind="copernicus",
        label="Nachtlicht",
        description="",
        data_type=data_type,
        layer_template={
            "id": "licht",
            "source": "file",
            "path": str(path),
            "data_type": data_type,
            "format": "tif",
        },
    )


def test_fa79_raster_profile_names_cell_size_and_value_range(tmp_path):
    """Happy Path: Skala 0 bis 63 - genau die Angabe, die P08 fehlte."""
    data = np.arange(64 * 64, dtype="float32").reshape(64, 64) % 64
    _write_raster(tmp_path / "licht.tif", data, nodata=-1.0)

    profile = catalog_service.raster_profile(_source(tmp_path / "licht.tif"))

    assert profile.startswith(
        "Kennwerte der Rasterquelle 'licht' (Nachtlicht): CRS EPSG:32633"
    )
    assert (
        "Zelle 100 x 100 m" in profile
        and "1 Band," in profile
        and "Nodata -1" in profile
    )
    assert (
        "Minimum 0," in profile and "Maximum 63" in profile and "Median 31.5" in profile
    )
    assert "100 % gültig" in profile
    assert "Einheit" not in profile  # die Datei trägt keine


def test_fa79_raster_profile_adds_nonzero_statistics_for_sparse_rasters(tmp_path):
    """Randfall: überwiegend Nullzellen (Zählraster mit Meer) - der Median 0
    sagt nichts, deshalb zusätzlich die Kennwerte der Zellen ungleich 0; Nodata
    zählt nicht mit."""
    data = np.zeros((50, 50), dtype="float32")
    data[0, :10] = 40.0
    data[1, :] = -200.0
    _write_raster(tmp_path / "pop.tif", data, nodata=-200.0)

    profile = catalog_service.raster_profile(_source(tmp_path / "pop.tif"))

    assert "98 % gültig" in profile
    assert "Median 0," in profile and "Maximum 40" in profile
    assert "nur Zellen ungleich 0 (0 %): Median 40" in profile


def test_fa79_raster_profile_in_degrees_names_the_approximate_metres(tmp_path):
    data = np.ones((20, 20), dtype="float32")
    with rasterio.open(
        tmp_path / "grad.tif",
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(12.0, 51.0, 0.01, 0.01),
    ) as dst:
        dst.write(data, 1)
    profile = catalog_service.raster_profile(_source(tmp_path / "grad.tif"))
    assert "0.01 x 0.01 Grad (rund 1113 m in Nord-Sued-Richtung)" in profile
    assert "Nodata" not in profile


def test_fa79_raster_profile_is_computed_once_per_file_version(tmp_path):
    """Nachbedingung (4): je Datei und Änderungszeit einmal; eine ersetzte
    Datei wird neu gelesen."""
    path = tmp_path / "einmal.tif"
    _write_raster(path, np.full((10, 10), 5.0, dtype="float32"))
    catalog_service._raster_profile.cache_clear()

    first = catalog_service.raster_profile(_source(path))
    assert catalog_service.raster_profile(_source(path)) == first
    info = catalog_service._raster_profile.cache_info()
    assert (info.misses, info.hits) == (1, 1)

    _write_raster(path, np.full((12, 12), 9.0, dtype="float32"))
    assert "Maximum 9" in catalog_service.raster_profile(_source(path))


def test_fa79_unreadable_raster_gives_no_profile_but_a_warning(tmp_path):
    """Nachbedingung (5), Fehlerfall: keine Kennwerte, aber eine Warnung -
    die Generierung läuft weiter (Goldene Regel 7)."""
    broken = tmp_path / "kaputt.tif"
    broken.write_text("kein GeoTIFF")
    with pytest.warns(UserWarning, match="Rasterkennwerte für 'licht'"):
        assert catalog_service.raster_profile(_source(broken)) is None
    with pytest.warns(UserWarning, match="Rasterkennwerte"):
        assert catalog_service.raster_profile(_source(tmp_path / "fehlt.tif")) is None


def test_fa79_vector_source_has_no_raster_profile(tmp_path):
    """Typfall: für eine Vektorquelle gibt es keine Rasterkennwerte."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert (
            catalog_service.raster_profile(_source(tmp_path / "x.geojson", "vector"))
            is None
        )


def test_fa79_grounding_carries_the_profile_of_every_local_raster(tmp_path):
    """Der Hinweis erreicht den Prompt: ``_grounding`` gibt je Raster des
    Katalogs Template und Kennwerte mit, auch ohne Vorauswahl."""
    _write_raster(
        tmp_path / "licht.tif", np.arange(100, dtype="float32").reshape(10, 10)
    )
    settings = Settings(copernicus_dir=tmp_path, llm_api_key="k")

    _templates, hints = config_router._grounding(
        settings, GenerateRequest(prompt="Wo ist es hell?"), "anonymous"
    )

    assert any(
        h.startswith("Verfügbare Rasterquelle") and "licht.tif" in h for h in hints
    )
    profile = next(
        h for h in hints if h.startswith("Kennwerte der Rasterquelle 'licht'")
    )
    assert "Maximum 99" in profile
    assert profile in build_system_prompt([], hints)
