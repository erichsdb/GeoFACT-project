"""Implements: FA40-FA43 (Ende-zu-Ende: Beispiel-Plugins aus examples/plugin_demo).

Führt examples/plugin_demo/szenario_plugins.yaml mit dem Plugin-Ordner
examples/plugin_demo/plugins aus: FlatGeobuf-Quelle (Dateiformat-Plugin),
Messstellen (source: rest), Puffer und Zuschnitt (Kern), konvexe Hülle
(op: rest) und KML-Export (type: rest) - alle drei REST-Bausteine in der
Konfiguration deklariert, gegen den lokal gestarteten Demo-Dienst. Hält das Beispiel
dauerhaft gültig.

Die Plugins stehen nicht in der Standard-Registry der Testsitzung: jeder Test baut
sich per ``discover(plugin_paths=[...])`` eine eigene Registry (Fixture
``demo_registry``) und reicht sie explizit durch. Nur Code ohne Registry-Parameter
(Web-App, ``Scenario(**...)``) braucht ``demo_plugins``: das installiert dieselbe
Registry für die Dauer des Tests als Standard und stellt danach die vorherige wieder her.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from geofact.api import PLUGIN_PATH_ENV
from geofact.core.registry import set_default_registry
from geofact.core.scenario import Scenario
from geofact.engine.discovery import discover
from geofact.engine.executor import run_scenario
from geofact.engine.outputs import write_outputs

DEMO_DIR = Path(__file__).resolve().parents[2] / "examples" / "plugin_demo"
PLUGINS = DEMO_DIR / "plugins"
SCENARIO = DEMO_DIR / "szenario_plugins.yaml"


@pytest.fixture
def demo_service(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "plugin_demo_dienst", DEMO_DIR / "demo_dienst.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("GEOFACT_REST_DEMO_PORT", str(server.server_address[1]))
    yield
    server.shutdown()


@pytest.fixture
def demo_registry(tmp_path, monkeypatch):
    """Frische Registry aus Kernbausteinen + dem Plugin-Ordner des Beispiels."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    return discover(plugin_paths=[PLUGINS])


@pytest.fixture
def demo_plugins(demo_registry):
    """Wie demo_registry, installiert sie zusätzlich für die Dauer des Tests
    als Standard-Registry (für Code ohne Registry-Parameter: Web-App,
    Scenario(**...)) und stellt danach die vorherige wieder her."""
    previous = set_default_registry(demo_registry)
    yield demo_registry
    set_default_registry(previous)


def test_plugin_demo_runs_end_to_end(demo_registry, demo_service, tmp_path):
    scenario = Scenario.model_validate(
        yaml.safe_load(SCENARIO.read_text(encoding="utf-8")),
        context={"registry": demo_registry},
    )
    result = run_scenario(scenario, base_dir=DEMO_DIR, registry=demo_registry)

    stations = result.store["umspannwerke"]
    area = result.store["versorgungsgebiet"]
    assert len(stations) == 5
    assert len(area) == 1
    assert area.iloc[0]["anzahl_objekte"] == 5
    assert area.iloc[0]["name"] == "Versorgungsgebiet"
    assert area.crs == stations.crs  # zurück im Arbeits-CRS
    # Die Hülle umschließt alle Einzugsgebiete (bis auf Rundung)
    buffers = result.store["einzugsgebiete"].union_all()
    assert area.geometry.iloc[0].buffer(1).contains(buffers)

    # REST-Quelle: 12 Messstellen im Raster der Region, im Arbeits-CRS
    stations_all = result.store["messstellen"]
    assert len(stations_all) == 12
    assert set(stations_all["art"]) == {"pegel"}
    assert stations_all.crs == stations.crs
    inside = result.store["messstellen_im_gebiet"]
    assert 0 < len(inside) < 12
    assert inside.within(area.geometry.iloc[0].buffer(1)).all()

    outcome = write_outputs(scenario, result, tmp_path / "out", registry=demo_registry)
    assert sorted(p.suffix for p in outcome.written) == [".geojson", ".html", ".kml"]
    assert outcome.skipped == []
    kml = next(p for p in outcome.written if p.suffix == ".kml").read_text(
        encoding="utf-8"
    )
    assert "<name>Versorgungsgebiet der Umspannwerke</name>" in kml
    assert "<Polygon>" in kml


def test_plugin_demo_is_valid_only_with_plugin_dir(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != PLUGIN_PATH_ENV}
    without = subprocess.run(
        [sys.executable, "-m", "geofact.cli", "validate", str(SCENARIO)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert without.returncode == 1
    assert "fgb" in without.stderr

    env[PLUGIN_PATH_ENV] = str(PLUGINS)
    with_plugins = subprocess.run(
        [sys.executable, "-m", "geofact.cli", "validate", str(SCENARIO)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert with_plugins.returncode == 0, with_plugins.stderr
