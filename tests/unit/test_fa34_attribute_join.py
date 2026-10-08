"""Implements: FA34 (nicht-räumlicher Attribut-Join).

Contract-Tests für src/geofact/builtin/operations/attribute_join.py.

Motivierender Fall: "Finde die 5 größten Bundesländer auf Basis ihres
Bruttosozialproduktes 2025" - die BIP-Zahlen liegen als Tabelle ohne
echte Geometrie vor und tragen ihren Raumbezug nur als Landesnamen. Ein
räumlicher Join (FA8) hilft dort nicht.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from geofact.core.scenario import Scenario
from geofact.engine.catalog import params_catalog
from geofact.builtin.operations.attribute_join import (
    AttributeJoinStep,
    normalize_key,
    run_attribute_join,
)

BBOX_REGION = "5.8,47.2,15.1,55.1"


def states_gdf(names) -> gpd.GeoDataFrame:
    """Bundesländer-Geometrien (links, behält die Geometrie)."""
    return gpd.GeoDataFrame(
        {"name": names},
        geometry=[
            Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)]) for i in range(len(names))
        ],
        crs="EPSG:25832",
    )


def gdp_table(lands, values, *, column="bip_2025") -> gpd.GeoDataFrame:
    """BIP-Tabelle (rechts). Die Geometrie ist der künstliche Platzhalter,
    den der Tabellen-Konnektor (FA14) vergibt - sie soll verworfen werden."""
    return gpd.GeoDataFrame(
        {"land": lands, column: values},
        geometry=[Point(0, 0)] * len(lands),
        crs="EPSG:25832",
    )


# =====================================================================
# Happy Path
# =====================================================================


def test_fa34_joins_table_attribute_onto_geometry():
    left = states_gdf(["Bayern", "Sachsen"])
    right = gdp_table(["Bayern", "Sachsen"], ["716000", "154000"])
    result = run_attribute_join(
        {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
    )
    assert result["bip_2025"].tolist() == ["716000", "154000"]


def test_fa34_keeps_left_geometry_and_drops_right_geometry():
    """Nachbedingung: die Geometrie des linken Layers bleibt erhalten,
    die künstliche des rechten wird verworfen."""
    left = states_gdf(["Bayern"])
    right = gdp_table(["Bayern"], ["716000"])
    result = run_attribute_join(
        {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
    )
    assert result.geometry.iloc[0].geom_type == "Polygon"
    assert result.crs == left.crs


def test_fa34_row_count_and_order_preserved():
    """Nachbedingung: Zeilenzahl und -reihenfolge von 'left' bleiben."""
    left = states_gdf(["Bayern", "Sachsen", "Bremen"])
    right = gdp_table(["Sachsen", "Bremen", "Bayern"], ["154000", "38000", "716000"])
    result = run_attribute_join(
        {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
    )
    assert result["name"].tolist() == ["Bayern", "Sachsen", "Bremen"]
    assert result["bip_2025"].tolist() == ["716000", "154000", "38000"]


def test_fa34_same_key_column_name_on_both_sides():
    """right_on ist optional - ohne Angabe gilt left_on für beide."""
    left = states_gdf(["Bayern"]).rename(columns={"name": "land"})
    right = gdp_table(["Bayern"], ["716000"])
    result = run_attribute_join({"left": left, "right": right}, {"left_on": "land"})
    assert result["bip_2025"].tolist() == ["716000"]


def test_fa34_columns_selection_takes_only_listed_attributes():
    left = states_gdf(["Bayern"])
    right = gpd.GeoDataFrame(
        {"land": ["Bayern"], "bip_2025": ["716000"], "quelle": ["Destatis"]},
        geometry=[Point(0, 0)],
        crs="EPSG:25832",
    )
    result = run_attribute_join(
        {"left": left, "right": right},
        {"left_on": "name", "right_on": "land", "columns": ["bip_2025"]},
    )
    assert "bip_2025" in result.columns
    assert "quelle" not in result.columns


# =====================================================================
# Schlüssel-Normalisierung (abweichende Behördenschreibweisen)
# =====================================================================


def test_fa34_normalize_matches_umlaut_spellings():
    """Kernfall der Praxis: 'Baden-Wuerttemberg' vs 'Baden-Württemberg'."""
    left = states_gdf(["Baden-Württemberg"])
    right = gdp_table(["Baden-Wuerttemberg"], ["580000"])
    result = run_attribute_join(
        {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
    )
    assert result["bip_2025"].tolist() == ["580000"]


def test_fa34_normalize_matches_case_and_punctuation():
    left = states_gdf(["Mecklenburg-Vorpommern"])
    right = gdp_table(["MECKLENBURG VORPOMMERN"], ["54000"])
    result = run_attribute_join(
        {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
    )
    assert result["bip_2025"].tolist() == ["54000"]


def test_fa34_normalize_strips_leading_zeros_of_numeric_keys():
    """AGS/Gemeindeschluessel kommen je Quelle mit und ohne führende Null."""
    left = states_gdf(["x"]).assign(ags=["09"])
    right = gpd.GeoDataFrame(
        {"ags": ["9"], "bip_2025": ["716000"]}, geometry=[Point(0, 0)], crs="EPSG:25832"
    )
    result = run_attribute_join({"left": left, "right": right}, {"left_on": "ags"})
    assert result["bip_2025"].tolist() == ["716000"]


def test_fa34_normalize_false_requires_exact_match():
    """Abgrenzung: ohne Normalisierung zählt die exakte Schreibweise."""
    left = states_gdf(["Baden-Württemberg"])
    right = gdp_table(["Baden-Wuerttemberg"], ["580000"])
    with pytest.warns(UserWarning, match="keinen Treffer"):
        result = run_attribute_join(
            {"left": left, "right": right},
            {"left_on": "name", "right_on": "land", "normalize": False},
        )
    assert result["bip_2025"].isna().all()


def test_fa34_normalize_key_is_idempotent():
    assert normalize_key(normalize_key("Baden-Württemberg")) == normalize_key(
        "Baden-Württemberg"
    )


# =====================================================================
# Fehler- und Randfälle (keine stillen Fehler)
# =====================================================================


def test_fa34_missing_left_key_column_is_an_error():
    left = states_gdf(["Bayern"])
    right = gdp_table(["Bayern"], ["716000"])
    with pytest.raises(ValueError, match="gibt_es_nicht"):
        run_attribute_join(
            {"left": left, "right": right},
            {"left_on": "gibt_es_nicht", "right_on": "land"},
        )


def test_fa34_missing_right_key_column_is_an_error():
    left = states_gdf(["Bayern"])
    right = gdp_table(["Bayern"], ["716000"])
    with pytest.raises(ValueError, match="gibt_es_nicht"):
        run_attribute_join(
            {"left": left, "right": right},
            {"left_on": "name", "right_on": "gibt_es_nicht"},
        )


def test_fa34_missing_selected_column_is_an_error():
    left = states_gdf(["Bayern"])
    right = gdp_table(["Bayern"], ["716000"])
    with pytest.raises(ValueError, match="fehlen im rechten Layer"):
        run_attribute_join(
            {"left": left, "right": right},
            {"left_on": "name", "right_on": "land", "columns": ["bip_2024"]},
        )


def test_fa34_unmatched_left_objects_are_kept_and_reported():
    """Goldene Regel 7: eine Lücke bleibt sichtbar - das Objekt fliegt
    nicht heraus, sein Zusatzattribut bleibt leer, und es wird gemeldet."""
    left = states_gdf(["Bayern", "Unbekanntland"])
    right = gdp_table(["Bayern"], ["716000"])
    with pytest.warns(UserWarning, match="keinen Treffer"):
        result = run_attribute_join(
            {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
        )
    assert len(result) == 2
    assert result["bip_2025"].isna().sum() == 1


def test_fa34_duplicate_right_keys_raise_by_default():
    """Eine Statistiktabelle soll je Gebiet genau eine Zeile haben."""
    left = states_gdf(["Bayern"])
    right = gdp_table(["Bayern", "Bayern"], ["716000", "999999"])
    with pytest.raises(ValueError, match="mehrere Zeilen je"):
        run_attribute_join(
            {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
        )


def test_fa34_duplicate_right_keys_first_wins_with_warning():
    left = states_gdf(["Bayern"])
    right = gdp_table(["Bayern", "Bayern"], ["716000", "999999"])
    with pytest.warns(UserWarning, match="mehrfach belegte"):
        result = run_attribute_join(
            {"left": left, "right": right},
            {"left_on": "name", "right_on": "land", "on_duplicate": "first"},
        )
    assert result["bip_2025"].tolist() == ["716000"]


def test_fa34_column_name_collision_does_not_overwrite_left():
    """Ein gleichnamiges Attribut links darf nicht unbemerkt
    überschrieben werden."""
    left = states_gdf(["Bayern"]).assign(bip_2025=["links"])
    right = gdp_table(["Bayern"], ["716000"])
    result = run_attribute_join(
        {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
    )
    assert result["bip_2025"].tolist() == ["links"]
    assert result["bip_2025_right"].tolist() == ["716000"]


def test_fa34_empty_left_layer():
    """Randfall: leerer linker Layer - Zielspalten werden angelegt, damit
    Folgeschritte nicht an einer fehlenden Spalte scheitern."""
    left = states_gdf([])
    right = gdp_table(["Bayern"], ["716000"])
    result = run_attribute_join(
        {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
    )
    assert result.empty
    assert "bip_2025" in result.columns


def test_fa34_empty_right_layer_keeps_all_left_objects():
    left = states_gdf(["Bayern", "Sachsen"])
    right = gdp_table([], [])
    with pytest.warns(UserWarning, match="keinen Treffer"):
        result = run_attribute_join(
            {"left": left, "right": right}, {"left_on": "name", "right_on": "land"}
        )
    assert len(result) == 2


# =====================================================================
# Contract/Schema (FA1: Konfiguration validiert)
# =====================================================================


def test_fa34_step_declares_contract():
    step = AttributeJoinStep(
        id="join", inputs={"left": "states", "right": "gdp"}, params={"left_on": "name"}
    )
    assert set(step.INPUT_PORTS) == {"left", "right"}
    assert step.OUTPUT_TYPE.value == "vector"
    assert "left_on" in params_catalog(AttributeJoinStep.Params)[1]


def test_fa34_invalid_on_duplicate_is_rejected():
    with pytest.raises(ValueError, match="on_duplicate"):
        AttributeJoinStep(
            id="join",
            inputs={"left": "a", "right": "b"},
            params={"left_on": "name", "on_duplicate": "letzte"},
        )


def test_fa34_scenario_with_attribute_join_validates():
    """Die Operation ist im DAG-Schema nutzbar (FA1/FA9)."""
    scenario = Scenario(
        **{
            "scenario": {"name": "bip", "region": BBOX_REGION},
            "layers": [
                {"id": "states", "source": "region", "data_type": "vector"},
                {
                    "id": "gdp",
                    "source": "table",
                    "path": "bip.csv",
                    "format": "csv",
                    "geometry": {
                        "x_column": "lon",
                        "y_column": "lat",
                        "crs": "EPSG:4326",
                    },
                    "columns": {},
                },
            ],
            "steps": [
                {
                    "id": "joined",
                    "op": "attribute_join",
                    "inputs": {"left": "states", "right": "gdp"},
                    "params": {"left_on": "name", "right_on": "land"},
                },
            ],
            "output": [{"type": "map", "source": "joined"}],
        }
    )
    assert scenario.execution_order() == ["joined"]
