"""Implements: FA66 (Freigabe von Zwischenergebnissen nach dem letzten Leser, Executor und Run).

Contract-Tests für ``engine/executor.py::execute(release_intermediates=True)``
und ``engine/run.py::run(release_intermediates=...)``: Knoten aus
``plan.releases`` verlassen nach ihrem letzten Leser den Store und werden in
``step_done.detail["released"]`` und ``ExecutionResult.released`` genannt; alle
deklarierten Ausgaben werden trotzdem geschrieben; zusammen mit
``dump_intermediate`` ist die Freigabe ein Fehler; bei einem breiten DAG ist der
Spitzenspeicher kleiner.
"""

from __future__ import annotations

import pytest

from _executor_doubles import BBOX_REGION, file_layer
from geofact.core.store import LayerStore
from geofact.engine import events as ev
from geofact.engine.run import load_scenario, run


def _chain_config() -> dict:
    return {
        "scenario": {"name": "Kette", "region": BBOX_REGION},
        "layers": [file_layer("substations")],
        "steps": [
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
        "output": [{"type": "geojson", "source": "s2"}],
    }


def _wide_config(branches: int = 4) -> dict:
    steps = []
    for i in range(branches):
        steps.append(
            {
                "id": f"b{i}",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 1},
            }
        )
        steps.append(
            {
                "id": f"c{i}",
                "op": "buffer",
                "inputs": {"geometry": f"b{i}"},
                "params": {"radius_km": 1},
            }
        )
    steps.append(
        {
            "id": "alle",
            "op": "collect",
            "inputs": {f"z{i}": f"c{i}" for i in range(branches)},
        }
    )
    return {
        "scenario": {"name": "Breit", "region": BBOX_REGION},
        "layers": [file_layer("substations")],
        "steps": steps,
        "output": [{"type": "geojson", "source": "alle"}],
    }


def test_fa66_executor_drops_released_nodes_from_the_store_and_reports_them():
    store = LayerStore()
    events: list = []
    report = run(
        load_scenario(_chain_config()),
        store=store,
        observer=events.append,
        release_intermediates=True,
    )
    assert report.result.released == ["substations", "s1"]
    assert "s2" in store and "s1" not in store and "substations" not in store
    assert store.released == {"substations", "s1"}
    done = {e.step: e.detail.get("released") for e in events if e.type == ev.STEP_DONE}
    assert done == {"s1": ["substations"], "s2": ["s1"]}


def test_fa66_without_flag_nothing_is_released():
    store = LayerStore()
    events: list = []
    report = run(load_scenario(_chain_config()), store=store, observer=events.append)
    assert report.result.released == []
    assert all(node in store for node in ("substations", "s1", "s2"))
    assert all("released" not in e.detail for e in events if e.type == ev.STEP_DONE)


def test_fa66_released_run_still_writes_all_declared_outputs(tmp_path):
    config = _chain_config()
    config["output"] = [
        {"type": "geojson", "source": "s2", "path": "ende.geojson"},
        {"type": "geojson", "source": "s1", "path": "zwischen.geojson"},
        {"type": "geojson", "source": "substations", "path": "roh.geojson"},
    ]
    report = run(load_scenario(config), out_dir=tmp_path, release_intermediates=True)
    assert report.outputs is not None and not report.outputs.skipped
    assert len(report.outputs.written) == 3
    for name in ("ende.geojson", "zwischen.geojson", "roh.geojson"):
        assert (tmp_path / name).is_file()
    # Quellen von Ausgaben werden nie freigegeben.
    assert not {"s1", "s2", "substations"} & set(report.result.released)


def test_fa66_release_with_dump_intermediate_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="dump_intermediate"):
        run(
            load_scenario(_chain_config()),
            dump_intermediate=tmp_path,
            release_intermediates=True,
        )


def _peak_store_size(release: bool) -> int:
    store = LayerStore()
    sizes: list[int] = []

    def observe(event) -> None:
        if event.type == ev.STEP_DONE:
            sizes.append(len(store))

    run(
        load_scenario(_wide_config()),
        store=store,
        observer=observe,
        release_intermediates=release,
    )
    return max(sizes)


def test_fa66_peak_store_size_of_a_wide_dag_is_smaller_with_release():
    without = _peak_store_size(release=False)
    with_release = _peak_store_size(release=True)
    assert without == 10  # Layer + 9 Schritte
    assert with_release < without
    assert with_release <= 6
