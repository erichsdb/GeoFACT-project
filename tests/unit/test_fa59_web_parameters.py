"""Implements: FA59 (Web-Teil: Parameter in validate/run), FA75 (Web-Teil: Knotenherkunft und Instanzen im Laufstatus).

Contract-Tests der Web-Schicht: ``POST /api/config/validate`` nennt die
deklarierten Parameter (auch bei ungültiger Konfiguration) und nimmt
Überschreibungen an; ``POST /api/runs`` läuft mit Überschreibungen, der
Download enthält sie als ``parameters.yaml``; der Laufstatus trägt die
Herkunft der aus Modulen erzeugten Knoten (``NodeOrigin.to_dict``) und die
Instanzen; Knoten-URLs mit ``[``/``]`` in der id funktionieren.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import geopandas as gpd
import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"
SUBSTATIONS = (FIXTURES_DIR / "substations.geojson").as_posix()

PARAM_YAML = f"""\
scenario:
  name: Web-Parameter
  region: "{BBOX_REGION}"
parameters:
  radius:
    type: float
    default: 1.0
    description: Pufferradius in km
  mode:
    type: string
    default: a
    choices: [a, b]
layers:
  - id: substations
    source: file
    data_type: vector
    path: "{SUBSTATIONS}"
steps:
  - id: catchment
    op: buffer
    inputs: {{geometry: substations}}
    params: {{radius_km: "${{radius}}"}}
output:
  - {{type: geojson, source: catchment, path: catchment.geojson}}
"""

FOREACH_YAML = f"""\
modules:
  puffer:
    args: {{r: {{type: integer}}}}
    steps:
      - id: x
        op: buffer
        inputs: {{geometry: substations}}
        params: {{radius_km: "${{r}}"}}
    exports: [x]
scenario:
  name: Web-Module
  region: "{BBOX_REGION}"
layers:
  - id: substations
    source: file
    data_type: vector
    path: "{SUBSTATIONS}"
instances:
  - {{id: puffer, module: puffer, foreach: {{var: r, in: [1, 2]}}, with: {{r: "${{r}}"}}}}
steps:
  - {{id: alle, op: collect, inputs: "puffer[*].x"}}
output:
  - {{type: geojson, source: alle, path: alle.geojson}}
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


def _run(client: TestClient, **body) -> tuple[str, dict]:
    created = client.post("/api/runs", json=body)
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    status = _wait(client, run_id)
    assert status["status"] == "done", status.get("error")
    return run_id, status


def test_fa59_web_validate_returns_parameter_info(client):
    body = client.post("/api/config/validate", json={"config_yaml": PARAM_YAML}).json()
    assert body["valid"] is True, body["issues"]
    params = {p["name"]: p for p in body["parameters"]}
    assert set(params) == {"radius", "mode"}
    assert params["radius"]["type"] == "float"
    assert params["radius"]["default"] == 1.0
    assert params["radius"]["value"] == 1.0
    assert params["radius"]["description"] == "Pufferradius in km"
    assert params["radius"]["overridden"] is False
    assert params["mode"]["choices"] == ["a", "b"]


def test_fa59_web_validate_applies_overrides(client):
    body = client.post(
        "/api/config/validate",
        json={"config_yaml": PARAM_YAML, "parameters": {"radius": 2.5}},
    ).json()
    assert body["valid"] is True, body["issues"]
    radius = next(p for p in body["parameters"] if p["name"] == "radius")
    assert radius["value"] == 2.5
    assert radius["overridden"] is True


def test_fa59_web_validate_unknown_override_is_an_issue_and_lists_declared(client):
    body = client.post(
        "/api/config/validate",
        json={"config_yaml": PARAM_YAML, "parameters": {"radiuss": 2}},
    ).json()
    assert body["valid"] is False
    issue = body["issues"][0]
    assert issue["location"] == "parameters -> radiuss"
    assert "radius" in issue["message"] and "mode" in issue["message"]
    # Das Formular braucht die Parameterliste gerade im Fehlerfall.
    assert {p["name"] for p in body["parameters"]} == {"radius", "mode"}


