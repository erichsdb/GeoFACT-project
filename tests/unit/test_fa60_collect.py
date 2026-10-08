"""Implements: FA60 (Sammelknoten `collect`, Operationsteil).

Contract tests for src/geofact/builtin/operations/collect.py: one input port per
declared input (dynamic contract like `rest`), lossless concatenation with the
port name in `source_field`, CRS mismatch, empty inputs, type check at
validation time, a clashing `source_field` column and catalog visibility.
Its inputs usually come from a selector ``instanz[*].export`` (FA75, test_fa75_modules.py).
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point, Polygon

from geofact.builtin.operations import collect
from geofact.builtin.operations.collect import CollectStep
from geofact.core.errors import ConfigError
from geofact.core.scenario import Scenario
from geofact.core.types import DataType
from geofact.engine import catalog
from geofact.engine.executor import run_scenario

BBOX_REGION = "12.85,50.78,12.98,50.87"
CRS = "EPSG:32633"


def _zones(names: list[str], crs: str = CRS, **extra) -> gpd.GeoDataFrame:
    polygons = [
        Polygon([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)]) for i in range(len(names))
    ]
    return gpd.GeoDataFrame({"name": names, **extra}, geometry=polygons, crs=crs)


def _empty(crs: str = CRS, columns: tuple[str, ...] = ("name",)) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({c: [] for c in columns}, geometry=[], crs=crs)


def _run(inputs: dict, **params) -> gpd.GeoDataFrame:
    validated = CollectStep.Params(**params).model_dump()
    return collect.run_collect(inputs, validated)


def _points_layer(layer_id: str, names: list[str]) -> dict:
    return {
        "id": layer_id,
        "source": "points",
        "features": [
            {"name": name, "lon": 12.90 + 0.01 * i, "lat": 50.82}
            for i, name in enumerate(names)
        ],
    }


def _scenario(
    inputs: dict, layers: list[dict], steps: list[dict] | None = None
) -> Scenario:
    return Scenario(
        **{
            "scenario": {"name": "collect", "region": BBOX_REGION},
            "layers": layers,
            "steps": [
                *(steps or []),
                {"id": "alle", "op": "collect", "inputs": inputs},
            ],
            "output": [{"type": "geojson", "source": "alle"}],
        }
    )


# =====================================================================
# Contract
# =====================================================================


def test_fa60_collect_creates_one_port_per_input():
    step = CollectStep(
        id="alle", inputs={"leipzig": "a", "dresden": "b", "chemnitz": "c"}
    )
    assert step.input_ports() == {
        "leipzig": DataType.VECTOR,
        "dresden": DataType.VECTOR,
        "chemnitz": DataType.VECTOR,
    }
    assert step.output_type() == DataType.VECTOR
    assert step.params == {"source_field": "source"}


def test_fa60_collect_requires_at_least_one_input():
    with pytest.raises(ValidationError, match="mindestens einen Eingang"):
        CollectStep(id="alle", inputs={})


def test_fa60_collect_source_field_must_be_an_identifier():
    with pytest.raises(ValidationError, match="source_field"):
        CollectStep(
            id="alle", inputs={"a": "x"}, params={"source_field": "mit leerzeichen"}
        )


# =====================================================================
# Happy path
# =====================================================================


def test_fa60_collect_concatenates_losslessly_with_source_column():
    a = _zones(["Nord", "Sued"], flaeche=[1.0, 2.0])
    b = _zones(["Mitte"], gruen=[0.4])
    result = _run({"leipzig": a, "dresden": b})

    assert len(result) == len(a) + len(b)
    assert list(result["source"]) == ["leipzig", "leipzig", "dresden"]
    assert list(result["name"]) == ["Nord", "Sued", "Mitte"]
    # column union like vector_union: missing values are filled, nothing is dropped
    assert {"name", "flaeche", "gruen", "source", "geometry"} == set(result.columns)
    assert result["flaeche"].isna().tolist() == [False, False, True]
    assert result["gruen"].isna().tolist() == [True, True, False]
    assert result.crs == CRS
    assert list(result.index) == [0, 1, 2]
    # inputs are not modified
    assert "source" not in a.columns and "source" not in b.columns


def test_fa60_collect_custom_and_disabled_source_field():
    a, b = _zones(["Nord"]), _zones(["Mitte"])
    named = _run({"leipzig": a, "dresden": b}, source_field="stadt_id")
    assert list(named["stadt_id"]) == ["leipzig", "dresden"]
    plain = _run({"leipzig": a, "dresden": b}, source_field=None)
    assert set(plain.columns) == {"name", "geometry"}


def test_fa60_collect_runs_in_a_scenario_with_ports_in_declaration_order():
    scenario = _scenario(
        {"zwei": "p2", "eins": "p1"},
        [_points_layer("p1", ["a"]), _points_layer("p2", ["b", "c"])],
    )
    result = run_scenario(scenario).store["alle"]
    assert list(result["source"]) == ["zwei", "zwei", "eins"]
    assert list(result["name"]) == ["b", "c", "a"]


# =====================================================================
# Error cases
# =====================================================================


def test_fa60_collect_rejects_crs_mismatch():
    with pytest.raises(ValueError, match="verschiedene CRS") as caught:
        _run(
            {
                "leipzig": _zones(["Nord"]),
                "dresden": _zones(["Mitte"], crs="EPSG:32632"),
            }
        )
    assert "leipzig" in str(caught.value) and "dresden" in str(caught.value)


def test_fa60_collect_rejects_non_vector_port_at_validation():
    """Type error case: a graph at a collect input fails before the run."""
    layers = [{"id": "reg", "source": "region"}, {"id": "l", "source": "region"}]
    steps = [
        {"id": "net", "op": "build_network", "inputs": {"lines": "l", "nodes": "reg"}}
    ]
    with pytest.raises(
        ValidationError, match="erwartet vector, erhält aber graph"
    ) as caught:
        _scenario({"zonen": "reg", "netz": "net"}, layers, steps)
    assert ("steps", 1, "inputs", "netz") in [
        error["loc"] for error in caught.value.errors()
    ]


def test_fa60_collect_existing_source_column_is_rejected():
    clashing = _zones(["Nord"], source=["osm"])
    with pytest.raises(ValueError, match="bereits eine Spalte 'source'") as caught:
        _run({"leipzig": _zones(["Mitte"]), "dresden": clashing})
    assert "dresden" in str(caught.value)


# =====================================================================
# Edge cases
# =====================================================================


def test_fa60_collect_empty_input_contributes_no_rows():
    result = _run(
        {"leipzig": _zones(["Nord", "Sued"]), "leer": _empty(columns=("name", "extra"))}
    )
    assert len(result) == 2
    assert list(result["source"]) == ["leipzig", "leipzig"]
    assert "extra" in result.columns  # columns of an empty input still join the union


def test_fa60_collect_all_inputs_empty_gives_empty_layer_with_columns():
    result = _run({"a": _empty(), "b": _empty(columns=("wert",))})
    assert result.empty
    assert {"name", "wert", "source", "geometry"} <= set(result.columns)
    assert result.crs == CRS


def test_fa60_collect_single_input_and_mixed_geometry_columns():
    points = gpd.GeoDataFrame({"name": ["p"]}, geometry=[Point(0, 0)], crs=CRS)
    points = points.rename_geometry("geom")
    result = _run({"nur": points})
    assert result.geometry.name == "geometry"
    assert list(result["source"]) == ["nur"]


def test_fa60_collect_is_visible_in_the_catalog():
    entry = next(e for e in catalog.operation_catalog() if e["op"] == "collect")
    assert entry["output_type"] == "vector"
    assert entry["required_params"] == []
    assert "source_field" in entry["params"]


def test_fa60_collect_step_reading_a_module_without_step_id_is_a_positioned_error():
    """Nachtreview 4 (now FA75): no raw KeyError('id') from a module step without id."""
    from geofact.core.expansion import expand

    raw = {
        "scenario": {"name": "c", "region": "12.85,50.78,12.98,50.87"},
        "layers": [
            {
                "id": "p",
                "source": "points",
                "features": [{"name": "a", "lon": 12.9, "lat": 50.82}],
            }
        ],
        "modules": {
            "m": {
                "steps": [
                    {
                        "op": "buffer",
                        "inputs": {"geometry": "p"},
                        "params": {"radius_km": 1},
                    }
                ],
                "exports": ["s"],
            }
        },
        "instances": [
            {"id": "i", "module": "m", "foreach": {"var": "c", "in": ["a", "b"]}}
        ],
        "steps": [{"id": "alle", "op": "collect", "inputs": "i[*].s"}],
        "output": [{"type": "geojson", "source": "alle"}],
    }
    with pytest.raises(ConfigError) as info:
        expand(raw)
    issues = dict(info.value.issues)
    assert issues["modules -> m -> steps -> 0 -> id"] == "Pflichtfeld fehlt"
