"""Implements: FA58 (CRS-Provenienz je Layer: Erfassung in der Ladestrecke, Bindung an die Ausgaben).

Contract-Tests für engine/loading.py (provenance_sink, layer_loaded_detail)
und engine/outputs.py (spec.provenance vor dem Schreiben)."""

from __future__ import annotations

import json

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from _loading_helpers import (
    DRESDEN_BBOX,
    FIXTURES_DIR,
    RECORDING_PLUGIN,
    context,
    layer_from,
)
from geofact.core.registry import default_registry
from geofact.core.scenario import Scenario
from geofact.core.types import LayerProvenance
from geofact.engine.discovery import discover
from geofact.engine.executor import run_scenario
from geofact.engine.loading import (
    SAMPLE_MAX_POINTS,
    SHAPE_MAX_VERTICES,
    layer_loaded_detail,
    load_layer,
)
from geofact.engine.outputs import write_outputs

SUBSTATIONS = {
    "id": "umspannwerke",
    "source": "file",
    "data_type": "vector",
    "path": str(FIXTURES_DIR / "substations.geojson"),
}


def test_fa58_layer_loaded_event_carries_source_and_target_crs():
    sink: dict[str, LayerProvenance] = {}
    ctx = context()

    data = load_layer(
        layer_from(SUBSTATIONS), ctx, default_registry(), provenance_sink=sink
    )

    provenance = sink["umspannwerke"]
    assert provenance.source == "file"
    assert provenance.source_crs == "EPSG:4326"
    assert provenance.target_crs == "EPSG:32633"
    assert provenance.source_crs_label.startswith("EPSG:4326 (WGS 84")
    assert provenance.crs_override is False
    assert provenance.feature_count == len(data)
    # Rohausdehnung in Grad, harmonisiert in Metern
    assert 13.0 < provenance.raw_bounds[0] < 14.0
    assert provenance.bounds[0] > 100_000
    detail = layer_loaded_detail("umspannwerke", sink)
    assert detail["provenance"]["source_crs"] == "EPSG:4326"
    assert detail["provenance"]["target_crs"] == "EPSG:32633"
    assert layer_loaded_detail("anderer", sink) == {}


def test_fa58_provenance_marks_crs_override():
    sink: dict[str, LayerProvenance] = {}
    layer = layer_from({**SUBSTATIONS, "crs": "EPSG:4326", "crs_override": True})

    load_layer(layer, context(), default_registry(), provenance_sink=sink)

    assert sink["umspannwerke"].crs_override is True
    assert sink["umspannwerke"].source_crs == "EPSG:4326"


def test_fa58_provenance_for_transform_none_layer_has_no_geometry():
    sink: dict[str, LayerProvenance] = {}
    layer = layer_from(
        {
            "id": "tabelle",
            "source": "table",
            "path": str(FIXTURES_DIR / "ladesaeulen.csv"),
        }
    )

    load_layer(layer, context(), default_registry(), provenance_sink=sink)

    provenance = sink["tabelle"]
    assert provenance.target_crs == (provenance.source_crs or "")
    assert provenance.raw_sample == []
    assert provenance.sample == []
    assert provenance.bounds is None
    assert provenance.shapes == ()


def test_fa58_raw_sample_is_deterministic_and_capped():
    points = gpd.GeoDataFrame(
        {"n": range(450)},
        geometry=[
            Point(13.61 + i * 0.0007, 51.02 + (i % 50) * 0.002) for i in range(450)
        ],
        crs="EPSG:4326",
    )
    layer = layer_from(SUBSTATIONS)
    first: dict[str, LayerProvenance] = {}
    second: dict[str, LayerProvenance] = {}

    load_layer(
        layer,
        context(),
        default_registry(),
        override=lambda _l: points.copy(),
        provenance_sink=first,
    )
    load_layer(
        layer,
        context(),
        default_registry(),
        override=lambda _l: points.copy(),
        provenance_sink=second,
    )

    raw = first["umspannwerke"].raw_sample
    assert 0 < len(raw) <= SAMPLE_MAX_POINTS
    assert raw == second["umspannwerke"].raw_sample
    assert raw[0] == pytest.approx((13.61, 51.02))
    assert len(first["umspannwerke"].sample) == len(raw)


