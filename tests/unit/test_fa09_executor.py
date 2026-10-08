"""Implements: FA9 (DAG-Executor mit Lazy Loading, Plan, Nachbedingung, Abbruch), FA6 (Referenz-Layer).

Contract-Tests für src/geofact/engine/executor.py und engine/loading.py.
Region wird über ein BBox-Literal aufgelöst (region.py, kein Netzzugriff).
"""

import textwrap
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from affine import Affine
from pyproj import CRS
from shapely.geometry import Point, box

from geofact.core.errors import LayerLoadError, RunCancelled, StepExecutionError
from geofact.core.scenario import Scenario
from geofact.core.types import RasterLayer
from geofact.engine import events as ev
from geofact.engine.discovery import discover
from geofact.engine.events import CancelToken
from geofact.engine.executor import run_scenario

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def base_scenario(**overrides) -> dict:
    config = {
        "scenario": {"name": "Test", "region": BBOX_REGION},
        "layers": [
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
        ],
        "steps": [
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 1},
            },
        ],
        "output": [{"type": "map", "source": "catchment"}],
    }
    config.update(overrides)
    return config


def test_fa09_execution_follows_topological_order():
    scenario = Scenario(**base_scenario())
    result = run_scenario(scenario)
    assert result.order == ["catchment"]
    assert "catchment" in result.store
    assert isinstance(result.store["catchment"], gpd.GeoDataFrame)


def test_fa09_base_dir_resolves_relative_layer_paths():
    """base_dir (FA4-Pfadauflösung): run_scenario reicht es bis zum
    Datei-Konnektor durch - ein relativer layer.path in der YAML muss
    unabhängig vom Arbeitsverzeichnis des Prozesses auflösen, wenn der
    Aufrufer (CLI/Web-Backend) das Verzeichnis der Szenario-Datei kennt."""
    scenario = Scenario(
        **base_scenario(
            layers=[
                {
                    "id": "substations",
                    "source": "file",
                    # Relativ statt des sonst üblichen absoluten FIXTURES_DIR-
                    # Pfads - ohne base_dir wäre das relativ zum Prozess-CWD
                    # (pytest-Aufrufverzeichnis) gemeint, nicht zu FIXTURES_DIR.
                    "path": "substations.geojson",
                    "data_type": "vector",
                },
            ],
        )
    )
    result = run_scenario(scenario, base_dir=FIXTURES_DIR)
    assert "catchment" in result.store


def test_fa09_lazy_unused_layer_never_loaded():
    config = base_scenario(
        layers=[
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
            {
                "id": "nie_gebraucht",
                "source": "file",
                "path": "does/not/exist.geojson",  # würde beim Laden fehlschlagen
                "data_type": "vector",
            },
        ],
    )
    scenario = Scenario(**config)
    calls = []

    def tracking_loader(layer):
        calls.append(layer.id)
        return gpd.read_file(layer.path)

    result = run_scenario(
        scenario,
        connector_override={
            "substations": tracking_loader,
            "nie_gebraucht": tracking_loader,
        },
    )
    assert calls == ["substations"]
    assert "nie_gebraucht" not in result.loaded_layers


def test_fa09_third_source_loaded_at_dependent_step():
    config = base_scenario(
        layers=[
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
            {
                "id": "power_lines",
                "source": "file",
                "path": str(FIXTURES_DIR / "power_lines.geojson"),
                "data_type": "vector",
            },
        ],
        steps=[
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 1},
            },
            {
                # zieht die zweite Quelle (power_lines) erst hier, im
                # zweiten Schritt, hinzu - nicht vorher
                "id": "dist",
                "op": "nearest_distance",
                "inputs": {"from": "catchment", "to": "power_lines"},
            },
        ],
        output=[{"type": "map", "source": "dist"}],
    )
    scenario = Scenario(**config)
    load_order = []

    def tracking_loader(layer):
        load_order.append(layer.id)
        return gpd.read_file(layer.path)

    result = run_scenario(
        scenario,
        connector_override={
            "substations": tracking_loader,
            "power_lines": tracking_loader,
        },
    )
    # power_lines wird erst beim zweiten Schritt geladen, nicht vorher
    assert load_order == ["substations", "power_lines"]
    assert result.order == ["catchment", "dist"]


