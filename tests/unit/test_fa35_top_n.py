"""Implements: FA35 (Sortieren und auf die N ersten Objekte begrenzen).

Contract-Tests für src/geofact/builtin/operations/top_n.py.

Motivierender Fall: "die 5 größten Bundesländer auf Basis ihres
Bruttosozialproduktes 2025" - filter kennt nur absolute Schwellen (man
müsste den Schwellenwert bereits kennen), ranking erzeugt nur einen
Score ohne die Menge zu begrenzen.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import Point

from geofact.core.scenario import Scenario
from geofact.engine.catalog import params_catalog
from geofact.builtin.operations.top_n import TopNStep, run_top_n

BBOX_REGION = "5.8,47.2,15.1,55.1"


def features_gdf(values, *, column="bip_2025") -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {column: values, "name": [f"L{i}" for i in range(len(values))]},
        geometry=[Point(i, i) for i in range(len(values))],
        crs="EPSG:25832",
    )


# =====================================================================
# Happy Path
# =====================================================================


def test_fa35_keeps_n_largest_by_default():
    gdf = features_gdf([100, 500, 300, 200, 400])
    result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 3})
    assert result["bip_2025"].tolist() == [500, 400, 300]


def test_fa35_result_is_sorted_descending():
    """Nachbedingung: das Ergebnis ist sortiert - eine Top-N-Liste ohne
    Reihenfolge wäre als Rangliste nicht lesbar."""
    gdf = features_gdf([1, 9, 5, 7])
    result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 4})
    assert result["bip_2025"].tolist() == [9, 7, 5, 1]


def test_fa35_order_asc_keeps_smallest():
    gdf = features_gdf([100, 500, 300])
    result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 2, "order": "asc"})
    assert result["bip_2025"].tolist() == [100, 300]


def test_fa35_sorts_string_values_numerically():
    """Kernfall: Werte aus OSM/Tabellenquellen kommen als String an - ein
    lexikographisches Sortieren wäre still falsch ("9" > "100")."""
    gdf = features_gdf(["100", "9", "1000"])
    result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 3})
    assert result["bip_2025"].tolist() == ["1000", "100", "9"]


def test_fa35_rank_field_numbers_from_one():
    gdf = features_gdf([100, 500, 300])
    result = run_top_n(
        {"features": gdf}, {"by": "bip_2025", "n": 3, "rank_field": "rang"}
    )
    assert result["rang"].tolist() == [1, 2, 3]
    assert result["bip_2025"].tolist() == [500, 300, 100]


def test_fa35_geometry_and_attributes_preserved():
    gdf = features_gdf([100, 500])
    result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 1})
    assert result.crs == gdf.crs
    assert result["name"].tolist() == ["L1"]
    assert result.geometry.iloc[0].geom_type == "Point"


# =====================================================================
# Randfälle
# =====================================================================


def test_fa35_fewer_objects_than_n_returns_all():
    """ "Die 5 größten" bei nur 3 Objekten sind eben diese 3 - kein Fehler."""
    gdf = features_gdf([100, 300, 200])
    result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 5})
    assert len(result) == 3
    assert result["bip_2025"].tolist() == [300, 200, 100]


def test_fa35_empty_layer():
    gdf = features_gdf([])
    result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 5})
    assert result.empty


def test_fa35_empty_layer_with_rank_field_has_the_column():
    gdf = features_gdf([])
    result = run_top_n(
        {"features": gdf}, {"by": "bip_2025", "n": 5, "rank_field": "rang"}
    )
    assert "rang" in result.columns


def test_fa35_ties_keep_input_order_stable():
    """Reproduzierbarkeit (NFA1): bei Wertgleichheit bleibt die
    Eingabereihenfolge erhalten, das Ergebnis hängt nicht von der
    Sortierimplementierung ab."""
    gdf = features_gdf([500, 500, 100])
    result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 2})
    assert result["name"].tolist() == ["L0", "L1"]


def test_fa35_missing_values_do_not_get_a_rank():
    gdf = features_gdf([500, None, 300])
    result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 3})
    assert result["bip_2025"].tolist() == [500.0, 300.0]


# =====================================================================
# Fehlerfälle (keine stillen Fehler)
# =====================================================================


def test_fa35_unknown_attribute_is_an_error():
    gdf = features_gdf([100])
    with pytest.raises(ValueError, match="gibt_es_nicht"):
        run_top_n({"features": gdf}, {"by": "gibt_es_nicht", "n": 1})


def test_fa35_non_numeric_values_are_reported():
    """Goldene Regel 7: nicht sortierbare Werte entfallen, aber gemeldet."""
    gdf = features_gdf(["500", "keine Angabe", "300"])
    with pytest.warns(UserWarning, match="nicht"):
        result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 3})
    assert result["bip_2025"].tolist() == ["500", "300"]


def test_fa35_all_values_unusable_yields_empty_with_warning():
    gdf = features_gdf(["keine Angabe", "n/a"])
    with pytest.warns(UserWarning):
        result = run_top_n({"features": gdf}, {"by": "bip_2025", "n": 2})
    assert result.empty


# =====================================================================
# Contract/Schema (FA1)
# =====================================================================


def test_fa35_step_declares_contract():
    step = TopNStep(id="top", inputs={"features": "x"}, params={"by": "bip", "n": 5})
    assert set(step.INPUT_PORTS) == {"features"}
    assert step.OUTPUT_TYPE.value == "vector"
    assert set(params_catalog(TopNStep.Params)[1]) == {"by", "n"}


@pytest.mark.parametrize("bad_n", [0, -1, "5", 2.5, True])
def test_fa35_invalid_n_is_rejected(bad_n):
    with pytest.raises(ValueError) as caught:
        TopNStep(id="top", inputs={"features": "x"}, params={"by": "bip", "n": bad_n})
    assert [error["loc"] for error in caught.value.errors()] == [("params", "n")]


def test_fa35_invalid_order_is_rejected():
    with pytest.raises(ValueError, match="order"):
        TopNStep(
            id="top",
            inputs={"features": "x"},
            params={"by": "bip", "n": 5, "order": "aufsteigend"},
        )


def test_fa35_rank_field_is_declared_as_produced():
    """FA12: eine von der Operation erzeugte Spalte muss als Contract
    deklariert sein, sonst verwirft die Web-Ansicht sie am Spaltenbudget."""
    step = TopNStep(
        id="top",
        inputs={"features": "x"},
        params={"by": "bip", "n": 5, "rank_field": "rang"},
    )
    assert step.produced_fields() == {"rang"}


def test_fa35_scenario_with_top_n_validates():
    scenario = Scenario(
        **{
            "scenario": {"name": "bip", "region": BBOX_REGION},
            "layers": [{"id": "states", "source": "region", "data_type": "vector"}],
            "steps": [
                {
                    "id": "größte",
                    "op": "top_n",
                    "inputs": {"features": "states"},
                    "params": {"by": "bip_2025", "n": 5},
                },
            ],
            "output": [{"type": "map", "source": "größte"}],
        }
    )
    assert scenario.execution_order() == ["größte"]
