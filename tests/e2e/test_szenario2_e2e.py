"""Implements: FA9 (DAG-Executor), Phase-2-DoD.

End-to-End-Test: Szenario 2 (Grünflächen-Erreichbarkeit) läuft
komplett aus der YAML bis vor die Ausgabeschicht. OSM-Fetch ist durch
Fixture-Layer ersetzt, Region-Auflösung durch eine eingecheckte
Nominatim-Fixture (kein Netzzugriff, deterministisch - siehe
test_fa03_region.py für denselben Fixture-Fetcher-Ansatz)."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
import yaml

from geofact.core.scenario import Scenario
from geofact.engine.executor import run_scenario

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.e2e


def _fixture_loader(fixture_name: str):
    def loader(layer):
        return gpd.read_file(FIXTURES_DIR / fixture_name)

    return loader


def _region_fixture_fetcher(name: str) -> list[dict]:
    raw = json.loads(
        (FIXTURES_DIR / "nominatim_dresden.json").read_text(encoding="utf-8")
    )
    return raw if name == "Dresden, Deutschland" else []


def test_e2e_szenario2_runs_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    raw = yaml.safe_load(
        (EXAMPLES_DIR / "szenario2_gruenflaechen.yaml").read_text(encoding="utf-8")
    )
    scenario = Scenario(**raw)

    result = run_scenario(
        scenario,
        connector_override={
            "parks": _fixture_loader("parks.geojson"),
            "buildings": _fixture_loader("residential_buildings.geojson"),
        },
        region_fetcher=_region_fixture_fetcher,
    )

    assert result.order == ["with_distance", "classified"]
    assert set(result.loaded_layers) == {"parks", "buildings"}

    with_distance = result.store["with_distance"]
    assert "distance" in with_distance.columns
    assert (with_distance["distance"] > 0).all()

    classified = result.store["classified"]
    assert "class" in classified.columns
    assert set(classified["class"]) <= {"sehr gut", "gut", "mäßig", "schlecht"}
    assert len(classified) == 4
