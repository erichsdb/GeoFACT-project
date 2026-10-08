"""Implements: FA82 (OSM-Quelle je Lauf wählbar).

Kern: ``api.run(connector_options={"osm.backend": ...})`` erreicht den
OSM-Konnektor als ``LoadContext.options`` und hat Vorrang vor
``GEOFACT_OSM_BACKEND`` - ohne die Umgebungsvariable anzufassen. Web: Lauf und
Probelauf nehmen die Wahl als ``options.osm_backend`` entgegen, nur aus der
Auswahl des Servers. Frontend: Quelltextverträge (kein Frontend-Testwerkzeug
im Repo, siehe test_fa54_frontend_lost_run.py); die reinen Helfer prüft
test_fa83_llm_choice.py zusammen mit der Modellwahl.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point

from geofact import api
from geofact.builtin.sources import osm
from geofact.core.contracts import LoadContext

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import generation  # noqa: E402
from geofact_web import options as option_module  # noqa: E402
from geofact_web.generation import TrialRun  # noqa: E402
from geofact_web.llm import LLMClient  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.models import GenerationOptions, RunOptions  # noqa: E402
from geofact_web.run_manager import RunManager  # noqa: E402
from geofact_web.settings import Settings, get_settings  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

_OSM_SCENARIO = {
    "scenario": {"name": "OSM-Quelle", "region": "13.60,51.01,13.94,51.15"},
    "layers": [{"id": "werke", "source": "osm", "tags": {"power": "substation"}}],
    "steps": [
        {
            "id": "umkreis",
            "op": "buffer",
            "inputs": {"geometry": "werke"},
            "params": {"radius_km": 1},
        }
    ],
    "output": [{"type": "geojson", "source": "umkreis"}],
}

_FILE_YAML = """\
scenario:
  name: Test
  region: "13.60,51.01,13.94,51.15"
layers:
  - id: substations
    source: file
    path: tests/fixtures/substations.geojson
    data_type: vector
steps:
  - id: catchment
    op: buffer
    inputs: {geometry: substations}
    params: {radius_km: 1}
output:
  - type: geojson
    source: catchment
