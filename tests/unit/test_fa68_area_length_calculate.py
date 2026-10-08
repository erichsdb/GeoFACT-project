"""Implements: FA68 (Geometriemasse und Feldrechnung: area, length, calculate_field).

Contract-Tests für src/geofact/builtin/operations/area.py, length.py,
calculate_field.py und den Helfer _geometry.require_projected_crs.
"""

from __future__ import annotations

import math
import warnings

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from geofact.builtin.operations._geometry import require_projected_crs
from geofact.builtin.operations.area import AreaStep, run_area
from geofact.builtin.operations.calculate_field import (
    CalculateFieldStep,
    run_calculate_field,
)
from geofact.builtin.operations.length import LengthStep, run_length
from geofact.core.scenario import Scenario
from geofact.core.types import DataType
from geofact.engine.catalog import params_catalog

UTM = "EPSG:25833"
BBOX_REGION = "12.8,50.7,13.1,50.9"


def gdf(geoms, crs=UTM, **columns) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns, geometry=list(geoms), crs=crs)


def scenario_with(step: dict, *, extra_layers: list | None = None) -> Scenario:
    return Scenario(
        **{
            "scenario": {"name": "t", "region": BBOX_REGION},
            "layers": [{"id": "flaechen", "source": "region", "data_type": "vector"}]
            + (extra_layers or []),
            "steps": [step],
            "output": [{"type": "geojson", "source": step["id"]}],
        }
    )


# =====================================================================
# Helfer: projiziertes CRS (Vorbedingung)
# =====================================================================


def test_fa68_require_projected_crs_rejects_geographic_with_fa5_hint():
    data = gdf([Point(13, 51)], crs="EPSG:4326")
    with pytest.raises(ValueError) as caught:
        require_projected_crs(data, op="area")
    message = str(caught.value)
    assert (
        "area: Eingang 'features' hat kein projiziertes CRS (EPSG:4326, Grad)"
        in message
    )
    assert "FA5" in message


def test_fa68_require_projected_crs_rejects_missing_crs():
    data = gpd.GeoDataFrame(geometry=[Point(0, 0)])
    with pytest.raises(ValueError, match="kein CRS"):
        require_projected_crs(data, op="length", port="lines")


def test_fa68_require_projected_crs_accepts_utm():
    require_projected_crs(gdf([Point(0, 0)]), op="area")


# =====================================================================
# area
# =====================================================================


def test_fa68_area_writes_square_metres_by_default():
    data = gdf([box(0, 0, 100, 50), box(0, 0, 10, 10)])
    result = run_area({"features": data}, AreaStep.Params().model_dump())
    assert result["area_m2"].tolist() == [5000.0, 100.0]


@pytest.mark.parametrize("unit, expected", [("ha", 0.5), ("km2", 0.005)])
def test_fa68_area_units(unit, expected):
    data = gdf([box(0, 0, 100, 50)])
    result = run_area(
        {"features": data}, {"unit": unit, "output_field": None, "decimals": None}
    )
    assert result[f"area_{unit}"].iloc[0] == pytest.approx(expected)


def test_fa68_area_output_field_and_decimals():
    data = gdf([Polygon([(0, 0), (3, 0), (0, 7)])])  # 10.5 m2
    result = run_area(
        {"features": data}, {"unit": "m2", "output_field": "flaeche", "decimals": 0}
    )
    assert (
        result["flaeche"].iloc[0] == 10.0
    )  # numpy rundet halbe Werte zur geraden Zahl
    assert "area_m2" not in result.columns


