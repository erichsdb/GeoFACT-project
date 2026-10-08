"""Implements: FA78 (erzeugte Konfiguration zur Probe ausführen und mit
Laufzeitmeldungen reparieren).

Die Schleife nach der Erzeugung: Prüfung (FA2) -> Probelauf -> Reparatur. Der
Probelauf fängt, was die Prüfung ohne Daten nicht sieht (Attributnamen,
Geometriearten, Namenskollisionen, Laufzeitwarnungen).

``refine`` ist ein Generator: er meldet die Phasen (für den SSE-Strom) und
zuletzt ein ``Outcome``; ``/generate`` und ``/generate/stream`` teilen sich ihn.

Der Probelauf nutzt ``api.run`` (FA44) ohne ``out_dir``, schreibt also nichts und
ruft keinen ``type: rest``-Ausgang auf. Er läuft in einem Thread mit Frist; danach
wird er kooperativ abgebrochen (CancelToken) und die Antwort wartet nicht mehr.

Implements: FA87 (fehlende Daten melden statt eine Quelle zu erfinden). Antwortet
das Modell statt mit einer Konfiguration mit ``missing_data: [...]``, endet die
Schleife ohne Prüfung und Probelauf; ``Outcome.missing_data`` nennt die Datensätze.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Generator, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import yaml

from geofact import api
from geofact.api import events as ev

from . import options as option_module
from . import scenario_service
from .llm import LLMClient, LLMError, _strip_code_fences
from .models import GenerationOptions, MissingDatasetOut, TrialRunOut, ValidateResponse
from .settings import Settings

# Phasen des Stroms (FA31/FA78); das Frontend kennt je Phase ein Label.
VALIDATING = "validating"
REPAIRING = "repairing"
TRIAL_RUN = "trial_run"
REVIEWING = "reviewing"

# Obergrenzen für Reparaturanfrage und Antwort; eine Meldung kann alle Spalten eines OSM-Layers auflisten.
_MAX_ERROR_CHARS = 3000
_MAX_WARNING_CHARS = 600
_MAX_WARNINGS = 12

NOTE_INVALID = "Konfiguration ist noch ungültig - bitte im Editor korrigieren."

# FA87: der eine Schlüssel, mit dem das Modell fehlende Daten meldet, und die
# Obergrenzen für das, was davon in die Antwort geht.
MISSING_DATA_KEY = "missing_data"
_MAX_MISSING = 8
_MAX_MISSING_CHARS = 300


@dataclass(frozen=True)
class Phase:
    """Die Schleife betritt eine Phase (``validating`` | ``repairing`` |
    ``trial_run`` | ``reviewing``)."""

    name: str


def option_defaults(settings: Settings) -> dict[str, Any]:
    """Implements: FA81 - Serverwerte der einstellbaren Felder (Vorbelegung des Dialogs)."""
    return {
        "llm_deadline_s": settings.llm_deadline_s,
        "trial_run": settings.llm_trial_run,
        "trial_run_s": settings.llm_trial_run_s,
    }


def apply_options(settings: Settings, options: GenerationOptions | None) -> Settings:
    """Implements: FA81 - Einstellungen einer Erzeugung: Serverwerte, überschrieben von
    den gesetzten Feldern der Anfrage (``Settings`` selbst bleibt unverändert).
    OSM-Quelle (FA82) und Sprachmodell (FA83) müssen aus der Auswahl des Servers
    stammen, sonst ValueError."""
    if options is None:
        return settings
    changes: dict[str, Any] = {}
    if options.osm_backend is not None:
        option_module.osm_connector_options(
            settings, options.osm_backend
        )  # prüft die Wahl
        changes["osm_backend"] = options.osm_backend
    if options.llm_choice is not None:
        choice = option_module.llm_choice(settings, options.llm_choice)
        changes["llm_model"] = choice.model
        changes["llm_providers"] = choice.providers
    if options.llm_deadline_s is not None:
        changes["llm_deadline_s"] = options.llm_deadline_s
    if options.trial_run is not None:
        changes["llm_trial_run"] = options.trial_run
    if options.trial_run_s is not None:
        changes["llm_trial_run_s"] = options.trial_run_s
    return dataclasses.replace(settings, **changes) if changes else settings


@dataclass(frozen=True)
class Delta:
    """Ein Stück der Modellantwort einer Korrekturrunde (``token`` = YAML-Text,
    ``thinking`` = Denkphase) - nur mit ``refine(..., stream=True)``."""

    kind: str
    text: str


@dataclass(frozen=True)
class TrialRun:
    """Ergebnis eines Probelaufs."""

    status: Literal["ok", "failed", "timeout"]
    error: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass
class Outcome:
    """Endzustand der Schleife: die Konfiguration, ihre Prüfung, der letzte
    Probelauf und ein Satz für den Nutzer."""

    config_yaml: str
    validation: ValidateResponse
    trial_run: TrialRunOut
    notes: str | None = None
    # FA87: nicht leer = das Modell meldet fehlende Daten; config_yaml ist dann leer.
    missing_data: list[MissingDatasetOut] = field(default_factory=list)


@dataclass
class GenerationContext:
    """Was jede Reparaturanfrage mitbekommt (FA31): dasselbe Grounding wie die
    Erzeugung und die ursprüngliche Anfrage."""

    prompt: str
    region: str | None = None
    templates: list[dict[str, Any]] = field(default_factory=list)
    data_hints: list[str] = field(default_factory=list)
    # FA82: Konnektor-Optionen des Probelaufs (z. B. OSM-Quelle); None = Werte des Servers.
    connector_options: dict[str, str] | None = None


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 15] + " ... (gekürzt)"


def _missing_text(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return _shorten(str(value), _MAX_MISSING_CHARS) or None


def missing_data(config_yaml: str) -> list[MissingDatasetOut]:
    """Implements: FA87 - liest die Meldung "mir fehlen Daten" aus einer Modellantwort.

    Vorbedingung: keine (beliebiger Text). Nachbedingung: eine nicht leere Liste
    genau dann, wenn der Text ein YAML-Dokument mit dem EINEN Schlüssel
    ``missing_data`` ist und mindestens ein Eintrag einen Namen trägt (Eintrag =
    Text oder Objekt mit ``name``, optional ``reason``, ``hint`` und
    ``alternative``). Alles
    andere - unlesbares YAML, der Schlüssel neben ``layers``/``steps``, Einträge
    ohne Namen - ist keine Meldung: der Text geht dann wie jede Konfiguration in
    die Prüfung und scheitert dort sichtbar (Goldene Regel 7)."""
    if MISSING_DATA_KEY not in config_yaml:
        return []  # der Normalfall: eine Konfiguration wird nicht zweimal geparst
    try:
        document = yaml.safe_load(config_yaml)
    except yaml.YAMLError:
        return []
    if not isinstance(document, dict) or set(document) != {MISSING_DATA_KEY}:
        return []
    entries = document[MISSING_DATA_KEY]
    if isinstance(entries, (str, dict)):
        entries = [entries]
    if not isinstance(entries, list):
        return []
    found: list[MissingDatasetOut] = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue
        name = _missing_text(entry.get("name"))
        if name is None:
            continue
        found.append(
            MissingDatasetOut(
                name=name,
                reason=_missing_text(entry.get("reason")),
                hint=_missing_text(entry.get("hint")),
                alternative=_missing_text(entry.get("alternative")),
            )
        )
    return found[:_MAX_MISSING]


def _missing_note(missing: list[MissingDatasetOut]) -> str:
    names = ", ".join(entry.name for entry in missing)
    return (
        f"Dem Sprachmodell {'fehlt ein Datensatz' if len(missing) == 1 else 'fehlen Datensaetze'}: "
        f"{names}. Es wurde keine Konfiguration erzeugt - Datensatz hochladen und die "
        "Erzeugung erneut starten, oder die Frage umformulieren und nennen, woran sie "
        "gemessen werden soll."
    )


def trial_run(
    document: api.ScenarioDocument,
    timeout_s: float,
    connector_options: dict[str, str] | None = None,
) -> TrialRun:
    """Führt ``document`` einmal ohne Ausgaben aus (FA78).

    ``ok`` mit den Warnungen des Laufs (nur Schweregrad ``warning``, ohne Wiederholungen);
    ``failed`` mit der Meldung des gescheiterten Schritts, Layers oder Vorab-Checks;
    ``timeout`` nach ``timeout_s`` Sekunden, dann ist der Abbruch angefordert und der
    Aufrufer wartet nicht auf das Ende des Threads."""
    warnings: list[str] = []
    result: dict[str, TrialRun] = {}
    cancel = api.CancelToken()

    def observe(event: ev.ProgressEvent) -> None:
        if event.type not in (ev.STEP_WARNING, ev.LAYER_WARNING, ev.REGION_WARNING):
            return
        if not event.message or event.detail.get("severity") == "info":
            return
        where = event.step or event.layer
        text = _shorten(event.message, _MAX_WARNING_CHARS)
        line = f"{where}: {text}" if where else text
        if line not in warnings:
            warnings.append(line)

    def work() -> None:
        try:
            api.run(
                document,
                observer=observe,
                cancel=cancel,
                connector_options=connector_options,
            )
        except api.RunCancelled:
            return  # nur nach der Frist; der Aufrufer hat längst geantwortet
        except api.ConfigError as exc:
            message = "; ".join(f"{location}: {text}" for location, text in exc.issues)
            result["run"] = TrialRun(
                "failed", _shorten(message or str(exc), _MAX_ERROR_CHARS)
            )
        except Exception as exc:  # noqa: BLE001 - die Meldung IST das Ergebnis
            # StepExecutionError und LayerLoadError nennen Schritt bzw. Layer selbst.
            known = isinstance(exc, (api.StepExecutionError, api.LayerLoadError))
            message = str(exc) if known else f"{type(exc).__name__}: {exc}"
            result["run"] = TrialRun("failed", _shorten(message, _MAX_ERROR_CHARS))
        else:
            result["run"] = TrialRun("ok", warnings=tuple(warnings[:_MAX_WARNINGS]))

    thread = threading.Thread(target=work, name="geofact-trial-run", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if "run" in result:
        return result["run"]
    cancel.cancel()
    return TrialRun("timeout")


def _same_config(left: str, right: str) -> bool:
    """Gleiche Konfiguration unabhängig von Formatierung und Kommentaren."""
    try:
        return yaml.safe_load(left) == yaml.safe_load(right)
    except yaml.YAMLError:
        return left.strip() == right.strip()


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _notes(
    validation: ValidateResponse,
    trial: TrialRunOut,
    settings: Settings,
    problem: str | None,
) -> str | None:
    """Ein Satz zum Endzustand - nie leer, wenn etwas offen bleibt (Regel 7)."""
    if not validation.valid:
        return NOTE_INVALID
    parts: list[str] = []
    if trial.repair_rounds:
        parts.append(
            f"Automatisch in {_plural(trial.repair_rounds, 'Reparatur-Runde', 'Reparatur-Runden')} "
            "korrigiert."
        )
    if trial.status == "failed":
        parts.append(
            f"Der Probelauf bricht ab: {trial.error} - bitte im Editor korrigieren."
        )
    elif trial.status == "timeout":
        parts.append(
            f"Der Probelauf war nach {settings.llm_trial_run_s:g} s nicht fertig - die "
            "Konfiguration ist gültig, aber ungeprüft."
        )
    elif trial.status == "ok":
        parts.append(
            "Probelauf erfolgreich"
            + (
                f", {_plural(len(trial.warnings), 'Warnung', 'Warnungen')}."
                if trial.warnings
                else "."
            )
        )
    if problem:
        parts.append(problem)
    return " ".join(parts) or None


def refine(
    client: LLMClient,
    config_yaml: str,
    context: GenerationContext,
    settings: Settings,
    stream: bool = False,
) -> Iterator[Phase | Delta | Outcome]:
    """Implements: FA78 - Prüfung, Probelauf und Reparatur bis zum Endzustand.

    Liefert ``Phase``-Elemente und zuletzt genau ein ``Outcome``. Mit ``stream``
    folgen auf die Phase ``Delta``-Elemente; bleibt ein Strom leer (Limit in der
    Denkphase verbraucht), folgt die nicht streamende Anfrage. Höchstens
    ``settings.llm_repair_rounds`` Modellanfragen (Reparatur und Durchsicht zählen
    gleich). Eine Fassung mit durchlaufenem Probelauf wird nie durch eine ungültige
    oder abbrechende ersetzt. ``LLMError`` einer Reparatur geht an den Aufrufer;
    scheitert nur die Durchsicht, bleibt die geprüfte Fassung, der Grund steht in ``notes``.

    FA87: meldet eine Fassung fehlende Daten (``missing_data``), bevor ein Probelauf
    durchlief, endet die Schleife sofort mit leerer Konfiguration und
    ``Outcome.missing_data``; nach einem durchgelaufenen Probelauf bleibt die geprüfte Fassung."""
    rounds = 0
    missing: list[MissingDatasetOut] = []
    reviewed = False
    problem: str | None = None
    # die letzte Fassung, deren Probelauf durchlief
    good: tuple[str, ValidateResponse, TrialRun] | None = None
    current = config_yaml
    validation: ValidateResponse
    trial: TrialRun | None = None

    def ask(
        streamed: Callable[..., Iterator[tuple[str, str]]],
        blocking: Callable[..., str],
        *args: Any,
        **kwargs: Any,
    ) -> Generator[Delta, None, str]:
        if not stream:
            return blocking(*args, **kwargs)
        tokens: list[str] = []
        for kind, text in streamed(*args, **kwargs):
            if kind == "token":
                tokens.append(text)
            yield Delta(kind, text)
        answer = _strip_code_fences("".join(tokens))
        return answer if answer.strip() else blocking(*args, **kwargs)

    def repair(issues: list[str], runtime: bool) -> Generator[Delta, None, str]:
        return (
            yield from ask(
                client.stream_repair_config if stream else None,
                client.repair_config,
                current,
                issues,
                context.templates,
                context.data_hints,
                prompt=context.prompt,
                region=context.region,
                runtime=runtime,
            )
        )

    while True:
        if good is None:
            missing = missing_data(current)
            if missing:
                break
        yield Phase(VALIDATING)
        document, validation = scenario_service.validate_config(current, None)
        trial = None
        if not validation.valid:
            if good is not None or rounds >= settings.llm_repair_rounds:
                break
            yield Phase(REPAIRING)
            issues = [f"{i.location}: {i.message}" for i in validation.issues]
            current = yield from repair(issues, runtime=False)
            rounds += 1
            continue
        if not settings.llm_trial_run:
            break
        assert document is not None
        yield Phase(TRIAL_RUN)
        trial = trial_run(document, settings.llm_trial_run_s, context.connector_options)
        if trial.status == "failed":
            if good is not None or rounds >= settings.llm_repair_rounds:
                break
            yield Phase(REPAIRING)
            current = yield from repair(
                [trial.error or "unbekannter Fehler"], runtime=True
            )
            rounds += 1
            continue
        if trial.status == "timeout":
            break
        good = (current, validation, trial)
        if not trial.warnings or reviewed or rounds >= settings.llm_repair_rounds:
            break
        reviewed = True
        yield Phase(REVIEWING)
        try:
            candidate = yield from ask(
                client.stream_review_config if stream else None,
                client.review_config,
                current,
                list(trial.warnings),
                context.templates,
                context.data_hints,
                prompt=context.prompt,
                region=context.region,
            )
        except LLMError as exc:
            problem = f"Die Durchsicht der Warnungen ist gescheitert: {exc}"
            break
        rounds += 1
        if _same_config(candidate, current):
            break
        current = candidate

    if missing:
        yield Outcome(
            config_yaml="",
            validation=ValidateResponse(valid=False),
            trial_run=TrialRunOut(status="skipped", repair_rounds=rounds),
            notes=_missing_note(missing),
            missing_data=missing,
        )
        return

    if good is not None and (
        not validation.valid or trial is None or trial.status != "ok"
    ):
        # Die Durchsicht lieferte eine schlechtere Fassung; die geprüfte bleibt.
        current, validation, trial = good
        problem = (
            "Eine geänderte Fassung nach der Durchsicht der Warnungen wurde verworfen."
        )

    if trial is None:
        out = TrialRunOut(status="skipped", repair_rounds=rounds)
    else:
        out = TrialRunOut(
            status=trial.status,
            error=trial.error,
            warnings=list(trial.warnings),
            repair_rounds=rounds,
        )
    yield Outcome(
        config_yaml=current,
        validation=validation,
        trial_run=out,
        notes=_notes(validation, out, settings, problem),
    )