def test_fa09_raster_layers_are_crs_harmonized():
    # Realer Bug: zonal_stats gegen ein Raster im nicht harmonisierten
    # Quell-CRS lieferte NaN, weil nur GeoDataFrames reprojiziert wurden.
    # Zone deckt hier bewusst die volle Ausdehnung der population.tif-
    # Fixture ab (wie test_fa08_zonal_stats_sum_known_raster), damit ein
    # CRS-Mismatch garantiert zu 0 Überlappung führt, statt sich mit
    # einer zufällig knapp daneben liegenden Zone zu vermischen.
    config = base_scenario(
        layers=[
            {
                "id": "region_zone",
                "source": "file",
                "path": str(FIXTURES_DIR / "sachsen_region.geojson"),
                "data_type": "vector",
            },
            {
                "id": "population",
                "source": "file",
                "path": str(FIXTURES_DIR / "population.tif"),
                "data_type": "raster",
                "format": "tif",
            },
        ],
        steps=[
            {
                "id": "affected",
                "op": "zonal_stats",
                "inputs": {"zones": "region_zone", "values": "population"},
                "params": {"stat": "sum"},
            },
        ],
        output=[{"type": "map", "source": "affected"}],
    )
    scenario = Scenario(**config)
    result = run_scenario(scenario)

    affected = result.store["affected"]
    assert affected["sum_value"].notna().any(), (
        "zonal_stats returned all-NaN - zones and raster likely ended up in different CRS"
    )
    assert affected["sum_value"].iloc[0] > 0


def test_fa09_layer_loaded_event_reports_duration():
    """Nachbedingung: layer_loaded-Ereignisse tragen duration_ms, damit die
    Web-Schicht Ladezeit von Schritt-Laufzeit unterscheiden kann (Bugfix:
    zuvor war die Ladezeit eines großen OSM-Layers dem ersten Schritt, der
    ihn braucht, optisch zugeschrieben, obwohl der Schritt selbst schnell ist)."""
    scenario = Scenario(**base_scenario())
    events = []
    run_scenario(scenario, on_event=events.append)

    loaded_events = [e for e in events if e.type == "layer_loaded"]
    assert loaded_events, "kein layer_loaded-Ereignis emittiert"
    for event in loaded_events:
        assert event.duration_ms is not None
        assert event.duration_ms >= 0


def test_fa09_intermediate_results_referencable():
    config = base_scenario(
        steps=[
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 1},
            },
            {
                "id": "catchment2",
                "op": "buffer",
                "inputs": {"geometry": "catchment"},
                "params": {"radius_km": 2},
            },
        ],
        output=[{"type": "map", "source": "catchment2"}],
    )
    scenario = Scenario(**config)
    result = run_scenario(scenario)
    assert "catchment" in result.store
    assert "catchment2" in result.store
    catchment_area = result.store["catchment"].geometry.area.iloc[0]
    catchment2_area = result.store["catchment2"].geometry.area.iloc[0]
    assert catchment2_area > catchment_area


# =====================================================================
# Plan: Layer, die nur ein Output liest; Referenz-Layer der Validierung
# =====================================================================


def test_fa09_layer_read_only_by_an_output_is_loaded_at_the_end():
    """Randfall: ein Output liest einen Layer direkt. Er wird nach dem letzten
    Schritt geladen (final_loads) und steht im Store - bisher fand der Output
    ihn nicht und wurde als 'ohne Ergebnis' übersprungen."""
    config = base_scenario(
        layers=[
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
            {
                "id": "lines",
                "source": "file",
                "path": str(FIXTURES_DIR / "power_lines.geojson"),
                "data_type": "vector",
            },
        ],
        output=[
            {"type": "geojson", "source": "catchment", "path": "a.geojson"},
            {"type": "geojson", "source": "lines", "path": "b.geojson"},
        ],
    )
    events = []
    result = run_scenario(Scenario(**config), on_event=events.append)
    assert result.loaded_layers == ["substations", "lines"]
    assert "lines" in result.store
    loading = [(e.layer, e.step) for e in events if e.type == ev.LAYER_LOADING]
    assert loading == [("substations", "catchment"), ("lines", None)]
    plan_event = next(e for e in events if e.type == ev.PLAN)
    assert plan_event.detail["final_loads"] == ["lines"]


