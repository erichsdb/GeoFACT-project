"""Implements: FA87 (fehlende Daten melden statt eine Quelle zu erfinden).

Vertragstests für ``generation.missing_data``, die Schleife ``generation.refine``
und beide Erzeugungsendpunkte. Kein echter Anbieter (das Modell ist ein
Stellvertreter mit festen Antworten) und kein Netz. Die Nachbedingungen an die
Oberfläche prüft der Quelltext (kein Frontend-Testwerkzeug im Repo, siehe
test_fa54_frontend_lost_run.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import generation  # noqa: E402
from geofact_web.generation import GenerationContext, Outcome, Phase, TrialRun  # noqa: E402
from geofact_web.llm import LLMClient, build_system_prompt  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.settings import Settings, get_settings  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"

_MISSING = """\
missing_data:
  - name: Kundenstandorte
    reason: Die Frage zählt Kunden je Stadtteil.
    hint: CSV mit Längen- und Breitengrad je Kunde
    alternative: Geschäfte je Stadtteil aus OSM
"""

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

# gültig, bricht aber beim Lauf ab (Spalte fehlt)
_BREAKS = _GOOD.replace(
    "op: buffer\n    inputs: {geometry: substations}\n    params: {radius_km: 1}",
    "op: top_n\n    inputs: {features: substations}\n    params: {by: gibt_es_nicht, n: 3}",
)


def _settings(**overrides) -> Settings:
    values = {
        "llm_api_key": "test-key",
        "llm_model": "test/model",
        "llm_repair_rounds": 3,
        "llm_trial_run": True,
        "llm_trial_run_s": 60.0,
    }
    values.update(overrides)
    return Settings(**values)


class FakeModel:
    model = "test/model"

    def __init__(self, repairs=(), reviews=()):
        self._repairs = iter(repairs)
        self._reviews = iter(reviews)
        self.calls = 0

    def repair_config(
        self, previous_yaml, issues, templates=None, hints=None, **kwargs
    ):
        self.calls += 1
        return next(self._repairs)

    def review_config(
        self, config_yaml, warnings, templates=None, hints=None, **kwargs
    ):
        self.calls += 1
        return next(self._reviews)


def _refine(model, config_yaml, **settings) -> tuple[list[str], Outcome]:
    context = GenerationContext(prompt="Kunden je Stadtteil", region="Dresden")
    *phases, outcome = generation.refine(
        model, config_yaml, context, _settings(**settings)
    )
    assert isinstance(outcome, Outcome)
    assert all(isinstance(p, Phase) for p in phases)
    return [p.name for p in phases], outcome


# =====================================================================
# Meldung lesen
# =====================================================================


def test_fa87_missing_data_report_is_read_with_name_reason_and_hint():
    """Happy Path."""
    (entry,) = generation.missing_data(_MISSING)
    assert entry.name == "Kundenstandorte"
    assert entry.reason == "Die Frage zählt Kunden je Stadtteil."
    assert entry.hint == "CSV mit Längen- und Breitengrad je Kunde"
    assert entry.alternative == "Geschäfte je Stadtteil aus OSM"


def test_fa87_missing_data_accepts_plain_names_and_a_single_entry():
    assert [
        e.name
        for e in generation.missing_data("missing_data: [Pegelstaende, Umsatz 2025]")
    ] == ["Pegelstaende", "Umsatz 2025"]
    (single,) = generation.missing_data("missing_data: {name: Pegelstaende}")
    assert (
        single.name == "Pegelstaende" and single.reason is None and single.hint is None
    )
    assert single.alternative is None  # ohne Vorschlag kein geratener


def test_fa87_a_config_is_not_a_missing_data_report():
    assert generation.missing_data(_GOOD) == []


def test_fa87_the_key_next_to_a_config_is_not_a_report():
    """Der Schlüssel neben layers/steps ist keine Meldung - die Prüfung lehnt ihn ab."""
    assert generation.missing_data(_GOOD + "missing_data: [Pegelstaende]\n") == []


@pytest.mark.parametrize(
    "text",
    [
        "",
        "kein: [yaml",
        "missing_data: []",
        "missing_data: 3",
        "missing_data: [{reason: ohne Name}]",
        "missing_data: [{name: ''}]",
        "- missing_data",
    ],
)
def test_fa87_unreadable_or_empty_reports_are_not_reports(text):
    """Randfall: leer, unlesbar, ohne Namen - nie eine geratene Meldung."""
    assert generation.missing_data(text) == []


def test_fa87_report_is_bounded():
    many = "missing_data:\n" + "".join(f"  - name: d{i}\n" for i in range(30))
    assert len(generation.missing_data(many)) == 8
    (long,) = generation.missing_data("missing_data: [{name: " + "x" * 2000 + "}]")
    assert len(long.name) <= 300


# =====================================================================
# Schleife
# =====================================================================


def test_fa87_report_ends_the_loop_without_validation_trial_run_or_model_call(
    monkeypatch,
):
    monkeypatch.setattr(
        generation, "trial_run", lambda *a, **k: pytest.fail("kein Probelauf")
    )
    model = FakeModel()
    phases, outcome = _refine(model, _MISSING)

    assert phases == []
    assert model.calls == 0
    assert outcome.config_yaml == ""
    assert outcome.validation.valid is False and outcome.validation.issues == []
    assert (
        outcome.trial_run.status == "skipped" and outcome.trial_run.repair_rounds == 0
    )
    assert [e.name for e in outcome.missing_data] == ["Kundenstandorte"]
    assert "fehlt ein Datensatz: Kundenstandorte" in outcome.notes
    assert (
        "hochladen" in outcome.notes and "umformulieren" in outcome.notes
    )  # beide Auswege


def test_fa87_a_repair_round_may_report_missing_data():
    """Der Probelauf bricht ab, die Reparatur meldet fehlende Daten: die Runde zählt."""
    phases, outcome = _refine(FakeModel(repairs=[_MISSING]), _BREAKS)
    assert phases == ["validating", "trial_run", "repairing"]
    assert outcome.config_yaml == "" and outcome.trial_run.repair_rounds == 1
    assert [e.name for e in outcome.missing_data] == ["Kundenstandorte"]


def test_fa87_a_report_after_a_passed_trial_run_keeps_the_checked_config(monkeypatch):
    """FA78 (5) bleibt: eine gelaufene Fassung wird nicht durch eine Meldung ersetzt."""
    trials = iter([TrialRun("ok", warnings=("w",))])
    monkeypatch.setattr(generation, "trial_run", lambda *a, **k: next(trials))
    _phases, outcome = _refine(FakeModel(reviews=[_MISSING]), _GOOD)
    assert outcome.config_yaml == _GOOD and outcome.validation.valid
    assert outcome.missing_data == []
    assert "verworfen" in outcome.notes


def test_fa87_a_normal_config_has_no_missing_data():
    _phases, outcome = _refine(FakeModel(), _GOOD)
    assert outcome.missing_data == [] and outcome.config_yaml == _GOOD


# =====================================================================
# System-Prompt und Endpunkte
# =====================================================================


def test_fa87_system_prompt_tells_the_model_how_to_report_missing_data():
    prompt = build_system_prompt([], [])
    assert "FEHLENDE DATEN" in prompt
    assert "missing_data:" in prompt and "- name:" in prompt
    assert "alternative:" in prompt and "Ersatzmass" in prompt
    assert "erfinde KEINEN Pfad" in prompt


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_fa87_generate_endpoint_returns_the_report_instead_of_a_config(
    http, monkeypatch
):
    calls = []
    monkeypatch.setattr(LLMClient, "_chat", lambda self, m: calls.append(m) or _MISSING)
    response = http.post("/api/config/generate", json={"prompt": "Kunden je Stadtteil"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["config_yaml"] == ""
    assert body["missing_data"] == [
        {
            "name": "Kundenstandorte",
            "reason": "Die Frage zählt Kunden je Stadtteil.",
            "hint": "CSV mit Längen- und Breitengrad je Kunde",
            "alternative": "Geschäfte je Stadtteil aus OSM",
        }
    ]
    assert body["validation"]["valid"] is False
    assert "Kundenstandorte" in body["notes"]
    assert len(calls) == 1  # keine Reparaturanfrage


def test_fa87_generate_endpoint_has_an_empty_list_for_a_normal_config(
    http, monkeypatch
):
    monkeypatch.setattr(LLMClient, "_chat", lambda self, m: _GOOD)
    body = http.post("/api/config/generate", json={"prompt": "Puffer"}).json()
    assert body["missing_data"] == [] and body["validation"]["valid"] is True


def test_fa87_stream_endpoint_ends_with_the_report_and_no_phase(http, monkeypatch):
    def fake_stream_lines(self, payload):
        yield "data: " + json.dumps(
            {"choices": [{"delta": {"content": "```yaml\n" + _MISSING + "```"}}]}
        )
        yield "data: [DONE]"

    monkeypatch.setattr(LLMClient, "_stream_lines", fake_stream_lines)
    response = http.post(
        "/api/config/generate/stream", json={"prompt": "Kunden je Stadtteil"}
    )

    assert response.status_code == 200
    names = [
        block.splitlines()[0][len("event:") :].strip()
        for block in response.text.split("\n\n")
        if block.startswith("event:")
    ]
    assert names == ["token", "done"]
    done = json.loads(
        next(
            line[len("data:") :]
            for block in response.text.split("\n\n")
            if block.startswith("event: done")
            for line in block.splitlines()
            if line.startswith("data:")
        )
    )
    assert [e["name"] for e in done["missing_data"]] == ["Kundenstandorte"]
    assert done["config_yaml"] == ""


# =====================================================================
# Oberfläche (Quelltext)
# =====================================================================


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_fa87_frontend_keeps_the_editor_text_and_shows_the_report():
    panel = _text(FRONTEND / "components" / "prompt-panel.tsx")
    start = panel.index("if (res.missing_data && res.missing_data.length > 0) {")
    branch = panel[start : panel.index("onGenerated(res.config_yaml", start)]
    assert "setMissing(res.missing_data);" in branch and "return;" in branch
    assert 'data-slot="missing-data"' in panel
    assert "Dem Sprachmodell fehlt ein Datensatz" in panel
    assert "Daten vormerken" in panel  # der Weg zum Upload
    report = panel[
        panel.index('data-slot="missing-data"') : panel.index("{thinking && (")
    ]
    # der Vorschlag des Modells lässt sich als Frage übernehmen (nur wenn es einen gibt)
    assert "{m.alternative && (" in report
    assert 'onClick={() => onPromptChange(m.alternative ?? "")}' in report
    assert (
        "formulieren Sie die Frage um" in panel
        and "woran sie gemessen werden soll" in panel
    )
    assert "onClick={generate}" in report and "Erneut generieren" in report


def test_fa87_frontend_clears_the_report_when_a_generation_starts():
    panel = _text(FRONTEND / "components" / "prompt-panel.tsx")
    begin = panel[
        panel.index("async function generate()") : panel.index("const controller")
    ]
    assert "setMissing([]);" in begin
    assert "missing_data?: MissingDataset[];" in _text(FRONTEND / "lib" / "types.ts")