"""


@pytest.fixture
def backends(monkeypatch, tmp_path):
    """Zwei Stellvertreter-Backends, die festhalten, welches gefragt wurde."""
    used: list[str] = []

    def make(name: str):
        class Fake:
            def __init__(self) -> None:
                self.name = name

            def fetch(self, tags, region, **kwargs):
                used.append(name)
                return gpd.GeoDataFrame(
                    {"power": ["substation"]},
                    geometry=[Point(13.75, 51.05)],
                    crs="EPSG:4326",
                )

        return Fake

    monkeypatch.setattr(
        osm, "_BACKENDS", {"overpass": make("overpass"), "postgis": make("postgis")}
    )
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    return used


# --- Kern --------------------------------------------------------------------


def test_fa82_without_option_the_environment_decides(backends, monkeypatch):
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "postgis")
    api.run(api.load_scenario(_OSM_SCENARIO))
    assert backends == ["postgis"]


def test_fa82_option_overrides_the_environment_for_this_run_only(backends, monkeypatch):
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "postgis")
    document = api.load_scenario(_OSM_SCENARIO)

    api.run(document, connector_options={"osm.backend": "overpass"})
    assert backends == ["overpass"]
    # die Umgebungsvariable bleibt, wie sie ist - der nächste Lauf ohne Option folgt ihr
    assert os.environ["GEOFACT_OSM_BACKEND"] == "postgis"
    api.run(document)
    assert backends == ["overpass", "postgis"]


def test_fa82_option_reaches_the_connector_through_the_load_context():
    assert LoadContext().options == {}
    assert (
        LoadContext(options={"osm.backend": "postgis"}).options["osm.backend"]
        == "postgis"
    )
    assert osm.BACKEND_OPTION == "osm.backend"


def test_fa82_unknown_backend_fails_when_the_layer_is_loaded(backends):
    with pytest.raises(api.LayerLoadError, match="Unbekanntes OSM-Backend 'planet'"):
        api.run(
            api.load_scenario(_OSM_SCENARIO),
            connector_options={"osm.backend": "planet"},
        )
    assert backends == []


@pytest.mark.parametrize(
    "options, message",
    [
        ({"backend": "overpass"}, "erwartet '<quellart>.<name>'"),
        ({"osm.": "overpass"}, "erwartet '<quellart>.<name>'"),
        ({"gibtsnicht.backend": "x"}, "unbekannte Quellart 'gibtsnicht'"),
        ({"osm.backend": ""}, "nicht leerer Text"),
        ({"osm.backend": 5}, "nicht leerer Text"),
    ],
)
def test_fa82_malformed_options_are_rejected_before_the_run(backends, options, message):
    """Typfehler- und Randfall: eine Option, die niemand lesen kann, verpufft nicht still."""
    with pytest.raises(ValueError, match=message):
        api.run(api.load_scenario(_OSM_SCENARIO), connector_options=options)
    assert backends == []


def test_fa82_run_without_osm_layer_ignores_the_backend_option(backends):
    """Randfall: ein Szenario ohne OSM-Layer läuft mit gesetzter Option unverändert."""
    document = api.load_scenario(_FILE_YAML_DICT())
    report = api.run(document, connector_options={"osm.backend": "postgis"})
    assert backends == [] and report.result.loaded_layers == ["substations"]


def _FILE_YAML_DICT() -> dict:
    import yaml

    return yaml.safe_load(_FILE_YAML)


def test_fa82_api_names_the_backends_and_builds_the_option():
    assert api.osm_backend_names() == ["overpass", "postgis"]
    assert api.osm_backend_option("postgis") == {"osm.backend": "postgis"}
    with pytest.raises(ValueError, match="Unbekanntes OSM-Backend 'planet'"):
        api.osm_backend_option("planet")


# --- Web: Auswahl des Servers ------------------------------------------------


def _settings(**overrides) -> Settings:
    values = {
        "llm_api_key": "k",
        "llm_model": "a/model",
        "osm_backend": "overpass",
        "pg_dsn_set": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_fa82_postgis_is_only_offered_with_a_dsn():
    assert option_module.osm_backends(_settings()) == ["overpass"]
    assert option_module.osm_backends(_settings(pg_dsn_set=True)) == [
        "overpass",
        "postgis",
    ]


def test_fa82_the_server_backend_is_offered_first_even_without_a_dsn():
    assert option_module.osm_backends(_settings(osm_backend="postgis")) == [
        "postgis",
        "overpass",
    ]


def test_fa82_no_choice_means_no_connector_option():
    assert option_module.osm_connector_options(_settings(), None) is None


def test_fa82_choice_becomes_the_connector_option():
    settings = _settings(pg_dsn_set=True)
    assert option_module.osm_connector_options(settings, "postgis") == {
        "osm.backend": "postgis"
    }


def test_fa82_choice_outside_the_offer_is_rejected():
    with pytest.raises(ValueError, match="steht nicht zur Wahl"):
        option_module.osm_connector_options(_settings(), "postgis")  # kein DSN
    with pytest.raises(ValueError, match="steht nicht zur Wahl"):
        option_module.osm_connector_options(_settings(pg_dsn_set=True), "planet")
    with pytest.raises(ValueError, match="steht nicht zur Wahl"):
        generation.apply_options(_settings(), GenerationOptions(osm_backend="postgis"))


def test_fa82_apply_options_records_the_choice():
    effective = generation.apply_options(
        _settings(pg_dsn_set=True), GenerationOptions(osm_backend="postgis")
    )
    assert effective.osm_backend == "postgis"


# --- Web: Endpunkte ----------------------------------------------------------


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "overpass")
    monkeypatch.setenv("GEOFACT_PG_DSN", "postgresql://nur-für-den-test/osm")
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_fa82_meta_names_default_and_available_backends(http):
    assert http.get("/api/meta").json()["osm_backends"] == {
        "default": "overpass",
        "available": ["overpass", "postgis"],
    }


def test_fa82_run_passes_the_chosen_backend_to_the_core(monkeypatch):
    seen: list = []

    def fake_run(document, **kwargs):
        seen.append(kwargs.get("connector_options"))
        raise RuntimeError("genug gesehen")

    monkeypatch.setattr("geofact_web.run_manager.api.run", fake_run)
    manager = RunManager(max_retained_runs=2)
    document = api.load_scenario(_FILE_YAML_DICT())

    manager._run(manager.create(document, refresh_snapshots=False))
    manager._run(
        manager.create(
            document,
            refresh_snapshots=False,
            connector_options={"osm.backend": "postgis"},
        )
    )
    assert seen == [None, {"osm.backend": "postgis"}]


def test_fa82_run_endpoint_accepts_a_backend_from_the_offer(http, monkeypatch):
    created: list = []
    from geofact_web.run_manager import manager

    def start(state):
        # nicht ausführen, aber als beendet führen: ein hängender "pending"-Lauf
        # zählte sonst gegen das Limit gleichzeitiger Läufe je Nutzer
        created.append(state.connector_options)
        state.status = "done"

    monkeypatch.setattr(manager, "start", start)
    ok = http.post(
        "/api/runs",
        json={"config_yaml": _FILE_YAML, "options": {"osm_backend": "postgis"}},
    )
    plain = http.post("/api/runs", json={"config_yaml": _FILE_YAML})

    assert ok.status_code == 200, ok.text
    assert plain.status_code == 200, plain.text
    assert created == [{"osm.backend": "postgis"}, None]


@pytest.mark.parametrize(
    "options", [{"osm_backend": "planet"}, {"unbekannt": 1}, {"osm_backend": 5}]
)
def test_fa82_run_endpoint_rejects_what_the_server_does_not_offer(
    http, monkeypatch, options
):
    from geofact_web.run_manager import manager

    monkeypatch.setattr(manager, "start", lambda state: pytest.fail("kein Lauf"))
    response = http.post(
        "/api/runs", json={"config_yaml": _FILE_YAML, "options": options}
    )
    assert response.status_code == 422, response.text


def test_fa82_trial_run_of_a_generation_uses_the_chosen_backend(http, monkeypatch):
    seen: list = []

    def trial(document, timeout_s, connector_options=None):
        seen.append(connector_options)
        return TrialRun("ok")

    monkeypatch.setattr(LLMClient, "_chat", lambda self, messages: _FILE_YAML)
    monkeypatch.setattr(generation, "trial_run", trial)
    http.post(
        "/api/config/generate",
        json={"prompt": "x", "options": {"osm_backend": "postgis"}},
    )
    http.post("/api/config/generate", json={"prompt": "x"})
    assert seen == [{"osm.backend": "postgis"}, None]


def test_fa82_generation_rejects_a_backend_outside_the_offer_before_asking_the_model(
    http, monkeypatch
):
    monkeypatch.setattr(
        LLMClient, "_chat", lambda self, m: pytest.fail("keine Modellanfrage")
    )
    response = http.post(
        "/api/config/generate",
        json={"prompt": "x", "options": {"osm_backend": "planet"}},
    )
    assert response.status_code == 422
    assert "steht nicht zur Wahl" in response.json()["detail"]


def test_fa82_run_options_model_knows_only_the_backend():
    assert RunOptions().osm_backend is None
    with pytest.raises(Exception):
        RunOptions(llm_choice="x")


# --- Frontend ----------------------------------------------------------------


def _read(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_fa82_dialog_offers_the_osm_source_only_as_a_choice_when_there_is_one() -> None:
    dialog = _read("components", "generation-settings.tsx")
    assert 'id="settings-osm-backend"' in dialog
    assert "choices.osm_backends.length > 1 ? (" in dialog
    assert "choices.osm_backends.map((name) => (" in dialog
    assert "Der Server bietet nur diese Quelle an." in dialog


def test_fa82_the_chosen_source_is_sent_with_the_run_and_named_at_the_settings_button() -> (
    None
):
    shell = _read("components", "app-shell.tsx")
    assert "const runSettings = runOptions(sentGenerationOptions);" in shell
    assert "api.createRun(configYaml, false, effectiveParams, runSettings)" in shell
    assert "OSM: ${active.osm_backend}" in _read(
        "components", "generation-settings.tsx"
    )
    api_ts = _read("lib", "api.ts")
    create = api_ts[api_ts.index("createRun: (") : api_ts.index("runStatus:")]
    assert (
        "...(options && Object.keys(options).length > 0 ? { options } : {})" in create
    )


def test_fa82_helpers_send_only_the_osm_source_with_a_run() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [
            node,
            str(
                Path(__file__).resolve().parent / "fa81_generation_settings_driver.mjs"
            ),
            str(FRONTEND / "lib" / "generationSettings.ts"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"node cannot run the TypeScript helpers: {proc.stderr[-300:]}")
    js = json.loads(proc.stdout.strip().splitlines()[-1])
    assert js["run"] == {
        "none": {},
        "other_only": {},
        "osm": {"osm_backend": "overpass"},
    }
    assert js["server"]["full"]["defaults"]["osm_backend"] == "postgis"
    assert js["server"]["full"]["choices"]["osm_backends"] == ["postgis", "overpass"]
    assert js["stored"]["choices"]["osm_backend"] == "overpass"
    # eine Quelle, die der Server nicht anbietet, wird nicht gesendet
    assert "osm_backend" not in js["stored"]["choices_not_offered"]
    assert js["stored"]["wrong_types"] == {}