def test_fa58_provenance_keeps_simplified_shapes_raw_and_harmonized():
    mixed = gpd.GeoDataFrame(
        {"n": [1, 2, 3]},
        geometry=[
            Point(13.62, 51.03),
            LineString([(13.61, 51.02), (13.63, 51.04)]),
            Polygon([(13.60, 51.00), (13.64, 51.00), (13.64, 51.05), (13.60, 51.05)]),
        ],
        crs="EPSG:4326",
    )
    sink: dict[str, LayerProvenance] = {}
    load_layer(
        layer_from(SUBSTATIONS),
        context(),
        default_registry(),
        override=lambda _l: mixed.copy(),
        provenance_sink=sink,
    )

    provenance = sink["umspannwerke"]
    assert [kind for kind, _ in provenance.raw_shapes] == ["point", "line", "polygon"]
    assert [kind for kind, _ in provenance.shapes] == ["point", "line", "polygon"]
    assert provenance.raw_shapes[0][1][0][0] == pytest.approx((13.62, 51.03))
    assert provenance.shapes[0][1][0][0][0] > 100_000  # metres after reprojection
    assert "raw_shapes" not in provenance.to_dict()  # events stay small


def test_fa58_provenance_shapes_are_capped_and_deterministic():
    wiggly = LineString([(13.6 + i * 1e-5, 51.0 + (i % 2) * 0.01) for i in range(4000)])
    lines = gpd.GeoDataFrame({"n": range(3)}, geometry=[wiggly] * 3, crs="EPSG:4326")
    first: dict[str, LayerProvenance] = {}
    second: dict[str, LayerProvenance] = {}
    for sink in (first, second):
        load_layer(
            layer_from(SUBSTATIONS),
            context(),
            default_registry(),
            override=lambda _l: lines.copy(),
            provenance_sink=sink,
        )

    shapes = first["umspannwerke"].shapes
    assert sum(len(ring) for _, rings in shapes for ring in rings) <= SHAPE_MAX_VERTICES
    assert shapes == second["umspannwerke"].shapes


def test_fa58_raster_provenance_counts_cells_and_samples_corners():
    sink: dict[str, LayerProvenance] = {}
    layer = layer_from(
        {
            "id": "bev",
            "source": "file",
            "data_type": "raster",
            "path": str(FIXTURES_DIR / "population.tif"),
        }
    )

    load_layer(layer, context(), default_registry(), provenance_sink=sink)

    provenance = sink["bev"]
    assert provenance.feature_count and provenance.feature_count > 0
    assert len(provenance.raw_sample) == 5
    assert provenance.raw_bounds == pytest.approx((13.7, 51.03, 13.78, 51.07))
    assert provenance.raw_shapes == () and provenance.shapes == ()


@pytest.fixture()
def recording_registry(tmp_path_factory):
    plugins = tmp_path_factory.mktemp("plugins_fa58")
    (plugins / "aufzeichnung_fa58.py").write_text(RECORDING_PLUGIN, encoding="utf-8")
    return discover(plugin_paths=[plugins])


def test_fa58_output_spec_gets_provenance_bound_before_write(
    recording_registry, tmp_path
):
    scenario = Scenario.model_validate(
        {
            "scenario": {"name": "Provenienz", "region": DRESDEN_BBOX},
            "layers": [SUBSTATIONS],
            "steps": [
                {
                    "id": "puffer",
                    "op": "buffer",
                    "inputs": {"geometry": "umspannwerke"},
                    "params": {"radius_km": 1},
                }
            ],
            "output": [
                {"type": "aufzeichnung", "source": "umspannwerke", "path": "rec.json"}
            ],
        },
        context={"registry": recording_registry},
    )
    result = run_scenario(scenario, registry=recording_registry)
    sink: dict[str, LayerProvenance] = {}
    load_layer(scenario.layers[0], context(), recording_registry, provenance_sink=sink)
    result.provenance = sink  # W2-02 füllt ExecutionResult.provenance im Executor

    write_outputs(scenario, result, tmp_path, registry=recording_registry)

    recorded = json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))
    assert recorded["provenance"]["umspannwerke"]["target_crs"] == "EPSG:32633"
