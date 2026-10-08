"""Implements: FA44 (Web-Demo auf dem Anwendungsfall), FA4 (Pfadregel), FA12 (Download), FA40 (Plugin-Zustand).

Contract-Tests der Web-Schicht: der Lauf ruft api.run und schreibt die
deklarierten Ausgaben einmal am Ende ins Laufverzeichnis; der Download packt
diese Dateien plus den ausgeführten Szenario-Text (scenario.yaml) und - bei
übersprungenen Ausgaben - SKIPPED_OUTPUTS.txt. Der Ursprung eines Beispiels
reist als Kopfzeile im Text; Text ohne Ursprung gilt gegen GEOFACT_WEB_BASE_DIR.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact import api  # noqa: E402
from geofact.core.registry import set_default_registry  # noqa: E402
from geofact.engine.discovery import discover  # noqa: E402
from geofact_web import scenario_service  # noqa: E402
from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web import run_manager as run_manager_module  # noqa: E402
from geofact_web.run_manager import RunManager  # noqa: E402

from nachthimmel_scenes import expected_ranking, nachthimmel_overrides  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"

YAML_TEXT = f"""\
# Mein Szenario - dieser Kommentar muss im Download erhalten bleiben
scenario:
  name: Web-Download
  region: "{BBOX_REGION}"
layers:
  - id: substations
    source: file
    data_type: vector
    path: "{(FIXTURES_DIR / "substations.geojson").as_posix()}"
steps:
  - id: catchment
    op: buffer
    inputs: {{geometry: substations}}
    params: {{radius_km: 1}}
output:
  - {{type: geojson, source: catchment, path: catchment.geojson}}
  - {{type: csv, source: catchment, path: catchment.csv}}
