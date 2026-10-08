"""Implements: FA44 (ein Anwendungsfall für Kommandozeile, Web und Skripte), FA40 (Plugin-Beispiel).

Ende-zu-Ende:
- api.run über das Plugin-Beispiel (examples/plugin_demo) schreibt jede
  deklarierte Ausgabe oder meldet sie als übersprungen - keine verschwindet.
- Kommandozeile und Web-Demo schreiben für dasselbe Szenario dieselben
  Ausgaben (derselbe Weg: engine/run.py).
"""

from __future__ import annotations

import io
import sys
import time
import zipfile
from pathlib import Path

import pytest
import yaml

from geofact import api, cli

# Wiederverwendet: lokaler Demo-Dienst und Registry mit dem Plugin-Ordner des Beispiels.
from .test_plugin_demo_e2e import SCENARIO, demo_registry, demo_service  # noqa: F401

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

pytestmark = pytest.mark.e2e


def test_fa44_api_run_over_the_plugin_demo_writes_or_reports_every_output(
    demo_registry,  # noqa: F811 - Fixtures importiert
    demo_service,  # noqa: F811
    tmp_path,
):
    document = api.load_scenario(SCENARIO, registry=demo_registry)
    assert (
        document.base_dir == SCENARIO.parent.resolve()
    )  # 'daten/...' gilt gegen die YAML

    report = api.run(document, out_dir=tmp_path / "ergebnis", registry=demo_registry)

    declared = len(document.scenario.output)
    assert report.outputs is not None
    assert len(report.outputs.written) + len(report.outputs.skipped) == declared
    assert report.skipped_outputs == []
    for path in report.outputs.written:
        assert path.parent == tmp_path / "ergebnis" and path.stat().st_size > 0
    assert sorted(p.suffix for p in report.outputs.written) == [
        ".geojson",
        ".html",
        ".kml",
    ]
    assert set(report.result.loaded_layers) == set(report.result.plan.required_layers)


_SHARED_SCENARIO = {
    "scenario": {"name": "Ein Weg", "region": "13.60,51.01,13.94,51.15"},
    "layers": [
        {
            "id": "umspannwerke",
            "source": "file",
            "data_type": "vector",
            "path": (FIXTURES_DIR / "substations.geojson").as_posix(),
        }
    ],
    "steps": [
        {
            "id": "einzug",
            "op": "buffer",
            "inputs": {"geometry": "umspannwerke"},
            "params": {"radius_km": 1},
        }
    ],
    "output": [
        {"type": "geojson", "source": "einzug", "path": "einzug.geojson"},
        {"type": "csv", "source": "einzug", "path": "einzug.csv"},
        {"type": "map", "source": "einzug", "path": "einzug.html"},
    ],
}


def test_fa44_cli_and_web_write_the_same_outputs_for_the_same_scenario(
    tmp_path, monkeypatch, capsys
):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from geofact_web import settings as settings_module
    from geofact_web.main import create_app

    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "web"))
    monkeypatch.setenv("GEOFACT_WEB_UPLOADS_DIR", str(tmp_path / "web" / "uploads"))
    settings_module.get_settings.cache_clear()
    text = yaml.safe_dump(_SHARED_SCENARIO, allow_unicode=True, sort_keys=False)

    # Kommandozeile
    config = tmp_path / "szenario.yaml"
    config.write_text(text, encoding="utf-8")
    out = tmp_path / "cli"
    monkeypatch.setattr(sys, "argv", ["geofact", "run", str(config), "--out", str(out)])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0, capsys.readouterr().err
    cli_files = {p.name: p.read_bytes() for p in out.iterdir()}

    # Web-Demo
    client = TestClient(create_app())
    run_id = client.post("/api/runs", json={"config_yaml": text}).json()["run_id"]
    deadline = time.monotonic() + 30
    while client.get(f"/api/runs/{run_id}").json()["status"] not in ("done", "error"):
        assert time.monotonic() < deadline, "web run did not finish"
        time.sleep(0.05)
    assert client.get(f"/api/runs/{run_id}").json()["status"] == "done"
    archive = zipfile.ZipFile(
        io.BytesIO(client.get(f"/api/runs/{run_id}/download").content)
    )
    web_files = {n: archive.read(n) for n in archive.namelist() if n != "scenario.yaml"}

    assert (
        set(cli_files)
        == set(web_files)
        == {"einzug.geojson", "einzug.csv", "einzug.html"}
    )
    # Daten identisch; die Karte (Folium) trägt zufällige Element-IDs.
    assert cli_files["einzug.geojson"] == web_files["einzug.geojson"]
    assert cli_files["einzug.csv"] == web_files["einzug.csv"]
    assert len(cli_files["einzug.html"]) > 1000 and len(web_files["einzug.html"]) > 1000