def test_fa09_executed_layers_equal_the_planned_layers():
    scenario = Scenario(**base_scenario())
    result = run_scenario(scenario)
    assert result.plan is not None
    assert set(result.loaded_layers) == result.plan.required_layers
    assert list(result.plan.load_order) == result.loaded_layers


def test_fa06_spatial_check_within_layer_uses_the_loaded_reference_layer():
    """spatial_check.within=<layer>: der Plan lädt den Referenz-Layer vor dem
    prüfenden Layer, die Prüfung vergleicht gegen dessen Geometrie (im
    Arbeits-CRS). Vorher war within=<layer> nie ausführbar."""
    reference = gpd.GeoDataFrame(
        geometry=[box(13.70, 51.03, 13.75, 51.06)], crs="EPSG:4326"
    )
    points = gpd.GeoDataFrame(
        {"name": ["innen", "außen"]},
        geometry=[Point(13.72, 51.04), Point(13.85, 51.10)],
        crs="EPSG:4326",
    )
    config = base_scenario(
        layers=[
            {
                "id": "grenze",
                "source": "file",
                "path": "grenze.geojson",
                "data_type": "vector",
            },
            {
                "id": "punkte",
                "source": "file",
                "path": "punkte.geojson",
                "data_type": "vector",
                "validation": {
                    "spatial_check": {"within": "grenze"},
                    "on_violation": "drop",
                },
            },
        ],
        steps=[
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "punkte"},
                "params": {"radius_km": 0.1},
            }
        ],
    )
    order: list[str] = []

    def load(frame):
        def _load(layer):
            order.append(layer.id)
            return frame.copy()

        return _load

    result = run_scenario(
        Scenario(**config),
        connector_override={"grenze": load(reference), "punkte": load(points)},
    )
    assert order == ["grenze", "punkte"]
    assert list(result.store["punkte"]["name"]) == ["innen"]


# =====================================================================
# Nachbedingung und Fehlerkontext
# =====================================================================

_WRONG_TYPE_PLUGIN = textwrap.dedent("""
    from typing import ClassVar, Literal

    from geofact.plugin_api import DataType, Graph, Step, register_operation


    class WrongTypeStep(Step):
        op: Literal["wrong_type"] = "wrong_type"
        INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
        OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR


    @register_operation(WrongTypeStep)
    def run(inputs, params):
        return Graph(None, None)


    class UnknownTypeStep(Step):
        op: Literal["unknown_type"] = "unknown_type"
        INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
        OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR


    @register_operation(UnknownTypeStep)
    def run_unknown(inputs, params):
        return "kein Layer"
""")


def _plugin_scenario_and_registry(tmp_path, op: str):
    (tmp_path / "typfehler.py").write_text(_WRONG_TYPE_PLUGIN, encoding="utf-8")
    registry = discover(plugin_paths=[tmp_path])
    config = base_scenario(
        steps=[
            {"id": "kaputt", "op": op, "inputs": {"features": "substations"}},
        ],
        output=[{"type": "geojson", "source": "kaputt"}],
    )
    return Scenario.model_validate(config, context={"registry": registry}), registry


@pytest.mark.parametrize(
    "op, detail",
    [
        ("wrong_type", "graph"),  # Graph statt des versprochenen Vektors
        ("unknown_type", "Unbekannter Datentyp"),  # nicht einmal ein Layer-Objekt
    ],
)
def test_fa09_result_type_postcondition_names_step_op_and_origin(tmp_path, op, detail):
    scenario, registry = _plugin_scenario_and_registry(tmp_path, op)
    events = []
    with pytest.raises(StepExecutionError) as excinfo:
        run_scenario(scenario, registry=registry, on_event=events.append)
    error = excinfo.value
    assert error.step_id == "kaputt" and error.op == op
    assert "typfehler.py" in error.origin
    assert "kaputt" in str(error) and op in str(error) and detail in str(error)
    assert isinstance(error.__cause__, TypeError)
    assert any(e.type == ev.STEP_ERROR and e.step == "kaputt" for e in events)
    run_error = next(e for e in events if e.type == ev.RUN_ERROR)
    assert "kaputt" in run_error.error


