"""Implements: FA66 (Freigabe von Zwischenergebnissen: Plan und Store).

Contract-Tests für ``ExecutionPlan.of(release_intermediates=True)``
(tiefensuchende Reihenfolge, ``releases`` = letzter Leser, Invariante) und
``LayerStore.pop``. Die Freigabe im Executor folgt in Welle 2 (W2-02)."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest

from geofact import api
from geofact.core.plan import ExecutionPlan
from geofact.core.scenario import Scenario
from geofact.core.store import LayerStore

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
PLUGIN_DEMO_DIR = EXAMPLES_DIR / "plugin_demo"
BBOX = "12.2,51.2,12.5,51.4"


def _layer(layer_id: str, **extra) -> dict:
    return {
        "id": layer_id,
        "source": "file",
        "path": f"data/{layer_id}.geojson",
        "data_type": "vector",
        **extra,
    }


def _buffer(step_id: str, source: str) -> dict:
    return {
        "id": step_id,
        "op": "buffer",
        "inputs": {"geometry": source},
        "params": {"radius_km": 1},
    }


def _join(step_id: str, left: str, right: str) -> dict:
    return {
        "id": step_id,
        "op": "nearest_distance",
        "inputs": {"from": left, "to": right},
    }


def _scenario(layers, steps, output) -> Scenario:
    return Scenario(
        **{
            "scenario": {"name": "Freigabe", "region": BBOX},
            "layers": layers,
            "steps": steps,
            "output": output,
        }
    )


def _two_branches() -> Scenario:
    """Zwei Zweige a1 -> a2 und b1 -> b2, vereinigt in c."""
    return _scenario(
        [_layer("la"), _layer("lb")],
        [
            _buffer("a1", "la"),
            _buffer("b1", "lb"),
            _buffer("a2", "a1"),
            _buffer("b2", "b1"),
            _join("c", "a2", "b2"),
        ],
        [{"type": "geojson", "source": "c"}],
    )


def test_fa66_default_plan_is_unchanged():
    scenario = _two_branches()
    plan = ExecutionPlan.of(scenario)
    assert plan.order == ("a1", "b1", "a2", "b2", "c")  # Kahn, alphabetisch
    assert dict(plan.releases) == {}
    assert (
        plan.full_order
        == ExecutionPlan.of(scenario, release_intermediates=True).full_order
    )


def test_fa66_depth_first_order_finishes_a_branch_first():
    plan = ExecutionPlan.of(_two_branches(), release_intermediates=True)
    assert plan.order == ("a1", "a2", "b1", "b2", "c")
    assert dict(plan.loads) == {"a1": ("la",), "b1": ("lb",)}


def test_fa66_depth_first_order_is_topological_and_deterministic():
    scenario = _two_branches()
    orders = {
        ExecutionPlan.of(scenario, release_intermediates=True).order for _ in range(5)
    }
    assert len(orders) == 1
    order = orders.pop()
    step_by_id = {s.id: s for s in scenario.steps}
    for index, step_id in enumerate(order):
        for source in step_by_id[step_id].inputs.values():
            if source in step_by_id:
                assert order.index(source) < index


def test_fa66_releases_name_the_last_reader():
    plan = ExecutionPlan.of(_two_branches(), release_intermediates=True)
    assert dict(plan.releases) == {
        "a1": ("la",),
        "a2": ("a1",),
        "b1": ("lb",),
        "b2": ("b1",),
        "c": ("a2", "b2"),
    }


def test_fa66_output_sources_are_never_released():
    scenario = _scenario(
        [_layer("la")],
        [_buffer("a1", "la"), _buffer("a2", "a1")],
        [
            {"type": "geojson", "source": "a2", "path": "a2.geojson"},
            {"type": "geojson", "source": "a1", "path": "a1.geojson"},
            {"type": "geojson", "source": "la", "path": "la.geojson"},
        ],
    )
    plan = ExecutionPlan.of(scenario, release_intermediates=True)
    released = {node for nodes in plan.releases.values() for node in nodes}
    assert released.isdisjoint({"a1", "a2", "la"})


def test_fa66_node_read_twice_is_released_after_the_later_reader():
    scenario = _scenario(
        [_layer("la"), _layer("lb")],
        [_buffer("p", "la"), _join("q", "p", "lb"), _join("r", "q", "la")],
        [{"type": "geojson", "source": "r"}],
    )
    plan = ExecutionPlan.of(scenario, release_intermediates=True)
    assert plan.order == ("p", "q", "r")
    assert dict(plan.releases) == {"q": ("lb", "p"), "r": ("la", "q")}


def test_fa66_spatial_check_reference_is_released_after_its_step():
    check = {"spatial_check": {"within": "grenze"}}
    scenario = _scenario(
        [_layer("grenze"), _layer("punkte", validation=check), _layer("lc")],
        [_buffer("p", "punkte"), _join("q", "p", "lc")],
        [{"type": "geojson", "source": "q"}],
    )
    plan = ExecutionPlan.of(scenario, release_intermediates=True)
    assert dict(plan.loads) == {"p": ("grenze", "punkte"), "q": ("lc",)}
    assert "grenze" in plan.releases["p"]
    assert set(plan.releases["p"]) == {"grenze", "punkte"}


def test_fa66_reference_of_a_final_load_is_kept():
    check = {"spatial_check": {"within": "grenze"}}
    scenario = _scenario(
        [_layer("grenze"), _layer("punkte", validation=check), _layer("la")],
        [_buffer("p", "la")],
        [
            {"type": "geojson", "source": "p", "path": "p.geojson"},
            {"type": "geojson", "source": "punkte", "path": "punkte.geojson"},
        ],
    )
    plan = ExecutionPlan.of(scenario, release_intermediates=True)
    released = {node for nodes in plan.releases.values() for node in nodes}
    assert "grenze" not in released and "punkte" not in released
    assert dict(plan.releases) == {"p": ("la",)}


@pytest.mark.parametrize(
    "path",
    sorted(EXAMPLES_DIR.rglob("*.yaml")),
    ids=lambda p: p.relative_to(EXAMPLES_DIR).as_posix(),
)
def test_fa66_every_example_plans_with_release(path):
    # plugin demo and the domain plugin of scenario 1 (FA94)
    needs_plugins = (
        PLUGIN_DEMO_DIR in path.parents or path.stem == "sachsen_netz_resilienz"
    )
    registry = (
        api.discover(
            plugin_paths=[PLUGIN_DEMO_DIR / "plugins", EXAMPLES_DIR / "plugins"]
        )
        if needs_plugins
        else None
    )
    scenario = api.load_scenario(path, registry=registry).scenario
    default = ExecutionPlan.of(scenario)
    released = ExecutionPlan.of(scenario, release_intermediates=True)
    assert set(released.order) == set(default.order)
    assert released.required_layers == default.required_layers
    sources = {out.source for out in scenario.output}
    assert sources.isdisjoint(n for nodes in released.releases.values() for n in nodes)


# ---------------------------------------------------------------- LayerStore.pop


def test_fa66_layer_store_pop_removes_and_remembers():
    store = LayerStore()
    data = gpd.GeoDataFrame(geometry=[], crs="EPSG:25833")
    store["a"] = data
    assert store.pop("a") is data
    assert "a" not in store and store.released == {"a"}
    with pytest.raises(KeyError, match="nach dem letzten Leser freigegeben"):
        store["a"]
    store["a"] = data
    assert store.released == set()


def test_fa66_layer_store_pop_unknown_is_key_error():
    with pytest.raises(KeyError, match="Layer 'x' ist nicht geladen"):
        LayerStore().pop("x")
