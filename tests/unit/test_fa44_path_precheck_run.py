"""Implements: FA44 (Vorab-Check der Layer-Pfade in ``run()``), FA4 (fehlende Datei mit Position).

Contract-Tests für ``engine/run.py::run``: ``check_before_run(base_dir)`` jedes
BENOETIGTEN Layers läuft nach dem Plan und vor Region (Netz) und erstem
Ladevorgang; eine fehlende Datei ist ein ``ConfigError`` an
``layers -> i (id) -> path``. ``validate`` prüft keine Pfade.
"""

from __future__ import annotations

import pytest

from _executor_doubles import FIXTURES_DIR, file_layer
from geofact.core.errors import ConfigError
from geofact.engine.run import load_scenario, run, validate


def _config(**layer_paths: str) -> dict:
    return {
        "scenario": {"name": "Vorab", "region": "Nirgendwo"},
        "layers": [
            file_layer(layer_id, path) for layer_id, path in layer_paths.items()
        ],
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


def _no_region(name: str) -> list[dict]:
    raise AssertionError(
        f"Region '{name}' darf vor dem Vorab-Check nicht aufgelöst werden"
    )


def test_fa44_missing_file_path_fails_before_region_and_loading_with_position(tmp_path):
    missing = tmp_path / "gibt_es_nicht.geojson"
    events: list = []
    with pytest.raises(ConfigError) as excinfo:
        run(
            load_scenario(_config(substations=str(missing))),
            observer=events.append,
            region_fetcher=_no_region,
        )
    assert excinfo.value.issues == [
        ("layers -> 0 (substations) -> path", f"Datei nicht gefunden: {missing}"),
    ]
    assert events == []  # weder Region noch Plan-Ereignis noch Ladevorgang


def test_fa44_precheck_resolves_relative_paths_against_base_dir():
    config = _config(substations="substations.geojson")
    config["scenario"]["region"] = "13.60,51.01,13.94,51.15"
    report = run(load_scenario(config, base_dir=FIXTURES_DIR))
    assert "catchment" in report.result.store


def test_fa44_precheck_covers_only_required_layers(tmp_path):
    config = _config(
        substations=str(FIXTURES_DIR / "substations.geojson"),
        nie_gebraucht=str(tmp_path / "fehlt.geojson"),
    )
    config["scenario"]["region"] = "13.60,51.01,13.94,51.15"
    report = run(load_scenario(config))
    assert "nie_gebraucht" not in report.result.loaded_layers


def test_fa44_validate_does_not_precheck_paths(tmp_path):
    report = validate(_config(substations=str(tmp_path / "fehlt.geojson")))
    assert report.valid, report.issues