def test_fa09_step_failure_is_wrapped_with_the_original_cause():
    config = base_scenario(
        steps=[
            {
                "id": "bad",
                "op": "ranking",
                "inputs": {"features": "substations"},
                "params": {"by": ["does_not_exist"], "weights": [1.0]},
            },
        ],
        output=[{"type": "geojson", "source": "bad"}],
    )
    with pytest.raises(StepExecutionError) as excinfo:
        run_scenario(Scenario(**config))
    error = excinfo.value
    assert (error.step_id, error.op) == ("bad", "ranking")
    assert "builtin.operations.ranking" in error.origin
    assert isinstance(error.__cause__, KeyError)


def test_fa09_layer_load_failure_is_wrapped_and_reported():
    config = base_scenario(
        layers=[
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "gibt_es_nicht.geojson"),
                "data_type": "vector",
            },
        ]
    )
    events = []
    with pytest.raises(LayerLoadError) as excinfo:
        run_scenario(Scenario(**config), on_event=events.append)
    error = excinfo.value
    assert error.layer_id == "substations" and error.source == "file"
    assert "substations" in str(error) and "file" in str(error)
    assert error.__cause__ is not None
    layer_error = next(e for e in events if e.type == ev.LAYER_ERROR)
    assert layer_error.layer == "substations" and layer_error.step == "catchment"
    assert any(e.type == ev.RUN_ERROR for e in events)
    # der Schritt lief nie
    assert not any(e.type == ev.STEP_RUNNING for e in events)


def test_fa09_connector_returning_the_wrong_data_type_is_rejected():
    """Nachbedingung des Ladens: der Datentyp entspricht dem deklarierten."""
    scenario = Scenario(**base_scenario())
    raster = RasterLayer(
        data=np.zeros((2, 2)), transform=Affine.identity(), crs=CRS.from_epsg(4326)
    )
    with pytest.raises(
        LayerLoadError, match="lieferte raster, deklariert ist data_type: vector"
    ):
        run_scenario(scenario, connector_override={"substations": lambda layer: raster})
    with pytest.raises(LayerLoadError, match="Unbekannter Datentyp"):
        run_scenario(
            scenario, connector_override={"substations": lambda layer: "kein Layer"}
        )


# =====================================================================
# Abbruch
# =====================================================================


def _two_step_scenario() -> Scenario:
    return Scenario(
        **base_scenario(
            steps=[
                {
                    "id": "s1",
                    "op": "buffer",
                    "inputs": {"geometry": "substations"},
                    "params": {"radius_km": 1},
                },
                {
                    "id": "s2",
                    "op": "buffer",
                    "inputs": {"geometry": "s1"},
                    "params": {"radius_km": 1},
                },
            ],
            output=[{"type": "geojson", "source": "s2"}],
        )
    )


def test_fa09_cancel_token_stops_the_run_at_the_next_step_boundary():
    token = CancelToken()
    events = []

    def observe(event):
        events.append(event)
        if event.type == ev.STEP_DONE and event.step == "s1":
            token.cancel()

    with pytest.raises(RunCancelled):
        run_scenario(_two_step_scenario(), on_event=observe, cancel=token)
    assert [e.step for e in events if e.type == ev.STEP_DONE] == ["s1"]
    assert not any(e.type == ev.STEP_RUNNING and e.step == "s2" for e in events)
    assert events[-1].type == ev.RUN_CANCELLED


