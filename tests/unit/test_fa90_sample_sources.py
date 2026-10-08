"""Tests für FA90 (mitgelieferte Beispieldaten im Quellenkatalog).

Deckt: Happy Path (jeder beschriebene Datensatz erscheint als Katalogquelle,
sein Layer-Fragment lädt die echte Datei), Fehlerfälle (fehlende Datei,
ungültige Beschreibung, Pfad außerhalb des Beispielordners), Randfall (leere
Beschreibung) sowie die Anbindung an /api/catalog, das Grounding und die
Oberfläche.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact import api  # noqa: E402
from geofact.core.scenario import Scenario  # noqa: E402
from geofact_web import catalog_service  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.models import GenerateRequest  # noqa: E402
from geofact_web.routers import config as config_router  # noqa: E402
from geofact_web.settings import Settings, get_settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"
FRONTEND = REPO_ROOT / "frontend"

# Region, in der jede Beispieldatei Objekte hat (Sachsen) - die bundesweiten
# Dateien decken sie mit ab.
REGION = "11.8,50.1,15.1,51.7"


@pytest.fixture(autouse=True)
def _fresh_cache():
    catalog_service.reset_sample_cache()
    yield
    catalog_service.reset_sample_cache()


def _custom(monkeypatch, tmp_path, text: str) -> Settings:
    manifest = tmp_path / "samples.yaml"
    manifest.write_text(text, encoding="utf-8")
    monkeypatch.setenv("GEOFACT_SAMPLE_SOURCES", str(manifest))
    return Settings(examples_dir=tmp_path)


def _scenario(template: dict) -> dict:
    return {
        "scenario": {"name": template["id"], "region": REGION},
        "layers": [template],
        "steps": [
            {
                "id": "alle",
                "op": "filter",
                "inputs": {"features": template["id"]},
                "params": {"condition": "1 == 1"},
            },
        ],
        "output": [{"type": "geojson", "source": "alle", "path": "alle.geojson"}],
    }


def test_fa90_every_described_sample_is_a_catalog_source():
    sources = catalog_service.sample_sources(get_settings())
    assert len(sources) >= 10
    assert len({s.id for s in sources}) == len(sources)
    for source in sources:
        assert source.kind == "sample"
        assert source.id.startswith("sample.")
        path = Path(source.layer_template["path"])
        assert path.is_absolute() and path.is_file(), source.id
        assert EXAMPLES_DIR in path.parents
        assert source.layer_template.get("license", {}).get("name"), source.id


def test_fa90_every_sample_template_is_a_valid_layer():
    for source in catalog_service.sample_sources(get_settings()):
        scenario = Scenario(**_scenario(source.layer_template))
        assert scenario.execution_order() == ["alle"], source.id


@pytest.mark.parametrize(
    "sample_id",
    [s.id for s in catalog_service.sample_sources(Settings())],
)
def test_fa90_sample_template_loads_its_file(sample_id, tmp_path):
    """Nachbedingung: das Fragment ist einbaufertig - die Datei lädt mit den
    deklarierten Spalten und Typen und liefert Zeilen."""
    source = catalog_service.find_source(get_settings(), sample_id, owner="")
    assert source is not None
    report = api.run(
        api.load_scenario(_scenario(source.layer_template)), out_dir=tmp_path
    )
    result = report.result.store.get("alle")
    assert len(result) > 0, sample_id
    declared = set(source.layer_template.get("columns") or {})
    assert declared <= set(result.columns), sample_id


def test_fa90_statistics_tables_name_their_join_key():
    """Eine Tabelle ohne Geometrie ist nur über einen Join nutzbar; die
    Beschreibung sagt, woran."""
    for source in catalog_service.sample_sources(get_settings()):
        template = source.layer_template
        if template["source"] == "table" and "geometry" not in template:
            assert "Schlüssel" in (source.description or ""), source.id


def test_fa90_missing_file_drops_only_that_entry_with_a_warning(monkeypatch, tmp_path):
    (tmp_path / "da.csv").write_text("lon,lat\n13.0,51.0\n", encoding="utf-8")
    settings = _custom(
        monkeypatch,
        tmp_path,
        """
