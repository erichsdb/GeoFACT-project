"""Implements: FA55 (Arbeits-CRS im Lauf sichtbar: Ereignis ``region_ready``), FA56 (Regionswarnungen).

Contract-Tests für ``engine/executor.py::execute(target_crs=, region_info=)``
und den Anschluss in ``engine/run.py::run``: mit ``region_info`` meldet der
Executor vor dem Plan ``REGION_READY`` (crs, crs_mode, bbox) und je Warnung
``REGION_WARNING`` + ``RunWarning("RegionWarning")``; das Arbeits-CRS gelangt in
den LoadContext. Ohne Angabe bleibt alles wie FA5 (UTM-Zone der Region, kein
neues Ereignis). ``RegionInfo``/``describe_region`` (W2-01) sind gestubbt.
"""

from __future__ import annotations

import pytest
from pyproj import CRS

from _executor_doubles import (
    BBOX_REGION,
    FakeRegionInfo,
    fake_loader,
    file_layer,
    points,
)
from geofact.core.errors import ConfigError
from geofact.core.plan import ExecutionPlan
from geofact.core.registry import default_registry
from geofact.core.scenario import Scenario
from geofact.engine import events as ev
from geofact.engine import executor as executor_module
from geofact.engine import region as region_module
from geofact.engine.executor import execute
from geofact.engine.run import load_scenario, run


def _config() -> dict:
    return {
        "scenario": {"name": "Region", "region": BBOX_REGION},
        "layers": [file_layer("substations")],
        "steps": [
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 1},
            }
        ],
        "output": [{"type": "geojson", "source": "catchment"}],
    }


def test_fa55_region_ready_event_carries_crs_mode_and_bbox(monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        executor_module,
        "load_layer",
        fake_loader(
            {"substations": points("EPSG:25833")},
            record=seen,
        ),
    )
    scenario = Scenario(**_config())
    plan = ExecutionPlan.of(scenario, default_registry())
    info = FakeRegionInfo(
        crs=CRS.from_epsg(25833), warnings=["Region breiter als eine UTM-Zone"]
    )
    events: list = []
    result = execute(
        scenario,
        plan,
        registry=default_registry(),
        region=region_module.resolve_region(BBOX_REGION),
        observer=events.append,
        target_crs=info.crs,
        region_info=info,
    )
    types = [e.type for e in events]
    assert (
        types.index(ev.REGION_READY)
        < types.index(ev.REGION_WARNING)
        < types.index(ev.PLAN)
    )
    ready = next(e for e in events if e.type == ev.REGION_READY)
    assert ready.detail["crs_mode"] == "declared"
    assert ready.detail["bbox"] == [13.60, 51.01, 13.94, 51.15]
    assert ready.detail["crs"] == "EPSG:25833"
    warning = next(e for e in events if e.type == ev.REGION_WARNING)
    assert warning.message == "Region breiter als eine UTM-Zone"
    assert [w.category for w in result.warnings] == ["RegionWarning"]
    assert result.region is info
    assert seen[0][1].target_crs == CRS.from_epsg(25833)


def test_fa55_without_target_crs_the_run_is_unchanged(monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        executor_module,
        "load_layer",
        fake_loader(
            {"substations": points("EPSG:32633")},
            record=seen,
        ),
    )
    scenario = Scenario(**_config())
    events: list = []
    result = execute(
        scenario,
        ExecutionPlan.of(scenario),
        registry=default_registry(),
        region=region_module.resolve_region(BBOX_REGION),
        observer=events.append,
    )
    assert ev.REGION_READY not in [e.type for e in events]
    assert events[0].type == ev.PLAN
    assert seen[0][1].target_crs == CRS.from_epsg(32633)  # FA5: UTM-Zone 33N
    assert result.region is None and result.warnings == []


def test_fa55_run_derives_the_working_crs_from_describe_region(monkeypatch):
    calls: list = []

    def describe_region(region, spec, meta=None):
        calls.append((spec, meta))
        return FakeRegionInfo(crs=CRS.from_epsg(25833), crs_mode="auto", crs_spec=spec)

    monkeypatch.setattr(
        region_module, "describe_region", describe_region, raising=False
    )
    monkeypatch.setattr(
        executor_module,
        "load_layer",
        fake_loader(
            {"substations": points("EPSG:25833")},
        ),
    )
    events: list = []
    report = run(load_scenario(_config()), observer=events.append)
    assert calls and calls[0][0] == "auto"
    assert report.region is not None and report.region.crs == CRS.from_epsg(25833)
    ready = next(e for e in events if e.type == ev.REGION_READY)
    assert ready.detail["crs_mode"] == "auto"


def test_fa55_region_not_mappable_is_a_config_error_before_loading(monkeypatch):
    def describe_region(region, spec, meta=None):
        raise ValueError(
            "Region ist im Arbeits-CRS nicht abbildbar - scenario.crs: equal_area"
        )

    monkeypatch.setattr(
        region_module, "describe_region", describe_region, raising=False
    )
    events: list = []
    with pytest.raises(ConfigError) as excinfo:
        run(load_scenario(_config()), observer=events.append)
    location, message = excinfo.value.issues[0]
    assert location == "scenario -> region"
    assert "equal_area" in message
    assert events == []


def test_fa55_run_scenario_uses_the_declared_working_crs(monkeypatch):
    """Nachtreview 13: api.run_scenario ignored scenario.crs and computed in UTM."""
    from geofact.engine.executor import run_scenario

    seen: list = []
    monkeypatch.setattr(
        executor_module,
        "load_layer",
        fake_loader(
            {"substations": points("EPSG:3035")},
            record=seen,
        ),
    )
    config = _config()
    config["scenario"]["crs"] = "EPSG:3035"
    events: list = []
    result = run_scenario(Scenario(**config), on_event=events.append)
    assert seen[0][1].target_crs == CRS.from_epsg(3035)
    assert result.region is not None and result.region.crs == CRS.from_epsg(3035)
    assert ev.REGION_READY in [e.type for e in events]


def test_fa56_run_scenario_checks_the_region_like_run():
    from geofact.engine.executor import run_scenario

    config = _config()
    config["scenario"]["region"] = (
        "10,85,11,86"  # jenseits 84 Grad: UTM nicht definiert
    )
    with pytest.raises(ValueError, match="84"):
        run_scenario(Scenario(**config))