def test_fa09_cancel_token_set_before_the_run_loads_nothing():
    token = CancelToken()
    token.cancel()
    events = []
    with pytest.raises(RunCancelled):
        run_scenario(_two_step_scenario(), on_event=events.append, cancel=token)
    # REGION_READY kommt vor dem Plan, seit run_scenario die Region beschreibt (FA55)
    assert [e.type for e in events] == [ev.REGION_READY, ev.PLAN, ev.RUN_CANCELLED]


def test_fa09_run_without_cancel_request_completes():
    result = run_scenario(_two_step_scenario(), cancel=CancelToken())
    assert result.order == ["s1", "s2"]


# =====================================================================
# Warnungen
# =====================================================================


def test_fa09_layer_load_warnings_become_layer_warning_events():
    def noisy(layer):
        warnings.warn("Quelle liefert gerundete Werte", UserWarning)
        return gpd.read_file(FIXTURES_DIR / "substations.geojson")

    events = []
    result = run_scenario(
        Scenario(**base_scenario()),
        connector_override={"substations": noisy},
        on_event=events.append,
    )
    layer_warnings = [e for e in events if e.type == ev.LAYER_WARNING]
    assert [(e.layer, e.step, e.message) for e in layer_warnings] == [
        ("substations", "catchment", "Quelle liefert gerundete Werte")
    ]
    assert layer_warnings[0].detail["category"] == "UserWarning"
    assert [(w.layer, w.message) for w in result.warnings] == [
        ("substations", "Quelle liefert gerundete Werte")
    ]
    # LAYER_WARNING kommt vor LAYER_LOADED desselben Layers
    types = [e.type for e in events]
    assert types.index(ev.LAYER_WARNING) < types.index(ev.LAYER_LOADED)


def test_fa09_step_warnings_are_collected_in_the_result():
    config = base_scenario(
        layers=[
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
            {
                "id": "power_lines",
                "source": "file",
                "path": str(FIXTURES_DIR / "power_lines.geojson"),
                "data_type": "vector",
            },
        ],
        steps=[
            {
                "id": "grid",
                "op": "build_network",
                "inputs": {"lines": "power_lines", "nodes": "substations"},
                "params": {"tolerance_m": 500},
            },
            {"id": "nodes", "op": "graph_to_vector", "inputs": {"graph": "grid"}},
        ],
        output=[{"type": "geojson", "source": "nodes"}],
    )
    result = run_scenario(Scenario(**config))
    step_warnings = [w for w in result.warnings if w.step == "grid"]
    assert step_warnings and step_warnings[0].category


def test_fa09_warning_of_a_step_that_then_fails_is_still_reported(tmp_path):
    """Vorher ging die Warnung verloren, wenn der Schritt danach scheiterte."""
    (tmp_path / "warnt.py").write_text(
        textwrap.dedent("""
        import warnings
        from typing import ClassVar, Literal

        from geofact.plugin_api import DataType, Step, register_operation


        class WarnThenFailStep(Step):
            op: Literal["warn_then_fail"] = "warn_then_fail"
            INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
            OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR


        @register_operation(WarnThenFailStep)
        def run(inputs, params):
            warnings.warn("Vorwarnung vor dem Fehler", UserWarning)
            raise ValueError("danach kaputt")
    """),
        encoding="utf-8",
    )
    registry = discover(plugin_paths=[tmp_path])
    config = base_scenario(
        steps=[
            {
                "id": "bad",
                "op": "warn_then_fail",
                "inputs": {"features": "substations"},
            },
        ],
        output=[{"type": "geojson", "source": "bad"}],
    )
    scenario = Scenario.model_validate(config, context={"registry": registry})
    events = []
    with pytest.raises(StepExecutionError, match="danach kaputt"):
        run_scenario(scenario, registry=registry, on_event=events.append)
    warned = [e for e in events if e.type == ev.STEP_WARNING]
    assert [e.message for e in warned] == ["Vorwarnung vor dem Fehler"]
    # Reihenfolge: erst die Warnung, dann Fehler und Lauf-Ende
    types = [e.type for e in events]
    assert (
        types.index(ev.STEP_WARNING)
        < types.index(ev.STEP_ERROR)
        < types.index(ev.RUN_ERROR)
    )