samples:
  - id: da
    label: Vorhanden
    layer: {id: da, source: table, path: da.csv, format: csv,
            geometry: {x_column: lon, y_column: lat, crs: "EPSG:4326"}}
  - id: weg
    label: Fehlt
    layer: {id: weg, source: table, path: weg.csv, format: csv}
""",
    )
    with pytest.warns(UserWarning, match="weg.csv.*fehlt"):
        sources = catalog_service.sample_sources(settings)
    assert [s.id for s in sources] == ["sample.da"]


def test_fa90_empty_description_is_an_empty_group(monkeypatch, tmp_path):
    assert (
        catalog_service.sample_sources(_custom(monkeypatch, tmp_path, "samples: []\n"))
        == []
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("samples: [", "kein gültiges YAML"),
        ("andere: []\n", "Schlüssel 'samples'"),
        ("samples:\n  - {id: a, label: A}\n", "braucht 'layer'"),
        ("samples:\n  - {id: a, label: A, layer: {id: a}}\n", "'id' und 'path'"),
        (
            "samples:\n  - {id: a, label: A, layer: {id: a, path: ../geheim.csv}}\n",
            "nicht verlassen",
        ),
        (
            "samples:\n  - {id: a, label: A, layer: {id: a, path: a.csv}}\n"
            "  - {id: a, label: B, layer: {id: b, path: b.csv}}\n",
            "doppelte id",
        ),
    ],
)
def test_fa90_invalid_description_is_an_explicit_error(
    monkeypatch, tmp_path, text, message
):
    settings = _custom(monkeypatch, tmp_path, text)
    with pytest.raises(catalog_service.SampleCatalogError, match=message):
        catalog_service.sample_sources(settings)


def test_fa90_missing_description_file_is_an_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setenv("GEOFACT_SAMPLE_SOURCES", str(tmp_path / "gibt_es_nicht.yaml"))
    with pytest.raises(catalog_service.SampleCatalogError, match="nicht gefunden"):
        catalog_service.sample_sources(Settings())


def test_fa90_catalog_endpoint_lists_the_samples():
    payload = TestClient(create_app()).get("/api/catalog").json()
    ids = {s["id"] for s in payload["samples"]}
    assert {
        "sample.bip_kreise_2023",
        "sample.unfaelle_sachsen_2024",
        "sample.bundestagswahl_2025_wahlkreise",
    } <= ids
    assert all(s["kind"] == "sample" for s in payload["samples"])


def test_fa90_grounding_names_every_sample_without_preselection():
    settings = get_settings()
    templates, hints = config_router._grounding(
        settings, GenerateRequest(prompt="x"), owner=""
    )
    assert templates == []
    text = "\n".join(hints)
    for source in catalog_service.sample_sources(settings):
        assert source.label in text
        assert Path(source.layer_template["path"]).name in text
    assert "bip_eur_je_einwohner" in text  # Spaltennamen aus der Datei


def test_fa90_preselected_sample_becomes_a_template_and_is_not_repeated():
    settings = get_settings()
    request = GenerateRequest(
        prompt="x", source_ids=["sample.ladesaeulen_sachsen_2026"]
    )
    templates, hints = config_router._grounding(settings, request, owner="")
    assert [t["id"] for t in templates] == ["ladesaeulen"]
    assert not any(
        h.startswith("Mitgelieferte Beispieldaten") and "Ladesäulen" in h for h in hints
    )


def test_fa90_frontend_shows_the_group():
    panel = (FRONTEND / "components" / "source-panel.tsx").read_text(encoding="utf-8")
    types = (FRONTEND / "lib" / "types.ts").read_text(encoding="utf-8")
    assert 'section("Beispieldaten", catalog?.samples ?? [])' in panel
    assert "samples?: CatalogSource[]" in types
    assert '"sample"' in types
