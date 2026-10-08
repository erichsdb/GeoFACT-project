"""Implements: FA65 (Lizenzen und Namensnennung: Bindung an jede Ausgabe, ATTRIBUTION.txt).

Contract-Tests für engine/outputs.py: jede Ausgabe bekommt vor dem Schreiben
die Attribution ihrer Quelle (``spec.attribution``); der Lauf schreibt
``ATTRIBUTION.txt`` nur, wenn eine Lizenz vorkommt; ein Schreibfehler der Datei
ist eine Warnung."""

from __future__ import annotations

import json

import pytest

from _loading_helpers import DRESDEN_BBOX, FIXTURES_DIR, RECORDING_PLUGIN
from geofact.core.scenario import Scenario
from geofact.engine.discovery import discover
from geofact.engine.executor import run_scenario
from geofact.engine.outputs import (
    ATTRIBUTION_FILE,
    AttributionFileWarning,
    write_outputs,
)

DL_DE = {
    "name": "dl-de/by-2-0",
    "attribution": "Stadt Dresden",
    "url": "https://www.govdata.de/dl-de/by-2-0",
}


@pytest.fixture()
def registry(tmp_path_factory):
    plugins = tmp_path_factory.mktemp("plugins_fa65")
    (plugins / "aufzeichnung_fa65.py").write_text(RECORDING_PLUGIN, encoding="utf-8")
    return discover(plugin_paths=[plugins])


def _scenario(registry, *, licensed: bool, outputs: list[dict]) -> Scenario:
    umspannwerke = {
        "id": "umspannwerke",
        "source": "file",
        "data_type": "vector",
        "path": str(FIXTURES_DIR / "substations.geojson"),
    }
    if licensed:
        umspannwerke["license"] = DL_DE
    return Scenario.model_validate(
        {
            "scenario": {"name": "Lizenzen", "region": DRESDEN_BBOX},
            "layers": [
                umspannwerke,
                {
                    "id": "leitungen",
                    "source": "file",
                    "data_type": "vector",
                    "path": str(FIXTURES_DIR / "power_lines.geojson"),
                },
            ],
            "steps": [
                {
                    "id": "puffer",
                    "op": "buffer",
                    "inputs": {"geometry": "umspannwerke"},
                    "params": {"radius_km": 1},
                },
                {
                    "id": "schnitt",
                    "op": "clip",
                    "inputs": {"features": "leitungen", "mask": "puffer"},
                },
            ],
            "output": outputs,
        },
        context={"registry": registry},
    )


def test_fa65_output_spec_receives_the_attribution_of_its_source_before_writing(
    registry, tmp_path
):
    scenario = _scenario(
        registry,
        licensed=True,
        outputs=[
            {"type": "aufzeichnung", "source": "schnitt", "path": "rec.json"},
        ],
    )
    result = run_scenario(scenario, registry=registry)

    write_outputs(scenario, result, tmp_path, registry=registry)

    recorded = json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))
    attribution = recorded["attribution"]
    assert attribution["lines"] == [
        "© Stadt Dresden (dl-de/by-2-0, https://www.govdata.de/dl-de/by-2-0)"
    ]
    assert attribution["undeclared"] == ["leitungen"]


def test_fa65_attribution_file_lists_every_written_output_with_its_licences_and_undeclared_layers(
    registry, tmp_path
):
    scenario = _scenario(
        registry,
        licensed=True,
        outputs=[
            {"type": "geojson", "source": "puffer", "path": "puffer.geojson"},
            {"type": "geojson", "source": "schnitt", "path": "schnitt.geojson"},
            {"type": "csv", "source": "leitungen", "path": "leitungen.csv"},
        ],
    )
    result = run_scenario(scenario, registry=registry)

    outcome = write_outputs(scenario, result, tmp_path, registry=registry)

    assert outcome.attribution_file == tmp_path / ATTRIBUTION_FILE
    assert outcome.attribution_file not in outcome.written
    text = outcome.attribution_file.read_text(encoding="utf-8")
    line = "© Stadt Dresden (dl-de/by-2-0, https://www.govdata.de/dl-de/by-2-0)"
    blocks = text.split("\n\n")
    puffer = next(b for b in blocks if b.startswith("puffer.geojson"))
    schnitt = next(b for b in blocks if b.startswith("schnitt.geojson"))
    leitungen = next(b for b in blocks if b.startswith("leitungen.csv"))
    assert (
        "(Quelle: puffer)" in puffer
        and line in puffer
        and "ohne Lizenzangabe" not in puffer
    )
    assert line in schnitt and "ohne Lizenzangabe: leitungen" in schnitt
    assert "ohne Lizenzangabe: leitungen" in leitungen and line not in leitungen


