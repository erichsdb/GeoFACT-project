"""Implements: FA29 (deklarierte Attributtypen tatsächlich casten).

Contract-Tests für zwei Wege, auf denen ein als Zahl gemeintes Attribut
numerisch wird:

1. explizit deklariert - validate.cast_declared_types() führt
   LayerValidation.field_types wirklich aus (bisher nur geprüft),
2. implizit aus der Bedingung - builtin/operations/filter.run_filter() castet
   Spalten, die gegen ein Zahlenliteral verglichen werden.

Motivierender Fall (Bug): OSM liefert alle Tagwerte als String; das
Szenario "Veranstaltungsorte in Sachsen" mit 'capacity > 250' brach mit
"TypeError: '>' not supported between instances of 'str' and 'int'" ab.
"""

import warnings

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from geofact.core.contracts import LayerValidation
from geofact.engine import validate as validate_module
from geofact.builtin.operations.filter import run_filter


def venues_gdf(capacities) -> gpd.GeoDataFrame:
    """Veranstaltungsorte wie aus OSM: capacity als String (OSM-Tagwerte
    sind ausnahmslos Strings), teils fehlend, teils gemischt getypt."""
    return gpd.GeoDataFrame(
        {"capacity": capacities, "amenity": ["theatre"] * len(capacities)},
        geometry=[Point(i, i) for i in range(len(capacities))],
        crs="EPSG:25833",
    )


# =====================================================================
# Happy Path: der Bug aus dem Report
# =====================================================================


def test_fa29_filter_compares_string_column_against_number():
    """Kernfall: reine String-Spalte, numerischer Vergleich."""
    gdf = venues_gdf(["300", "100", "500"])
    result = run_filter({"features": gdf}, {"condition": "capacity > 250"})
    assert len(result) == 2
    assert sorted(float(v) for v in result["capacity"]) == [300.0, 500.0]


def test_fa29_filter_compares_mixed_int_and_string_column():
    """Der im Report genannte Fall: die Spalte enthält int UND str -
    ohne Cast scheitert der Vergleich am ersten String."""
    gdf = venues_gdf(["300", 100, "500", 700])
    result = run_filter({"features": gdf}, {"condition": "capacity > 250"})
    assert sorted(float(v) for v in result["capacity"]) == [300.0, 500.0, 700.0]


def test_fa29_filter_does_not_raise_type_error_on_string_column():
    """Nachbedingung: der konkrete TypeError tritt nicht mehr auf."""
    gdf = venues_gdf(["300", "100"])
    run_filter({"features": gdf}, {"condition": "capacity > 250"})  # kein Raise


def test_fa29_filter_reverse_operand_order():
    """Auch 'Literal OP Spalte' wird erkannt."""
    gdf = venues_gdf(["300", "100", "500"])
    result = run_filter({"features": gdf}, {"condition": "250 < capacity"})
    assert len(result) == 2


def test_fa29_filter_float_literal_and_compound_condition():
    gdf = gpd.GeoDataFrame(
        {"score": ["0.9", "0.2", "0.7"], "place": ["village", "village", "town"]},
        geometry=[Point(i, i) for i in range(3)],
        crs="EPSG:25833",
    )
    result = run_filter(
        {"features": gdf}, {"condition": "score >= 0.5 and place == 'village'"}
    )
    assert len(result) == 1
    assert float(result["score"].iloc[0]) == 0.9


# =====================================================================
# Typfehler-Fall: nicht konvertierbare Werte, nicht stillschweigend
# =====================================================================


def test_fa29_filter_warns_about_non_numeric_values():
    """Goldene Regel 7: nicht konvertierbare Werte verschwinden nicht
    stillschweigend, sondern werden mit Anzahl und Beispiel gemeldet."""
    gdf = venues_gdf(["300", "unbekannt", "500"])
    with pytest.warns(UserWarning, match="nicht-numerische"):
        result = run_filter({"features": gdf}, {"condition": "capacity > 250"})
    assert len(result) == 2


