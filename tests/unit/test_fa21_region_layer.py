"""Implements: FA21 (Szenario-Region als Layer referenzieren).

Contract-Tests für den neuen source-Typ 'region': Schema-Union-Dispatch,
Katalog-Eintrag, sowie End-to-End über run_scenario (Executor lädt den
region-Layer mit exakt der intern aufgelösten Regionsgeometrie).
Region wird über ein BBox-Literal aufgelöst (region.py, kein
Netzzugriff) - Muster aus tests/unit/test_fa09_executor.py.
"""

import geopandas as gpd

from geofact.core.scenario import Scenario
from geofact.engine import region as region_module
from geofact.builtin.sources.region import RegionLayer
from geofact.engine.executor import run_scenario

BBOX_REGION = "13.60,51.01,13.94,51.15"


# =====================================================================
# Schema-Union-Dispatch + Katalog
# =====================================================================


def test_fa21_layer_union_dispatch_region():
    minimal = {
        "scenario": {"name": "t", "region": BBOX_REGION},
        "layers": [{"id": "gebiet", "source": "region"}],
        "steps": [{"id": "s", "op": "to_points", "inputs": {"features": "gebiet"}}],
        "output": [{"type": "geojson", "source": "s", "path": "out.geojson"}],
    }
    scenario = Scenario(**minimal)
    assert isinstance(scenario.layers[0], RegionLayer)
    assert scenario.layers[0].source == "region"


def test_fa21_catalog_lists_region_source_type():
    from geofact.engine import catalog

    sources = {s["source"] for s in catalog.source_types()}
    assert "region" in sources


# =====================================================================
# Happy Path (End-to-End über run_scenario)
# =====================================================================


def _region_scenario() -> dict:
    return {
        "scenario": {"name": "Test", "region": BBOX_REGION},
        "layers": [{"id": "gebiet", "source": "region"}],
        "steps": [
            {"id": "punkte", "op": "to_points", "inputs": {"features": "gebiet"}},
        ],
        "output": [{"type": "map", "source": "punkte"}],
    }


def test_fa21_region_layer_loads_as_single_polygon():
    scenario = Scenario(**_region_scenario())
    result = run_scenario(scenario)
    gebiet = result.store["gebiet"]
    assert isinstance(gebiet, gpd.GeoDataFrame)
    assert len(gebiet) == 1
    assert gebiet.geometry.iloc[0].geom_type in ("Polygon", "MultiPolygon")


def test_fa21_region_layer_matches_internally_resolved_region():
    """Nachbedingung: der Layer-Inhalt ist bytegleich mit dem region_gdf,
    das der Executor für alle Layer desselben Laufs zur BBox-/
    Containment-Berechnung verwendet (kein zweiter Auflösungspfad)."""
    scenario = Scenario(**_region_scenario())
    result = run_scenario(scenario)
    gebiet = result.store["gebiet"]

    expected = region_module.resolve_region(BBOX_REGION).to_crs(gebiet.crs)
    assert gebiet.geometry.iloc[0].equals_exact(
        expected.geometry.iloc[0], tolerance=1e-6
    )


def test_fa21_region_layer_is_crs_harmonized_like_other_layers():
    """Der region-Layer durchläuft dieselbe FA5-Harmonisierung wie jeder
    andere Layer - Ergebnis liegt in der UTM-Zielprojektion, nicht in
    EPSG:4326 (auch wenn resolve_region() intern EPSG:4326 liefert)."""
    scenario = Scenario(**_region_scenario())
    result = run_scenario(scenario)
    gebiet = result.store["gebiet"]
    assert gebiet.crs.to_epsg() != 4326


def test_fa21_region_layer_unused_is_never_loaded():
    config = _region_scenario()
    config["layers"].append({"id": "ungebraucht", "source": "region"})
    scenario = Scenario(**config)
    result = run_scenario(scenario)
    assert "ungebraucht" not in result.loaded_layers
