"""Implements: FA65 (Lizenzen und Namensnennung: Ereignisse und RunReport).

Contract-Tests für ``engine/executor.py`` und ``engine/run.py``: das
``plan``-Ereignis trägt die Attribution je Knoten (``plan.attribution``), das
``run_done``-Ereignis und ``RunReport.attribution`` die Vereinigung der
Ausgabequellen - Layer ohne Lizenz stehen unter ``undeclared``.
"""

from __future__ import annotations

from _executor_doubles import BBOX_REGION, file_layer
from geofact.engine import events as ev
from geofact.engine.run import load_scenario, run


def _config() -> dict:
    return {
        "scenario": {"name": "Lizenzen", "region": BBOX_REGION},
        "layers": [
            file_layer(
                "offen", license={"name": "CC0-1.0", "attribution": "Testdaten GeoFACT"}
            ),
            file_layer("ohne"),
            file_layer("unbenutzt", license={"name": "ODbL-1.0"}),
        ],
        "steps": [
            {
                "id": "verknüpft",
                "op": "spatial_join",
                "inputs": {"left": "offen", "right": "ohne"},
                "params": {"predicate": "intersects"},
            }
        ],
        "output": [{"type": "geojson", "source": "verknüpft"}],
    }


def test_fa65_plan_and_run_done_events_carry_the_attribution():
    events: list = []
    run(load_scenario(_config()), observer=events.append)
    plan_event = next(e for e in events if e.type == ev.PLAN)
    attribution = plan_event.detail["attribution"]
    assert set(attribution) == {
        "offen",
        "ohne",
        "verknüpft",
    }  # nur Benötigtes (Lazy Loading)
    step = attribution["verknüpft"]
    assert [lic["name"] for lic in step["licenses"]] == ["CC0-1.0"]
    assert step["undeclared"] == ["ohne"]
    assert any("Testdaten GeoFACT" in line for line in step["lines"])

    done = next(e for e in events if e.type == ev.RUN_DONE)
    assert done.detail["attribution"] == step


def test_fa65_run_report_names_the_licences(tmp_path):
    report = run(load_scenario(_config()), out_dir=tmp_path)
    assert [lic.name for lic in report.attribution.licenses] == ["CC0-1.0"]
    assert report.attribution.undeclared == ("ohne",)
    # ATTRIBUTION.txt schreibt engine/outputs.py (W2-04); bis dahin None.
    assert report.attribution_file is None or report.attribution_file.is_file()


def test_fa65_run_without_any_licence_reports_an_empty_attribution():
    config = _config()
    config["layers"][0].pop("license")
    report = run(load_scenario(config))
    assert report.attribution.empty
    assert set(report.attribution.undeclared) == {"offen", "ohne"}