def test_fa68_area_non_polygons_get_nan_with_one_summary_warning():
    data = gdf(
        [box(0, 0, 10, 10), Point(1, 1), LineString([(0, 0), (5, 0)]), Point(2, 2)]
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_area(
            {"features": data}, {"unit": "m2", "output_field": None, "decimals": None}
        )
    relevant = [str(w.message) for w in caught if "area:" in str(w.message)]
    assert len(relevant) == 1
    assert "3 von 4" in relevant[0]
    assert "Point: 2" in relevant[0] and "LineString: 1" in relevant[0]
    assert result["area_m2"].iloc[0] == 100.0
    assert result["area_m2"].iloc[1:].isna().all()


def test_fa68_area_keeps_rows_order_and_input_unchanged():
    data = gdf(
        [box(0, 0, 2, 2), box(0, 0, 1, 1), box(0, 0, 3, 3)], name=["a", "b", "c"]
    )
    before = data.copy()
    result = run_area(
        {"features": data}, {"unit": "m2", "output_field": None, "decimals": None}
    )
    assert result["name"].tolist() == ["a", "b", "c"]
    assert result["area_m2"].tolist() == [4.0, 1.0, 9.0]
    assert list(data.columns) == list(before.columns)
    assert result.crs == data.crs


def test_fa68_area_empty_layer_has_the_column():
    data = gdf([], name=[])
    result = run_area(
        {"features": data}, {"unit": "ha", "output_field": None, "decimals": None}
    )
    assert result.empty
    assert "area_ha" in result.columns


def test_fa68_area_rejects_geographic_crs():
    data = gdf([box(13, 51, 13.1, 51.1)], crs="EPSG:4326")
    with pytest.raises(ValueError, match="projiziertes CRS"):
        run_area(
            {"features": data}, {"unit": "m2", "output_field": None, "decimals": None}
        )


def test_fa68_area_existing_result_column_is_an_error():
    data = gdf([box(0, 0, 1, 1)], area_m2=[3.0])
    with pytest.raises(ValueError, match="existiert bereits"):
        run_area(
            {"features": data}, {"unit": "m2", "output_field": None, "decimals": None}
        )


def test_fa68_area_step_declares_contract_and_produced_field():
    step = AreaStep(id="a", inputs={"features": "x"}, params={"unit": "ha"})
    assert step.INPUT_PORTS == {"features": DataType.VECTOR}
    assert step.OUTPUT_TYPE == DataType.VECTOR
    assert step.produced_fields() == {"area_ha"}
    named = AreaStep(id="a", inputs={"features": "x"}, params={"output_field": "f"})
    assert named.produced_fields() == {"f"}
    assert params_catalog(AreaStep.Params)[1] == []


def test_fa68_area_invalid_unit_is_rejected():
    with pytest.raises(ValueError, match="unit"):
        AreaStep(id="a", inputs={"features": "x"}, params={"unit": "acres"})


def test_fa68_area_type_error_raster_input_rejected_at_validation():
    raster_layer = {
        "id": "dem",
        "source": "file",
        "path": "dem.tif",
        "format": "tif",
        "data_type": "raster",
    }
    with pytest.raises(ValueError, match="erwartet vector, erhält aber raster"):
        scenario_with(
            {"id": "a", "op": "area", "inputs": {"features": "dem"}},
            extra_layers=[raster_layer],
        )


def test_fa68_area_scenario_validates():
    scenario = scenario_with(
        {
            "id": "a",
            "op": "area",
            "inputs": {"features": "flaechen"},
            "params": {"unit": "km2"},
        }
    )
    assert scenario.execution_order() == ["a"]


# =====================================================================
# length
# =====================================================================


def test_fa68_length_writes_metres_by_default():
    data = gdf([LineString([(0, 0), (300, 400)]), LineString([(0, 0), (0, 10)])])
    result = run_length({"features": data}, LengthStep.Params().model_dump())
    assert result["length_m"].tolist() == [500.0, 10.0]


def test_fa68_length_km_and_decimals():
    data = gdf([LineString([(0, 0), (1234.5678, 0)])])
    result = run_length(
        {"features": data}, {"unit": "km", "output_field": None, "decimals": 2}
    )
    assert result["length_km"].iloc[0] == 1.23


def test_fa68_length_polygons_and_points_get_nan_with_warning():
    data = gdf([LineString([(0, 0), (1, 0)]), box(0, 0, 1, 1), Point(0, 0)])
    with pytest.warns(UserWarning, match=r"length: 2 von 3 .*keine Linien"):
        result = run_length(
            {"features": data}, {"unit": "m", "output_field": None, "decimals": None}
        )
    assert result["length_m"].iloc[0] == 1.0
    assert result["length_m"].iloc[1:].isna().all()


def test_fa68_length_missing_geometry_is_reported():
    data = gdf([LineString([(0, 0), (1, 0)]), None])
    with pytest.warns(UserWarning, match="ohne Geometrie: 1"):
        result = run_length(
            {"features": data}, {"unit": "m", "output_field": None, "decimals": None}
        )
    assert len(result) == 2


def test_fa68_length_empty_layer_has_the_column():
    result = run_length(
        {"features": gdf([])}, {"unit": "m", "output_field": None, "decimals": None}
    )
    assert result.empty and "length_m" in result.columns


def test_fa68_length_rejects_geographic_crs():
    data = gdf([LineString([(13, 51), (13.1, 51)])], crs="EPSG:4326")
    with pytest.raises(ValueError, match="FA5"):
        run_length(
            {"features": data}, {"unit": "m", "output_field": None, "decimals": None}
        )


def test_fa68_length_step_declares_contract():
    step = LengthStep(id="l", inputs={"features": "x"}, params={"unit": "km"})
    assert step.INPUT_PORTS == {"features": DataType.VECTOR}
    assert step.produced_fields() == {"length_km"}


# =====================================================================
# calculate_field
# =====================================================================


def calc(data, expression, output_field="ergebnis", **extra):
    params = CalculateFieldStep.Params(
        expression=expression, output_field=output_field, **extra
    ).model_dump()
    return run_calculate_field({"features": data}, params)


def test_fa68_calculate_field_ratio_of_two_columns():
    data = gdf([Point(0, 0), Point(1, 1)], gruen=[50.0, 10.0], flaeche=[100.0, 40.0])
    result = calc(data, "gruen / flaeche", "anteil")
    assert result["anteil"].tolist() == [0.5, 0.25]


def test_fa68_calculate_field_functions_and_where():
    data = gdf([Point(0, 0), Point(1, 1)], a=[4.0, -9.0])
    result = calc(data, "where(a > 0, sqrt(a), abs(a))")
    assert result["ergebnis"].tolist() == [2.0, 9.0]


def test_fa68_calculate_field_comparison_gives_one_and_zero():
    data = gdf([Point(0, 0), Point(1, 1)], a=[1.0, 5.0])
    result = calc(data, "a > 2")
    assert result["ergebnis"].tolist() == [0.0, 1.0]


def test_fa68_calculate_field_decimals():
    data = gdf([Point(0, 0)], a=[1.0])
    assert calc(data, "a / 3", decimals=2)["ergebnis"].iloc[0] == 0.33


def test_fa68_calculate_field_string_numbers_are_read_numerically():
    data = gdf([Point(0, 0), Point(1, 1)], a=["10", "2.5"])
    assert calc(data, "a * 2")["ergebnis"].tolist() == [20.0, 5.0]


def test_fa68_calculate_field_non_convertible_values_become_nan_with_warning():
    data = gdf([Point(0, 0), Point(1, 1)], a=["10", "keine Angabe"])
    with pytest.warns(
        UserWarning,
        match=r"Spalte 'a' wird numerisch gelesen; 1 Wert\(e\) nicht numerisch",
    ):
        result = calc(data, "a + 1")
    assert result["ergebnis"].iloc[0] == 11.0
    assert math.isnan(result["ergebnis"].iloc[1])


def test_fa68_calculate_field_division_by_zero_is_nan_and_reported():
    data = gdf([Point(0, 0), Point(1, 1)], a=[1.0, 1.0], b=[2.0, 0.0])
    with pytest.warns(UserWarning, match="keinen endlichen Wert"):
        result = calc(data, "a / b")
    assert result["ergebnis"].iloc[0] == 0.5
    assert math.isnan(result["ergebnis"].iloc[1])


def test_fa68_calculate_field_comparison_with_missing_value_is_nan():
    data = gdf([Point(0, 0), Point(1, 1)], a=[3.0, np.nan])
    result = calc(data, "a > 1")
    assert result["ergebnis"].iloc[0] == 1.0
    assert math.isnan(result["ergebnis"].iloc[1])


def test_fa68_calculate_field_unknown_column_lists_available_columns():
    data = gdf([Point(0, 0)], a=[1.0], b=[2.0])
    with pytest.raises(ValueError) as caught:
        calc(data, "a + c")
    assert "['c']" in str(caught.value)
    assert "['a', 'b']" in str(caught.value)


def test_fa68_calculate_field_existing_column_needs_overwrite():
    data = gdf([Point(0, 0)], a=[1.0])
    with pytest.raises(ValueError, match="overwrite"):
        calc(data, "a * 2", "a")
    assert calc(data, "a * 2", "a", overwrite=True)["a"].iloc[0] == 2.0


def test_fa68_calculate_field_keeps_rows_and_input_unchanged():
    data = gdf([Point(0, 0), Point(1, 1)], a=[1.0, 2.0])
    result = calc(data, "a + 1")
    assert "ergebnis" not in data.columns
    assert len(result) == 2 and result["a"].tolist() == [1.0, 2.0]


def test_fa68_calculate_field_empty_layer_has_the_column():
    result = calc(gdf([]), "a + 1")
    assert result.empty and "ergebnis" in result.columns


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "a.real",
        "a[0]",
        "open(a)",
        "a and b",
        "",
        "1 + 2",
    ],
)
def test_fa68_calculate_field_invalid_expression_fails_at_validation(expression):
    with pytest.raises(ValueError, match="params"):
        CalculateFieldStep(
            id="c",
            inputs={"features": "x"},
            params={"expression": expression, "output_field": "e"},
        )


