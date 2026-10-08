"""Implements: FA9 (DAG-Executor), FA8 (dissolve), Phase-3-Bonus (B2).

End-to-End-Test: Szenario 3 (Grünflächen-Erreichbarkeit nach
Bevölkerung) läuft komplett aus der YAML. OSM-Fetch, Region-
Auflösung und die GHS-POP-Rasterquelle sind durch Fixture-Layer
ersetzt (kein Netzzugriff, kein Download der echten GHS-POP-Datei
nötig - population.tif ist dieselbe kleine Fixture wie in den
FA8-Tests)."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
import rasterio
import yaml

from geofact.core.scenario import Scenario
from geofact.core.types import RasterLayer
from geofact.engine.executor import run_scenario

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.e2e


def _fixture_vector_loader(fixture_name: str):
    def loader(layer):
        return gpd.read_file(FIXTURES_DIR / fixture_name)

    return loader


def _fixture_population_loader(layer):
    with rasterio.open(FIXTURES_DIR / "population.tif") as src:
        return RasterLayer(
            data=src.read(1), transform=src.transform, crs=src.crs, path=str(src.name)
        )


def _region_fixture_fetcher(name: str) -> list[dict]:
    raw = json.loads(
        (FIXTURES_DIR / "nominatim_dresden.json").read_text(encoding="utf-8")
    )
    return raw if name == "Dresden, Deutschland" else []


def test_e2e_szenario3_runs_end_to_end_without_double_counting(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    raw = yaml.safe_load(
        (EXAMPLES_DIR / "szenario3_gruenflaechen_bevoelkerung.yaml").read_text(
            encoding="utf-8"
        )
    )
    scenario = Scenario(**raw)

    result = run_scenario(
        scenario,
        connector_override={
            "parks": _fixture_vector_loader("parks.geojson"),
            "population": _fixture_population_loader,
        },
        region_fetcher=_region_fixture_fetcher,
    )

    assert set(result.loaded_layers) == {"parks", "population"}

    # Ringe dürfen sich untereinander nicht überlappen (sonst wäre
    # Bevölkerung doppelt gezählt - der ursprüngliche Bug).
    reach_300 = result.store["reach_300"]
    reach_500 = result.store["reach_500"]
    ring_gut = result.store["ring_gut"]
    ring_maessig = result.store["ring_maessig"]

    for gdf in (reach_300, reach_500, ring_gut, ring_maessig):
        union_area = gdf.geometry.union_all().area
        sum_area = gdf.geometry.area.sum()
        assert sum_area == pytest.approx(union_area, rel=1e-6), (
            "Ring-Geometrien überlappen sich - Bevölkerung würde doppelt gezählt"
        )

    pop_sehr_gut = result.store["pop_sehr_gut"]
    pop_gut = result.store["pop_gut"]
    pop_maessig = result.store["pop_maessig"]

    for gdf in (pop_sehr_gut, pop_gut, pop_maessig):
        assert "sum_value" in gdf.columns
        assert gdf["sum_value"].notna().any()
        assert (gdf["sum_value"].dropna() >= 0).all()
