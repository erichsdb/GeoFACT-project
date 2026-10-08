"""Implements: FA83 (Sprachmodell und Anbieter aus einer Vorauswahl des Servers).

Backend: ``GEOFACT_LLM_CHOICES`` nennt weitere wählbare Modelle (mit
Anbieterbindung); eine Erzeugung nimmt eine Wahl als ``options.llm_choice``
entgegen - nur aus dieser Vorauswahl, nie als freien Modellnamen. Frontend:
die reinen Helfer laufen unter node, die Einbindung prüft der Quelltext (kein
Frontend-Testwerkzeug im Repo, siehe test_fa54_frontend_lost_run.py).
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

from geofact_web import generation  # noqa: E402
from geofact_web import options as option_module  # noqa: E402
from geofact_web.generation import TrialRun  # noqa: E402
from geofact_web.llm import LLMClient  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.models import GenerationOptions  # noqa: E402
from geofact_web.options import LLMChoice  # noqa: E402
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


def _settings(**overrides) -> Settings:
    values = {
        "llm_api_key": "k",
        "llm_model": "a/model",
        "llm_providers": ("Relace",),
        "llm_choices_raw": "",
    }
    values.update(overrides)
    return Settings(**values)


# --- Vorauswahl aus der Umgebung ---------------------------------------------


def test_fa83_without_choices_there_is_only_the_server_model():
    assert option_module.llm_choices(_settings()) == [
        LLMChoice(id="a/model@Relace", model="a/model", providers=("Relace",))
    ]


def test_fa83_server_model_without_provider_binding_has_a_plain_id():
    assert option_module.llm_choices(_settings(llm_providers=())) == [
        LLMChoice(id="a/model", model="a/model", providers=())
    ]


def test_fa83_choices_follow_the_server_model_in_the_given_order():
    choices = option_module.llm_choices(
        _settings(
            llm_choices_raw="b/other, c/third@Fireworks+Together ,, a/model@Relace"
        )
    )
    assert choices == [
        LLMChoice("a/model@Relace", "a/model", ("Relace",)),
        LLMChoice("b/other", "b/other", ()),
        LLMChoice("c/third@Fireworks+Together", "c/third", ("Fireworks", "Together")),
    ]  # leere Einträge und das doppelte Server-Modell fallen weg


def test_fa83_same_model_with_another_provider_is_its_own_choice():
    choices = option_module.llm_choices(
        _settings(llm_choices_raw="a/model@Novita, a/model")
    )
    assert [c.id for c in choices] == ["a/model@Relace", "a/model@Novita", "a/model"]


@pytest.mark.parametrize("raw", ["@Relace", "b/other@", "b/other@+", " @ "])
def test_fa83_malformed_choice_is_an_explicit_error(raw):
    """Typfehlerfall: ein Eintrag ohne Modell oder mit leerer Anbieterliste."""
    with pytest.raises(ValueError, match="GEOFACT_LLM_CHOICES"):
        option_module.llm_choices(_settings(llm_choices_raw=raw))


def test_fa83_choices_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEOFACT_LLM_CHOICES", "b/other@Fireworks")
    assert Settings().llm_choices_raw == "b/other@Fireworks"
    monkeypatch.delenv("GEOFACT_LLM_CHOICES")
    assert Settings().llm_choices_raw == ""


# --- Wahl je Anfrage ---------------------------------------------------------


def test_fa83_chosen_model_and_providers_apply_to_this_request_only():
    settings = _settings(llm_choices_raw="b/other@Fireworks+Together")
    effective = generation.apply_options(
        settings, GenerationOptions(llm_choice="b/other@Fireworks+Together")
    )
    assert effective.llm_model == "b/other"
    assert effective.llm_providers == ("Fireworks", "Together")
    assert (settings.llm_model, settings.llm_providers) == ("a/model", ("Relace",))


def test_fa83_choosing_a_model_without_binding_removes_the_server_binding():
    """Randfall: die Anbieterbindung gehört zur Wahl - die des Servers gilt nicht weiter."""
    settings = _settings(llm_choices_raw="b/other")
    effective = generation.apply_options(
        settings, GenerationOptions(llm_choice="b/other")
    )
    assert effective.llm_model == "b/other" and effective.llm_providers == ()


def test_fa83_choosing_the_server_model_changes_nothing():
    settings = _settings(llm_choices_raw="b/other")
    effective = generation.apply_options(
        settings, GenerationOptions(llm_choice="a/model@Relace")
    )
    assert (effective.llm_model, effective.llm_providers) == ("a/model", ("Relace",))


@pytest.mark.parametrize("choice", ["c/unlisted", "b/other@Novita", "a/model", ""])
def test_fa83_a_model_outside_the_preselection_is_rejected(choice):
    """Der Schlüssel des Servers wird nie für ein nicht freigegebenes Modell benutzt."""
    with pytest.raises(ValueError, match="steht nicht zur Wahl"):
        generation.apply_options(
            _settings(llm_choices_raw="b/other"), GenerationOptions(llm_choice=choice)
        )


# --- Endpunkte ---------------------------------------------------------------


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("GEOFACT_LLM_MODEL", "a/model")
    monkeypatch.setenv("GEOFACT_LLM_PROVIDER", "Relace")
    monkeypatch.setenv("GEOFACT_LLM_CHOICES", "b/other@Fireworks+Together, c/third")
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_fa83_meta_lists_the_choices_with_the_server_model_first(http):
    assert http.get("/api/meta").json()["llm_choices"] == [
        {"id": "a/model@Relace", "model": "a/model", "providers": ["Relace"]},
        {
            "id": "b/other@Fireworks+Together",
            "model": "b/other",
            "providers": ["Fireworks", "Together"],
        },
        {"id": "c/third", "model": "c/third", "providers": []},
    ]


def test_fa83_generate_asks_the_chosen_model_with_its_provider_binding(
    http, monkeypatch
):
    payloads: list[dict] = []

    def post_json(self, payload):
        payloads.append(payload)
        return {"choices": [{"message": {"content": _GOOD}, "finish_reason": "stop"}]}

    monkeypatch.setattr(LLMClient, "_post_json", post_json)
    monkeypatch.setattr(generation, "trial_run", lambda *a, **k: TrialRun("ok"))

    chosen = http.post(
        "/api/config/generate",
        json={"prompt": "x", "options": {"llm_choice": "b/other@Fireworks+Together"}},
    )
    unbound = http.post(
        "/api/config/generate",
        json={"prompt": "x", "options": {"llm_choice": "c/third"}},
    )
    default = http.post("/api/config/generate", json={"prompt": "x"})

    assert chosen.status_code == unbound.status_code == default.status_code == 200
    assert chosen.json()["model"] == "b/other" and default.json()["model"] == "a/model"
    assert payloads[0]["model"] == "b/other"
    assert payloads[0]["provider"] == {
        "order": ["Fireworks", "Together"],
        "allow_fallbacks": False,
    }
    assert payloads[1]["model"] == "c/third" and "provider" not in payloads[1]
    assert payloads[2]["model"] == "a/model"
    assert payloads[2]["provider"] == {"order": ["Relace"], "allow_fallbacks": False}


def test_fa83_stream_endpoint_asks_the_chosen_model(http, monkeypatch):
    payloads: list[dict] = []

    def stream_lines(self, payload):
        payloads.append(payload)
        yield "data: " + json.dumps({"choices": [{"delta": {"content": _GOOD}}]})
        yield "data: [DONE]"

    monkeypatch.setattr(LLMClient, "_stream_lines", stream_lines)
    monkeypatch.setattr(generation, "trial_run", lambda *a, **k: TrialRun("ok"))
    response = http.post(
        "/api/config/generate/stream",
        json={"prompt": "x", "options": {"llm_choice": "c/third"}},
    )
    assert response.status_code == 200 and "event: done" in response.text
    assert payloads[0]["model"] == "c/third" and "provider" not in payloads[0]


@pytest.mark.parametrize(
    "path", ["/api/config/generate", "/api/config/generate/stream"]
)
def test_fa83_endpoints_reject_an_unlisted_model_before_asking_any_model(
    http, monkeypatch, path
):
    monkeypatch.setattr(
        LLMClient, "_post_json", lambda self, p: pytest.fail("keine Modellanfrage")
    )
    monkeypatch.setattr(
        LLMClient, "_stream_lines", lambda self, p: pytest.fail("keine Modellanfrage")
    )
    response = http.post(
        path, json={"prompt": "x", "options": {"llm_choice": "teuer/modell"}}
    )
    assert response.status_code == 422
    assert "steht nicht zur Wahl" in response.json()["detail"]


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


def test_fa83_server_settings_take_the_first_choice_as_default(js: dict) -> None:
    full = js["server"]["full"]
    assert full["defaults"]["llm_choice"] == "a/model@Relace"
    assert [c["id"] for c in full["choices"]["llm_choices"]] == [
        "a/model@Relace",
        "b/other",
    ]


def test_fa83_without_meta_there_are_no_settings(js: dict) -> None:
    assert js["server"]["no_meta"] is None
    assert js["server"]["old_backend"] is None  # Backend ohne generation_defaults


def test_fa83_backend_without_choices_yields_exactly_the_server_values(
    js: dict,
) -> None:
    """Randfall: älteres Backend ohne osm_backends/llm_choices - eine Quelle, ein Modell."""
    plain = js["server"]["no_choices"]
    assert plain["choices"] == {
        "osm_backends": ["overpass"],
        "llm_choices": [{"id": "a/model", "model": "a/model", "providers": []}],
    }
    assert (
        plain["defaults"]["osm_backend"] == "overpass"
        and plain["defaults"]["llm_choice"] == "a/model"
    )


def test_fa83_stored_choice_is_kept_only_while_the_server_offers_it(js: dict) -> None:
    assert js["stored"]["choices"] == {
        "osm_backend": "overpass",
        "llm_choice": "b/other",
    }
    assert js["stored"]["choices_not_offered"] == {}
    # solange die Auswahl des Servers unbekannt ist, bleibt die Wahl vorerst stehen
    assert js["stored"]["choices_unchecked"] == {
        "osm_backend": "planet",
        "llm_choice": "c/unlisted",
    }


def test_fa83_choice_equal_to_the_server_value_is_not_an_override(js: dict) -> None:
    assert js["overrides"]["same"] == {}
    assert js["overrides"]["choices"] == {
        "osm_backend": "overpass",
        "llm_choice": "b/other",
    }


def test_fa83_label_names_model_and_bound_providers(js: dict) -> None:
    assert js["labels"] == ["a/model · Relace", "b/other"]
    assert "Sprachmodell b/other" in js["describe"]["all"]
    assert "OSM-Quelle overpass" in js["describe"]["all"]


# --- Frontend: Einbindung (Quelltextverträge) -------------------------------


def _read(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_fa83_dialog_offers_the_model_only_as_a_choice_when_there_is_a_preselection() -> (
    None
):
    dialog = _read("components", "generation-settings.tsx")
    assert 'id="settings-llm-choice"' in dialog
    assert "choices.llm_choices.length > 1 ? (" in dialog
    assert "{llmChoiceLabel(choice)}" in dialog
    assert "Der Server gibt keine Auswahl vor (GEOFACT_LLM_CHOICES)." in dialog
    # kein freies Eingabefeld für einen Modellnamen
    assert (
        'id="settings-llm-choice"'
        not in dialog[dialog.index("<Input") :].split("/>")[0]
    )


def test_fa83_chosen_model_is_sent_with_the_generation_and_named_at_the_settings_button() -> (
    None
):
    shell = _read("components", "app-shell.tsx")
    assert "options={generationOptions}" in shell
    dialog = _read("components", "generation-settings.tsx")
    assert "Sprachmodell: ${activeLlm?.model ?? active.llm_choice}" in dialog
    # die Kopfzeile meldet sich nur, wenn kein Sprachmodell konfiguriert ist
    assert "{meta && !meta.llm_configured && (" in shell
