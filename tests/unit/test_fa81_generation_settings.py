"""Implements: FA81 (Einstellungen der Erzeugung je Anfrage: Fristen und Probelauf).

Backend: ``GenerationOptions`` in der Anfrage überschreibt die Werte des Servers
für genau diese Erzeugung; ``/api/meta`` nennt die Werte des Servers (die
Umgebungsvariablen) als Vorbelegung. Frontend: die reinen Helfer
(`frontend/lib/generationSettings.ts`) laufen unter node, die Einbindung des
Dialogs prüft der Quelltext (kein Frontend-Testwerkzeug im Repo, siehe
test_fa54_frontend_lost_run.py).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from geofact_web import generation  # noqa: E402
from geofact_web.generation import TrialRun  # noqa: E402
from geofact_web.llm import LLMClient  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.models import (  # noqa: E402
    MAX_LLM_DEADLINE_S,
    MAX_TRIAL_RUN_S,
    GenerateRequest,
    GenerationOptions,
)
from geofact_web.settings import Settings, get_settings  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
HELPERS = FRONTEND / "lib" / "generationSettings.ts"
DRIVER = Path(__file__).resolve().parent / "fa81_generation_settings_driver.mjs"

_GOOD = """\
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

_ENV = ("GEOFACT_LLM_DEADLINE_S", "GEOFACT_LLM_TRIAL_RUN", "GEOFACT_LLM_TRIAL_RUN_S")


