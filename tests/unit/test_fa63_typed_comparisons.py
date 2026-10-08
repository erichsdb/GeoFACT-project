"""Implements: FA63 (typisierte Vergleiche, kein lexikografischer Zahlenvergleich).

Contract tests for the shared helper ``geofact.support.numeric`` and the
attribute operations that use it (filter, classify, ranking, aggregate,
top_n) plus ``engine/validate.cast_declared_types``:

- text is never compared with an ordering operator unless that is declared
  (``filter.params.types``); a number-like text literal at ``>=`` is a
  validation error with position;
- values that cannot be read as numbers are counted and reported, they
  become missing (NaN) instead of crashing or being compared as text;
- existing scenarios with number literals behave as before (FA29);
- an empty layer passes every touched operation.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point, box

from geofact import api
from geofact.builtin.operations.aggregate import run_aggregate
from geofact.builtin.operations.classify import ClassifyStep, run_classify
from geofact.builtin.operations.filter import FilterStep, run_filter
from geofact.builtin.operations.ranking import run_ranking
from geofact.builtin.operations.top_n import run_top_n
from geofact.core.contracts import LayerValidation
from geofact.engine import validate as validate_module
from geofact.support.numeric import (
    Coercion,
    NumericCoercionWarning,
    coerce_numeric,
    require_numeric,
)

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
CRS = "EPSG:25833"


def cities(**columns) -> gpd.GeoDataFrame:
    length = len(next(iter(columns.values())))
    return gpd.GeoDataFrame(
        columns, geometry=[Point(i * 10, i * 10) for i in range(length)], crs=CRS
    )


def empty_layer(*columns: str) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {c: pd.Series(dtype=object) for c in columns},
        geometry=gpd.GeoSeries([], crs=CRS),
        crs=CRS,
    )


def filter_step(**params) -> FilterStep:
    return FilterStep(id="f", op="filter", inputs={"features": "x"}, params=params)


# =====================================================================
# Helper: coerce_numeric / require_numeric
# =====================================================================


def test_fa63_coerce_numeric_counts_lost_values_with_examples():
    result = coerce_numeric(pd.Series(["9", "100", "n/a", None, "unbekannt"]))
    assert isinstance(result, Coercion)
    assert result.series.dtype == "float64"
    assert result.series.iloc[0] == 9.0 and result.series.iloc[1] == 100.0
    # None was missing before the cast - it is not a loss
    assert result.lost == 2
    assert result.examples == ("'n/a'", "'unbekannt'")


def test_fa63_coerce_numeric_integer_keeps_na_and_rejects_fractions():
    result = coerce_numeric(
        pd.Series(["110000", "110000.5", None, True]), kind="integer"
    )
    assert str(result.series.dtype) == "Int64"
    assert result.series.iloc[0] == 110000
    assert result.series.iloc[1:].isna().all()
    # the fraction is not rounded silently, the boolean is not a number
    assert result.lost == 2


def test_fa63_require_numeric_warns_once_per_column():
    with pytest.warns(NumericCoercionWarning) as record:
        values = require_numeric(
            pd.Series(["1", "x", "y"]), where="classify", column="einwohner"
        )
    assert len(record) == 1
    message = str(record[0].message)
    assert "classify" in message and "'einwohner'" in message
    assert "2 Wert(e) nicht numerisch" in message and "gelten als fehlend" in message
    assert values.iloc[0] == 1.0 and values.iloc[1:].isna().all()


def test_fa63_require_numeric_leaves_numeric_columns_untouched():
    series = pd.Series([3, 1, 2])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert require_numeric(series, where="ranking", column="x") is series


# =====================================================================
# filter: validation guard (position steps -> i -> params -> condition)
# =====================================================================


def test_fa63_filter_number_like_text_literal_at_ordering_operator_is_rejected():
    with pytest.raises(ValidationError) as exc:
        filter_step(condition="population >= '100000'")
    errors = exc.value.errors()
    assert errors[0]["loc"] == ("params", "condition")
    message = errors[0]["msg"]
    assert "'100000'" in message and "'>='" in message
    assert "types: {population: string}" in message


def test_fa63_filter_text_literal_error_names_the_scenario_position():
    config = {
        "scenario": {"name": "Typen", "region": "13.60,51.01,13.94,51.15"},
        "layers": [
            {
                "id": "orte",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "substations.geojson"),
            }
        ],
        "steps": [
            {
                "id": "groß",
                "op": "filter",
                "inputs": {"features": "orte"},
                "params": {"condition": "population >= '100000'"},
            }
        ],
        "output": [{"type": "geojson", "source": "groß"}],
    }
    report = api.validate(config)
    assert not report.valid
    locations = [location for location, _ in report.issues]
    assert locations == ["steps -> 0 -> params -> condition"]


def test_fa63_filter_declared_string_allows_text_ordering():
    gdf = cities(name=["Aue", "Leipzig", "Zwickau"])
    filter_step(condition="name >= 'L'", types={"name": "string"})
    result = run_filter(
        {"features": gdf}, {"condition": "name >= 'L'", "types": {"name": "string"}}
    )
    assert list(result["name"]) == ["Leipzig", "Zwickau"]


def test_fa63_filter_declared_string_against_number_literal_is_rejected():
    with pytest.raises(ValidationError) as exc:
        filter_step(condition="plz == 4109", types={"plz": "string"})
    assert exc.value.errors()[0]["loc"] == ("params", "condition")
    assert "types: {plz: integer}" in exc.value.errors()[0]["msg"]


def test_fa63_filter_equality_and_membership_on_text_stay_allowed():
    filter_step(condition="name == '100'")
    filter_step(condition="name != 'Leipzig' and kind in ['a', 'b']")
    gdf = cities(name=["100", "Leipzig"], kind=["a", "c"])
    assert list(
        run_filter({"features": gdf}, {"condition": "name == '100'"})["name"]
    ) == ["100"]


# =====================================================================
# filter: runtime
# =====================================================================


def test_fa63_filter_text_column_ordered_without_declaration_is_a_runtime_error():
    gdf = cities(name=["Aue", "Leipzig"])
    with pytest.raises(
        ValueError,
        match=r"Spalte 'name' hat Typ Text \((object|str)\) und wird mit '>=' verglichen",
    ):
        run_filter({"features": gdf}, {"condition": "name >= 'L'"})


def test_fa63_filter_declared_integers_compare_numerically_not_lexicographically():
    # lexicographic: "9" > "10" would be True
    gdf = cities(a=["9", "20"], b=["10", "10"])
    result = run_filter(
        {"features": gdf},
        {"condition": "a > b", "types": {"a": "integer", "b": "integer"}},
    )
    assert list(result["a"]) == [20]


def test_fa63_filter_declared_float_reports_unreadable_values():
    gdf = cities(einwohner=["120000", "ca. 5000", "90000"])
    with pytest.warns(NumericCoercionWarning, match="1 Wert\\(e\\) nicht numerisch"):
        result = run_filter(
            {"features": gdf},
            {
                "condition": "einwohner > 100000",
                "types": {"einwohner": "float"},
                "on_missing": "include",
            },
        )
    assert len(result) == 2


def test_fa63_filter_declared_boolean_reads_text_flags():
    gdf = cities(barrierefrei=["yes", "no", "true", "kaputt"])
    with pytest.warns(NumericCoercionWarning, match="kaputt"):
        result = run_filter(
            {"features": gdf},
            {"condition": "barrierefrei == True", "types": {"barrierefrei": "boolean"}},
        )
    assert len(result) == 2


def test_fa63_filter_number_literal_on_text_column_behaves_as_before():
    """Nachbedingung (1): FA29 path unchanged, its warning text stays."""
    gdf = cities(capacity=["300", "unbekannt", "500"])
    with pytest.warns(UserWarning, match="nicht-numerische"):
        result = run_filter({"features": gdf}, {"condition": "capacity > 250"})
    assert len(result) == 2


def test_fa63_filter_unknown_column_in_types_is_an_error():
    gdf = cities(name=["Aue"])
    with pytest.raises(ValueError, match="types.*'einwohner'.*vorhanden"):
        run_filter(
            {"features": gdf},
            {"condition": "name == 'Aue'", "types": {"einwohner": "integer"}},
        )


# =====================================================================
# classify / ranking / aggregate / top_n / cast_declared_types
# =====================================================================


def test_fa63_classify_unreadable_values_become_nan_not_a_crash():
    gdf = cities(score=["0.2", "kein Wert", "0.9"])
    with pytest.warns(NumericCoercionWarning):
        result = run_classify(
            {"features": gdf},
            {"field": "score", "breaks": [0.5], "labels": ["low", "high"]},
        )
    assert result["class"].iloc[0] == "low" and result["class"].iloc[2] == "high"
    assert pd.isna(result["class"].iloc[1])


def test_fa63_classify_breaks_not_ascending_is_a_validation_error():
    with pytest.raises(ValidationError, match="aufsteigend"):
        ClassifyStep(
            id="c",
            op="classify",
            inputs={"features": "x"},
            params={"field": "s", "breaks": [5, 1], "labels": ["a", "b", "c"]},
        )


def test_fa63_ranking_text_column_gives_nan_score_not_a_crash():
    gdf = cities(a=["1", "x", "3"])
    with pytest.warns(NumericCoercionWarning):
        result = run_ranking({"features": gdf}, {"by": ["a"], "weights": [1.0]})
    assert result["score"].iloc[0] == 0.0 and result["score"].iloc[2] == 1.0
    assert np.isnan(result["score"].iloc[1])


def test_fa63_aggregate_sum_adds_numeric_strings_instead_of_concatenating():
    zones = gpd.GeoDataFrame({"name": ["z"]}, geometry=[box(-1, -1, 100, 100)], crs=CRS)
    features = cities(einwohner=["100", "250"])
    result = run_aggregate(
        {"features": features, "zones": zones},
        {"statistic": "sum", "value_field": "einwohner"},
    )
    assert result["sum_einwohner"].iloc[0] == 350.0


def test_fa63_aggregate_all_values_unreadable_is_an_error():
    zones = gpd.GeoDataFrame({"name": ["z"]}, geometry=[box(-1, -1, 100, 100)], crs=CRS)
    features = cities(einwohner=["viele", "wenige"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(ValueError, match="keinen numerischen Wert"):
            run_aggregate(
                {"features": features, "zones": zones},
                {"statistic": "mean", "value_field": "einwohner"},
            )


def test_fa63_top_n_sorts_text_numbers_numerically_via_the_helper():
    gdf = cities(einwohner=["9", "100", "kaum"])
    with pytest.warns(NumericCoercionWarning):
        result = run_top_n({"features": gdf}, {"by": "einwohner", "n": 1})
    assert list(result["einwohner"]) == ["100"]


def test_fa63_cast_declared_types_keeps_its_message():
    gdf = cities(voltage=["110000", "unbekannt"])
    with pytest.warns(UserWarning, match="ließen sich nicht umwandeln"):
        result = validate_module.validate(
            "leitungen", gdf, LayerValidation(field_types={"voltage": "integer"})
        )
    assert str(result.gdf["voltage"].dtype) == "Int64"
    assert result.gdf["voltage"].iloc[0] == 110000


def test_fa63_empty_layer_passes_every_touched_operation():
    empty = empty_layer("v")
    zones = gpd.GeoDataFrame({"name": ["z"]}, geometry=[box(0, 0, 1, 1)], crs=CRS)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert run_filter(
            {"features": empty}, {"condition": "v > 1", "types": {"v": "integer"}}
        ).empty
        assert run_filter({"features": empty}, {"condition": "v >= 'a'"}).empty
        assert "class" in run_classify(
            {"features": empty}, {"field": "v", "breaks": [1], "labels": ["a", "b"]}
        )
        assert "score" in run_ranking(
            {"features": empty}, {"by": ["v"], "weights": [1.0]}
        )
        assert run_top_n({"features": empty}, {"by": "v", "n": 3}).empty
        result = run_aggregate(
            {"features": empty, "zones": zones},
            {"statistic": "sum", "value_field": "v"},
        )
        assert result["sum_v"].iloc[0] == 0
        assert validate_module.cast_declared_types("x", empty, {"v": "integer"}).empty


# =====================================================================
# Nachtreview 02.10.2026: große Ganzzahlen
# =====================================================================


def test_fa63_coerce_integer_counts_values_outside_int64_as_lost():
    """Nachtreview 21a: '1e30' declared as integer raised a raw TypeError
    ('cannot safely cast non-equivalent float64 to int64')."""
    from geofact.support.numeric import coerce_numeric

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        result = coerce_numeric(pd.Series(["1e30", "5", "-1e30"]), kind="integer")
    assert result.series.isna().tolist() == [True, False, True]
    assert result.series.iloc[1] == 5
    assert result.lost == 2


def test_fa63_top_n_sorts_large_integers_exactly():
    """Nachtreview 21b: top_n cast int64 ids above 2**53 to float64, where they
    collapse to one value, and returned the wrong maximum."""
    from geofact.builtin.operations.top_n import run_top_n

    big = 2**60
    gdf = gpd.GeoDataFrame(
        {"id": [big + 1, big, big + 3]},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:25833",
    )
    top = run_top_n({"features": gdf}, {"by": "id", "n": 1, "order": "desc"})
    assert top["id"].tolist() == [big + 3]
    bottom = run_top_n({"features": gdf}, {"by": "id", "n": 1, "order": "asc"})
    assert bottom["id"].tolist() == [big]


def test_fa63_top_n_sorts_nullable_large_integers_exactly():
    from geofact.builtin.operations.top_n import run_top_n

    big = 2**60
    gdf = gpd.GeoDataFrame(
        {"id": pd.array([big + 1, None, big + 3], dtype="Int64")},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:25833",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        top = run_top_n({"features": gdf}, {"by": "id", "n": 2, "order": "desc"})
    assert top["id"].tolist() == [big + 3, big + 1]