"""


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "web"))
    monkeypatch.setenv("GEOFACT_WEB_UPLOADS_DIR", str(tmp_path / "web" / "uploads"))
    settings_module.get_settings.cache_clear()
    return TestClient(create_app())


def _wait(client: TestClient, run_id: str) -> dict:
    deadline = time.monotonic() + 30
    while True:
        status = client.get(f"/api/runs/{run_id}").json()
        if status["status"] in ("done", "error", "cancelled"):
            return status
        assert time.monotonic() < deadline, "web run did not finish"
        time.sleep(0.05)


def _run(client: TestClient, **body) -> str:
    created = client.post("/api/runs", json=body)
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    status = _wait(client, run_id)
    assert status["status"] == "done", status.get("error")
    return run_id


def _download(client: TestClient, run_id: str) -> zipfile.ZipFile:
    response = client.get(f"/api/runs/{run_id}/download")
    assert response.status_code == 200
    return zipfile.ZipFile(io.BytesIO(response.content))


# =====================================================================
# Download: geschriebene Dateien + scenario.yaml
# =====================================================================


def test_fa44_web_download_contains_the_written_files_and_the_scenario(client):
    run_id = _run(client, config_yaml=YAML_TEXT)
    archive = _download(client, run_id)

    assert set(archive.namelist()) == {
        "catchment.geojson",
        "catchment.csv",
        "scenario.yaml",
    }
    assert archive.read("catchment.geojson").startswith(b"{")
    # scenario.yaml ist der ausgeführte Text, nicht ein Dump des Modells:
    # Kommentare und Schlüsselreihenfolge bleiben erhalten.
    assert archive.read("scenario.yaml").decode("utf-8") == YAML_TEXT


def test_fa44_web_download_of_a_dict_config_ships_its_yaml_form_without_losing_layer_fields(
    client,
):
    """Ein Lauf aus einem dict (API-Client): scenario.yaml ist dessen YAML-Form.
    Ein model_dump verlore die quellartspezifischen Layerfelder (z. B. path)."""
    config = yaml.safe_load(YAML_TEXT)
    run_id = _run(client, config=config)
    archive = _download(client, run_id)
    assert yaml.safe_load(archive.read("scenario.yaml")) == config


def test_fa44_web_download_lists_skipped_outputs(client):
    config = yaml.safe_load(YAML_TEXT)
    # gültig, aber nicht schreibbar: RDF verlangt ein CRS, eine Tabelle ohne Geometrie hat keins
    config["layers"].append(
        {
            "id": "bevoelkerung",
            "source": "table",
            "delimiter": ";",
            "path": (FIXTURES_DIR / "bip_mini.csv").as_posix(),
        }
    )
    config["output"].append(
        {"type": "rdf", "source": "bevoelkerung", "path": "statistik.ttl"}
    )
    with pytest.warns(UserWarning, match="nicht alle deklarierten Ausgaben"):
        run_id = _run(client, config=config)
        archive = _download(client, run_id)

    names = set(archive.namelist())
    assert {
        "catchment.geojson",
        "catchment.csv",
        "scenario.yaml",
        "SKIPPED_OUTPUTS.txt",
    } == names
    note = archive.read("SKIPPED_OUTPUTS.txt").decode("utf-8")
    assert "bevoelkerung" in note and "CRS" in note


def test_fa44_web_outputs_are_written_once_at_the_end_of_the_run(client):
    """Zwei Downloads schreiben nichts neu (ein type: rest-Ausgang soll nicht
    bei jedem Abruf einen Dienst aufrufen)."""
    from geofact_web.run_manager import manager

    run_id = _run(client, config_yaml=YAML_TEXT)
    state = manager.get(run_id)
    written = {p: p.stat().st_mtime_ns for p in state.report.outputs.written}
    assert written and all(p.parent == state.run_dir for p in written)

    _download(client, run_id)
    _download(client, run_id)
    assert {p: p.stat().st_mtime_ns for p in state.report.outputs.written} == written


def test_fa44_web_download_before_the_run_is_done_is_a_conflict(client):
    from geofact_web.run_manager import manager

    document, _ = scenario_service.validate_config(YAML_TEXT, None)
    state = manager.create(document, refresh_snapshots=False)
    assert client.get(f"/api/runs/{state.run_id}/download").status_code == 409


def test_fa44_run_dir_is_removed_when_the_run_is_evicted():
    manager = RunManager(max_retained_runs=1)
    document = api.load_scenario(yaml.safe_load(YAML_TEXT))

    first = manager.create(document, refresh_snapshots=False)
    manager._run(first)
    first_dir = first.run_dir
    assert first_dir is not None and any(first_dir.iterdir())

    second = manager.create(document, refresh_snapshots=False)
    manager._run(second)

    assert manager.get(first.run_id) is None
    assert not first_dir.exists()
    assert second.run_dir is not None and second.run_dir.exists()
    kept = second.run_dir
    second.remove_run_dir()
    assert second.run_dir is None and not kept.exists()


def test_fa44_run_dir_lies_below_the_configured_data_dir(tmp_path, monkeypatch):
    """Das Laufverzeichnis liegt unter ``<GEOFACT_WEB_DATA_DIR>/runs`` und nicht
    im System-Temp - so liegt der Plattenbedarf an einem bekannten Ort."""
    data_dir = tmp_path / "daten"
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(data_dir))
    settings_module.get_settings.cache_clear()
    manager = RunManager()
    state = manager.create(
        api.load_scenario(yaml.safe_load(YAML_TEXT)), refresh_snapshots=False
    )
    manager._run(state)

    assert state.status == "done"
    assert state.run_dir is not None
    assert state.run_dir.parent == (data_dir / "runs").resolve()
    assert any(state.run_dir.iterdir())
    state.remove_run_dir()


def _make_stale(path: Path, hours: float = 24) -> None:
    """Datiert den Eintrag zurück: nur veraltete Reste räumt der Start ab (FA54)."""
    import os

    past = time.time() - hours * 3600
    os.utime(path, (past, past))


def test_fa44_orphaned_run_dirs_of_an_earlier_process_are_purged(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "daten"))
    settings_module.get_settings.cache_clear()
    runs_dir = settings_module.get_settings().runs_dir
    stale = runs_dir / "geofact_run_alt_abc"
    stale.mkdir(parents=True)
    (stale / "catchment.geojson").write_text("{}", encoding="utf-8")
    (runs_dir / "lose_datei.tmp").write_text("x", encoding="utf-8")
    _make_stale(stale)
    _make_stale(runs_dir / "lose_datei.tmp")

    manager = RunManager()
    live_state = manager.create(
        api.load_scenario(yaml.safe_load(YAML_TEXT)), refresh_snapshots=False
    )
    live_state.run_dir = (
        runs_dir / "geofact_run_laeuft_xyz"
    )  # gehört zu einem Lauf dieses Prozesses
    live_state.run_dir.mkdir()
    _make_stale(live_state.run_dir)  # selbst alt: bleibt, weil er einem Lauf gehört

    removed = manager.purge_orphaned_run_dirs()

    assert sorted(p.name for p in removed) == ["geofact_run_alt_abc", "lose_datei.tmp"]
    assert not stale.exists() and not (runs_dir / "lose_datei.tmp").exists()
    assert live_state.run_dir.exists()  # laufende Läufe bleiben
    assert manager.purge_orphaned_run_dirs() == []  # idempotent


def test_fa44_backend_startup_purges_orphaned_run_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "daten"))
    settings_module.get_settings.cache_clear()
    stale = settings_module.get_settings().runs_dir / "geofact_run_alt_abc"
    stale.mkdir(parents=True)
    (stale / "ergebnis.csv").write_text("a;b", encoding="utf-8")
    _make_stale(stale)

    with TestClient(create_app()) as started:  # Lifespan läuft beim Eintritt
        assert started.get("/api/health").status_code == 200
        assert not stale.exists()


def test_fa44_a_leftover_that_cannot_be_removed_is_reported_not_skipped_silently(
    tmp_path, monkeypatch
):
    from geofact_web import run_manager as run_manager_module

    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "daten"))
    settings_module.get_settings.cache_clear()
    stuck = settings_module.get_settings().runs_dir / "geofact_run_gesperrt_abc"
    stuck.mkdir(parents=True)
    _make_stale(stuck)
    monkeypatch.setattr(run_manager_module.shutil, "rmtree", lambda *a, **k: None)

    with pytest.warns(UserWarning, match="konnte nicht entfernt werden"):
        removed = RunManager().purge_orphaned_run_dirs()
    assert removed == [] and stuck.exists()


# =====================================================================
# Pfadregel (FA4/FA44): Ursprung im Text, sonst GEOFACT_WEB_BASE_DIR
# =====================================================================


def test_fa44_example_loader_puts_the_origin_into_the_text(client):
    loaded = client.get(
        "/api/scenarios/examples/leipzig/leipzig13_busnetz_dichte"
    ).json()["config_yaml"]
    assert loaded.startswith("# geofact-origin: leipzig/leipzig13_busnetz_dichte\n")
    assert scenario_service.origin_of(loaded) == "leipzig/leipzig13_busnetz_dichte"
    # der Text ist weiterhin das unveränderte Beispiel
    original = (
        settings_module.EXAMPLES_DIR / "leipzig" / "leipzig13_busnetz_dichte.yaml"
    ).read_text(encoding="utf-8")
    assert loaded.endswith(original)


def test_fa44_origin_header_sets_base_dir_to_the_example_directory(client):
    text = client.get(
        "/api/scenarios/examples/leipzig/leipzig13_busnetz_dichte"
    ).json()["config_yaml"]
    document, response = scenario_service.validate_config(text, None)
    assert response.valid, response.issues
    assert document.base_dir == (settings_module.EXAMPLES_DIR / "leipzig").resolve()
    assert document.source_text == text


def test_fa44_origin_survives_editing_the_text(client):
    text = client.get("/api/scenarios/examples/szenario4_bip_bundeslaender").json()[
        "config_yaml"
    ]
    edited = (
        text.replace("Die 5 größten", "Die 3 größten") + "\n# angehängt\n"
    )  # Bearbeitung im Editor
    assert edited != text
    document, response = scenario_service.validate_config(edited, None)
    assert response.valid, response.issues
    assert document.scenario.scenario.name.startswith("Die 3 größten")
    assert document.base_dir == settings_module.EXAMPLES_DIR.resolve()


def test_fa44_unknown_origin_is_a_validation_issue_not_a_silent_fallback():
    text = "# geofact-origin: gibt/es/nicht\n" + YAML_TEXT
    document, response = scenario_service.validate_config(text, None)
    assert document is None and response.valid is False
    assert (
        response.issues[0].location == "(Ursprung)"
        and "gibt/es/nicht" in response.issues[0].message
    )


def test_fa44_origin_cannot_leave_the_examples_directory():
    for origin in ("../backend/geofact_web/settings", "../../etc/passwd"):
        document, response = scenario_service.validate_config(
            f"# geofact-origin: {origin}\n" + YAML_TEXT, None
        )
        assert document is None and response.issues[0].location == "(Ursprung)"


def test_fa44_origin_is_only_read_from_the_first_lines():
    late = YAML_TEXT + "\n" * 6 + "# geofact-origin: szenario4_bip_bundeslaender\n"
    assert scenario_service.origin_of(late) is None


def test_fa44_text_without_origin_uses_geofact_web_base_dir(client, monkeypatch):
    configuration = yaml.safe_load(YAML_TEXT)
    configuration["layers"][0]["path"] = "substations.geojson"  # relativ
    text = yaml.safe_dump(configuration)

    monkeypatch.setenv("GEOFACT_WEB_BASE_DIR", str(FIXTURES_DIR))
    settings_module.get_settings.cache_clear()
    document, response = scenario_service.validate_config(text, None)
    assert response.valid and document.base_dir == FIXTURES_DIR.resolve()
    run_id = _run(client, config_yaml=text)
    assert client.get(f"/api/runs/{run_id}").json()["loaded_layers"] == ["substations"]


def test_fa44_base_dir_default_is_the_repo_root(monkeypatch):
    monkeypatch.delenv("GEOFACT_WEB_BASE_DIR", raising=False)
    settings_module.get_settings.cache_clear()
    assert (
        settings_module.get_settings().base_dir == settings_module.REPO_ROOT.resolve()
    )
    document, _ = scenario_service.validate_config(None, yaml.safe_load(YAML_TEXT))
    assert document.base_dir == settings_module.REPO_ROOT.resolve()


# =====================================================================
# Validierung, Meta
# =====================================================================


def test_fa44_validate_endpoint_reports_german_positions(client):
    bad = yaml.safe_load(YAML_TEXT)
    bad["steps"][0]["params"]["radius_km"] = "5km"
    body = client.post("/api/config/validate", json={"config": bad}).json()
    assert body["valid"] is False
    assert body["issues"] == [
        {
            "location": "steps -> 0 -> params -> radius_km",
            "message": "muss eine Zahl sein (erhalten: '5km')",
            "type": None,
        }
    ]


def test_fa44_validate_endpoint_without_text_or_dict_is_an_issue(client):
    body = client.post("/api/config/validate", json={}).json()
    assert body["valid"] is False and body["issues"][0]["location"] == "(YAML)"


def test_fa44_meta_shows_plugins_that_failed_to_load(client, tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "kaputt.py").write_text(
        "raise RuntimeError('Plugin kaputt')\n", encoding="utf-8"
    )
    with pytest.warns(UserWarning, match="übersprungen"):
        broken = discover(plugin_paths=[plugins])
    previous = set_default_registry(broken)
    try:
        body = client.get("/api/meta").json()
    finally:
        set_default_registry(previous)
    assert any(
        "Plugin kaputt" in message for message in body["failed_plugins"].values()
    ), body


# =====================================================================
# Beispiel sachsen_nachthimmel in der Web-Demo (FA12)
# =====================================================================

NACHTHIMMEL = "sachsen_nachthimmel"
NACHTHIMMEL_NAME = "Sterneschauen in Sachsen"


def test_fa12_nachthimmel_example_is_listed_loadable_and_valid_through_the_web_path(
    client,
):
    listed = {entry["id"]: entry for entry in client.get("/api/scenarios").json()}
    entry = listed[NACHTHIMMEL]
    assert entry["source"] == "example" and entry["name"] == NACHTHIMMEL_NAME
    assert (
        entry["description"]
        == "Dunkle, hoch gelegene und erreichbare Aussichtspunkte in Sachsen"
    )

    loaded = client.get(f"/api/scenarios/examples/{NACHTHIMMEL}")
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["name"] == NACHTHIMMEL_NAME
    text = loaded.json()["config_yaml"]
    assert scenario_service.origin_of(text) == NACHTHIMMEL

    # base_dir is the examples directory, so `data/...` is examples/data/... like on the command line
    document, response = scenario_service.validate_config(text, None)
    assert response.valid, response.issues
    assert document.base_dir == settings_module.EXAMPLES_DIR.resolve()
    body = client.post("/api/config/validate", json={"config_yaml": text}).json()
    assert body["valid"] is True and body["issues"] == []
    assert body["execution_order"] == [
        "orte",
        "mit_parkplatz",
        "umfeld_hoehe",
        "mit_hoehe",
        "umfeld_licht",
        "mit_licht",
        "geeignet",
        "beste",
    ]
    assert sorted(body["required_layers"]) == [
        "aussichtspunkte",
        "hoehe",
        "nachtlicht",
        "parkplaetze",
    ]
    assert body["unused_layers"] == []


def test_fa12_nachthimmel_example_runs_through_the_web_api_with_its_declared_outputs(
    client, monkeypatch
):
    """The loaded example runs through POST /api/runs like in the demo; only the data sources are
    replaced by the synthetic stand-ins (OSM and the two raster downloads are not part of the
    checkout)."""
    real_run = api.run

    def offline_run(document, **kwargs):
        return real_run(
            document,
            connector_override=nachthimmel_overrides(),
            region_fetcher=lambda name: [{"geojson": _sachsen_region_geometry()}],
            **kwargs,
        )

    monkeypatch.setattr(run_manager_module.api, "run", offline_run)
    text = client.get(f"/api/scenarios/examples/{NACHTHIMMEL}").json()["config_yaml"]
    run_id = _run(client, config_yaml=text)

    status = client.get(f"/api/runs/{run_id}").json()
    assert sorted(status["loaded_layers"]) == [
        "aussichtspunkte",
        "hoehe",
        "nachtlicht",
        "parkplaetze",
    ]
    assert {name: step["status"] for name, step in status["steps"].items()} == {
        name: "done" for name in status["order"]
    }
    archive = _download(client, run_id)
    # ATTRIBUTION.txt (FA65): the OSM layers carry the ODbL default licence
    assert set(archive.namelist()) == {
        "sachsen_nachthimmel.html",
        "sachsen_nachthimmel_beste_orte.geojson",
        "scenario.yaml",
        "ATTRIBUTION.txt",
    }
    features = json.loads(archive.read("sachsen_nachthimmel_beste_orte.geojson"))[
        "features"
    ]
    assert [feature["properties"]["id"] for feature in features] == expected_ranking()


def _sachsen_region_geometry() -> dict:
    import geopandas as gpd
    from shapely.geometry import mapping

    return mapping(
        gpd.read_file(FIXTURES_DIR / "sachsen_region.geojson").geometry.iloc[0]
    )