def _settings(**overrides) -> Settings:
    values = {
        "llm_api_key": "test-key",
        "llm_model": "test/model",
        "llm_deadline_s": 300.0,
        "llm_trial_run": True,
        "llm_trial_run_s": 60.0,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


# --- Vertrag der Optionen ----------------------------------------------------


def test_fa81_no_options_keep_the_server_settings():
    settings = _settings()
    assert generation.apply_options(settings, None) is settings
    assert generation.apply_options(settings, GenerationOptions()) is settings


def test_fa81_set_fields_override_only_this_request():
    settings = _settings()
    effective = generation.apply_options(
        settings, GenerationOptions(llm_deadline_s=120, trial_run=False, trial_run_s=30)
    )
    assert (
        effective.llm_deadline_s,
        effective.llm_trial_run,
        effective.llm_trial_run_s,
    ) == (
        120,
        False,
        30,
    )
    # die Einstellungen des Prozesses bleiben, wie sie sind
    assert (
        settings.llm_deadline_s,
        settings.llm_trial_run,
        settings.llm_trial_run_s,
    ) == (
        300.0,
        True,
        60.0,
    )
    # alles andere wird übernommen
    assert effective.llm_model == settings.llm_model
    assert effective.llm_repair_rounds == settings.llm_repair_rounds


def test_fa81_a_single_field_leaves_the_others_on_the_server_value():
    effective = generation.apply_options(
        _settings(), GenerationOptions(trial_run=False)
    )
    assert effective.llm_trial_run is False
    assert effective.llm_deadline_s == 300.0 and effective.llm_trial_run_s == 60.0


@pytest.mark.parametrize(
    "field, value",
    [
        ("llm_deadline_s", 0),
        ("llm_deadline_s", -5),
        ("llm_deadline_s", MAX_LLM_DEADLINE_S + 1),
        ("llm_deadline_s", float("nan")),
        ("llm_deadline_s", float("inf")),
        ("llm_deadline_s", "abc"),
        ("trial_run_s", 0),
        ("trial_run_s", MAX_TRIAL_RUN_S + 1),
        ("trial_run", "vielleicht"),
    ],
)
def test_fa81_values_outside_the_contract_are_rejected(field, value):
    """Typfehler- und Grenzfall: keine Frist <= 0, keine über der Obergrenze,
    keine Nicht-Zahl - ausdrücklicher Fehler statt stiller Korrektur."""
    with pytest.raises(ValidationError):
        GenerationOptions(**{field: value})


def test_fa81_limits_themselves_are_allowed():
    options = GenerationOptions(
        llm_deadline_s=MAX_LLM_DEADLINE_S, trial_run_s=MAX_TRIAL_RUN_S
    )
    assert (
        options.llm_deadline_s == MAX_LLM_DEADLINE_S
        and options.trial_run_s == MAX_TRIAL_RUN_S
    )


def test_fa81_unknown_option_is_rejected():
    with pytest.raises(ValidationError):
        GenerationOptions(repair_rounds=9)


def test_fa81_request_without_options_is_the_previous_request():
    assert GenerateRequest(prompt="x").options is None


# --- /api/meta: die Defaults sind die Umgebungsvariablen ---------------------


def test_fa81_meta_reports_the_built_in_defaults(http):
    meta = http.get("/api/meta").json()
    assert meta["generation_defaults"] == {
        "llm_deadline_s": 300.0,
        "trial_run": True,
        "trial_run_s": 60.0,
    }
    assert meta["generation_limits"] == {
        "llm_deadline_s": MAX_LLM_DEADLINE_S,
        "trial_run_s": MAX_TRIAL_RUN_S,
    }


def test_fa81_meta_defaults_follow_the_environment(monkeypatch):
    monkeypatch.setenv("GEOFACT_LLM_DEADLINE_S", "123")
    monkeypatch.setenv("GEOFACT_LLM_TRIAL_RUN", "false")
    monkeypatch.setenv("GEOFACT_LLM_TRIAL_RUN_S", "45")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            defaults = client.get("/api/meta").json()["generation_defaults"]
    finally:
        get_settings.cache_clear()
    assert defaults == {
        "llm_deadline_s": 123.0,
        "trial_run": False,
        "trial_run_s": 45.0,
    }


# --- Endpunkte ---------------------------------------------------------------


def test_fa81_generate_without_options_uses_the_server_values(http, monkeypatch):
    seen: dict = {}

    def chat(self, messages):
        seen["deadline"] = self._settings.llm_deadline_s
        return _GOOD

    def trial(document, timeout_s, connector_options=None):
        seen["trial_s"] = timeout_s
        return TrialRun("ok")

    monkeypatch.setattr(LLMClient, "_chat", chat)
    monkeypatch.setattr(generation, "trial_run", trial)
    response = http.post("/api/config/generate", json={"prompt": "x"})

    assert response.status_code == 200, response.text
    assert seen == {"deadline": 300.0, "trial_s": 60.0}


def test_fa81_generate_applies_the_deadlines_of_the_request(http, monkeypatch):
    seen: dict = {}

    def chat(self, messages):
        seen["deadline"] = self._settings.llm_deadline_s
        return _GOOD

    def trial(document, timeout_s, connector_options=None):
        seen["trial_s"] = timeout_s
        return TrialRun("ok")

    monkeypatch.setattr(LLMClient, "_chat", chat)
    monkeypatch.setattr(generation, "trial_run", trial)
    response = http.post(
        "/api/config/generate",
        json={"prompt": "x", "options": {"llm_deadline_s": 90, "trial_run_s": 15}},
    )

    assert response.status_code == 200, response.text
    assert seen == {"deadline": 90.0, "trial_s": 15.0}
    # die nächste Anfrage ohne Optionen gilt wieder mit den Werten des Servers
    http.post("/api/config/generate", json={"prompt": "x"})
    assert seen == {"deadline": 300.0, "trial_s": 60.0}


def test_fa81_generate_can_switch_the_trial_run_off(http, monkeypatch):
    monkeypatch.setattr(LLMClient, "_chat", lambda self, messages: _GOOD)
    monkeypatch.setattr(
        generation, "trial_run", lambda *a, **k: pytest.fail("kein Probelauf")
    )
    response = http.post(
        "/api/config/generate", json={"prompt": "x", "options": {"trial_run": False}}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validation"]["valid"] is True
    assert body["trial_run"]["status"] == "skipped"


def test_fa81_request_can_switch_the_trial_run_on_against_the_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("GEOFACT_LLM_TRIAL_RUN", "false")
    monkeypatch.setenv("GEOFACT_LLM_TRIAL_RUN_S", "60")
    get_settings.cache_clear()
    ran: list = []
    monkeypatch.setattr(LLMClient, "_chat", lambda self, messages: _GOOD)
    monkeypatch.setattr(
        generation,
        "trial_run",
        lambda document, timeout_s, connector_options=None: (
            ran.append(timeout_s) or TrialRun("ok")
        ),
    )
    try:
        with TestClient(create_app()) as client:
            off = client.post("/api/config/generate", json={"prompt": "x"}).json()
            on = client.post(
                "/api/config/generate",
                json={"prompt": "x", "options": {"trial_run": True}},
            ).json()
    finally:
        get_settings.cache_clear()
    assert off["trial_run"]["status"] == "skipped"
    assert on["trial_run"]["status"] == "ok" and ran == [60.0]


def test_fa81_timeout_note_names_the_deadline_of_the_request(http, monkeypatch):
    monkeypatch.setattr(LLMClient, "_chat", lambda self, messages: _GOOD)
    monkeypatch.setattr(generation, "trial_run", lambda *a, **k: TrialRun("timeout"))
    body = http.post(
        "/api/config/generate", json={"prompt": "x", "options": {"trial_run_s": 15}}
    ).json()
    assert body["trial_run"]["status"] == "timeout"
    assert "nach 15 s" in body["notes"]


def test_fa81_stream_endpoint_applies_the_options(http, monkeypatch):
    seen: dict = {}

    def stream_lines(self, payload):
        seen["deadline"] = self._settings.llm_deadline_s
        yield "data: " + json.dumps({"choices": [{"delta": {"content": _GOOD}}]})
        yield "data: [DONE]"

    monkeypatch.setattr(LLMClient, "_stream_lines", stream_lines)
    monkeypatch.setattr(
        generation, "trial_run", lambda *a, **k: pytest.fail("kein Probelauf")
    )
    response = http.post(
        "/api/config/generate/stream",
        json={"prompt": "x", "options": {"llm_deadline_s": 75, "trial_run": False}},
    )

    assert response.status_code == 200
    assert seen == {"deadline": 75.0}
    assert '"phase": "trial_run"' not in response.text
    assert "event: done" in response.text


@pytest.mark.parametrize(
    "options",
    [
        {"llm_deadline_s": 0},
        {"llm_deadline_s": MAX_LLM_DEADLINE_S + 1},
        {"trial_run_s": -1},
        {"trial_run_s": "lang"},
        {"unbekannt": 1},
    ],
)
@pytest.mark.parametrize(
    "path", ["/api/config/generate", "/api/config/generate/stream"]
)
def test_fa81_endpoints_reject_invalid_options_before_asking_the_model(
    http, monkeypatch, path, options
):
    monkeypatch.setattr(
        LLMClient, "_chat", lambda self, m: pytest.fail("keine Modellanfrage")
    )
    monkeypatch.setattr(
        LLMClient, "_stream_lines", lambda self, p: pytest.fail("keine Modellanfrage")
    )
    response = http.post(path, json={"prompt": "x", "options": options})
    assert response.status_code == 422, response.text


# --- Frontend: reine Helfer (unter node ausgeführt) -------------------------


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(DRIVER), str(HELPERS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"node cannot run the TypeScript helpers: {proc.stderr[-300:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_fa81_nothing_stored_means_the_server_values(js: dict) -> None:
    for key in ("none", "empty", "broken", "array"):
        assert js["stored"][key] == {}, key
    assert js["effective"]["none"] == {
        "llm_deadline_s": 300,
        "trial_run": True,
        "trial_run_s": 60,
        "osm_backend": "postgis",
        "llm_choice": "a/model@Relace",
    }
    assert js["has"] == {"empty": False, "some": True}


def test_fa81_stored_overrides_are_read_back_and_applied(js: dict) -> None:
    assert js["stored"]["full"] == {
        "llm_deadline_s": 120,
        "trial_run": False,
        "trial_run_s": 30,
    }
    assert js["effective"]["some"] == {
        "llm_deadline_s": 120,
        "trial_run": False,
        "trial_run_s": 60,
        "osm_backend": "overpass",
        "llm_choice": "a/model@Relace",
    }


def test_fa81_unusable_stored_values_fall_back_to_the_server_value(js: dict) -> None:
    """Typfehler- und Grenzfall im Browser: falscher Typ, außerhalb der Grenzen,
    unbekanntes Feld - nie ein Wert, den das Backend ablehnen würde."""
    assert js["stored"]["wrong_types"] == {}
    assert js["stored"]["out_of_range"] == {}
    assert js["stored"]["unknown_field"] == {"trial_run": False}
    # ohne bekannte Grenzen (Meta noch nicht geladen) bleibt der Wert vorerst stehen
    assert js["stored"]["no_limits"] == {"llm_deadline_s": 5000}


def test_fa81_only_deviations_from_the_server_value_are_kept(js: dict) -> None:
    assert js["overrides"]["same"] == {}
    assert js["overrides"]["changed"] == {"llm_deadline_s": 120, "trial_run": False}


def test_fa81_seconds_input_is_checked_with_a_message(js: dict) -> None:
    seconds = js["seconds"]
    assert seconds["120"] == {"ok": True, "value": 120}
    assert seconds[" 45,5 "] == {"ok": True, "value": 45.5}
    assert seconds["0.5"] == {"ok": True, "value": 0.5}
    assert seconds["1800"] == {"ok": True, "value": 1800}
    for bad in ("", "abc", "0", "-3", "1800.1", "Infinity"):
        assert seconds[bad]["ok"] is False and seconds[bad]["error"], bad


def test_fa81_overrides_are_described_for_the_button(js: dict) -> None:
    assert js["describe"]["empty"] == ""
    text = js["describe"]["all"]
    assert "120 s" in text and "Probelauf aus" in text and "30 s" in text


# --- Frontend: Einbindung (Quelltextverträge) -------------------------------


def _read(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_fa81_settings_button_sits_in_the_header() -> None:
    shell = _read("components", "app-shell.tsx")
    header = shell[shell.index("<header") : shell.index("</header>")]
    assert "<GenerationSettings" in header
    assert "server={settingsServer}" in header
    assert (
        "const settingsServer = useMemo(() => serverSettings(meta), [meta]);" in shell
    )


def test_fa81_dialog_offers_the_three_settings_and_the_server_defaults() -> None:
    dialog = _read("components", "generation-settings.tsx")
    for element in (
        'id="generation-deadline"',
        'id="generation-trial-run"',
        'id="generation-trial-seconds"',
    ):
        assert element in dialog, element
    assert "<Switch" in dialog and "disabled={!trialRun}" in dialog
    assert "Standard: ${defaults.llm_deadline_s} s" in dialog
    assert "Standard: ${defaults.trial_run_s} s" in dialog
    assert 'defaults.trial_run ? "an" : "aus"' in dialog
    # ungültige Eingabe: Meldung am Feld, kein Übernehmen
    assert "disabled={!valid}" in dialog and "deadlineResult.error" in dialog
    # zurück auf die Werte des Servers
    assert (
        "Standardwerte" in dialog
        and "setDeadline(String(defaults.llm_deadline_s))" in dialog
    )
    # ohne Server-Werte kein Dialog
    assert "disabled={!server}" in dialog


def test_fa81_overrides_are_stored_in_the_browser_and_sent_with_the_generation() -> (
    None
):
    shell = _read("components", "app-shell.tsx")
    assert (
        "parseStoredOptions(readLocalStorage(GENERATION_SETTINGS_STORAGE_KEY))" in shell
    )
    assert (
        "localStorage.setItem(GENERATION_SETTINGS_STORAGE_KEY, JSON.stringify(generationOptions))"
        in shell
    )
    assert "options={generationOptions}" in shell
    panel = _read("components", "prompt-panel.tsx")
    call = panel[
        panel.index("api.generateStream(") : panel.index("onGenerated(res.config_yaml")
    ]
    assert "options" in call
    api_ts = _read("lib", "api.ts")
    assert (
        "...(options && Object.keys(options).length > 0 ? { options } : {})" in api_ts
    )
    assert 'GENERATION_SETTINGS_STORAGE_KEY = "geofact:generationSettings"' in _read(
        "lib", "generationSettings.ts"
    )
