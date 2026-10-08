"""Implements: FA12 (Ausgaben schreiben), FA2-Zusatz (EINE Dateinamensregel: OutputSpec.target_name).

engine/outputs.py benennt jede Ausgabe über ``OutputSpec.target_name`` -
dieselbe Regel, mit der ``validate_dag`` doppelte Dateinamen ablehnt."""

from __future__ import annotations

from _loading_helpers import DRESDEN_BBOX, FIXTURES_DIR
from geofact.core.registry import default_registry
from geofact.core.scenario import OutputSpec, Scenario
from geofact.engine.executor import run_scenario
from geofact.engine.outputs import write_outputs


def _scenario(outputs: list[dict]) -> Scenario:
    return Scenario.model_validate(
        {
            "scenario": {"name": "Namen", "region": DRESDEN_BBOX},
            "layers": [
                {
                    "id": "umspannwerke",
                    "source": "file",
                    "data_type": "vector",
                    "path": str(FIXTURES_DIR / "substations.geojson"),
                }
            ],
            "steps": [
                {
                    "id": "puffer",
                    "op": "buffer",
                    "inputs": {"geometry": "umspannwerke"},
                    "params": {"radius_km": 1},
                }
            ],
            "output": outputs,
        }
    )


def test_fa12_write_outputs_uses_target_name(tmp_path, monkeypatch):
    scenario = _scenario([{"type": "geojson", "source": "puffer"}])
    result = run_scenario(scenario, registry=default_registry())
    calls: list[tuple[int, str]] = []
    original = OutputSpec.target_name

    def spy(self, index: int, extension: str) -> str:
        calls.append((index, extension))
        return original(self, index, extension)

    monkeypatch.setattr(OutputSpec, "target_name", spy)

    outcome = write_outputs(scenario, result, tmp_path)

    assert calls == [(0, "geojson")]
    assert [p.name for p in outcome.written] == ["00_puffer.geojson"]


def test_fa12_written_names_match_the_validated_names(tmp_path):
    outputs = [
        {"type": "geojson", "source": "puffer"},
        {"type": "csv", "source": "puffer", "path": "unterordner/puffer.csv"},
    ]
    scenario = _scenario(outputs)
    result = run_scenario(scenario, registry=default_registry())

    outcome = write_outputs(scenario, result, tmp_path)

    expected = [
        spec.target_name(i, ext)
        for i, (spec, ext) in enumerate(zip(scenario.output, ("geojson", "csv")))
    ]
    assert (
        [p.name for p in outcome.written]
        == expected
        == ["00_puffer.geojson", "puffer.csv"]
    )