def test_fa68_calculate_field_expression_error_names_position():
    with pytest.raises(ValueError, match="Zeichen"):
        CalculateFieldStep(
            id="c",
            inputs={"features": "x"},
            params={"expression": "a + b.c", "output_field": "e"},
        )


def test_fa68_calculate_field_output_field_must_be_identifier():
    with pytest.raises(ValueError, match="output_field"):
        CalculateFieldStep(
            id="c",
            inputs={"features": "x"},
            params={"expression": "a + 1", "output_field": "mit leerzeichen"},
        )


def test_fa68_calculate_field_step_declares_contract():
    step = CalculateFieldStep(
        id="c",
        inputs={"features": "x"},
        params={"expression": "a + 1", "output_field": "e"},
    )
    assert step.INPUT_PORTS == {"features": DataType.VECTOR}
    assert step.OUTPUT_TYPE == DataType.VECTOR
    assert step.produced_fields() == {"e"}
    assert set(params_catalog(CalculateFieldStep.Params)[1]) == {
        "expression",
        "output_field",
    }


def test_fa68_operations_are_visible_in_the_catalog():
    from geofact import api

    entries = {entry["op"]: entry for entry in api.operation_catalog()}
    assert {"area", "length", "calculate_field"} <= set(entries)
    assert entries["area"]["input_ports"] == {"features": "vector"}
    assert entries["calculate_field"]["required_params"] == [
        "expression",
        "output_field",
    ]


# =====================================================================
# Nachtreview 02.10.2026: Wahrheitswerte mit Lücken
# =====================================================================


def test_fa68_calculate_field_reads_object_booleans_as_one_and_zero():
    """Nachtreview 17: a boolean column with a null (object dtype, typical for
    GeoJSON/OSM) turned into all NaN instead of 1.0/0.0."""
    features = gpd.GeoDataFrame(
        {"flag": [True, False, None]},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:25833",
    )
    result = run_calculate_field(
        {"features": features}, {"expression": "flag * 10", "output_field": "v"}
    )
    values = result["v"].tolist()
    assert values[:2] == [10.0, 0.0] and math.isnan(values[2])


def test_fa68_calculate_field_reads_nullable_booleans_as_one_and_zero():
    import pandas as pd

    features = gpd.GeoDataFrame(
        {"flag": pd.array([True, False, None], dtype="boolean")},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:25833",
    )
    result = run_calculate_field(
        {"features": features}, {"expression": "flag * 10", "output_field": "v"}
    )
    values = result["v"].tolist()
    assert values[:2] == [10.0, 0.0] and math.isnan(values[2])
