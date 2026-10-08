"""Implements: FA9 (DAG-Executor), FA12, FA13, Phase-3-DoD.

End-to-End-Test: Szenario 1 (Energieinfrastruktur, tests/fixtures/szenario1_infrastruktur.yaml;
bis 08.10.2026 ein mitgeliefertes Beispiel) läuft komplett aus
der YAML bis inklusive Karte und RDF-Export (ergebnis.ttl). OSM-Fetch
und die Populationsraster-Quelle sind durch Fixture-Layer ersetzt
(kein Netzzugriff, deterministisch). Auch die Regionsauflösung ist ersetzt
(Fixture statt Nominatim, FA45): ohne region_fetcher fragte dieser Test das
Netz ab und der Standardlauf war nicht offline.
"""

from pathlib import Path

import geopandas as gpd
import pytest
import rasterio
import rdflib
import yaml
from shapely.geometry import mapping

from geofact.core.scenario import Scenario
from geofact.core.types import RasterLayer
from geofact.engine.executor import run_scenario
from geofact.builtin.outputs import export as export_module
from geofact.builtin.outputs import map as map_module
from geofact.builtin.outputs import rdf as rdf_module

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.e2e


def _fixture_vector_loader(fixture_name: str):
    def loader(layer):
        return gpd.read_file(FIXTURES_DIR / fixture_name)

    return loader


def _region_fixture_fetcher(name: str) -> list[dict]:
    """Nominatim-Ersatz: Sachsen aus der Fixture (Region des Szenarios)."""
    if name != "Sachsen, Deutschland":
        return []
    geometry = gpd.read_file(FIXTURES_DIR / "sachsen_region.geojson").geometry.iloc[0]
    return [{"geojson": mapping(geometry)}]


def _fixture_population_loader(layer):
    with rasterio.open(FIXTURES_DIR / "population.tif") as src:
        return RasterLayer(
            data=src.read(1), transform=src.transform, crs=src.crs, path=str(src.name)
        )


def test_e2e_szenario1_runs_end_to_end_with_map_and_rdf(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    raw = yaml.safe_load(
        (FIXTURES_DIR / "szenario1_infrastruktur.yaml").read_text(encoding="utf-8")
    )
    scenario = Scenario(**raw)

    result = run_scenario(
        scenario,
        region_fetcher=_region_fixture_fetcher,
        connector_override={
            "substations": _fixture_vector_loader("substations.geojson"),
            "power_lines": _fixture_vector_loader("power_lines.geojson"),
            "population": _fixture_population_loader,
        },
    )

    assert "critical" in result.store
    assert "ranked" in result.store
    ranked = result.store["ranked"]
    assert "risk_score" in ranked.columns

    # Ausgabeschicht: Karte + RDF, wie in output: der YAML deklariert
    map_path = tmp_path / "karte.html"
    map_module.write_map(ranked, map_path, color_field="risk_score")
    assert map_path.is_file()

    ttl_path = tmp_path / "ergebnis.ttl"
    critical = result.store["critical"]
    rdf_module.write_turtle(critical, ttl_path, layer_name="critical")
    assert ttl_path.is_file()

    graph = rdflib.Graph()
    graph.parse(str(ttl_path), format="turtle")
    # leer ist zulässig (hängt von den Fixture-Werten ab), aber die
    # Datei muss ein valides (ggf. leeres) Turtle-Dokument sein
    assert graph is not None

    geojson_path = tmp_path / "ergebnis.geojson"
    export_module.write_geojson(critical, geojson_path)
    assert geojson_path.is_file()
