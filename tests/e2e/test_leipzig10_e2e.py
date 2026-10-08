"""Implements: FA15 (Erreichbarkeitsanalyse), Phase-2-DoD-Analogon.

End-to-End-Test: Leipzig-Szenario 10 (Tram-/S-Bahn-/Bus-Erreichbarkeit)
läuft komplett aus der YAML bis vor die Ausgabeschicht. OSM-Fetch UND
der GTFS-Fetch (FA20) sind durch Fixture-Layer ersetzt, Region-Auflösung
durch eine eingecheckte Nominatim-Fixture (kein Netzzugriff,
deterministisch - Muster aus tests/e2e/test_szenario2_e2e.py
übernommen). Seit FA21/FA22 kommen die Analyse-Ursprungspunkte
('zentren') aus einem Hexraster (hex_grid) über dem region-Layer (FA21)
statt aus Ortsteil-Mittelpunkten - der region-Layer braucht keinen
connector_override, er wird direkt aus derselben Nominatim-Fixture
aufgelöst, die auch die OSM-/GTFS-Layer räumlich begrenzt."""

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
        (FIXTURES_DIR / "nominatim_leipzig.json").read_text(encoding="utf-8")
    )
    return raw if name == "Leipzig, Deutschland" else []


def test_e2e_leipzig10_runs_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    raw = yaml.safe_load(
        (EXAMPLES_DIR / "leipzig" / "leipzig10_erreichbarkeit.yaml").read_text(
            encoding="utf-8"
        )
    )
    scenario = Scenario(**raw)

    result = run_scenario(
        scenario,
        connector_override={
            "tramgleise": _fixture_loader("leipzig_tram_lines.geojson"),
            # FA19: kein eigenes S-Bahn-Fixture vorhanden - das bestehende
            # Tram-Linien-Fixture wird als Stand-in wiederverwendet, um die
            # Pipeline-Verdrahtung (vector_union -> network_nodes ->
            # build_network) zu prüfen. Geometrische Realitätsnähe ist
            # für diesen Regressionstest nicht erforderlich.
            "sbahn_gleise": _fixture_loader("leipzig_tram_lines.geojson"),
            # FA20: kein eigenes GTFS-Bus-Fixture vorhanden - dasselbe
            # Tram-Linien-Fixture dient wie schon bei sbahn_gleise als
            # Stand-in für den GTFS-abgeleiteten Buskanten-Layer, um die
            # zweite verkettete vector_union-Stufe (gleisnetz_bahn ∪
            # bus_liniennetz) zu prüfen. Auch hier zählt nur die
            # Pipeline-Verdrahtung, keine geometrische Realitätsnähe.
            "bus_liniennetz": _fixture_loader("leipzig_tram_lines.geojson"),
            "bahnhoefe": _fixture_loader("leipzig_hbf.geojson"),
        },
        region_fetcher=_region_fixture_fetcher,
    )

    assert set(result.order) == {
        "gleisnetz_bahn",
        "gleisnetz",
        "netz_knoten",
        "netz",
        "raster",
        "zentren",
        "hbf",
        "anbindung",
        "anbindung_zellen",
        "einzugsbereiche",
    }
    assert set(result.loaded_layers) == {
        "tramgleise",
        "sbahn_gleise",
        "bus_liniennetz",
        "gebiet",
        "bahnhoefe",
    }

    # anbindung_zellen (spatial_join von raster mit anbindung) ist der
    # eigentliche Karten-Output: die Hexagon-Polygone selbst, nicht nur
    # ihre Zellmittelpunkte (FA22).
    anbindung_zellen = result.store["anbindung_zellen"]
    assert "network_distance" in anbindung_zellen.columns
    assert (anbindung_zellen.geometry.geom_type == "Polygon").all()
    # Hexraster-Zellmittelpunkte überdecken die gesamte (großflächige)
    # Nominatim-Fixture-Region; das Tram-Fixture-Netz deckt nur eine
    # kleine Fläche davon ab -> einige Zellen liegen nah genug am Netz
    # für eine endliche Distanz, die meisten liegen außerhalb von
    # max_snap_m -> NaN. Beide Fälle müssen im Ergebnis vorkommen.
    assert anbindung_zellen["network_distance"].notna().sum() >= 1
    assert anbindung_zellen["network_distance"].isna().sum() >= 1

    einzugsbereiche = result.store["einzugsbereiche"]
    assert list(einzugsbereiche["break_m"]) == [2000, 4000, 6000]
    areas = einzugsbereiche.geometry.area.tolist()
    assert areas == sorted(areas)  # kumulatives Wachstum mit dem break
