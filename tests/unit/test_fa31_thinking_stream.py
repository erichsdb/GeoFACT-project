"""Implements: FA31 (Reasoning-/Thinking-Ausgabe mitstreamen).

Contract-Tests für die Chunk-Zerlegung des LLM-Streams und die
SSE-Ereignisse des /api/config/generate/stream-Endpunkts. Kein echter
LLM-/Netzzugriff - der Client wird gepatcht.

Motivierender Fall: bei langen Generierungen sah der Nutzer nur einen
Spinner. Reasoning-Deltas kommen mit content=None an und wurden von der
alten Extraktion (nur delta.content) vollständig verworfen.
"""

from __future__ import annotations

import json

import pytest

# llm.py braucht httpx (Web-Extra); ohne Extra überspringt sich das ganze Modul.
pytest.importorskip("httpx")

from geofact_web import llm as llm_module  # noqa: E402
from geofact_web.llm import _stream_chunk_deltas  # noqa: E402


def chunk(delta: dict) -> dict:
    return {"choices": [{"delta": delta, "index": 0}]}


# =====================================================================
# Chunk-Zerlegung: getrennte Kanäle für Text und Reasoning
# =====================================================================


def test_fa31_content_delta_yields_token():
    assert list(_stream_chunk_deltas(chunk({"content": "scenario:"}))) == [
        ("token", "scenario:")
    ]


def test_fa31_reasoning_delta_yields_thinking():
    """Kernfall: Reasoning-Chunk tragt content=None und wurde zuvor
    verworfen."""
    assert list(
        _stream_chunk_deltas(chunk({"reasoning": "Ich prüfe ", "content": None}))
    ) == [("thinking", "Ich prüfe ")]


def test_fa31_reasoning_content_field_also_recognized():
    """Anbieterunterschied: manche spiegeln das Feld als
    'reasoning_content' - beide Namen werden gelesen, damit ein
    Anbieterwechsel den Kanal nicht stillschweigend leer lässt."""
    assert list(_stream_chunk_deltas(chunk({"reasoning_content": "denk"}))) == [
        ("thinking", "denk")
    ]


def test_fa31_thinking_is_not_mixed_into_yaml_channel():
    """Nachbedingung: Reasoning darf NICHT im token-Kanal landen - es ist
    nicht Teil der erzeugten YAML."""
    deltas = list(
        _stream_chunk_deltas(chunk({"reasoning": "Überlegung", "content": "layers:"}))
    )
    assert ("thinking", "Überlegung") in deltas
    assert ("token", "layers:") in deltas
    assert [kind for kind, _ in deltas] == ["thinking", "token"]


def test_fa31_empty_and_missing_deltas_yield_nothing():
    """Randfälle: leere Strings, fehlende Felder, leeres choices."""
    assert list(_stream_chunk_deltas(chunk({"content": ""}))) == []
    assert list(_stream_chunk_deltas(chunk({"reasoning": ""}))) == []
    assert list(_stream_chunk_deltas(chunk({}))) == []
    assert list(_stream_chunk_deltas({})) == []
    assert list(_stream_chunk_deltas({"choices": []})) == []


def test_fa31_non_string_delta_is_ignored():
    """Randfall: ein Anbieter, der ein Objekt statt eines Strings
    schickt, darf keinen TypeError auslösen."""
    assert list(_stream_chunk_deltas(chunk({"reasoning": {"text": "x"}}))) == []
    assert list(_stream_chunk_deltas(chunk({"content": 42}))) == []


# =====================================================================
# Request-Seite: Abbruchkriterium und Reasoning-Opt-in
# =====================================================================


def test_fa31_stream_payload_has_max_tokens_and_reasoning(monkeypatch):
    """Vorbedingung/NFA2: die Anfrage tragt ein max_tokens-Limit (sonst
    kann das Modell praktisch endlos generieren) und - falls aktiviert -
    das Reasoning-Opt-in."""
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def iter_lines(self):
            yield "data: " + json.dumps(chunk({"reasoning": "denke"}))
            yield "data: " + json.dumps(chunk({"content": "scenario:"}))
            yield "data: [DONE]"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured.update(json or {})
            return FakeResponse()

    monkeypatch.setattr(llm_module.httpx, "Client", FakeClient)

    settings = _settings(reasoning=True, max_tokens=1234)
    client = llm_module.LLMClient(settings)
    result = list(client.stream_generate_config("finde etwas", [], None, []))

    assert captured["max_tokens"] == 1234
    assert captured["reasoning"] == {"enabled": True}
    assert captured["stream"] is True
    assert result == [("thinking", "denke"), ("token", "scenario:")]