def test_fa29_filter_non_numeric_value_counts_as_missing():
    """Ein nicht konvertierbarer Wert verhält sich wie ein fehlendes
    Attribut - on_missing entscheidet (FA8), nicht ein Abbruch."""
    gdf = venues_gdf(["300", "unbekannt"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        included = run_filter(
            {"features": gdf}, {"condition": "capacity > 250", "on_missing": "include"}
        )
        excluded = run_filter(
            {"features": gdf}, {"condition": "capacity > 250", "on_missing": "exclude"}
        )
    assert len(included) == 2
    assert len(excluded) == 1


def test_fa29_filter_on_missing_error_still_aborts():
    """on_missing=error bleibt wirksam (FA8-Vertrag unverändert)."""
    gdf = venues_gdf(["300", None])
    with pytest.raises(ValueError, match="on_missing=error"):
        run_filter(
            {"features": gdf}, {"condition": "capacity > 250", "on_missing": "error"}
        )


# =====================================================================
# Abgrenzung: kein Cast, wo keiner gemeint ist
# =====================================================================


def test_fa29_filter_string_literal_comparison_stays_string():
    """Vergleich gegen ein STRINGliteral löst keinen numerischen Cast
    aus - wer Strings vergleichen will, bekommt Strings."""
    gdf = venues_gdf(["300", "100"])
    result = run_filter({"features": gdf}, {"condition": "capacity == '300'"})
    assert len(result) == 1
    assert result["capacity"].iloc[0] == "300"


def test_fa29_filter_numeric_column_untouched():
    """Randfall: bereits numerische Spalte wird nicht angefasst (Dtype
    bleibt erhalten, kein unnötiges Kopieren der Semantik)."""
    gdf = gpd.GeoDataFrame(
        {"capacity": [300, 100, 500]},
        geometry=[Point(i, i) for i in range(3)],
        crs="EPSG:25833",
    )
    result = run_filter({"features": gdf}, {"condition": "capacity > 250"})
    assert pd.api.types.is_integer_dtype(result["capacity"])
    assert len(result) == 2


def test_fa29_filter_method_call_condition_not_treated_as_numeric():
    """Randfall: '.notna()'-Bedingungen enthalten kein Zahlenliteral und
    dürfen keinen Cast der Spalte auslösen (relevant für Wildcard-
    Szenarien wie 'highway.notna()', FA30)."""
    gdf = gpd.GeoDataFrame(
        {"highway": ["residential", None, "primary"]},
        geometry=[Point(i, i) for i in range(3)],
        crs="EPSG:25833",
    )
    # on_missing=exclude (Default): Objekte ohne das Attribut fallen
    # heraus, die Spalte bleibt aber String (kein NaN-Cast).
    result = run_filter({"features": gdf}, {"condition": "highway.notna()"})
    assert result["highway"].tolist() == ["residential", "primary"]


def test_fa29_filter_empty_layer():
    """Randfall: leerer Layer bleibt leer, kein Absturz im Cast-Pfad."""
    gdf = venues_gdf([])
    result = run_filter({"features": gdf}, {"condition": "capacity > 250"})
    assert result.empty


# =====================================================================
# Explizit deklarierte Typen (LayerValidation.field_types) werden
# tatsächlich ausgeführt, nicht nur geprüft
# =====================================================================


def test_fa29_declared_integer_field_is_cast():
    gdf = venues_gdf(["300", "100"])
    rules = LayerValidation(field_types={"capacity": "integer"})
    result = validate_module.validate("venues", gdf, rules)
    assert pd.api.types.is_integer_dtype(result.gdf["capacity"])
    assert result.gdf["capacity"].tolist() == [300, 100]


def test_fa29_declared_integer_keeps_missing_values_as_na():
    """Nullable Int64 statt numpy int64: OSM-Attribute fehlen regelmäßig,
    und ein fehlender Wert darf die Spalte nicht zu float machen."""
    gdf = venues_gdf(["300", None, "500"])
    rules = LayerValidation(field_types={"capacity": "integer"})
    result = validate_module.validate("venues", gdf, rules)
    assert str(result.gdf["capacity"].dtype) == "Int64"
    assert result.gdf["capacity"].isna().sum() == 1


def test_fa29_declared_float_field_is_cast():
    gdf = gpd.GeoDataFrame(
        {"score": ["0.9", "0.25"]},
        geometry=[Point(i, i) for i in range(2)],
        crs="EPSG:25833",
    )
    rules = LayerValidation(field_types={"score": "float"})
    result = validate_module.validate("scores", gdf, rules)
    assert pd.api.types.is_float_dtype(result.gdf["score"])
    assert result.gdf["score"].tolist() == [0.9, 0.25]


def test_fa29_declared_string_field_is_not_cast():
    """Abgrenzung: 'string' bleibt unangetastet."""
    gdf = venues_gdf(["300", "100"])
    rules = LayerValidation(field_types={"capacity": "string"})
    result = validate_module.validate("venues", gdf, rules)
    assert result.gdf["capacity"].tolist() == ["300", "100"]


def test_fa29_declared_integer_does_not_silently_round_fractions():
    """Goldene Regel 7 / FA6-Vertrag: ein echter Bruchwert in einer als
    integer deklarierten Spalte wird als Verletzung gemeldet und gilt als
    fehlend - er wird NICHT stillschweigend gerundet."""
    gdf = gpd.GeoDataFrame(
        {"voltage": [110000.5, 110000.0]},
        geometry=[Point(i, i) for i in range(2)],
        crs="EPSG:25833",
    )
    rules = LayerValidation(field_types={"voltage": "integer"}, on_violation="warn")
    with pytest.warns(UserWarning, match="nicht umwandeln"):
        result = validate_module.validate("substations", gdf, rules)
    assert any(v.rule == "field_types" for v in result.violations)
    assert result.gdf["voltage"].isna().sum() == 1
    assert result.gdf["voltage"].dropna().tolist() == [110000]


def test_fa29_cast_declared_types_without_rules_is_noop():
    """Randfall: ohne field_types bleibt der Layer identisch."""
    gdf = venues_gdf(["300", "100"])
    result = validate_module.cast_declared_types("venues", gdf, {})
    assert result is gdf


# =====================================================================
# Nachtreview 02.10.2026: der Cast gilt nur für den Vergleich
# =====================================================================


def test_fa29_filter_returns_the_original_values_not_the_cast_column():
    """Nachtreview 15: filter returned the coerced column, so 'ca. 300' became NaN
    (include) and '0100' came back as 100.0 (exclude)."""
    gdf = venues_gdf(["250", "ca. 300", "0100", None])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        included = run_filter(
            {"features": gdf}, {"condition": "capacity > 50", "on_missing": "include"}
        )
        excluded = run_filter({"features": gdf}, {"condition": "capacity > 50"})
    assert included["capacity"].tolist()[:3] == ["250", "ca. 300", "0100"]
    assert pd.isna(included["capacity"].iloc[3])
    assert excluded["capacity"].tolist() == ["250", "0100"]


def test_fa29_filter_declared_types_stay_in_the_output_column():
    """params.types is a declaration of the column type (FA63), so the declared
    values are the result; only the implicit cast of FA29 is undone."""
    gdf = venues_gdf(["250", "0100", "40"])
    result = run_filter(
        {"features": gdf},
        {"condition": "capacity > 50", "types": {"capacity": "integer"}},
    )
    assert result["capacity"].tolist() == [250, 100]


def test_fa29_filter_keeps_input_order_and_duplicate_index():
    gdf = venues_gdf(["300", None, "100", "500"])
    gdf.index = [3, 3, 1, 0]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = run_filter(
            {"features": gdf}, {"condition": "capacity > 200", "on_missing": "include"}
        )
    assert result["capacity"].iloc[[0, 2]].tolist() == ["300", "500"]
    assert pd.isna(result["capacity"].iloc[1])
    assert list(result.index) == [3, 3, 0]
