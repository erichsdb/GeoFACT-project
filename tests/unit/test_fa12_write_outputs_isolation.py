"""Implements: FA12 (Ausgaben schreiben: jede Ausgabe für sich), FA2 (data_type_of meldet unbekannte Objekte).

Contract-Tests für src/geofact/engine/outputs.py: scheitert EIN Schreiber mit
irgendeiner Ausnahme, wird genau diese Ausgabe als übersprungen mit Grund
gemeldet; die übrigen Ausgaben werden geschrieben, eine halb geschriebene
Datei bleibt nicht liegen.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from geofact.core.scenario import Scenario
from geofact.engine.discovery import discover
from geofact.engine.executor import run_scenario
from geofact.engine.outputs import write_outputs

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"

_FAILING_WRITER_PLUGIN = textwrap.dedent("""
    from geofact.plugin_api import register_output
    from geofact.core.types import DataType


    @register_output("platte_voll", extension="txt", accepts=(DataType.VECTOR,))
    def write_failing(data, target, spec):
        target.write_text("halb geschrieben", encoding="utf-8")
        raise OSError("Platte voll")


    @register_output("kaputt_im_plugin", extension="txt", accepts=(DataType.VECTOR,))
    def write_buggy(data, target, spec):
        return data.gibt_es_nicht
""")


@pytest.fixture()
def registry(tmp_path_factory):
    plugins = tmp_path_factory.mktemp("plugins")
    (plugins / "schreiber.py").write_text(_FAILING_WRITER_PLUGIN, encoding="utf-8")
    return discover(plugin_paths=[plugins])


def _scenario(registry, outputs: list[dict]) -> Scenario:
    config = {
        "scenario": {"name": "Ausgaben", "region": BBOX_REGION},
        "layers": [
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            }
        ],
        "steps": [
            {
                "id": "puffer",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 1},
            }
        ],
        "output": outputs,
    }
    return Scenario.model_validate(config, context={"registry": registry})


def test_fa12_failing_writer_skips_only_its_own_output(registry, tmp_path):
    scenario = _scenario(
        registry,
        [
            {"type": "geojson", "source": "puffer", "path": "gut.geojson"},
            {"type": "platte_voll", "source": "puffer", "path": "voll.txt"},
            {"type": "csv", "source": "puffer", "path": "gut.csv"},
        ],
    )
    result = run_scenario(scenario, registry=registry)

    outcome = write_outputs(scenario, result, tmp_path, registry=registry)

    assert {p.name for p in outcome.written} == {"gut.geojson", "gut.csv"}
    assert len(outcome.skipped) == 1
    skipped = outcome.skipped[0]
    assert (skipped.index, skipped.source, skipped.type) == (1, "puffer", "platte_voll")
    assert "OSError" in skipped.reason and "Platte voll" in skipped.reason
    # keine halb geschriebene Datei bleibt liegen
    assert not (tmp_path / "voll.txt").exists()


def test_fa12_any_exception_type_from_a_writer_is_isolated(registry, tmp_path):
    scenario = _scenario(
        registry,
        [
            {"type": "kaputt_im_plugin", "source": "puffer", "path": "kaputt.txt"},
            {"type": "geojson", "source": "puffer", "path": "gut.geojson"},
        ],
    )
    result = run_scenario(scenario, registry=registry)

    outcome = write_outputs(scenario, result, tmp_path, registry=registry)

    assert [p.name for p in outcome.written] == ["gut.geojson"]
    assert "AttributeError" in outcome.skipped[0].reason
    assert outcome.skipped[0].type == "kaputt_im_plugin"


def test_fa12_unknown_object_in_the_store_is_skipped_with_reason(registry, tmp_path):
    """data_type_of meldet ein unbekanntes Objekt als TypeError (statt still
    'graph' anzunehmen); write_outputs macht daraus eine übersprungene Ausgabe."""
    scenario = _scenario(
        registry,
        [
            {"type": "geojson", "source": "puffer", "path": "puffer.geojson"},
            {"type": "geojson", "source": "substations", "path": "roh.geojson"},
        ],
    )
    result = run_scenario(scenario, registry=registry)
    result.store["puffer"] = (
        "kein Layer"  # ein fehlerhafter Baustein hat Unsinn abgelegt
    )

    outcome = write_outputs(scenario, result, tmp_path, registry=registry)

    assert [p.name for p in outcome.written] == ["roh.geojson"]
    assert len(outcome.skipped) == 1
    assert outcome.skipped[0].source == "puffer"
    assert "Unbekannter Datentyp" in outcome.skipped[0].reason


def test_fa12_all_writers_succeeding_reports_nothing_skipped(registry, tmp_path):
    scenario = _scenario(
        registry, [{"type": "geojson", "source": "puffer", "path": "a.geojson"}]
    )
    result = run_scenario(scenario, registry=registry)
    outcome = write_outputs(scenario, result, tmp_path, registry=registry)
    assert [p.name for p in outcome.written] == ["a.geojson"]
    assert outcome.skipped == []