def test_fa65_no_attribution_file_when_no_layer_declares_a_licence(registry, tmp_path):
    scenario = _scenario(
        registry,
        licensed=False,
        outputs=[
            {"type": "geojson", "source": "schnitt", "path": "schnitt.geojson"},
        ],
    )
    result = run_scenario(scenario, registry=registry)

    outcome = write_outputs(scenario, result, tmp_path, registry=registry)

    assert outcome.attribution_file is None
    assert not (tmp_path / ATTRIBUTION_FILE).exists()
    assert [p.name for p in outcome.written] == ["schnitt.geojson"]


def test_fa65_attribution_file_failure_is_a_warning_not_an_abort(registry, tmp_path):
    scenario = _scenario(
        registry,
        licensed=True,
        outputs=[
            {"type": "geojson", "source": "puffer", "path": "puffer.geojson"},
        ],
    )
    result = run_scenario(scenario, registry=registry)
    (tmp_path / ATTRIBUTION_FILE).mkdir()  # Schreiben der Datei muss scheitern

    with pytest.warns(AttributionFileWarning, match="konnte nicht geschrieben werden"):
        outcome = write_outputs(scenario, result, tmp_path, registry=registry)

    assert outcome.attribution_file is None
    assert [p.name for p in outcome.written] == ["puffer.geojson"]


def test_fa65_stale_attribution_file_is_removed_when_the_new_run_has_no_licence(
    registry, tmp_path
):
    """Nachtreview 22: a reused out_dir kept the ATTRIBUTION.txt of an earlier run
    and credited licences the new outputs do not carry."""
    outputs = [{"type": "geojson", "source": "puffer", "path": "puffer.geojson"}]
    licensed = _scenario(registry, licensed=True, outputs=outputs)
    first = write_outputs(
        licensed, run_scenario(licensed, registry=registry), tmp_path, registry=registry
    )
    assert first.attribution_file is not None

    unlicensed = _scenario(registry, licensed=False, outputs=outputs)
    second = write_outputs(
        unlicensed,
        run_scenario(unlicensed, registry=registry),
        tmp_path,
        registry=registry,
    )
    assert second.attribution_file is None
    assert not (tmp_path / ATTRIBUTION_FILE).exists()


def test_fa65_stale_attribution_file_is_removed_when_writing_fails(
    registry, tmp_path, monkeypatch
):
    from pathlib import Path

    outputs = [{"type": "geojson", "source": "puffer", "path": "puffer.geojson"}]
    scenario = _scenario(registry, licensed=True, outputs=outputs)
    result = run_scenario(scenario, registry=registry)
    (tmp_path / ATTRIBUTION_FILE).write_text(
        "alt: (c) jemand anderes", encoding="utf-8"
    )
    original_write_text = Path.write_text

    def failing_write_text(self, *args, **kwargs):
        if self.name == ATTRIBUTION_FILE:
            raise PermissionError("schreibgeschützt")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    with pytest.warns(AttributionFileWarning, match="konnte nicht geschrieben werden"):
        outcome = write_outputs(scenario, result, tmp_path, registry=registry)
    assert outcome.attribution_file is None
    assert not (tmp_path / ATTRIBUTION_FILE).exists()
