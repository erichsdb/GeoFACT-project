"""Tests für backend/geofact_web/routers/config.py:

Der abschließende YAML-Reformat-Schritt in /api/config/generate war
früher `except yaml.YAMLError: pass` - scheitert das (kosmetische)
Reformat, bleibt das bereits validierte config_yaml zwar erhalten, muss
aber sichtbar (warnings.warn) statt stillschweigend geschehen (Regel 7).

LLMClient wird gemonkeypatcht (kein echter Provider im Unit-Test).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from geofact_web.llm import LLMClient  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.settings import get_settings  # noqa: E402

_VALID_YAML = """
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
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    yield TestClient(create_app())
    get_settings.cache_clear()


def test_generate_reformat_failure_warns_but_keeps_valid_yaml(client, monkeypatch):
    """Vorbedingung: generate_config liefert bereits gültiges YAML, aber
    das abschließende Reformat (yaml.safe_load -> yaml.safe_dump) schlägt
    fehl (hier simuliert durch ein YAML, dessen erneutes Parsen zwar
    gelingt aber dessen Dump-Objekt kein dict ist - stattdessen wird direkt
    ein YAMLError erzwungen, indem safe_load fehlschlägt). Nachbedingung:
    die Antwort liefert trotzdem das unveränderte, bereits validierte
    config_yaml, und es wird sichtbar gewarnt statt geschluckt."""
    monkeypatch.setattr(LLMClient, "generate_config", lambda self, *a, **k: _VALID_YAML)

    import yaml as _yaml

    original_safe_load = _yaml.safe_load
    calls = {"n": 0}

    def flaky_safe_load(text):
        calls["n"] += 1
        # Erster Aufruf: Validierung braucht ein echtes Ergebnis.
        if calls["n"] == 1:
            return original_safe_load(text)
        # Zweiter Aufruf: der kosmetische Reformat-Schritt - hier gezielt
        # zum Scheitern bringen.
        raise _yaml.YAMLError("simulierter Parse-Fehler im Reformat-Schritt")

    monkeypatch.setattr(_yaml, "safe_load", flaky_safe_load)

    with pytest.warns(UserWarning, match="YAML-Reformat"):
        resp = client.post(
            "/api/config/generate",
            json={"prompt": "irrelevant", "source_ids": [], "region": None},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["validation"]["valid"] is True
    # Reformat ist gescheitert -> Original-YAML (unformatiert) bleibt erhalten.
    assert body["config_yaml"] == _VALID_YAML
