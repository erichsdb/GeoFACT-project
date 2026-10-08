"""Implements: FA69 (Spaltenauswahl und Dublettenbereinigung: select_columns, deduplicate).

Contract-Tests für src/geofact/builtin/operations/select_columns.py und
deduplicate.py.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, box

from geofact import api
from geofact.builtin.operations.deduplicate import (
    DeduplicateNotice,
    DeduplicateStep,
    run_deduplicate,
)
from geofact.builtin.operations.select_columns import (
    SelectColumnsStep,
    run_select_columns,
)
from geofact.core.scenario import Scenario
from geofact.core.types import DataType

UTM = "EPSG:25833"
BBOX_REGION = "12.8,50.7,13.1,50.9"


def gdf(geoms, **columns) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns, geometry=list(geoms), crs=UTM)


def osm_like() -> gpd.GeoDataFrame:
    return gdf(
        [Point(0, 0), Point(1, 1), Point(2, 2)],
        name=["A", "B", "C"],
        leisure=["park", "park", None],
        wikidata=["Q1", None, "Q3"],
    )


# =====================================================================
# select_columns
# =====================================================================


def test_fa69_select_columns_keep_retains_listed_columns_and_geometry():
    result = run_select_columns(
        {"features": osm_like()}, {"keep": ["name"], "drop": None}
    )
    assert list(result.columns) == ["name", "geometry"]
    assert result.crs == UTM


def test_fa69_select_columns_keep_sets_column_order():
    result = run_select_columns(
        {"features": osm_like()}, {"keep": ["wikidata", "name"], "drop": None}
    )
    assert list(result.columns) == ["wikidata", "name", "geometry"]


def test_fa69_select_columns_drop_removes_listed_columns():
    result = run_select_columns(
        {"features": osm_like()}, {"keep": None, "drop": ["wikidata"]}
    )
    assert list(result.columns) == ["name", "leisure", "geometry"]


def test_fa69_select_columns_keeps_rows_and_does_not_modify_input():
    data = osm_like()
    result = run_select_columns({"features": data}, {"keep": ["name"], "drop": None})
    assert result["name"].tolist() == ["A", "B", "C"]
    assert "wikidata" in data.columns


def test_fa69_select_columns_missing_column_lists_available():
    with pytest.raises(ValueError) as caught:
        run_select_columns({"features": osm_like()}, {"keep": ["nmae"], "drop": None})
    assert "['nmae']" in str(caught.value)
    assert "['leisure', 'name', 'wikidata']" in str(caught.value)


def test_fa69_select_columns_empty_layer_stays_empty():
    empty = gdf([])
    result = run_select_columns({"features": empty}, {"keep": ["name"], "drop": None})
    assert result.empty and list(result.columns) == ["geometry"]


@pytest.mark.parametrize("params", [{}, {"keep": ["a"], "drop": ["b"]}])
def test_fa69_select_columns_requires_exactly_one_of_keep_and_drop(params):
    with pytest.raises(ValueError, match="genau eines"):
        SelectColumnsStep(id="s", inputs={"features": "x"}, params=params)


def test_fa69_select_columns_geometry_in_drop_is_rejected():
    with pytest.raises(ValueError, match="Geometriespalte"):
        SelectColumnsStep(
            id="s", inputs={"features": "x"}, params={"drop": ["geometry"]}
        )


def test_fa69_select_columns_step_declares_contract():
    step = SelectColumnsStep(id="s", inputs={"features": "x"}, params={"keep": ["a"]})
    assert step.INPUT_PORTS == {"features": DataType.VECTOR}
    assert step.OUTPUT_TYPE == DataType.VECTOR
    assert step.produced_fields() == set()


def test_fa69_select_columns_type_error_raster_input_rejected_at_validation():
    with pytest.raises(ValueError, match="erwartet vector, erhält aber raster"):
        Scenario(
            **{
                "scenario": {"name": "t", "region": BBOX_REGION},
                "layers": [
                    {
                        "id": "dem",
                        "source": "file",
                        "path": "dem.tif",
                        "format": "tif",
                        "data_type": "raster",
                    }
                ],
                "steps": [
                    {
                        "id": "s",
                        "op": "select_columns",
                        "inputs": {"features": "dem"},
                        "params": {"keep": ["a"]},
                    }
                ],
                "output": [{"type": "geojson", "source": "s"}],
            }
        )


# =====================================================================
# deduplicate
# =====================================================================


def dedup(data, by, keep="first"):
    params = DeduplicateStep.Params(by=by, keep=keep).model_dump()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_deduplicate({"features": data}, params)
    return result, caught


def test_fa69_deduplicate_keeps_first_by_default():
    data = gdf(
        [Point(0, 0), Point(1, 1), Point(2, 2)], name=["A", "B", "A"], n=[1, 2, 3]
    )
    result, _ = dedup(data, ["name"])
    assert result["n"].tolist() == [1, 2]


def test_fa69_deduplicate_keep_last_preserves_original_order():
    data = gdf(
        [Point(0, 0), Point(1, 1), Point(2, 2)], name=["A", "B", "A"], n=[1, 2, 3]
    )
    result, _ = dedup(data, ["name"], keep="last")
    assert result["n"].tolist() == [2, 3]


def test_fa69_deduplicate_multiple_key_columns():
    data = gdf([Point(0, 0)] * 3, name=["A", "A", "A"], typ=["x", "y", "x"])
    result, _ = dedup(data, ["name", "typ"])
    assert len(result) == 2


def test_fa69_deduplicate_largest_geometry_polygon_beats_point():
    """B-23: ein Polygon schlägt einen Punkt, auch wenn der Punkt zuerst kommt."""
    data = gdf(
        [Point(0, 0), box(0, 0, 1, 1), box(0, 0, 5, 5), LineString([(0, 0), (99, 0)])],
        name=["Park"] * 4,
        n=[1, 2, 3, 4],
    )
    result, _ = dedup(data, ["name"], keep="largest_geometry")
    assert result["n"].tolist() == [3]


def test_fa69_deduplicate_largest_geometry_line_beats_point_and_uses_length():
    data = gdf(
        [Point(0, 0), LineString([(0, 0), (1, 0)]), LineString([(0, 0), (9, 0)])],
        name=["Weg"] * 3,
        n=[1, 2, 3],
    )
    result, _ = dedup(data, ["name"], keep="largest_geometry")
    assert result["n"].tolist() == [3]


def test_fa69_deduplicate_largest_geometry_tie_keeps_first():
    data = gdf([box(0, 0, 1, 1), box(5, 5, 6, 6)], name=["P", "P"], n=[1, 2])
    result, _ = dedup(data, ["name"], keep="largest_geometry")
    assert result["n"].tolist() == [1]


def test_fa69_deduplicate_rows_without_key_are_kept_and_counted():
    data = gdf([Point(0, 0)] * 4, name=["A", None, None, "A"], n=[1, 2, 3, 4])
    result, caught = dedup(data, ["name"])
    assert result["n"].tolist() == [1, 2, 3]
    messages = [str(w.message) for w in caught]
    assert any("2 Objekt(e) ohne Wert" in m for m in messages)


def test_fa69_deduplicate_notice_names_counts():
    data = gdf([Point(0, 0)] * 3, name=["A", "A", "B"])
    _, caught = dedup(data, ["name"])
    notices = [w for w in caught if issubclass(w.category, DeduplicateNotice)]
    assert len(notices) == 1
    message = str(notices[0].message)
    assert (
        "1 Dublette(n)" in message and "vorher 3" in message and "nachher 2" in message
    )


def test_fa69_deduplicate_no_duplicates_no_notice():
    data = gdf([Point(0, 0)] * 2, name=["A", "B"])
    result, caught = dedup(data, ["name"])
    assert len(result) == 2
    assert not [w for w in caught if issubclass(w.category, DeduplicateNotice)]


def test_fa69_deduplicate_does_not_modify_input_and_keeps_geometry():
    data = gdf([Point(0, 0), Point(1, 1)], name=["A", "A"])
    result, _ = dedup(data, ["name"])
    assert len(data) == 2
    assert result.geometry.name == "geometry" and result.crs == UTM


def test_fa69_deduplicate_missing_column_lists_available():
    data = gdf([Point(0, 0)], name=["A"])
    with pytest.raises(ValueError, match=r"\['nmae'\].*\['name'\]"):
        dedup(data, ["nmae"])


def test_fa69_deduplicate_empty_layer_stays_empty():
    result, _ = dedup(gdf([]), ["name"])
    assert result.empty


def test_fa69_deduplicate_by_must_not_be_empty():
    with pytest.raises(ValueError, match="by"):
        DeduplicateStep(id="d", inputs={"features": "x"}, params={"by": []})


def test_fa69_deduplicate_invalid_keep_rule_is_rejected():
    with pytest.raises(ValueError, match="keep"):
        DeduplicateStep(
            id="d", inputs={"features": "x"}, params={"by": ["a"], "keep": "biggest"}
        )


def test_fa69_deduplicate_type_error_graph_input_rejected_at_validation():
    with pytest.raises(ValueError, match="erwartet vector, erhält aber graph"):
        Scenario(
            **{
                "scenario": {"name": "t", "region": BBOX_REGION},
                "layers": [{"id": "linien", "source": "region", "data_type": "vector"}],
                "steps": [
                    {"id": "netz", "op": "line_network", "inputs": {"lines": "linien"}},
                    {
                        "id": "d",
                        "op": "deduplicate",
                        "inputs": {"features": "netz"},
                        "params": {"by": ["a"]},
                    },
                ],
                "output": [{"type": "geojson", "source": "d"}],
            }
        )


def test_fa69_operations_are_visible_in_the_catalog():
    entries = {entry["op"]: entry for entry in api.operation_catalog()}
    assert entries["select_columns"]["input_ports"] == {"features": "vector"}
    assert entries["deduplicate"]["required_params"] == ["by"]
    assert entries["deduplicate"]["params"]["keep"]["default"] == "first"
