"""Implements: FA62 (Durchlassknoten `gate`, Laufzeitteil).

Contract tests for src/geofact/builtin/operations/gate.py: the layer passes
unchanged when row and column conditions hold; otherwise `on_fail` decides
(`empty`: empty frame with the same columns and CRS plus a notice, `warn`:
the layer passes with a warning, `error`: a step error naming the failed
conditions). `min_rows: 0` lets an empty layer through. The `when`
conditions at expansion time are W2-03.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point

from geofact.builtin.operations import gate
from geofact.builtin.operations.gate import GateClosedNotice, GateStep, GateWarning
from geofact.core.scenario import Scenario
from geofact.engine import catalog

CRS = "EPSG:32633"


def _points(n: int, **extra) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": [f"p{i}" for i in range(n)], **extra},
        geometry=[Point(i, i) for i in range(n)],
        crs=CRS,
    )


def _run(features: gpd.GeoDataFrame, **params) -> gpd.GeoDataFrame:
    validated = GateStep.Params(**params).model_dump()
    return gate.run_gate({"features": features}, validated)


def test_fa62_gate_passes_layer_unchanged_when_conditions_hold():
    features = _points(3, wert=[1, 2, 3])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = _run(features, min_rows=2, max_rows=5, require_fields=["wert"])
    assert result.equals(features)
    assert result is not features  # a copy, the input stays owned by its node


def test_fa62_gate_closes_to_empty_frame_with_same_columns_and_crs():
    features = _points(1, wert=[1])
    with pytest.warns(GateClosedNotice, match=r"min_rows: 2.*1 Zeile"):
        result = _run(features, min_rows=2)
    assert result.empty
    assert list(result.columns) == list(features.columns)
    assert result.crs == CRS


def test_fa62_gate_missing_field_with_on_fail_error_is_an_error():
    with pytest.raises(ValueError, match=r"gate: Bedingung nicht erfüllt.*'flaeche'"):
        _run(_points(3), require_fields=["flaeche"], on_fail="error")


def test_fa62_gate_on_fail_warn_passes_the_layer_with_a_warning():
    features = _points(4)
    with pytest.warns(GateWarning, match=r"max_rows: 3.*4 Zeilen"):
        result = _run(features, max_rows=3, on_fail="warn")
    assert len(result) == 4


def test_fa62_gate_min_rows_zero_lets_the_empty_layer_pass():
    empty = gpd.GeoDataFrame({"name": []}, geometry=[], crs=CRS)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = _run(empty, min_rows=0)
    assert result.empty and result.crs == CRS
    # default min_rows 1 closes on an empty layer (reported, never silent)
    with pytest.warns(GateClosedNotice):
        _run(empty)


def test_fa62_gate_rejects_max_rows_below_min_rows():
    with pytest.raises(ValidationError, match="max_rows"):
        GateStep(
            id="g", inputs={"features": "x"}, params={"min_rows": 5, "max_rows": 2}
        )


def test_fa62_gate_rejects_non_vector_input_at_validation():
    config = {
        "scenario": {"name": "gate", "region": "12.85,50.78,12.98,50.87"},
        "layers": [{"id": "reg", "source": "region"}, {"id": "l", "source": "region"}],
        "steps": [
            {
                "id": "net",
                "op": "build_network",
                "inputs": {"lines": "l", "nodes": "reg"},
            },
            {"id": "g", "op": "gate", "inputs": {"features": "net"}},
        ],
        "output": [{"type": "geojson", "source": "g"}],
    }
    with pytest.raises(ValidationError, match="erwartet vector, erhält aber graph"):
        Scenario(**config)


def test_fa62_gate_is_visible_in_the_catalog():
    entry = next(e for e in catalog.operation_catalog() if e["op"] == "gate")
    assert entry["input_ports"] == {"features": "vector"}
    assert entry["output_type"] == "vector"
    assert set(entry["params"]) == {"min_rows", "max_rows", "require_fields", "on_fail"}
