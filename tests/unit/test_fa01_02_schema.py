"""Implements: FA1, FA2 (Schema-Anteile von FA8, FA9, FA14).

Contract-Tests für Scenario/Step-Schema (src/geofact/core/scenario.py).
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from geofact.core.scenario import Scenario

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


def load(config: dict) -> Scenario:
    return Scenario(**config)


def base_scenario(**overrides) -> dict:
    """Minimales valides Grundgerüst (ein Layer, ein Schritt) zum Überschreiben."""
    config = {
        "scenario": {"name": "Test", "region": "Sachsen, Deutschland"},
        "layers": [
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
        ],
        "steps": [
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 10},
            },
        ],
        "output": [{"type": "map", "source": "catchment"}],
    }
    config.update(overrides)
    return config


# =====================================================================
# FA1 - Szenario-Definition einlesen
# =====================================================================


def test_fa01_valid_scenario_parses():
    scenario = load(base_scenario())
    assert scenario.scenario.name == "Test"
    assert [s.id for s in scenario.steps] == ["catchment"]


def test_fa01_execution_order_topological():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
            {"id": "power_lines", "source": "osm", "tags": {"power": "line"}},
        ],
        steps=[
            {
                "id": "graph",
                "op": "build_network",
                "inputs": {"lines": "power_lines", "nodes": "substations"},
            },
            {
                "id": "scored",
                "op": "centrality",
                "inputs": {"graph": "graph"},
            },
            {
                "id": "as_vector",
                "op": "graph_to_vector",
                "inputs": {"graph": "scored"},
            },
        ],
        output=[{"type": "map", "source": "as_vector"}],
    )
    scenario = load(config)
    order = scenario.execution_order()
    assert order.index("graph") < order.index("scored") < order.index("as_vector")


@pytest.mark.parametrize(
    "example_file",
    ["sachsen_nachthimmel.yaml", "szenario2_gruenflaechen.yaml"],
)
def test_fa01_example_scenarios_parse(example_file):
    raw = yaml.safe_load((EXAMPLES_DIR / example_file).read_text(encoding="utf-8"))
    scenario = load(raw)
    assert scenario.execution_order()


# =====================================================================
# FA2 - Konfiguration vor Ausführung prüfen
# =====================================================================


def test_fa02_type_mismatch_raster_into_vector_port():
    config = base_scenario(
        layers=[
            {
                "id": "population",
                "source": "file",
                "path": "data/pop.tif",
                "data_type": "raster",
                "format": "tif",
            },
        ],
        steps=[
            {
                "id": "buffered",
                "op": "buffer",
                "inputs": {"geometry": "population"},
                "params": {"radius_km": 10},
            },
        ],
        output=[{"type": "map", "source": "buffered"}],
    )
    with pytest.raises(ValidationError, match="erwartet"):
        load(config)


def test_fa02_cycle_detected():
    config = base_scenario(
        steps=[
            {
                "id": "a",
                "op": "filter",
                "inputs": {"features": "b"},
                "params": {"condition": "x > 1"},
            },
            {
                "id": "b",
                "op": "filter",
                "inputs": {"features": "a"},
                "params": {"condition": "x > 1"},
            },
        ],
        output=[{"type": "map", "source": "a"}],
    )
    with pytest.raises(ValidationError, match="Zyklus"):
        load(config)


def test_fa02_unknown_input_reference():
    config = base_scenario(
        steps=[
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "does_not_exist"},
                "params": {"radius_km": 10},
            },
        ],
    )
    with pytest.raises(ValidationError, match="unbekannte Quelle"):
        load(config)


def test_fa02_missing_port():
    config = base_scenario(
        steps=[
            {
                "id": "dist",
                "op": "nearest_distance",
                "inputs": {"from": "substations"},
            },
        ],
        output=[{"type": "map", "source": "dist"}],
    )
    with pytest.raises(ValidationError, match="fehlende Eingänge"):
        load(config)


def test_fa02_missing_required_param():
    config = base_scenario(
        steps=[
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
            },
        ],
    )
    with pytest.raises(ValidationError) as caught:
        load(config)
    (error,) = caught.value.errors()
    assert error["type"] == "missing"
    assert error["loc"] == ("steps", 0, "params", "radius_km")


def test_fa02_duplicate_ids():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
        ],
        steps=[
            {
                "id": "substations",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 10},
            },
        ],
        output=[{"type": "map", "source": "substations"}],
    )
    with pytest.raises(ValidationError, match="ID .substations. ist doppelt"):
        load(config)


def test_fa02_spatial_check_unknown_layer():
    config = base_scenario(
        layers=[
            {
                "id": "substations",
                "source": "osm",
                "tags": {"power": "substation"},
                "validation": {
                    "spatial_check": {
                        "within": "does_not_exist",
                        "on_violation": "warn",
                    },
                },
            },
        ],
    )
    with pytest.raises(ValidationError, match="unbekannten Layer"):
        load(config)


# =====================================================================
# FA8 - SpatialJoinStep (Schema-Ebene)
# =====================================================================


def test_fa08_spatial_join_valid_predicate_parses():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
            {"id": "power_lines", "source": "osm", "tags": {"power": "line"}},
        ],
        steps=[
            {
                "id": "joined",
                "op": "spatial_join",
                "inputs": {"left": "substations", "right": "power_lines"},
                "params": {"predicate": "within"},
            },
        ],
        output=[{"type": "map", "source": "joined"}],
    )
    scenario = load(config)
    assert scenario.execution_order() == ["joined"]


def test_fa08_spatial_join_unknown_predicate_raises():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
            {"id": "power_lines", "source": "osm", "tags": {"power": "line"}},
        ],
        steps=[
            {
                "id": "joined",
                "op": "spatial_join",
                "inputs": {"left": "substations", "right": "power_lines"},
                "params": {"predicate": "touches"},
            },
        ],
        output=[{"type": "map", "source": "joined"}],
    )
    with pytest.raises(ValidationError, match="predicate"):
        load(config)


# =====================================================================
# FA14 - GeometryMapping (Schema-Ebene)
# =====================================================================


def test_fa14_geometry_mapping_xy_xor_wkt():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
            {
                "id": "ladesaeulen",
                "source": "table",
                "path": "data/ladesaeulen.csv",
                "geometry": {
                    "x_column": "lon",
                    "y_column": "lat",
                    "wkt_column": "geom",
                },
            },
        ],
    )
    with pytest.raises(ValidationError, match="entweder x_column"):
        load(config)


def test_fa14_geometry_mapping_neither_xy_nor_wkt_raises():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
            {
                "id": "ladesaeulen",
                "source": "table",
                "path": "data/ladesaeulen.csv",
                "geometry": {},
            },
        ],
    )
    with pytest.raises(ValidationError, match="entweder x_column"):
        load(config)


# =====================================================================
# FA8 - filter.on_missing-Policy (Schema-Ebene)
# =====================================================================


def test_fa08_filter_on_missing_policy_validated():
    config = base_scenario(
        steps=[
            {
                "id": "gefiltert",
                "op": "filter",
                "inputs": {"features": "substations"},
                "params": {
                    "condition": "voltage > 1000",
                    "on_missing": "invalid_policy",
                },
            },
        ],
        output=[{"type": "map", "source": "gefiltert"}],
    )
    with pytest.raises(ValidationError, match="on_missing"):
        load(config)


def test_fa08_filter_on_missing_default_is_exclude():
    config = base_scenario(
        steps=[
            {
                "id": "gefiltert",
                "op": "filter",
                "inputs": {"features": "substations"},
                "params": {"condition": "voltage > 1000"},
            },
        ],
        output=[{"type": "map", "source": "gefiltert"}],
    )
    scenario = load(config)
    step = next(s for s in scenario.steps if s.id == "gefiltert")
    assert step.params.get("on_missing", "exclude") == "exclude"


# =====================================================================
# FA9 - required_sources() / load_schedule() (Lazy Loading by design)
# =====================================================================


def test_fa09_required_sources_excludes_unused_layer():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
            {
                "id": "nie_gebraucht",
                "source": "file",
                "path": "data/irrelevant.geojson",
                "data_type": "vector",
            },
        ],
    )
    scenario = load(config)
    req = scenario.required_sources()
    assert "nie_gebraucht" in req["unused_layers"]
    assert "nie_gebraucht" not in req["layers"]
    assert "substations" in req["layers"]


def test_fa09_load_schedule_defers_late_source():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
            {"id": "power_lines", "source": "osm", "tags": {"power": "line"}},
        ],
        steps=[
            {
                "id": "dist",
                "op": "nearest_distance",
                "inputs": {"from": "substations", "to": "substations"},
            },
            {
                "id": "gefiltert",
                "op": "filter",
                "inputs": {"features": "power_lines"},
                "params": {"condition": "x > 1"},
            },
        ],
        output=[{"type": "map", "source": "gefiltert"}],
    )
    scenario = load(config)
    schedule = scenario.load_schedule()
    # 'power_lines' wird erst unmittelbar vor 'gefiltert' geladen, nicht vorher.
    assert "power_lines" in schedule["gefiltert"]
    assert all(
        "power_lines" not in layers
        for step, layers in schedule.items()
        if step != "gefiltert"
    )


# =====================================================================
# FA11-Vorbereitung - Registry-Auflösung statt geschlossener Union (WP 2.2)
# =====================================================================


def test_fa11_unknown_op_lists_registered_ops():
    config = base_scenario(
        steps=[
            {
                "id": "mystery",
                "op": "does_not_exist",
                "inputs": {"geometry": "substations"},
            },
        ],
    )
    with pytest.raises(ValidationError, match="buffer"):
        load(config)


# =====================================================================
# FA3 - OsmLayer.tags: einzelnes Tag-Set oder Liste von Alternativen
# =====================================================================


def test_fa03_osm_layer_accepts_single_tag_set():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": {"power": "substation"}},
        ],
    )
    scenario = load(config)
    assert scenario.layers[0].tags == {"power": "substation"}


def test_fa03_osm_layer_accepts_tag_alternatives_list():
    config = base_scenario(
        layers=[
            {
                "id": "substations",
                "source": "osm",
                "tags": [{"leisure": "park"}, {"leisure": "garden"}],
            },
        ],
    )
    scenario = load(config)
    assert scenario.layers[0].tags == [{"leisure": "park"}, {"leisure": "garden"}]


def test_fa03_osm_layer_rejects_empty_tag_alternative():
    config = base_scenario(
        layers=[
            {"id": "substations", "source": "osm", "tags": [{"leisure": "park"}, {}]},
        ],
    )
    with pytest.raises(ValidationError, match="Tag-Alternative"):
        load(config)


def test_fa03_osm_layer_rejects_empty_tags():
    config = base_scenario(
        layers=[{"id": "substations", "source": "osm", "tags": {}}],
    )
    with pytest.raises(ValidationError, match="mindestens einen Tag-Filter"):
        load(config)
