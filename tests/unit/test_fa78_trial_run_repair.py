"""Implements: FA78 (erzeugte Konfiguration zur Probe ausführen und mit
Laufzeitmeldungen reparieren).

Vertragstests der Schleife ``generation.refine`` und des Probelaufs
``generation.trial_run``. Kein echter Anbieter (das Modell ist ein Stellvertreter
mit festen Antworten) und kein Netz: die Konfigurationen lesen eine Fixture-
Datei, die Region ist ein BBox-Literal.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
import yaml

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from geofact_web import generation, scenario_service  # noqa: E402
from geofact_web.generation import GenerationContext, Outcome, Phase, TrialRun  # noqa: E402
from geofact_web.llm import LLMClient, LLMError  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.settings import Settings, get_settings  # noqa: E402

_HEAD = """\
scenario:
  name: Test
  region: "13.60,51.01,13.94,51.15"
layers:
  - id: substations
    source: file
    path: tests/fixtures/substations.geojson
    data_type: vector
"""

# gültig und ausführbar
_GOOD = (
    _HEAD
    + """\
steps:
  - id: catchment
    op: buffer
    inputs: {geometry: substations}
    params: {radius_km: 1}
output:
  - type: geojson
    source: catchment
"""
)

# gültig, bricht aber beim Lauf ab: die Spalte gibt es nicht (wie 'ewz' in P10)
_BREAKS = (
    _HEAD
    + """\
steps:
  - id: größte
    op: top_n
    inputs: {features: substations}
    params: {by: gibt_es_nicht, n: 3}
output:
  - type: geojson
    source: größte
"""
)

# ungültig: Pflichtparameter fehlt
_INVALID = (
    _HEAD
    + """\
steps:
  - id: catchment
    op: buffer
    inputs: {geometry: substations}
output:
  - type: geojson
    source: catchment
