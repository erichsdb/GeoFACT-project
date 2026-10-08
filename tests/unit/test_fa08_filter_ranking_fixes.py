"""Implements: FA8 (filter: Spaltenerkennung per Syntaxbaum, ranking: Richtung je Kriterium).

Contract-Tests für src/geofact/builtin/operations/filter.py und ranking.py:

- filter erkennt die von der Bedingung verwendeten Spalten im Syntaxbaum
  (``ast``), nicht per Teilstring - eine Spalte 'id' gilt nicht als
  referenziert, weil "width" die Buchstaben i-d enthält.
- ranking kennt eine Richtung je Kriterium (``ascending``); ohne Angabe
  bleiben alle Ergebnisse wie bisher.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point

from geofact.builtin.operations.filter import (
    _numerically_compared_columns,
    _referenced_columns,
    run_filter,
)
from geofact.builtin.operations.ranking import RankingStep, run_ranking
from geofact.engine.catalog import params_catalog


def _points(**columns) -> gpd.GeoDataFrame:
    count = len(next(iter(columns.values())))
    return gpd.GeoDataFrame(
        columns, geometry=[Point(i, i) for i in range(count)], crs="EPSG:4326"
    )


# =====================================================================
# filter: referenzierte Spalten aus dem Syntaxbaum
# =====================================================================


def test_fa08_filter_referenced_columns_come_from_the_syntax_tree():
    columns = ["id", "width", "name", "geometry"]
    assert _referenced_columns("width > 3", columns) == ["width"]
    assert _referenced_columns("width > 3 and name == 'x'", columns) == [
        "name",
        "width",
    ][::-1] or _referenced_columns("width > 3 and name == 'x'", columns) == [
        "width",
        "name",
    ]
    assert _referenced_columns("1 == 1", columns) == []


def test_fa08_filter_column_whose_letters_occur_in_another_name_is_not_referenced():
    """Bugfix: 'id' ist ein Teilstring von 'width'. Früher galt 'id' als
    referenziert, und ein Objekt mit leerem 'id' fiel fälschlich aus dem
    Filter, obwohl die Bedingung 'id' nie liest."""
    gdf = _points(id=["a", None, "c"], width=[5, 6, 7])
    result = run_filter({"features": gdf}, {"condition": "width > 3"})
    assert len(result) == 3  # keiner ausgeschlossen: width ist vollständig


def test_fa08_filter_column_name_inside_a_string_literal_is_not_a_reference():
    gdf = _points(id=["a", None, "c"], kind=["id", "x", "id"])
    result = run_filter({"features": gdf}, {"condition": "kind == 'id'"})
    assert list(result["kind"]) == ["id", "id"]
    assert (
        len(
            run_filter(
                {"features": gdf}, {"condition": "kind == 'id'", "on_missing": "error"}
            )
        )
        == 2
    )


def test_fa08_filter_missing_value_in_a_referenced_column_still_follows_on_missing():
    gdf = _points(width=[5, None, 7])
    assert len(run_filter({"features": gdf}, {"condition": "width > 3"})) == 2
    assert (
        len(
            run_filter(
                {"features": gdf}, {"condition": "width > 3", "on_missing": "include"}
            )
        )
        == 3
    )
    with pytest.raises(ValueError, match="on_missing=error"):
        run_filter({"features": gdf}, {"condition": "width > 3", "on_missing": "error"})


def test_fa08_filter_method_call_on_a_column_counts_as_a_reference():
    gdf = _points(name=["abc", None, "xyz"])
    result = run_filter({"features": gdf}, {"condition": "name.str.contains('a')"})
    assert list(result["name"]) == ["abc"]


def test_fa08_filter_backtick_quoted_column_is_detected_and_cast():
    gdf = _points(**{"max voltage": ["10", None, "300"], "id": [1, 2, 3]})
    result = run_filter({"features": gdf}, {"condition": "`max voltage` > 50"})
    assert list(result["id"]) == [3]
    assert _referenced_columns("`max voltage` > 50", list(gdf.columns)) == [
        "max voltage"
    ]


def test_fa08_filter_invalid_condition_is_an_error_naming_the_condition():
    gdf = _points(width=[1, 2])
    with pytest.raises(
        ValueError, match="Bedingung 'width = 1 AND x' ist kein gültiger Ausdruck"
    ):
        run_filter({"features": gdf}, {"condition": "width = 1 AND x"})


def test_fa08_filter_invalid_condition_on_an_empty_layer_is_still_an_error():
    """Randfall: ein leerer Layer darf eine kaputte Bedingung nicht verbergen."""
    empty = gpd.GeoDataFrame({"width": []}, geometry=[], crs="EPSG:4326")
    with pytest.raises(ValueError, match="kein gültiger Ausdruck"):
        run_filter({"features": empty}, {"condition": "width ="})


def test_fa08_filter_numeric_comparison_detection_uses_the_syntax_tree():
    columns = ["capacity", "v1", "label"]
    assert _numerically_compared_columns("capacity > 250", columns) == ["capacity"]
    assert _numerically_compared_columns("250 < capacity", columns) == ["capacity"]
    assert _numerically_compared_columns("capacity > -5.5", columns) == ["capacity"]
    assert _numerically_compared_columns("10 < capacity < 300", columns) == ["capacity"]
    # ein Vergleich INNERHALB eines Stringliterals ist kein Vergleich der Spalte v1
    assert _numerically_compared_columns("label == 'v1 > 2'", columns) == []
    # Methodenaufruf und Stringvergleich sind keine numerischen Vergleiche
    assert _numerically_compared_columns("label.str.contains('3')", columns) == []
    assert _numerically_compared_columns("label == '300'", columns) == []


# =====================================================================
# ranking: Richtung je Kriterium
# =====================================================================


def _two_criteria() -> gpd.GeoDataFrame:
    return _points(a=[0, 5, 10], b=[10, 5, 0])


def test_fa08_ranking_without_ascending_is_unchanged_bigger_is_better():
    result = run_ranking(
        {"features": _two_criteria()}, {"by": ["a", "b"], "weights": [0.5, 0.5]}
    )
    assert list(result["score"]) == pytest.approx([0.5, 0.5, 0.5])


def test_fa08_ranking_explicit_all_false_equals_the_default():
    default = run_ranking(
        {"features": _two_criteria()}, {"by": ["a", "b"], "weights": [0.7, 0.3]}
    )
    explicit = run_ranking(
        {"features": _two_criteria()},
        {"by": ["a", "b"], "weights": [0.7, 0.3], "ascending": [False, False]},
    )
    pd.testing.assert_series_equal(default["score"], explicit["score"])


def test_fa08_ranking_ascending_inverts_only_the_marked_criterion():
    """b: kleiner ist besser. a steigt 0 -> 1, b (invertiert) steigt 0 -> 1:
    der Score steigt mit der Zeile, statt konstant 0.5 zu bleiben."""
    result = run_ranking(
        {"features": _two_criteria()},
        {"by": ["a", "b"], "weights": [0.5, 0.5], "ascending": [False, True]},
    )
    assert list(result["score"]) == pytest.approx([0.0, 0.5, 1.0])


def test_fa08_ranking_ascending_single_criterion_reverses_the_order():
    gdf = _points(entfernung=[100.0, 300.0, 200.0])
    result = run_ranking(
        {"features": gdf}, {"by": ["entfernung"], "weights": [1.0], "ascending": [True]}
    )
    # nächstes Objekt (100) bekommt den besten Score 1.0, das fernste 0.0
    assert list(result["score"]) == pytest.approx([1.0, 0.0, 0.5])


def test_fa08_ranking_constant_ascending_criterion_contributes_zero():
    gdf = _points(a=[5, 5])
    result = run_ranking(
        {"features": gdf}, {"by": ["a"], "weights": [1.0], "ascending": [True]}
    )
    assert (result["score"] == 0).all()


def test_fa08_ranking_ascending_on_an_empty_layer():
    gdf = gpd.GeoDataFrame({"a": []}, geometry=[], crs="EPSG:4326")
    result = run_ranking(
        {"features": gdf}, {"by": ["a"], "weights": [1.0], "ascending": [True]}
    )
    assert len(result) == 0


def test_fa08_ranking_ascending_length_must_match_by():
    with pytest.raises(ValidationError, match="ascending.*by"):
        RankingStep(
            id="r",
            op="ranking",
            inputs={"features": "x"},
            params={"by": ["a", "b"], "weights": [0.5, 0.5], "ascending": [True]},
        )
    with pytest.raises(ValueError, match="ascending"):
        run_ranking(
            {"features": _two_criteria()},
            {"by": ["a", "b"], "weights": [0.5, 0.5], "ascending": [True]},
        )


def test_fa08_ranking_ascending_must_be_a_list_of_booleans():
    with pytest.raises(ValidationError) as caught:
        RankingStep(
            id="r",
            op="ranking",
            inputs={"features": "x"},
            params={"by": ["a"], "weights": [1.0], "ascending": ["ja"]},
        )
    assert [(e["type"], e["loc"]) for e in caught.value.errors()] == [
        ("bool_type", ("params", "ascending", 0))
    ]
    with pytest.raises(ValidationError) as caught:
        RankingStep(
            id="r",
            op="ranking",
            inputs={"features": "x"},
            params={"by": ["a"], "weights": [1.0], "ascending": True},
        )
    assert [(e["type"], e["loc"]) for e in caught.value.errors()] == [
        ("list_type", ("params", "ascending"))
    ]


def test_fa08_ranking_declares_ascending_in_its_contract():
    specs, required = params_catalog(RankingStep.Params)
    spec = specs["ascending"]
    assert spec["type"] == "array" and spec["item_type"] == "boolean"
    assert spec["required"] is False
    assert "ascending" not in required
