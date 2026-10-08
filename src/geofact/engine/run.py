"""Implements: FA44 (Szenario als ein Anwendungsfall ausführen, Vorab-Check der Layer-Pfade), FA2 (Prüfung mit Position), FA4 (base_dir), FA9, FA12, FA55 (Arbeits-CRS aus scenario.crs), FA58/FA65 (Provenienz und Attribution im RunReport), FA66 (Freigabe, opt-in), FA59 (Parameter-Überschreibungen und Expansionsbericht im ScenarioDocument), FA75 (Bericht der einen Expansion).

Der eine Weg, ein Szenario auszuführen: Kommandozeile, Web-Demo und Skripte rufen
genau diese Funktionen (über ``geofact.api``), keiner setzt "parsen, prüfen,
planen, ausführen, Ausgaben schreiben" selbst zusammen:

    load_scenario(...)  -> ScenarioDocument   YAML-Datei, YAML-Text oder dict
    validate(...)       -> ValidationReport   gültig? Probleme mit Position, Plan
    plan(doc)           -> ExecutionPlan      welche Schritte, welche Layer wann
    run(doc, ...)       -> RunReport          Plan -> Vorab-Check -> Region
                                              (Arbeits-CRS) -> Ausführen ->
                                              Ausgaben schreiben -> Zwischenergebnisse

``ScenarioDocument`` hält das validierte Szenario, sein ``base_dir`` (FA4: gegen
das relative Pfade aufgelöst werden; nie ein stillschweigend angenommener
Repo-Root) und den Original-Text.

Der eine Formatierer für Konfigurationsfehler (``issues_of``): Pydantic-Fehler,
ValueError, TypeError und PluginError werden zu Meldungen mit Position
(``steps -> 3 -> params -> radius_km``), häufige Pydantic-Fehlerarten auf Deutsch.
``load_scenario`` wirft deshalb nie einen rohen ``TypeError`` aus einem Validator,
sondern immer einen ``ConfigError``.

Fehlerarten von ``run``: ``ConfigError`` (Plan, Region), ``LayerLoadError``,
``StepExecutionError``, ``RunCancelled`` (Goldene Regel 7). Diese Datei kennt keinen
konkreten Baustein; die Registry liefert alles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Union

import yaml
from geopandas import GeoDataFrame
from pydantic import ValidationError

from geofact.core.errors import ConfigError, PluginError, german_message
from geofact.core.expansion import ExpansionReport
from geofact.core.plan import ExecutionPlan
from geofact.core.provenance import NodeAttribution
from geofact.core.registry import Registry, default_registry
from geofact.core.scenario import Scenario
from geofact.core.store import LayerStore
from geofact.core.types import LayerProvenance
from geofact.engine import region as region_module
from geofact.engine.events import CancelToken, ProgressCallback, RunWarning
from geofact.engine.executor import ExecutionResult, execute, output_attribution
from geofact.engine.loading import ConnectorOverride
from geofact.engine.outputs import SkippedOutput, WriteOutputsResult, write_outputs

if TYPE_CHECKING:
    from geofact.engine.region import RegionInfo

Issue = tuple[str, str]
"""Ein Konfigurationsproblem: (Position, Meldung)."""

ROOT_LOCATION = "(Wurzel)"


# --- Ergebnistypen ---


@dataclass(frozen=True)
class ScenarioDocument:
    """Ein geladenes, validiertes Szenario mit Herkunft (FA44).

    base_dir: Verzeichnis, gegen das relative Layer-Pfade aufgelöst werden
    (FA4); None = relativ zum Arbeitsverzeichnis des Prozesses (nur für
    Szenarien ohne Datei-Ursprung, die der Aufrufer nicht verankert hat).
    source_text: der Original-YAML-Text; bei einem dict dessen YAML-Darstellung
    (None nur, wenn das dict nicht als YAML darstellbar ist) - immer die Form
    MIT Platzhaltern, nie die ausgedehnte (FA59 Nachbedingung 4).
    parameters (FA59): die Überschreibungen des Aufrufers (leer = Defaults).
    expansion (FA59, FA62, FA75): der Bericht der Makro-Expansion (wirksame
    Werte, Herkunft je Knoten, Instanzen, durch ``when`` entfernte Elemente,
    ausgedehntes Dict; ``Scenario.expansion``, nur einmal berechnet);
    None nur bei einem von Hand gebauten Dokument."""

    scenario: Scenario
    base_dir: Path | None = None
    source_text: str | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    expansion: ExpansionReport | None = None


@dataclass(frozen=True)
class ValidationReport:
    """Ergebnis von ``validate``: ``valid``, die Probleme (Position, Meldung)
    und - bei Gültigkeit - Ausführungsplan und Dokument."""

    valid: bool
    issues: list[Issue] = field(default_factory=list)
    plan: ExecutionPlan | None = None
    document: ScenarioDocument | None = None


@dataclass
class RunReport:
    """Ergebnis von ``run``: das Ausführungsergebnis, die geschriebenen und
    übersprungenen Ausgaben (None, wenn kein ``out_dir`` gegeben war), die
    gesammelten Warnungen und die abgelegten Zwischenergebnisse.

    region (FA55/FA56): Beschreibung der Region mit Arbeits-CRS (None, solange
    ``engine/region.py`` keine ``describe_region`` anbietet). attribution
    (FA65): Vereinigung der Lizenzen aller Ausgabequellen. attribution_file:
    die geschriebene ``ATTRIBUTION.txt`` (None ohne ``out_dir`` oder ohne
    Lizenz). provenance (FA58): CRS-Herkunft je geladenem Layer."""

    result: ExecutionResult
    outputs: WriteOutputsResult | None = None
    warnings: list[RunWarning] = field(default_factory=list)
    intermediates: list[Path] = field(default_factory=list)
    intermediates_skipped: list[tuple[str, str]] = field(default_factory=list)
    region: "RegionInfo | None" = None
    attribution: NodeAttribution = field(default_factory=NodeAttribution)
    attribution_file: Path | None = None
    provenance: dict[str, LayerProvenance] = field(default_factory=dict)

    @property
    def skipped_outputs(self) -> list[SkippedOutput]:
        return list(self.outputs.skipped) if self.outputs is not None else []


ScenarioSource = Union[ScenarioDocument, Path, str, dict]


# --- Fehlerformatierer ---


def _location(parts: tuple[Any, ...] | list[Any]) -> str:
    return " -> ".join(str(part) for part in parts) or ROOT_LOCATION


def _issues_of_validation_error(
    exc: ValidationError, prefix: tuple[Any, ...] = ()
) -> list[Issue]:
    return [
        (_location((*prefix, *error["loc"])), german_message(error))
        for error in exc.errors()
    ]


def _describe(exc: BaseException) -> str:
    """Meldungstext einer nicht-Pydantic-Ausnahme (ein ValueError trägt schon eine fertige Meldung)."""
    if isinstance(exc, (ValueError, PluginError)):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def issues_of(exc: BaseException) -> list[Issue]:
    """Der eine Formatierer: eine Ausnahme der Konfigurationsprüfung -> Liste von
    (Position, Meldung). Ein Pydantic-Fehler trägt die Position jedes Einzelfehlers;
    alles andere (ValueError, TypeError, PluginError) steht unter ``(Wurzel)``."""
    if isinstance(exc, ConfigError):
        return list(exc.issues)
    if isinstance(exc, ValidationError):
        return _issues_of_validation_error(exc)
    return [(ROOT_LOCATION, _describe(exc))]


# --- Laden ---


def _yaml_error_text(exc: yaml.YAMLError) -> str:
    """Einzeilige Meldung mit Zeile und Spalte (soweit PyYAML sie kennt)."""
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None)
    if mark is None or problem is None:
        return f"YAML-Syntaxfehler: {exc}"
    return f"YAML-Syntaxfehler in Zeile {mark.line + 1}, Spalte {mark.column + 1}: {problem}"


def _read_source(source: Path | str | dict) -> tuple[Any, str | None, Path | None]:
    """(rohe Daten, Original-Text, Verzeichnis der Datei) einer Quelle."""
    if isinstance(source, dict):
        try:
            text = yaml.safe_dump(source, allow_unicode=True, sort_keys=False)
        except yaml.YAMLError:
            text = None  # z. B. Path-Objekte im dict: kein YAML-Text
        return source, text, None
    if isinstance(source, str):
        text, origin = source, None
    elif isinstance(source, Path):
        if not source.is_file():
            raise ConfigError(
                [("(Datei)", f"Konfigurationsdatei nicht gefunden: {source}")]
            )
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(
                [("(Datei)", f"Konfigurationsdatei nicht lesbar: {source} ({exc})")]
            ) from exc
        origin = source.resolve().parent
    else:
        raise TypeError(
            "load_scenario erwartet einen Path (YAML-Datei), einen str (YAML-Text) oder "
            f"ein dict, nicht {type(source).__name__}"
        )
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([("(YAML)", _yaml_error_text(exc))]) from exc
    if not isinstance(raw, dict):
        hint = ""
        if isinstance(raw, str) and Path(raw.strip()).suffix.lower() in (
            ".yaml",
            ".yml",
        ):
            hint = f" Ist '{raw.strip()}' ein Dateipfad? Dann als Path(...) übergeben."
        raise ConfigError(
            [("(YAML)", "Konfiguration muss ein YAML-Objekt (Mapping) sein." + hint)]
        )
    return raw, text, origin


def load_scenario(
    source: Path | str | dict,
    base_dir: Path | str | None = None,
    registry: Registry | None = None,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> ScenarioDocument:
    """Lädt und validiert ein Szenario.

    source: ``Path`` = YAML-Datei, ``str`` = YAML-Text, ``dict`` = geparste
    Konfiguration. base_dir (FA4): Verzeichnis für relative Pfade; ohne Angabe das
    der Datei, bei Text/dict keines. registry: None = Standard-Registry.
    parameters (FA59): Überschreibungen der deklarierten Parameter (schon typisiert).

    Ist die Konfiguration ungültig (FA2), folgt ein ``ConfigError`` mit (Position,
    Meldung) je Problem, nie ein roher ``TypeError``/``ValidationError``."""
    registry = registry if registry is not None else default_registry()
    overrides = dict(parameters or {})
    raw, text, origin = _read_source(source)
    scenario = _build_scenario(raw, registry, overrides)
    # FA75: die Expansion lief einmal im Validator, ihr Bericht hängt am Szenario.
    expansion = scenario.expansion
    anchor = Path(base_dir) if base_dir is not None else origin
    return ScenarioDocument(
        scenario=scenario,
        base_dir=anchor.resolve() if anchor is not None else None,
        source_text=text,
        parameters=overrides,
        expansion=expansion,
    )


def _build_scenario(
    raw: dict, registry: Registry, parameters: Mapping[str, Any]
) -> Scenario:
    context: dict[str, Any] = {"registry": registry}
    if parameters:
        context["parameters"] = dict(parameters)
    try:
        return Scenario.model_validate(raw, context=context)
    except ConfigError:
        raise
    except ValidationError as exc:
        raise ConfigError(_issues_of_validation_error(exc)) from exc
    except (TypeError, ValueError, AttributeError, KeyError, PluginError) as exc:
        raise ConfigError(issues_of(exc)) from exc


# --- Prüfen und planen ---


def validate(
    source: ScenarioSource,
    base_dir: Path | str | None = None,
    registry: Registry | None = None,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> ValidationReport:
    """Prüft ein Szenario, ohne auszuführen (FA2): Schema, DAG, Porttypen und
    registrierte Bausteine. Liefert immer einen Bericht. parameters (FA59) wie bei
    ``load_scenario``; bei einem ``ScenarioDocument`` ist eine weitere Angabe ein
    ``ValueError``, das Dokument ist schon ausgedehnt."""
    registry = registry if registry is not None else default_registry()
    if isinstance(source, ScenarioDocument) and parameters:
        raise ValueError(
            "validate: parameters gelten nur für Datei, YAML-Text oder dict - ein "
            "ScenarioDocument ist bereits mit seinen Parametern geladen (load_scenario)"
        )
    try:
        document = (
            source
            if isinstance(source, ScenarioDocument)
            else load_scenario(source, base_dir, registry, parameters=parameters)
        )
        execution_plan = ExecutionPlan.of(document.scenario, registry)
    except ConfigError as exc:
        return ValidationReport(valid=False, issues=list(exc.issues))
    return ValidationReport(valid=True, plan=execution_plan, document=document)


def plan(document: ScenarioDocument, registry: Registry | None = None) -> ExecutionPlan:
    """Der Ausführungsplan eines geladenen Szenarios (FA9)."""
    registry = registry if registry is not None else default_registry()
    return ExecutionPlan.of(document.scenario, registry)


# --- Ausführen ---

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def _dump_intermediates(
    result: ExecutionResult, directory: Path
) -> tuple[list[Path], list[tuple[str, str]]]:
    """Legt die Ergebnisse der ausgeführten Schritte als GeoJSON ab; Nicht-Vektor-
    Ergebnisse werden mit Grund gemeldet. Fällt ein Dateiname doppelt an (ersetzte
    Zeichen, Groß-/Kleinschreibung), bekommt der spätere ein Suffix ``__2`` ...
    statt den früheren zu überschreiben. Ein nicht anlegbares Verzeichnis wird
    je Schritt gemeldet; der Bericht des Laufs bleibt."""
    written: list[Path] = []
    skipped: list[tuple[str, str]] = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        reason = (
            f"Verzeichnis '{directory}' nicht anlegbar ({type(exc).__name__}): {exc}"
        )
        return written, [(step_id, reason) for step_id in result.order]
    taken: set[str] = set()
    for step_id in result.order:
        data = result.store.get(step_id)
        if not isinstance(data, GeoDataFrame):
            skipped.append(
                (step_id, f"{type(data).__name__} wird nicht als GeoJSON abgelegt")
            )
            continue
        stem = _UNSAFE_FILENAME.sub("_", step_id)
        name, counter = f"{stem}.geojson", 1
        while name.casefold() in taken:
            counter += 1
            name = f"{stem}__{counter}.geojson"
        taken.add(name.casefold())
        target = directory / name
        try:
            data.to_file(target, driver="GeoJSON")
        except Exception as exc:  # noqa: BLE001 - je Schritt gemeldet, die übrigen laufen weiter
            target.unlink(missing_ok=True)
            skipped.append(
                (step_id, f"Schreiben fehlgeschlagen ({type(exc).__name__}): {exc}")
            )
            continue
        written.append(target)
    return written, skipped


def _precheck_layers(
    scenario: Scenario,
    execution_plan: ExecutionPlan,
    base_dir: Path | None,
    connector_override: ConnectorOverride | None = None,
    expansion: ExpansionReport | None = None,
) -> None:
    """FA44/FA4: Vorab-Check der benötigten Layer (nur diese, Lazy Loading). Ein
    Layer mit ersetztem Konnektor (``connector_override``) liest seine Quelle nicht
    und wird nicht geprüft. Die Position ist die Fachposition (FA75), wenn
    ``expansion`` den Layer erzeugt hat."""
    issues: list[Issue] = []
    overridden = set(connector_override or ())
    for index, layer in enumerate(scenario.layers):
        if layer.id not in execution_plan.required_layers or layer.id in overridden:
            continue
        check = getattr(layer, "check_before_run", None)
        if not callable(check):
            continue
        loc: tuple = ("layers", index)
        if expansion is not None:
            loc = expansion.relocate(loc)
        position = _location(loc)
        for field_name, message in check(base_dir) or ():
            issues.append((f"{position} ({layer.id}) -> {field_name}", message))
    if issues:
        raise ConfigError(issues)


def _resolve_region(
    scenario: Scenario,
    region_fetcher: region_module.Fetcher | None,
    refresh: bool = False,
) -> tuple[GeoDataFrame, "RegionInfo"]:
    """Region auflösen und beschreiben (Arbeits-CRS aus ``scenario.crs``, FA55;
    Plausibilität, FA56). Jeder ValueError wird zum ``ConfigError`` an ``scenario -> region``."""
    try:
        region, meta = region_module.resolve_region_with_meta(
            scenario.scenario.region, fetcher=region_fetcher, refresh=refresh
        )
        return region, region_module.describe_region(
            region, scenario.scenario.crs, meta
        )
    except ValueError as exc:
        raise ConfigError([("scenario -> region", str(exc))]) from exc


def _check_connector_options(
    options: Mapping[str, str] | None, registry: Registry
) -> None:
    """FA82: jeder Schlüssel hat die Form ``<quellart>.<name>`` mit registrierter
    Quellart, jeder Wert ist Text; die Bedeutung prüft der Konnektor beim Laden."""
    for key, value in (options or {}).items():
        source, _, name = str(key).partition(".")
        if not source or not name:
            raise ValueError(
                f"Konnektor-Option '{key}': erwartet '<quellart>.<name>', z. B. 'osm.backend'"
            )
        if source not in registry.names("source"):
            raise ValueError(
                f"Konnektor-Option '{key}': unbekannte Quellart '{source}', registriert sind: "
                f"{registry.names('source')}{registry.failed_hint()}"
            )
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Konnektor-Option '{key}': der Wert muss ein nicht leerer Text sein, nicht {value!r}"
            )


def run(
    document: ScenarioDocument,
    *,
    out_dir: Path | str | None = None,
    observer: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    store: LayerStore | None = None,
    refresh: bool = False,
    dump_intermediate: Path | str | None = None,
    connector_override: ConnectorOverride | None = None,
    region_fetcher: region_module.Fetcher | None = None,
    registry: Registry | None = None,
    release_intermediates: bool = False,
    connector_options: Mapping[str, str] | None = None,
) -> RunReport:
    """Führt ein Szenario aus: Plan ableiten, Layer-Pfade vorab prüfen, Region
    auflösen (Arbeits-CRS aus ``scenario.crs``), Plan ausführen, Ausgaben nach
    ``out_dir`` schreiben (falls angegeben), Zwischenergebnisse nach
    ``dump_intermediate`` ablegen.

    observer: ProgressEvent-Callback. cancel: CancelToken. store: vom Aufrufer
    gehaltener LayerStore (auch nach einem Fehler lesbar, FA33). refresh:
    Snapshots neu beziehen (FA7). connector_override / region_fetcher: Nahtstellen
    für Tests und Offline-Läufe. release_intermediates (FA66): nicht zusammen mit
    ``dump_intermediate`` (ValueError). connector_options (FA82): Schlüssel
    ``<quellart>.<name>`` (z. B. ``{"osm.backend": "postgis"}``), als
    ``LoadContext.options`` an die Konnektoren; ein ungültiger Schlüssel oder Wert
    ist ein ValueError vor dem Lauf, damit eine Option nicht still verpufft.

    Vorab-Check (FA44): ``check_before_run(base_dir)`` jedes benötigten Layers
    scheitert als ``ConfigError`` (``layers -> i (id) -> feld``) vor Region und
    erstem Ladevorgang. Nachbedingung: jede Ausgabe ist geschrieben oder mit Grund
    in ``RunReport.outputs.skipped`` gemeldet (FA12)."""
    if not isinstance(document, ScenarioDocument):
        raise TypeError(
            f"run erwartet ein ScenarioDocument (load_scenario), nicht {type(document).__name__}"
        )
    if release_intermediates and dump_intermediate is not None:
        raise ValueError(
            "release_intermediates und dump_intermediate schließen sich aus: freigegebene "
            "Zwischenergebnisse können nicht abgelegt werden (FA66)"
        )
    registry = registry if registry is not None else default_registry()
    _check_connector_options(connector_options, registry)
    scenario = document.scenario

    # Plan zuerst: ein unregistrierter Baustein fällt vor Netzzugriff und Laden auf.
    execution_plan = ExecutionPlan.of(
        scenario, registry, release_intermediates=release_intermediates
    )
    _precheck_layers(
        scenario,
        execution_plan,
        document.base_dir,
        connector_override,
        document.expansion,
    )
    if dump_intermediate is not None:
        # Vor dem Lauf, damit ein nicht anlegbares Verzeichnis vor dem Laden scheitert.
        Path(dump_intermediate).mkdir(parents=True, exist_ok=True)
    region, region_info = _resolve_region(scenario, region_fetcher, refresh)

    result = execute(
        scenario,
        execution_plan,
        registry=registry,
        region=region,
        observer=observer,
        cancel=cancel,
        store=store,
        base_dir=document.base_dir,
        refresh_snapshots=refresh,
        connector_override=connector_override,
        target_crs=region_info.crs,
        region_info=region_info,
        release_intermediates=release_intermediates,
        region_fetcher=region_fetcher,
        connector_options=connector_options,
    )
    report = RunReport(
        result=result,
        warnings=list(result.warnings),
        region=region_info,
        attribution=output_attribution(scenario, execution_plan),
        provenance=dict(result.provenance),
    )
    if out_dir is not None:
        report.outputs = write_outputs(scenario, result, out_dir, registry=registry)
        report.attribution_file = report.outputs.attribution_file
    if dump_intermediate is not None:
        report.intermediates, report.intermediates_skipped = _dump_intermediates(
            result, Path(dump_intermediate)
        )
    return report