def test_fa59_web_validate_invalid_choice_override_is_rejected(client):
    body = client.post(
        "/api/config/validate",
        json={"config_yaml": PARAM_YAML, "parameters": {"mode": "c"}},
    ).json()
    assert body["valid"] is False
    assert any("mode" in i["location"] for i in body["issues"])


def test_fa59_web_validate_without_parameters_has_empty_list(client):
    text = (
        PARAM_YAML.split("parameters:")[0] + "layers:" + PARAM_YAML.split("layers:")[1]
    )
    text = text.replace('"${radius}"', "1")
    body = client.post("/api/config/validate", json={"config_yaml": text}).json()
    assert body["valid"] is True, body["issues"]
    assert body["parameters"] == []
    assert body["expansion"] is None


def test_fa59_web_run_with_overrides_writes_parameters_yaml_into_download(client):
    run_id, _status = _run(client, config_yaml=PARAM_YAML, parameters={"radius": 2.0})
    archive = zipfile.ZipFile(
        io.BytesIO(client.get(f"/api/runs/{run_id}/download").content)
    )
    assert "parameters.yaml" in archive.namelist()
    assert yaml.safe_load(archive.read("parameters.yaml")) == {"radius": 2.0}
    # scenario.yaml bleibt der Original-Text (mit Platzhalter), nicht die ausgedehnte Form.
    assert archive.read("scenario.yaml").decode("utf-8") == PARAM_YAML


def test_fa59_web_run_override_changes_the_result(client):
    small, _ = _run(client, config_yaml=PARAM_YAML)
    large, _ = _run(client, config_yaml=PARAM_YAML, parameters={"radius": 3.0})
    widths = []
    for run_id in (small, large):
        archive = zipfile.ZipFile(
            io.BytesIO(client.get(f"/api/runs/{run_id}/download").content)
        )
        frame = gpd.read_file(io.BytesIO(archive.read("catchment.geojson")))
        assert len(frame) > 0
        minx, _miny, maxx, _maxy = frame.total_bounds
        widths.append(maxx - minx)
    # radius 3 km statt 1 km: die Pufferflächen reichen weiter.
    assert widths[1] > widths[0]


def test_fa59_web_run_without_overrides_has_no_parameters_yaml(client):
    run_id, _status = _run(client, config_yaml=PARAM_YAML)
    archive = zipfile.ZipFile(
        io.BytesIO(client.get(f"/api/runs/{run_id}/download").content)
    )
    assert "parameters.yaml" not in archive.namelist()


def test_fa59_web_run_rejects_unknown_override_with_422(client):
    response = client.post(
        "/api/runs", json={"config_yaml": PARAM_YAML, "parameters": {"x": 1}}
    )
    assert response.status_code == 422
    issues = response.json()["detail"]["issues"]
    assert issues[0]["location"] == "parameters -> x"


def test_fa75_web_run_status_carries_node_origins(client):
    run_id, status = _run(client, config_yaml=FOREACH_YAML)
    origins = status["origins"]
    assert origins["puffer[1].x"] == {
        "section": "steps",
        "index": 0,
        "module": "puffer",
        "instance": "puffer",
        "key": "1",
        "inner_id": "x",
        "location": "instances -> puffer[1] -> steps -> x",
    }
    assert origins["puffer[2].x"]["key"] == "2"
    assert origins["substations"]["module"] is None
    assert origins["substations"]["location"] == "layers -> 0"
    # ids with brackets work as URL path segments (encoded like the frontend does)
    node = client.get(f"/api/runs/{run_id}/nodes/{quote('puffer[2].x', safe='')}")
    assert node.status_code == 200, node.text