"""
)

# eine zweite gültige Fassung (anderer Radius) für die Durchsicht der Warnungen
_GOOD_OTHER = _GOOD.replace("radius_km: 1", "radius_km: 2")


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
    """Stellvertreter für LLMClient: feste Antworten in Reihenfolge, alle
    Aufrufe protokolliert."""

    model = "test/model"

    def __init__(self, repairs=(), reviews=()):
        self._repairs = iter(repairs)
        self._reviews = iter(reviews)
        self.repair_calls: list[dict] = []
        self.review_calls: list[dict] = []

    def repair_config(
        self, previous_yaml, issues, templates=None, hints=None, **kwargs
    ):
        self.repair_calls.append(
            {"yaml": previous_yaml, "issues": list(issues), **kwargs}
        )
        return next(self._repairs)

    def review_config(
        self, config_yaml, warnings, templates=None, hints=None, **kwargs
    ):
        self.review_calls.append(
            {"yaml": config_yaml, "warnings": list(warnings), **kwargs}
        )
        answer = next(self._reviews)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _refine(model, config_yaml, **settings) -> tuple[list[str], Outcome]:
    context = GenerationContext(prompt="Puffer um Umspannwerke", region="Dresden")
    *phases, outcome = generation.refine(
        model, config_yaml, context, _settings(**settings)
    )
    assert isinstance(outcome, Outcome)
    assert all(isinstance(p, Phase) for p in phases)
    return [p.name for p in phases], outcome


def _document(config_yaml: str):
    document, validation = scenario_service.validate_config(config_yaml, None)
    assert validation.valid, validation.issues
    return document


# =====================================================================
# Probelauf
# =====================================================================


def test_fa78_trial_run_ok_on_a_runnable_config():
    """Happy Path: eine gültige, ausführbare Konfiguration ergibt ``ok``."""
    result = generation.trial_run(_document(_GOOD), timeout_s=60)
    assert result.status == "ok"
    assert result.error is None


def test_fa78_trial_run_reports_the_failing_step_with_its_message():
    """Nachbedingung: ``failed`` mit der Meldung des Schritts - sie nennt den
    Schritt und das fehlende Attribut, also das, was die Reparatur braucht."""
    result = generation.trial_run(_document(_BREAKS), timeout_s=60)
    assert result.status == "failed"
    assert "größte" in result.error
    assert "gibt_es_nicht" in result.error


def test_fa78_trial_run_on_an_empty_layer_is_not_a_failure():
    """Randfall leerer Layer: eine Region ohne Objekte ist kein Abbruch."""
    empty = _GOOD.replace('"13.60,51.01,13.94,51.15"', '"8.00,48.00,8.01,48.01"')
    assert generation.trial_run(_document(empty), timeout_s=60).status == "ok"


def test_fa78_trial_run_writes_no_outputs(monkeypatch, tmp_path):
    """Nachbedingung (3): der Probelauf ruft ``api.run`` ohne ``out_dir`` - es
    wird keine Ausgabe geschrieben und kein rest-Ausgang aufgerufen."""
    monkeypatch.chdir(tmp_path)
    seen: dict = {}
    real_run = generation.api.run

    def spy(document, **kwargs):
        seen.update(kwargs)
        return real_run(document, **kwargs)

    monkeypatch.setattr(generation.api, "run", spy)
    document, _ = scenario_service.validate_config(
        _GOOD.replace(
            "source: catchment", "source: catchment\n    path: probe.geojson"
        ),
        None,
    )
    assert generation.trial_run(document, timeout_s=60).status == "ok"
    assert seen.get("out_dir") is None
    assert list(tmp_path.rglob("probe.geojson")) == []


def test_fa78_trial_run_times_out_and_requests_cancel(monkeypatch):
    """Nachbedingung (4): nach der Frist kommt ``timeout``, der Aufrufer wartet
    nicht auf den Lauf, und der Abbruch ist angefordert."""
    cancelled = threading.Event()

    def slow_run(document, *, cancel, **kwargs):
        for _ in range(100):
            if cancel.cancelled:
                cancelled.set()
                raise generation.api.RunCancelled()
            time.sleep(0.05)

    monkeypatch.setattr(generation.api, "run", slow_run)
    started = time.monotonic()
    result = generation.trial_run(_document(_GOOD), timeout_s=0.2)
    assert result.status == "timeout"
    assert time.monotonic() - started < 2.0
    assert cancelled.wait(2.0)


def test_fa78_trial_run_collects_warnings_but_not_notices(monkeypatch):
    """Warnungen (Schweregrad warning) werden gesammelt, ohne Wiederholung;
    Hinweise (severity info, z. B. Snapshot-Vermerk) nicht."""
    ev = generation.ev

    def run(document, *, observer, **kwargs):
        observer(
            ev.ProgressEvent(
                ev.STEP_WARNING, step="zählen", message="47 Punkte verworfen"
            )
        )
        observer(
            ev.ProgressEvent(
                ev.STEP_WARNING, step="zählen", message="47 Punkte verworfen"
            )
        )
        observer(
            ev.ProgressEvent(
                ev.LAYER_WARNING,
                layer="osm",
                message="Snapshot verwendet",
                detail={"severity": "info"},
            )
        )
        observer(ev.ProgressEvent(ev.REGION_WARNING, message="Region sehr groß"))

    monkeypatch.setattr(generation.api, "run", run)
    result = generation.trial_run(_document(_GOOD), timeout_s=5)
    assert result.status == "ok"
    assert result.warnings == ("zählen: 47 Punkte verworfen", "Region sehr groß")


# =====================================================================
# Schleife: Prüfung -> Probelauf -> Reparatur
# =====================================================================


def test_fa78_runnable_config_needs_no_model_call():
    phases, outcome = _refine(FakeModel(), _GOOD)
    assert phases == ["validating", "trial_run"]
    assert outcome.validation.valid
    assert outcome.trial_run.status == "ok"
    assert outcome.trial_run.repair_rounds == 0
    assert outcome.notes == "Probelauf erfolgreich."


def test_fa78_runtime_error_goes_into_the_repair_round():
    """Kern der FA: die Konfiguration ist gültig, bricht aber ab - die
    Meldung des Laufs geht als Laufzeitmeldung in die Reparatur, und die
    reparierte Fassung wird erneut geprüft und ausgeführt."""
    model = FakeModel(repairs=[_GOOD])
    phases, outcome = _refine(model, _BREAKS)

    assert phases == ["validating", "trial_run", "repairing", "validating", "trial_run"]
    assert len(model.repair_calls) == 1
    call = model.repair_calls[0]
    assert call["runtime"] is True
    assert "gibt_es_nicht" in call["issues"][0]
    assert call["prompt"] == "Puffer um Umspannwerke" and call["region"] == "Dresden"
    assert outcome.config_yaml == _GOOD
    assert outcome.trial_run.status == "ok"
    assert outcome.trial_run.repair_rounds == 1
    assert "1 Reparatur-Runde" in outcome.notes


def test_fa78_validation_error_then_runtime_error_take_two_rounds():
    """Eine Prüfmeldung und danach ein Abbruch: zwei Runden, die erste mit
    Position der Prüfmeldung (runtime False), die zweite mit der Laufmeldung."""
    model = FakeModel(repairs=[_BREAKS, _GOOD])
    phases, outcome = _refine(model, _INVALID)

    assert [c["runtime"] for c in model.repair_calls] == [False, True]
    assert "radius_km" in model.repair_calls[0]["issues"][0]
    assert phases.count("repairing") == 2
    assert outcome.trial_run.status == "ok" and outcome.trial_run.repair_rounds == 2


def test_fa78_repair_rounds_are_limited_and_the_failure_is_reported():
    """Nachbedingung (1) und (6): höchstens ``llm_repair_rounds`` Anfragen;
    danach steht der Abbruch mit seiner Meldung in trial_run und notes."""
    model = FakeModel(repairs=[_BREAKS, _BREAKS, _BREAKS])
    _phases, outcome = _refine(model, _BREAKS, llm_repair_rounds=2)

    assert len(model.repair_calls) == 2
    assert outcome.validation.valid
    assert outcome.trial_run.status == "failed"
    assert "gibt_es_nicht" in outcome.trial_run.error
    assert (
        "Der Probelauf bricht ab" in outcome.notes and "gibt_es_nicht" in outcome.notes
    )


def test_fa78_invalid_after_the_last_round_stays_invalid():
    model = FakeModel(repairs=[_INVALID])
    _phases, outcome = _refine(model, _INVALID, llm_repair_rounds=1)
    assert not outcome.validation.valid
    assert outcome.trial_run.status == "skipped"
    assert outcome.notes == generation.NOTE_INVALID


def test_fa78_zero_rounds_means_no_repair():
    model = FakeModel()
    phases, outcome = _refine(model, _INVALID, llm_repair_rounds=0)
    assert phases == ["validating"]
    assert model.repair_calls == [] and not outcome.validation.valid


def test_fa78_trial_run_can_be_switched_off(monkeypatch):
    """Nachbedingung (7): ohne Probelauf bleibt es bei Prüfung und Reparatur
    der Prüfmeldungen; der Status ist ``skipped``."""
    monkeypatch.setattr(
        generation, "trial_run", lambda *a, **k: pytest.fail("kein Probelauf")
    )
    model = FakeModel(repairs=[_BREAKS])
    phases, outcome = _refine(model, _INVALID, llm_trial_run=False)
    assert phases == ["validating", "repairing", "validating"]
    assert outcome.validation.valid
    assert (
        outcome.trial_run.status == "skipped" and outcome.trial_run.repair_rounds == 1
    )


def test_fa78_timeout_keeps_the_config_unchecked(monkeypatch):
    monkeypatch.setattr(generation, "trial_run", lambda *a, **k: TrialRun("timeout"))
    model = FakeModel()
    _phases, outcome = _refine(model, _GOOD, llm_trial_run_s=7.0)
    assert model.repair_calls == []
    assert outcome.trial_run.status == "timeout"
    assert "nach 7 s nicht fertig" in outcome.notes and "ungeprüft" in outcome.notes


# =====================================================================
# Durchsicht der Warnungen
# =====================================================================


def _trials(monkeypatch, *results: TrialRun) -> list:
    """Ersetzt den Probelauf durch feste Ergebnisse; liefert die Liste der
    dabei ausgeführten Dokumente."""
    queue = iter(results)
    ran: list = []

    def fake(document, timeout_s, connector_options=None):
        ran.append(document)
        return next(queue)

    monkeypatch.setattr(generation, "trial_run", fake)
    return ran


def test_fa78_warnings_are_reviewed_once_and_a_running_change_is_taken(monkeypatch):
    """Der Lauf warnt (P02: Punkte verworfen): das Modell bekommt die Warnungen
    einmal; die geänderte Fassung läuft durch und wird übernommen."""
    ran = _trials(
        monkeypatch,
        TrialRun("ok", warnings=("zählen: 47 Punkte verworfen",)),
        TrialRun("ok", warnings=("noch eine Warnung",)),
    )
    model = FakeModel(reviews=[_GOOD_OTHER])
    phases, outcome = _refine(model, _GOOD)

    assert phases == ["validating", "trial_run", "reviewing", "validating", "trial_run"]
    assert (
        len(model.review_calls) == 1
    )  # die zweite Warnung löst keine weitere Durchsicht aus
    assert model.review_calls[0]["warnings"] == ["zählen: 47 Punkte verworfen"]
    assert len(ran) == 2
    assert outcome.config_yaml == _GOOD_OTHER
    assert outcome.trial_run.repair_rounds == 1
    assert outcome.trial_run.warnings == ["noch eine Warnung"]
    assert "1 Warnung" in outcome.notes


def test_fa78_reviewed_config_that_breaks_is_discarded(monkeypatch):
    """Nachbedingung (5): eine Fassung, deren Probelauf durchlief, wird nicht
    durch eine ersetzt, die abbricht."""
    _trials(
        monkeypatch,
        TrialRun("ok", warnings=("w",)),
        TrialRun("failed", error="Schritt 'x': kaputt"),
    )
    model = FakeModel(reviews=[_GOOD_OTHER])
    _phases, outcome = _refine(model, _GOOD)

    assert outcome.config_yaml == _GOOD
    assert outcome.trial_run.status == "ok" and outcome.trial_run.warnings == ["w"]
    assert model.repair_calls == []  # kein Reparaturversuch an der verworfenen Fassung
    assert "verworfen" in outcome.notes


def test_fa78_reviewed_config_that_is_invalid_is_discarded(monkeypatch):
    _trials(monkeypatch, TrialRun("ok", warnings=("w",)))
    model = FakeModel(reviews=[_INVALID])
    _phases, outcome = _refine(model, _GOOD)
    assert outcome.config_yaml == _GOOD and outcome.validation.valid
    assert outcome.trial_run.status == "ok"


def test_fa78_unchanged_review_ends_the_loop_without_a_second_run(monkeypatch):
    """Gibt das Modell die Konfiguration unverändert zurück (auch anders
    formatiert), endet die Schleife ohne weiteren Probelauf."""
    ran = _trials(monkeypatch, TrialRun("ok", warnings=("w",)))
    reformatted = "# unverändert\n" + _GOOD.replace(
        "{radius_km: 1}", "\n      radius_km: 1"
    )
    model = FakeModel(reviews=[reformatted])
    _phases, outcome = _refine(model, _GOOD)
    assert len(ran) == 1
    assert outcome.config_yaml == _GOOD


def test_fa78_failed_review_keeps_the_checked_config(monkeypatch):
    """Scheitert nur die Durchsicht (Anbieterfehler), bleibt die geprüfte
    Fassung, und der Grund steht in notes - kein 502 für ein gutes Ergebnis."""
    _trials(monkeypatch, TrialRun("ok", warnings=("w",)))
    model = FakeModel(reviews=[LLMError("LLM-Anbieter antwortete mit 429")])
    _phases, outcome = _refine(model, _GOOD)
    assert outcome.config_yaml == _GOOD and outcome.trial_run.status == "ok"
    assert (
        "Durchsicht der Warnungen ist gescheitert" in outcome.notes
        and "429" in outcome.notes
    )


def test_fa78_no_review_when_the_rounds_are_used_up(monkeypatch):
    _trials(
        monkeypatch,
        TrialRun("failed", error="Schritt 'x': kaputt"),
        TrialRun("ok", warnings=("w",)),
    )
    model = FakeModel(repairs=[_GOOD])
    _phases, outcome = _refine(model, _BREAKS, llm_repair_rounds=1)
    assert model.review_calls == []
    assert outcome.trial_run.status == "ok" and outcome.trial_run.warnings == ["w"]


# =====================================================================
# Reparaturanfrage, Einstellungen, Endpunkte
# =====================================================================


def test_fa78_runtime_repair_request_says_the_config_is_valid_but_breaks(monkeypatch):
    """Nachbedingung (2): die Laufzeit-Reparatur trägt den System-Prompt der
    Erzeugung und nennt die Meldung als Abbruch beim Lauf, nicht als
    Validierungsfehler."""
    from geofact_web.llm import build_system_prompt

    captured: list[list[dict]] = []
    monkeypatch.setattr(LLMClient, "_chat", lambda self, m: captured.append(m) or _GOOD)
    client = LLMClient(_settings())

    client.repair_config(
        _BREAKS, ["Schritt 'größte': Attribut fehlt"], [], [], runtime=True
    )
    client.review_config(_GOOD, ["zählen: 47 Punkte verworfen"], [], [], prompt="Frage")

    repair_system, repair_user = captured[0][0]["content"], captured[0][1]["content"]
    assert repair_system == build_system_prompt([], [])
    assert "bricht aber beim Lauf" in repair_user and "Attribut fehlt" in repair_user
    assert "Validierungsfehler" not in repair_user
    review_system, review_user = captured[1][0]["content"], captured[1][1]["content"]
    assert review_system == build_system_prompt([], [])
    assert "47 Punkte verworfen" in review_user and "UNVERAENDERT" in review_user
    assert "Frage" in review_user


def test_fa78_settings_defaults_and_validation(monkeypatch):
    for name in (
        "GEOFACT_LLM_REPAIR_ROUNDS",
        "GEOFACT_LLM_TRIAL_RUN",
        "GEOFACT_LLM_TRIAL_RUN_S",
    ):
        monkeypatch.delenv(name, raising=False)
    defaults = Settings()
    assert (
        defaults.llm_repair_rounds,
        defaults.llm_trial_run,
        defaults.llm_trial_run_s,
    ) == (3, True, 60.0)

    monkeypatch.setenv("GEOFACT_LLM_REPAIR_ROUNDS", "0")
    monkeypatch.setenv("GEOFACT_LLM_TRIAL_RUN", "false")
    monkeypatch.setenv("GEOFACT_LLM_TRIAL_RUN_S", "12.5")
    changed = Settings()
    assert (
        changed.llm_repair_rounds,
        changed.llm_trial_run,
        changed.llm_trial_run_s,
    ) == (0, False, 12.5)

    monkeypatch.setenv("GEOFACT_LLM_TRIAL_RUN_S", "0")
    with pytest.raises(ValueError, match="GEOFACT_LLM_TRIAL_RUN_S muss > 0"):
        Settings()
    monkeypatch.setenv("GEOFACT_LLM_TRIAL_RUN_S", "60")
    monkeypatch.setenv("GEOFACT_LLM_REPAIR_ROUNDS", "-1")
    with pytest.raises(ValueError, match="GEOFACT_LLM_REPAIR_ROUNDS muss >= 0"):
        Settings()


@pytest.fixture
def http(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_fa78_generate_endpoint_repairs_a_runtime_error(http, monkeypatch):
    """Beide Stufen im Endpunkt: die erste Antwort bricht beim Lauf ab, die
    Reparatur liefert eine lauffähige Fassung; die Antwort nennt den Probelauf."""
    answers = iter([_BREAKS, _GOOD])
    calls: list[list[dict]] = []

    def fake_chat(self, messages):
        calls.append(messages)
        return next(answers)

    monkeypatch.setattr(LLMClient, "_chat", fake_chat)
    response = http.post("/api/config/generate", json={"prompt": "Die drei größten"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validation"]["valid"] is True
    assert body["trial_run"] == {
        "status": "ok",
        "error": None,
        "warnings": [],
        "repair_rounds": 1,
    }
    assert "catchment" in body["config_yaml"]
    assert "gibt_es_nicht" in calls[1][1]["content"]


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        name = next(
            (
                line[len("event:") :].strip()
                for line in lines
                if line.startswith("event:")
            ),
            None,
        )
        data = next(
            (line[len("data:") :] for line in lines if line.startswith("data:")), None
        )
        if name and data:
            events.append((name, json.loads(data)))
    return events


def test_fa78_stream_endpoint_emits_the_trial_run_phase(http, monkeypatch):
    calls = []

    def fake_stream_lines(self, payload):
        calls.append(payload)
        if len(calls) == 1:  # Erzeugung
            yield "data: " + json.dumps({"choices": [{"delta": {"content": _BREAKS}}]})
        else:  # Reparatur: Denkphase, dann die korrigierte Fassung in zwei Stücken
            half = len(_GOOD) // 2
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"reasoning": "Spalte fehlt"}}]}
            )
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"content": _GOOD[:half]}}]}
            )
            yield "data: " + json.dumps(
                {"choices": [{"delta": {"content": _GOOD[half:]}}]}
            )
        yield "data: [DONE]"

    monkeypatch.setattr(LLMClient, "_stream_lines", fake_stream_lines)
    monkeypatch.setattr(
        LLMClient,
        "_chat",
        lambda self, messages: pytest.fail("Reparatur muss streamen"),
    )
    response = http.post("/api/config/generate/stream", json={"prompt": "x"})

    assert response.status_code == 200
    # Die Reparatur streamt: nach "repairing" folgen Denkphase und die Tokens der
    # korrigierten Fassung, vor der nächsten Prüfung.
    events = _sse_events(response.text)
    names = [name for name, _ in events]
    repairing = next(
        i
        for i, (n, d) in enumerate(events)
        if n == "phase" and d["phase"] == "repairing"
    )
    next_phase = next(
        i for i in range(repairing + 1, len(events)) if names[i] == "phase"
    )
    between = events[repairing + 1 : next_phase]
    assert [n for n, _ in between] == ["thinking", "token", "token"]
    assert "".join(d["delta"] for n, d in between if n == "token") == _GOOD
    assert len(calls) == 2 and all(p["stream"] is True for p in calls)
    phases = [
        json.loads(line[len("data:") :])["phase"]
        for block in response.text.split("\n\n")
        if block.startswith("event: phase")
        for line in block.splitlines()
        if line.startswith("data:")
    ]
    assert phases == ["validating", "trial_run", "repairing", "validating", "trial_run"]
    done = json.loads(
        next(
            line[len("data:") :]
            for block in response.text.split("\n\n")
            if block.startswith("event: done")
            for line in block.splitlines()
            if line.startswith("data:")
        )
    )
    assert (
        done["trial_run"]["status"] == "ok" and done["trial_run"]["repair_rounds"] == 1
    )


class StreamingModel(FakeModel):
    """Wie FakeModel, zusätzlich mit den streamenden Varianten: jede Antwort
    ist eine Liste getaggter Chunks."""

    def __init__(self, repair_streams=(), review_streams=(), **kwargs):
        super().__init__(**kwargs)
        self._repair_streams = iter(repair_streams)
        self._review_streams = iter(review_streams)
        self.stream_calls: list[str] = []

    def stream_repair_config(
        self, previous_yaml, issues, templates=None, hints=None, **kwargs
    ):
        self.stream_calls.append("repair")
        yield from next(self._repair_streams)

    def stream_review_config(
        self, config_yaml, warnings, templates=None, hints=None, **kwargs
    ):
        self.stream_calls.append("review")
        yield from next(self._review_streams)


def _refine_stream(model, config_yaml, **settings) -> tuple[list, Outcome]:
    context = GenerationContext(prompt="Puffer um Umspannwerke", region="Dresden")
    *items, outcome = generation.refine(
        model, config_yaml, context, _settings(**settings), stream=True
    )
    assert isinstance(outcome, Outcome)
    return items, outcome


def test_fa78_streamed_repair_yields_its_deltas_after_the_phase_and_is_taken():
    model = StreamingModel(
        repair_streams=[
            [
                ("thinking", "die Spalte gibt es nicht"),
                ("token", "```yaml\n"),
                ("token", _GOOD),
                ("token", "\n```"),
            ]
        ]
    )
    items, outcome = _refine_stream(model, _BREAKS)

    assert outcome.trial_run.status == "ok" and outcome.trial_run.repair_rounds == 1
    assert yaml.safe_load(outcome.config_yaml) == yaml.safe_load(
        _GOOD
    )  # Code-Zaun entfernt
    assert model.stream_calls == ["repair"] and model.repair_calls == []
    repairing = next(
        i
        for i, item in enumerate(items)
        if isinstance(item, Phase) and item.name == "repairing"
    )
    deltas = items[repairing + 1 : repairing + 5]
    assert all(isinstance(d, generation.Delta) for d in deltas)
    assert [d.kind for d in deltas] == ["thinking", "token", "token", "token"]
    assert isinstance(items[repairing + 5], Phase)  # danach wieder die Prüfung


def test_fa78_streamed_repair_without_content_falls_back_to_the_blocking_request():
    """Randfall: der Strom liefert nur Denkphase (Limit dort verbraucht) - die
    nicht streamende Anfrage mit ihrer Wiederholung übernimmt, nie eine leere Fassung."""
    model = StreamingModel(
        repair_streams=[[("thinking", "lange nachgedacht")]], repairs=[_GOOD]
    )
    items, outcome = _refine_stream(model, _BREAKS)

    assert outcome.validation.valid and outcome.trial_run.status == "ok"
    assert model.stream_calls == ["repair"] and len(model.repair_calls) == 1
    assert [d.kind for d in items if isinstance(d, generation.Delta)] == ["thinking"]


def test_fa78_streamed_review_yields_its_deltas(monkeypatch):
    _trials(monkeypatch, TrialRun("ok", warnings=("w",)), TrialRun("ok"))
    model = StreamingModel(review_streams=[[("token", _GOOD_OTHER)]])
    items, outcome = _refine_stream(model, _GOOD)

    assert yaml.safe_load(outcome.config_yaml) == yaml.safe_load(_GOOD_OTHER)
    assert model.stream_calls == ["review"] and model.review_calls == []
    reviewing = next(
        i
        for i, item in enumerate(items)
        if isinstance(item, Phase) and item.name == "reviewing"
    )
    assert items[reviewing + 1] == generation.Delta("token", _GOOD_OTHER)


def test_fa78_without_stream_the_loop_yields_no_deltas_and_asks_blocking():
    """Typfall der Schnittstelle: der nicht streamende Endpunkt bekommt nur Phasen."""
    model = StreamingModel(repairs=[_GOOD])
    phases, outcome = _refine(model, _BREAKS)  # prüft: alle Elemente sind Phase

    assert outcome.trial_run.status == "ok"
    assert model.stream_calls == [] and len(model.repair_calls) == 1


def test_fa78_stream_and_blocking_requests_send_the_same_messages(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    blocking: list = []
    streamed: list = []
    monkeypatch.setattr(LLMClient, "_chat", lambda self, m: blocking.append(m) or _GOOD)
    monkeypatch.setattr(
        LLMClient, "_stream_chat", lambda self, m: streamed.append(m) or iter(())
    )
    client = LLMClient(get_settings())

    client.repair_config(
        _BREAKS, ["Schritt 'größte': Attribut fehlt"], [], [], prompt="F", runtime=True
    )
    list(
        client.stream_repair_config(
            _BREAKS,
            ["Schritt 'größte': Attribut fehlt"],
            [],
            [],
            prompt="F",
            runtime=True,
        )
    )
    client.review_config(_GOOD, ["w"], [], [], prompt="F")
    list(client.stream_review_config(_GOOD, ["w"], [], [], prompt="F"))

    assert blocking == streamed and len(blocking) == 2
    get_settings.cache_clear()


def test_fa78_frontend_restarts_the_preview_for_a_correction_round():
    """Quelltextvertrag: mit Beginn einer Reparatur oder Durchsicht beginnt die
    Live-Vorschau neu (die Tokens gehören zur korrigierten Fassung), und ein
    Token ändert die Phase nur aus der ersten Denkphase heraus - sonst stünde
    während der Korrektur wieder "Generiere YAML"."""
    from pathlib import Path

    panel = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "components"
        / "prompt-panel.tsx"
    ).read_text(encoding="utf-8")
    assert 'if (serverPhase === "repairing" || serverPhase === "reviewing") {' in panel
    assert (
        'setStreamedYaml("");'
        in panel[
            panel.index('serverPhase === "repairing" || serverPhase === "reviewing"') :
        ]
    )
    assert 'setPhase((p) => (p === "thinking" ? "streaming" : p));' in panel
    assert "const receiving =" in panel


def test_fa78_frontend_knows_every_phase_of_the_loop():
    """Quelltextvertrag: jede Phase, die die Schleife meldet, hat im
    Prompt-Panel ein Label und wird vom Strom übernommen - sonst stünde
    während des Probelaufs weiter "Validiere Konfiguration"."""
    from pathlib import Path

    panel = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "components"
        / "prompt-panel.tsx"
    ).read_text(encoding="utf-8")
    for phase in (
        generation.VALIDATING,
        generation.REPAIRING,
        generation.TRIAL_RUN,
        generation.REVIEWING,
    ):
        assert f'  {phase}: "' in panel, phase
        assert f'serverPhase === "{phase}"' in panel, phase
