"""Implements: FA31 (LLM-Anbindung: Reparatur-Runde geerdet, Gesamtfrist je Anfrage).

Regressionstests zu zwei Befunden der LLM-Vorstudie (03.10.2026):

1. Die Reparatur-Runde bekam nur einen Einzeiler als System-Prompt, nicht den
   Operationskatalog. Das Modell erfand dort Operationen (``add_field``) und
   Port-Namen. Jetzt geht in beiden Pfaden (``/generate`` und
   ``/generate/stream``) derselbe System-Prompt mit.
2. httpx' ``timeout`` gilt je Leseoperation; zwei Anfragen hingen über
   20 Minuten. Jetzt hat jede Anfrage eine Gesamtfrist
   (``GEOFACT_LLM_DEADLINE_S``) mit expliziter Fehlermeldung.

Kein echter Anbieter: die blockierenden Netzfunktionen werden gepatcht.
"""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import llm as llm_module  # noqa: E402
from geofact_web.llm import LLMClient, LLMError, build_system_prompt  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.settings import Settings, get_settings  # noqa: E402

_INVALID_YAML = "scenario:\n  name: kaputt\n"

_VALID_YAML = """\
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

_REQUEST = {
    "prompt": "Puffer um alle Umspannwerke",
    "region": "Dresden, Deutschland",
    # eine API-Auswahl, damit das Grounding mehr als den Grundtext enthält
    "source_ids": ["bkg_vg250"],
}


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def _settings(**overrides) -> Settings:
    values = {
        "llm_api_key": "test-key",
        "llm_model": "test/model",
        "llm_deadline_s": 300.0,
    }
    values.update(overrides)
    return Settings(**values)


# =====================================================================
# Reparatur-Runde mit demselben System-Prompt wie die Erzeugung
# =====================================================================


def test_fa31_repair_round_uses_the_generation_system_prompt(http, monkeypatch):
    """Nachbedingung (nicht streamender Pfad): die Reparatur-Anfrage trägt
    denselben System-Prompt wie die Erzeugung - mit Operationskatalog und der
    ausgewählten API - und die ursprüngliche Anfrage."""
    calls: list[list[dict]] = []
    answers = iter([_INVALID_YAML, _VALID_YAML])

    def fake_chat(self, messages):
        calls.append(messages)
        return next(answers)

    monkeypatch.setattr(LLMClient, "_chat", fake_chat)
    response = http.post("/api/config/generate", json=_REQUEST)

    assert response.status_code == 200, response.text
    assert response.json()["validation"]["valid"] is True
    assert len(calls) == 2
    generation_system, repair_system = calls[0][0], calls[1][0]
    assert generation_system["role"] == repair_system["role"] == "system"
    assert repair_system["content"] == generation_system["content"]
    # der Katalog steht wirklich darin (nicht nur zwei gleiche Einzeiler)
    assert "buffer" in repair_system["content"]
    assert "vg250:vg250_lan" in repair_system["content"]
    repair_user = calls[1][1]["content"]
    assert "Puffer um alle Umspannwerke" in repair_user
    assert "Dresden, Deutschland" in repair_user
    assert "kaputt" in repair_user


def test_fa31_stream_repair_round_uses_the_generation_system_prompt(http, monkeypatch):
    """Nachbedingung (Streaming-Pfad): wie oben, der System-Prompt der
    Reparatur ist der des gestreamten Erzeugungsaufrufs. Die Reparatur streamt
    dort selbst (FA78) - sie ist die zweite streamende Anfrage."""
    stream_payloads: list[dict] = []

    def fake_stream_lines(self, payload):
        stream_payloads.append(payload)
        content = _INVALID_YAML if len(stream_payloads) == 1 else _VALID_YAML
        yield "data: " + json.dumps({"choices": [{"delta": {"content": content}}]})
        yield "data: [DONE]"

    monkeypatch.setattr(LLMClient, "_stream_lines", fake_stream_lines)
    monkeypatch.setattr(
        LLMClient,
        "_chat",
        lambda self, messages: pytest.fail("Reparatur muss streamen"),
    )
    response = http.post("/api/config/generate/stream", json=_REQUEST)

    assert response.status_code == 200
    assert '"phase": "repairing"' in response.text
    assert "event: done" in response.text
    assert len(stream_payloads) == 2
    generation_system = stream_payloads[0]["messages"][0]
    repair_messages = stream_payloads[1]["messages"]
    assert repair_messages[0] == generation_system
    assert "vg250:vg250_lan" in repair_messages[0]["content"]
    assert "Puffer um alle Umspannwerke" in repair_messages[1]["content"]


def test_fa31_repair_config_builds_the_same_prompt_as_build_system_prompt(monkeypatch):
    """Vertrag der Methode: System-Prompt = build_system_prompt(templates, hints)."""
    captured: list[list[dict]] = []
    monkeypatch.setattr(
        LLMClient, "_chat", lambda self, m: captured.append(m) or _VALID_YAML
    )
    templates = [{"id": "x", "source": "osm", "tags": {"power": "substation"}}]
    hints = ["Bekannte Spalte: voltage"]

    LLMClient(_settings()).repair_config("a: 1", ["steps: fehlt"], templates, hints)

    assert captured[0][0]["content"] == build_system_prompt(templates, hints)


# =====================================================================
# Gesamtfrist je Anfrage
# =====================================================================


def test_fa31_request_deadline_aborts_a_hanging_request(monkeypatch):
    """Vorbedingung: der Anbieter antwortet nicht. Nachbedingung: nach der
    Gesamtfrist kommt ein expliziter LLMError, nicht erst nach dem
    Lese-Timeout von httpx oder nie."""

    def hanging_post(self, payload):
        time.sleep(5)
        return {"choices": [{"message": {"content": "zu spät"}}]}

    monkeypatch.setattr(LLMClient, "_post_json", hanging_post)
    client = LLMClient(_settings(llm_deadline_s=0.3))

    started = time.monotonic()
    with pytest.raises(LLMError, match="Gesamtfrist"):
        client.generate_config("x", [], None, [])
    assert time.monotonic() - started < 2.0


def test_fa31_request_deadline_covers_a_trickling_stream(monkeypatch):
    """Der eigentliche Befund: ein Strom, der regelmäßig ein Stück liefert,
    löst den Lese-Timeout nie aus. Die Gesamtfrist bricht ihn trotzdem ab."""

    def trickling_lines(self, payload):
        while True:
            time.sleep(0.05)
            yield ": keep-alive"

    monkeypatch.setattr(LLMClient, "_stream_lines", trickling_lines)
    client = LLMClient(_settings(llm_deadline_s=0.4))

    started = time.monotonic()
    with pytest.raises(LLMError, match="0.4 s abgebrochen"):
        list(client.stream_generate_config("x", [], None, []))
    assert time.monotonic() - started < 2.0


def test_fa31_request_within_the_deadline_is_unchanged(monkeypatch):
    """Randfall: eine Antwort innerhalb der Frist kommt unverändert an, und
    Fehler der Anfrage selbst bleiben ihre eigenen Fehler."""
    monkeypatch.setattr(
        LLMClient,
        "_post_json",
        lambda self, payload: {
            "choices": [{"message": {"content": "```yaml\na: 1\n```"}}]
        },
    )
    assert (
        LLMClient(_settings(llm_deadline_s=5)).generate_config("x", [], None, [])
        == "a: 1"
    )

    def failing_post(self, payload):
        raise LLMError("LLM-Anbieter antwortete mit 429: rate limit")

    monkeypatch.setattr(LLMClient, "_post_json", failing_post)
    with pytest.raises(LLMError, match="429"):
        LLMClient(_settings(llm_deadline_s=5)).generate_config("x", [], None, [])


def test_fa31_non_stream_payload_has_max_tokens(monkeypatch):
    """NFA2: auch die nicht streamende Anfrage (Erzeugung und Reparatur)
    trägt max_tokens."""
    captured: dict = {}

    def capture(self, payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "a: 1"}}]}

    monkeypatch.setattr(LLMClient, "_post_json", capture)
    LLMClient(_settings(llm_max_tokens=1234)).repair_config("a: 1", ["x"], [], [])
    assert captured["max_tokens"] == 1234


def test_fa31_settings_deadline_default_and_override(monkeypatch):
    monkeypatch.delenv("GEOFACT_LLM_DEADLINE_S", raising=False)
    assert Settings().llm_deadline_s == 300
    monkeypatch.setenv("GEOFACT_LLM_DEADLINE_S", "45")
    assert Settings().llm_deadline_s == 45


def test_fa31_deadline_error_reaches_the_endpoint_as_502(http, monkeypatch):
    """Nachbedingung im Endpunkt: die Fristüberschreitung ist ein 502 mit
    der Ursache, kein hängender Request."""

    def deadline(self, *args, **kwargs):
        raise llm_module._deadline_error(300)

    monkeypatch.setattr(LLMClient, "generate_config", deadline)
    response = http.post("/api/config/generate", json={"prompt": "x"})
    assert response.status_code == 502
    assert "GEOFACT_LLM_DEADLINE_S" in response.json()["detail"]


# =====================================================================
# Prompt-Hinweis zu gemischter OSM-Geometrie vor aggregate (Vorstudie P02)
# =====================================================================


def test_fa31_prompt_recommends_to_points_before_aggregate():
    """Befund der Vorstudie: alle drei Läufe von P02 zählten Apotheken mit
    aggregate auf dem gemischten OSM-Layer und fanden 4 statt 51, weil der
    Prompt to_points nur vor spatial_join/overlay empfahl."""
    prompt = " ".join(build_system_prompt([], []).split())
    assert "vor spatial_join/overlay/ aggregate einen 'to_points'-Schritt" in prompt


# =====================================================================
# Anbieterbindung und Reasoning in allen Anfragen (Vorstudie, Befund 7)
# =====================================================================


def _payloads(monkeypatch, client: LLMClient) -> dict[str, dict]:
    """Die drei Anfragen (Erzeugung, Streaming, Reparatur) eines Clients."""
    posted: list[dict] = []
    streamed: list[dict] = []

    def post(self, payload):
        posted.append(payload)
        return {"choices": [{"message": {"content": "a: 1"}}]}

    def stream(self, payload):
        streamed.append(payload)
        yield "data: [DONE]"

    monkeypatch.setattr(LLMClient, "_post_json", post)
    monkeypatch.setattr(LLMClient, "_stream_lines", stream)
    client.generate_config("x", [], None, [])
    client.repair_config("a: 1", ["x"], [], [])
    list(client.stream_generate_config("x", [], None, []))
    generate, repair = posted
    return {"generate": generate, "repair": repair, "stream": streamed[0]}


def test_fa31_no_provider_field_without_configuration(monkeypatch):
    """Nachbedingung (1): ohne GEOFACT_LLM_PROVIDER bleibt die Anfrage, wie sie war."""
    # ausdrücklich leer: eine lokal in .env gesetzte Bindung darf nicht durchsickern
    payloads = _payloads(monkeypatch, LLMClient(_settings(llm_providers=())))
    assert set(payloads) == {"generate", "repair", "stream"}
    assert all("provider" not in payload for payload in payloads.values())


def test_fa31_provider_binding_is_in_every_request(monkeypatch):
    """Nachbedingung (2): Erzeugung, Streaming und Reparatur tragen dieselbe
    Bindung, in der angegebenen Reihenfolge und ohne Ausweichen."""
    client = LLMClient(_settings(llm_providers=("Fireworks", "DeepInfra")))
    for payload in _payloads(monkeypatch, client).values():
        assert payload["provider"] == {
            "order": ["Fireworks", "DeepInfra"],
            "allow_fallbacks": False,
        }


def test_fa31_all_requests_share_temperature_max_tokens_and_reasoning(monkeypatch):
    """Nachbedingung (3): in der Vorstudie fehlte reasoning im nicht
    streamenden Pfad; jetzt unterscheiden sich die Anfragen nur in 'stream'."""
    payloads = _payloads(
        monkeypatch, LLMClient(_settings(llm_max_tokens=777, llm_reasoning=True))
    )
    for payload in payloads.values():
        assert payload["temperature"] == 0.2
        assert payload["max_tokens"] == 777
        assert payload["reasoning"] == {"enabled": True}
    assert payloads["stream"]["stream"] is True
    assert "stream" not in payloads["generate"] and "stream" not in payloads["repair"]

    off = _payloads(monkeypatch, LLMClient(_settings(llm_reasoning=False)))
    assert all("reasoning" not in payload for payload in off.values())


def test_fa31_settings_read_the_provider_list(monkeypatch):
    monkeypatch.delenv("GEOFACT_LLM_PROVIDER", raising=False)
    assert Settings().llm_providers == ()
    monkeypatch.setenv("GEOFACT_LLM_PROVIDER", " Fireworks , DeepInfra,")
    assert Settings().llm_providers == ("Fireworks", "DeepInfra")


def test_fa31_empty_answer_is_retried_once_with_more_room(monkeypatch):
    """Randfall (Stichprobe 05.10.2026, P05/P08): mit Reasoning verbraucht das
    Modell max_tokens in der Denkphase und antwortet ohne Inhalt. Es folgt
    genau eine Wiederholung mit doppeltem Limit und knappem Reasoning - nicht
    mit abgeschaltetem: darauf antworten manche Endpunkte mit 400."""
    payloads: list[dict] = []

    def post(self, payload):
        payloads.append(payload)
        content = "a: 1" if len(payloads) == 2 else None
        return {
            "choices": [{"message": {"content": content}, "finish_reason": "length"}]
        }

    monkeypatch.setattr(LLMClient, "_post_json", post)
    client = LLMClient(_settings(llm_reasoning=True, llm_max_tokens=1000))
    assert client.generate_config("x", [], None, []) == "a: 1"
    first, second = payloads
    assert (first["max_tokens"], first["reasoning"]) == (1000, {"enabled": True})
    assert (second["max_tokens"], second["reasoning"]) == (2000, {"effort": "low"})


def test_fa31_empty_answer_is_an_explicit_error(monkeypatch):
    """Bleibt die Antwort auch in der Wiederholung leer, ist das ein LLMError
    mit Ursache - kein Absturz und keine leere Konfiguration. Ohne Reasoning
    trägt die Wiederholung kein reasoning-Feld."""
    calls: list[dict] = []

    def post(self, payload):
        calls.append(payload)
        return {"choices": [{"message": {"content": "  "}, "finish_reason": "length"}]}

    monkeypatch.setattr(LLMClient, "_post_json", post)
    with pytest.raises(LLMError, match="ohne Inhalt.*length.*GEOFACT_LLM_MAX_TOKENS"):
        LLMClient(_settings(llm_reasoning=False)).generate_config("x", [], None, [])
    assert len(calls) == 2
    assert all("reasoning" not in payload for payload in calls)