def test_fa31_reasoning_can_be_disabled(monkeypatch):
    """Abgrenzung: GEOFACT_LLM_REASONING=false lässt das Opt-in weg
    (Modelle ohne Reasoning-Unterstützung, Kostenkontrolle)."""
    captured: dict = {}

    class FakeResponse:
        status_code = 200

        def iter_lines(self):
            yield "data: [DONE]"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured.update(json or {})
            return FakeResponse()

    monkeypatch.setattr(llm_module.httpx, "Client", FakeClient)
    client = llm_module.LLMClient(_settings(reasoning=False))
    list(client.stream_generate_config("x", [], None, []))
    assert "reasoning" not in captured


def _settings(*, reasoning: bool = True, max_tokens: int = 8000):
    """Minimal-Settings ohne Umgebungsabhängigkeit."""
    from geofact_web.settings import Settings

    return Settings(
        llm_api_key="test-key",
        llm_model="test/model",
        llm_reasoning=reasoning,
        llm_max_tokens=max_tokens,
    )


def test_fa31_settings_reasoning_default_and_override(monkeypatch):
    from geofact_web.settings import Settings

    monkeypatch.delenv("GEOFACT_LLM_REASONING", raising=False)
    assert Settings().llm_reasoning is True
    monkeypatch.setenv("GEOFACT_LLM_REASONING", "false")
    assert Settings().llm_reasoning is False
    monkeypatch.setenv("GEOFACT_LLM_REASONING", "1")
    assert Settings().llm_reasoning is True


def test_fa31_settings_max_tokens_has_finite_default(monkeypatch):
    """NFA2: es gibt immer ein Abbruchkriterium, auch ohne
    Umgebungsvariable."""
    from geofact_web.settings import Settings

    monkeypatch.delenv("GEOFACT_LLM_MAX_TOKENS", raising=False)
    assert Settings().llm_max_tokens > 0


# =====================================================================
# SSE-Ereignisse des Endpunkts
# =====================================================================


def test_fa31_sse_stream_emits_thinking_and_phase_events(monkeypatch):
    """Nachbedingung: der Endpunkt gibt thinking- und phase-Ereignisse
    getrennt vom token-Kanal aus, und das Reasoning landet nicht in der
    erzeugten YAML."""
    from fastapi.testclient import TestClient

    from geofact_web import settings as settings_module
    from geofact_web.main import app
    from geofact_web.routers import config as config_router

    valid_yaml = (
        "scenario:\n"
        "  name: Test\n"
        "  region: 12.8,50.7,13.0,50.9\n"
        "layers:\n"
        "- id: streets\n"
        "  source: osm\n"
        "  tags:\n"
        "    name: '*'\n"
        "  data_type: vector\n"
        "steps:\n"
        "- id: named\n"
        "  op: filter\n"
        "  inputs:\n"
        "    features: streets\n"
        "  params:\n"
        "    condition: name.notna()\n"
        "output:\n"
        "- type: map\n"
        "  source: named\n"
    )

    class FakeClient:
        model = "test/model"

        def __init__(self, *args, **kwargs):
            pass

        def stream_generate_config(self, *args, **kwargs):
            yield ("thinking", "Ich waehle OSM-Tags aus.")
            yield ("token", valid_yaml)

    monkeypatch.setattr(config_router, "LLMClient", FakeClient)
    monkeypatch.setenv("GEOFACT_LLM_MODEL", "test/model")
    # FA78: kein Probelauf - die Konfiguration fragt OSM ab, der Test prüft
    # nur die Ereigniskanäle (Probelauf: test_fa78_trial_run_repair.py).
    monkeypatch.setenv("GEOFACT_LLM_TRIAL_RUN", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    settings_module.get_settings.cache_clear()

    with TestClient(app) as http:
        response = http.post(
            "/api/config/generate/stream", json={"prompt": "Straßen mit Namen"}
        )
        assert response.status_code == 200
        body = response.text

    settings_module.get_settings.cache_clear()

    assert "event: thinking" in body
    assert "Ich waehle OSM-Tags aus." in body
    assert "event: phase" in body
    assert '"phase": "validating"' in body
    assert "event: done" in body

    done_payload = json.loads(
        [
            line[len("data:") :].strip()
            for block in body.split("\n\n")
            if "event: done" in block
            for line in block.splitlines()
            if line.startswith("data:")
        ][0]
    )
    # Der Reasoning-Text darf nicht in der Konfiguration landen.
    assert "Ich waehle OSM-Tags aus." not in done_payload["config_yaml"]
    assert done_payload["validation"]["valid"] is True