def test_fa75_web_validate_expansion_carries_node_origins_and_instances(client):
    # Vorschau vor dem Lauf: dieselbe Herkunft wie später RunStatus.origins.
    response = client.post("/api/config/validate", json={"config_yaml": FOREACH_YAML})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"], body["issues"]
    expansion = body["expansion"]
    assert expansion["instances"] == [
        {"id": "puffer", "module": "puffer", "keys": ["1", "2"], "dropped_keys": []}
    ]
    assert "foreach_count" not in expansion and "template_count" not in expansion
    origins = expansion["node_origins"]
    assert origins["puffer[1].x"]["instance"] == "puffer"
    assert origins["puffer[2].x"]["section"] == "steps"


def test_fa75_web_validate_planned_steps_carry_the_expanded_edges(client):
    # Graph vor dem Lauf: der Selektor "puffer[*].x" ist schon zu Kanten je
    # Instanzknoten aufgelöst - dieselben Eingänge wie später StepState.inputs.
    body = client.post(
        "/api/config/validate", json={"config_yaml": FOREACH_YAML}
    ).json()
    assert body["valid"], body["issues"]
    planned = {step["id"]: step for step in body["planned_steps"]}
    assert set(planned) == set(body["execution_order"])
    assert planned["alle"]["op"] == "collect"
    assert sorted(planned["alle"]["inputs"].values()) == ["puffer[1].x", "puffer[2].x"]
    assert planned["puffer[1].x"]["inputs"] == {"geometry": "substations"}
    _, status = _run(client, config_yaml=FOREACH_YAML)
    assert {sid: step["inputs"] for sid, step in status["steps"].items()} == {
        sid: step["inputs"] for sid, step in planned.items()
    }


def test_fa75_web_validate_invalid_config_has_no_planned_steps(client):
    body = client.post(
        "/api/config/validate", json={"config_yaml": "scenario: {}"}
    ).json()
    assert not body["valid"]
    assert body["planned_steps"] is None


def test_fa75_llm_prompt_explains_parameters_modules_selector_and_when():
    from geofact_web import llm

    prompt = llm._SYSTEM_TEMPLATE.format(
        operations="",
        outputs="",
        sources="",
        formats="",
        examples="",
        api_catalog="",
        selected_sources="",
        data_hints="",
    )
    for keyword in (
        "parameters:",
        "modules:",
        "instances:",
        "[*]",
        "when:",
        "${radius}",
    ):
        assert keyword in prompt, keyword
    assert "foreach:" in prompt and "collect:" not in prompt
    # FA65: keine erfundenen Lizenzen
    assert "NUR angeben, wenn die" in prompt and "license:" in prompt


def test_fa59_web_override_on_scenario_without_parameters_is_not_ignored(client):
    body = client.post(  # FOREACH_YAML declares no parameters
        "/api/config/validate",
        json={"config_yaml": FOREACH_YAML, "parameters": {"radius": 2}},
    ).json()
    assert body["valid"] is False
    assert body["issues"][0]["location"] == "parameters -> radius"


def test_fa59_web_form_string_override_is_coerced_to_the_declared_type(client):
    body = client.post(
        "/api/config/validate",
        json={"config_yaml": PARAM_YAML, "parameters": {"radius": "2.5"}},
    ).json()
    assert body["valid"] is True, body["issues"]
    run_id, _status = _run(client, config_yaml=PARAM_YAML, parameters={"radius": "2.5"})
    archive = zipfile.ZipFile(
        io.BytesIO(client.get(f"/api/runs/{run_id}/download").content)
    )
    assert yaml.safe_load(archive.read("parameters.yaml")) == {"radius": 2.5}


def test_fa59_web_form_string_override_of_wrong_type_is_an_issue(client):
    body = client.post(
        "/api/config/validate",
        json={"config_yaml": PARAM_YAML, "parameters": {"radius": "weit"}},
    ).json()
    assert body["valid"] is False
    assert body["issues"][0]["location"] == "parameters -> radius"
