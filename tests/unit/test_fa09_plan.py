"""Implements: FA9 (Ausführungsplan), FA6 (Referenz-Layer der räumlichen Prüfung).

Contract-Tests für src/geofact/core/plan.py: ``ExecutionPlan.of(scenario,
registry)`` ist der eine Plan, den Executor und Web-Schicht lesen. Der Plan
wird ohne Laden von Daten abgeleitet; die Layer sind deshalb nur
deklariert (Pfade müssen nicht existieren).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geofact.api import registry as default_registry
from geofact.core.errors import ConfigError
from geofact.core.plan import ExecutionPlan, topological_order
from geofact.core.scenario import Scenario

BBOX_REGION = "13.60,51.01,13.94,51.15"


def _file_layer(layer_id: str, **extra) -> dict:
    return {
        "id": layer_id,
        "source": "file",
        "path": f"data/{layer_id}.geojson",
        "data_type": "vector",
        **extra,
    }


def _buffer(step_id: str, source: str, radius_km: float = 1) -> dict:
    return {
        "id": step_id,
        "op": "buffer",
        "inputs": {"geometry": source},
        "params": {"radius_km": radius_km},
    }


def _scenario(layers, steps, output) -> Scenario:
    return Scenario(
        **{
            "scenario": {"name": "Plan", "region": BBOX_REGION},
            "layers": layers,
            "steps": steps,
            "output": output,
        }
    )


def test_fa09_plan_order_contains_required_steps_only():
    scenario = _scenario(
        [_file_layer("a"), _file_layer("b")],
        [
            _buffer("puffer_a", "a"),
            _buffer("puffer_b", "b"),
            _buffer("nachher", "puffer_a", 2),
        ],
        [{"type": "geojson", "source": "nachher"}],
    )
    plan = ExecutionPlan.of(scenario)
    assert plan.order == ("puffer_a", "nachher")
    # execution_order() bleibt die Reihenfolge ALLER Schritte (UML-Entwurf)
    assert set(plan.full_order) == {"puffer_a", "puffer_b", "nachher"}
    assert plan.required_steps == {"puffer_a", "nachher"}
    assert plan.unused_steps == {"puffer_b"}
    assert plan.unused_layers == {"b"}


def test_fa09_plan_loads_layer_right_before_first_consuming_step():
    scenario = _scenario(
        [_file_layer("substations"), _file_layer("power_lines")],
        [
            _buffer("puffer", "substations"),
            {
                "id": "dist",
                "op": "nearest_distance",
                "inputs": {"from": "puffer", "to": "power_lines"},
            },
        ],
        [{"type": "geojson", "source": "dist"}],
    )
    plan = ExecutionPlan.of(scenario)
    assert dict(plan.loads) == {"puffer": ("substations",), "dist": ("power_lines",)}
    assert plan.load_order == ("substations", "power_lines")
    assert plan.final_loads == ()


def test_fa09_plan_layer_used_by_two_steps_is_loaded_once():
    scenario = _scenario(
        [_file_layer("a")],
        [_buffer("p1", "a", 1), _buffer("p2", "a", 2)],
        [
            {"type": "geojson", "source": "p1", "path": "p1.geojson"},
            {"type": "geojson", "source": "p2", "path": "p2.geojson"},
        ],
    )
    plan = ExecutionPlan.of(scenario)
    assert plan.load_order == ("a",)
    assert dict(plan.loads) == {"p1": ("a",)}


def test_fa09_plan_layer_read_only_by_an_output_is_a_final_load():
    """Randfall: ein Output liest einen Layer direkt (kein Schritt braucht ihn).
    Er ist benötigt und wird nach dem letzten Schritt geladen - sonst fande
    der Output ihn nicht im Store."""
    scenario = _scenario(
        [_file_layer("a"), _file_layer("nur_ausgabe")],
        [_buffer("puffer", "a")],
        [
            {"type": "geojson", "source": "puffer", "path": "puffer.geojson"},
            {"type": "geojson", "source": "nur_ausgabe", "path": "roh.geojson"},
        ],
    )
    plan = ExecutionPlan.of(scenario)
    assert plan.final_loads == ("nur_ausgabe",)
    assert plan.required_layers == {"a", "nur_ausgabe"}
    assert plan.load_order == ("a", "nur_ausgabe")


def test_fa09_plan_invariant_required_equals_loaded():
    scenario = _scenario(
        [
            _file_layer("a"),
            _file_layer("b"),
            _file_layer("c"),
            _file_layer("ungenutzt"),
        ],
        [
            _buffer("p", "a"),
            {"id": "d", "op": "nearest_distance", "inputs": {"from": "p", "to": "b"}},
        ],
        [
            {"type": "geojson", "source": "d", "path": "d.geojson"},
            {"type": "geojson", "source": "c", "path": "c.geojson"},
        ],
    )
    plan = ExecutionPlan.of(scenario)
    assert set(plan.load_order) == plan.required_layers
    assert len(plan.load_order) == len(set(plan.load_order))
    assert plan.unused_layers == {"ungenutzt"}
    assert not (set(plan.load_order) & plan.unused_layers)


def test_fa09_scenario_methods_delegate_to_the_plan():
    scenario = _scenario(
        [_file_layer("a"), _file_layer("b")],
        [_buffer("p", "a"), _buffer("q", "b")],
        [{"type": "geojson", "source": "p"}],
    )
    plan = ExecutionPlan.of(scenario)
    assert scenario.execution_order() == list(plan.full_order)
    assert scenario.required_sources() == plan.required()
    assert scenario.load_schedule() == plan.schedule() == {"p": ["a"]}
    assert scenario.required_sources()["unused_layers"] == {"b"}


def test_fa09_plan_is_immutable():
    plan = ExecutionPlan.of(
        _scenario(
            [_file_layer("a")],
            [_buffer("p", "a")],
            [{"type": "geojson", "source": "p"}],
        )
    )
    with pytest.raises(Exception):
        plan.order = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        plan.loads["p"] = ()  # type: ignore[index]


def test_fa09_plan_unregistered_block_fails_before_anything_is_loaded():
    """Vorbedingung: Szenario und Registry passen nicht zusammen (z. B. in einer
    Registry ohne Plugin validiert, mit einer anderen ausgeführt). Der Plan
    meldet das mit Position, bevor ein Layer geladen wird."""
    scenario = _scenario(
        [_file_layer("a")],
        [_buffer("p", "a")],
        [{"type": "geojson", "source": "p"}],
    )
    registry = default_registry()

    class _EmptyOperations:
        def source(self, name):
            return registry.source(name)

        def operation(self, name):
            raise ValueError(f"Unbekannte Operation '{name}'")

        def output(self, name):
            return registry.output(name)

    with pytest.raises(ConfigError, match=r"steps -> 0 \(p\) -> op"):
        ExecutionPlan.of(scenario, _EmptyOperations())  # type: ignore[arg-type]


def test_fa09_topological_order_is_deterministic_and_detects_cycles():
    edges = {"b": {"a"}, "c": {"a"}, "d": {"b", "c"}, "a": {"layer"}}
    assert topological_order(edges, ["a", "b", "c", "d"]) == ["a", "b", "c", "d"]
    with pytest.raises(ValueError, match="Zyklus"):
        topological_order({"x": {"y"}, "y": {"x"}}, ["x", "y"])


# ---------------------------------------------------------------------------
# FA6 - Referenz-Layer der räumlichen Prüfung (spatial_check.within=<layer>)
# ---------------------------------------------------------------------------


def test_fa06_plan_loads_spatial_check_reference_before_the_referencing_layer():
    """Die Prüfung braucht den Referenz-Layer geladen. Der Plan lädt ihn VOR
    dem prüfenden Layer, auch wenn kein Schritt ihn braucht."""
    scenario = _scenario(
        [
            _file_layer("grenze"),
            _file_layer("punkte", validation={"spatial_check": {"within": "grenze"}}),
        ],
        [_buffer("puffer", "punkte")],
        [{"type": "geojson", "source": "puffer"}],
    )
    plan = ExecutionPlan.of(scenario)
    assert dict(plan.loads) == {"puffer": ("grenze", "punkte")}
    assert plan.required_layers == {"grenze", "punkte"}
    assert plan.unused_layers == set()


def test_fa06_plan_reference_already_loaded_by_an_earlier_step_is_not_loaded_twice():
    scenario = _scenario(
        [
            _file_layer("grenze"),
            _file_layer("punkte", validation={"spatial_check": {"within": "grenze"}}),
        ],
        [_buffer("p_grenze", "grenze"), _buffer("p_punkte", "punkte")],
        [
            {"type": "geojson", "source": "p_grenze", "path": "g.geojson"},
            {"type": "geojson", "source": "p_punkte", "path": "p.geojson"},
        ],
    )
    plan = ExecutionPlan.of(scenario)
    assert plan.load_order == ("grenze", "punkte")
    assert plan.required_layers == {"grenze", "punkte"}


def test_fa06_plan_chained_references_are_loaded_dependency_first():
    scenario = _scenario(
        [
            _file_layer("land"),
            _file_layer("kreis", validation={"spatial_check": {"within": "land"}}),
            _file_layer("ort", validation={"spatial_check": {"within": "kreis"}}),
        ],
        [_buffer("puffer", "ort")],
        [{"type": "geojson", "source": "puffer"}],
    )
    plan = ExecutionPlan.of(scenario)
    assert plan.load_order == ("land", "kreis", "ort")


def test_fa06_plan_scenario_region_is_not_a_layer_dependency():
    scenario = _scenario(
        [
            _file_layer(
                "punkte", validation={"spatial_check": {"within": "scenario_region"}}
            )
        ],
        [_buffer("puffer", "punkte")],
        [{"type": "geojson", "source": "puffer"}],
    )
    assert ExecutionPlan.of(scenario).load_order == ("punkte",)


def test_fa06_cyclic_spatial_check_references_are_rejected_at_validation():
    with pytest.raises(ValidationError, match="zyklisch"):
        _scenario(
            [
                _file_layer("a", validation={"spatial_check": {"within": "b"}}),
                _file_layer("b", validation={"spatial_check": {"within": "a"}}),
            ],
            [_buffer("p", "a")],
            [{"type": "geojson", "source": "p"}],
        )


def test_fa06_spatial_check_within_itself_is_rejected_at_validation():
    with pytest.raises(ValidationError, match="sich selbst"):
        _scenario(
            [_file_layer("a", validation={"spatial_check": {"within": "a"}})],
            [_buffer("p", "a")],
            [{"type": "geojson", "source": "p"}],
        )


def test_fa06_spatial_check_reference_must_be_a_vector_layer():
    with pytest.raises(ValidationError, match="Vektor-Layer"):
        _scenario(
            [
                {
                    "id": "raster",
                    "source": "file",
                    "path": "data/r.tif",
                    "data_type": "raster",
                    "format": "tif",
                },
                _file_layer("a", validation={"spatial_check": {"within": "raster"}}),
            ],
            [_buffer("p", "a")],
            [{"type": "geojson", "source": "p"}],
        )
