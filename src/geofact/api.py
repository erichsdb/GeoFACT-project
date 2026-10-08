"""Implements: FA44 (ein Anwendungsfall), FA40 (explizite Erkennung), FA45 (öffentliche Fassade für Auslieferungscode), FA59 (Parameter-Überschreibungen, Koerzierung in engine/parameters.py), FA75 (InstanceInfo), FA65 (Attribution der Ausgaben), FA66 (Freigabe opt-in), FA73 (Lizenzkompatibilität der Ausgaben), FA74 (Herkunft, Re-Export).

Die Fassade von GeoFACT für Auslieferungscode (Kommandozeile, Web-Backend,
Skripte, externe Auswertungsskripte). Auslieferungscode importiert nur dieses
Modul (geprüft in tests/unit/test_architecture_layers.py); Autoren von
Bausteinen nutzen ``geofact.plugin_api``.

Der Anwendungsfall (FA44), in dieser Reihenfolge:

- ``load_extensions()``   die eine explizite Erkennung aller Bausteine (Kern und
  Plugins) und Installation der Standard-Registry. Idempotent und thread-sicher.
- ``load_scenario(...)``  YAML-Datei, YAML-Text oder dict -> ``ScenarioDocument``
  (Szenario + ``base_dir`` + Original-Text + Parameter + Expansionsbericht);
  ``ConfigError`` mit Positionen. ``parameters=`` überschreibt deklarierte
  Parameter (FA59); ``parameter_overrides`` koerziert Texte der Kommandozeile.
- ``validate(...)``       -> ``ValidationReport`` (gültig?, Probleme, Plan).
- ``plan(doc)``           -> ``ExecutionPlan``; ``output_attribution(doc, plan)``
  die Lizenzen aller Ausgabequellen (FA65), ohne Lauf; ``check_licenses(doc)``
  prüft sie je Ausgabe samt Ausgabelizenz gegen DALICC (FA73, netzgebunden,
  nur ausdrücklich; Antworten unter ``GEOFACT_SNAPSHOT_DIR/dalicc``).
- ``run(doc, ...)``       -> ``RunReport`` (Region, Plan, Ausführen, deklarierte
  Ausgaben schreiben, Warnungen, Zwischenergebnisse); ``release_intermediates``
  (FA66) ist hier opt-in (Default aus), die CLI schaltet es ein.

Dazu die Kataloge (``extension_catalog``, ``operation_catalog``, ``source_types``,
``scenario_json_schema``), ``osm_backend_status()`` und die Re-Exporte der
Datentypen, Ereignisse und Fehler, die Auslieferungscode braucht (u. a. ``License``,
``NodeAttribution``, ``LayerProvenance``, ``crs_label``, ``CRS_PRESETS``,
``ParameterSpec``, ``ExpansionReport``, ``NodeOrigin``, ``InstanceInfo``, ``RegionInfo``).

Es gibt keine versteckte Erkennung beim ersten Zugriff: ohne ``load_extensions()``
meldet ``core.registry.default_registry()`` einen Fehler. Jeder Einstiegspunkt
dieser Fassade ruft ``load_extensions()`` idempotent selbst auf."""

from __future__ import annotations

import threading
import weakref
from pathlib import Path
from typing import Any, Mapping, Sequence

from geofact.core.errors import (
    ConfigError,
    LayerLoadError,
    OutputSkipped,
    PluginError,
    PluginLoadWarning,
    RunCancelled,
    StepExecutionError,
)
from geofact.core.contracts import License
from geofact.core.crs import CRS_PRESETS, crs_label
from geofact.core.expansion import (
    ExpansionReport,
    InstanceInfo,
    NodeOrigin,
    ParameterSpec,
)
from geofact.core.plan import ExecutionPlan
from geofact.core.provenance import (
    LineageNode,
    NodeAttribution,
    OutputLicense,
    RunLineage,
)
from geofact.core.registry import (
    PLUGIN_PATH_ENV,
    Registry,
    default_registry,
    installed_registry,
    set_default_registry,
)
from geofact.core.scenario import Scenario
from geofact.core.store import LayerStore
from geofact.core.types import (
    DataType,
    Graph,
    LayerData,
    LayerProvenance,
    RasterLayer,
    data_type_of,
)
from geofact.engine import events
from geofact.engine import parameters as _parameters
from geofact.engine import run as _use_case
from geofact.engine.discovery import discover, plugin_paths_from_env
from geofact.engine.events import CancelToken, ProgressEvent, RunWarning
from geofact.engine.executor import ExecutionResult, execute, run_scenario
from geofact.engine.executor import output_attribution as _output_attribution
from geofact.engine.licenses import (
    LicenseCheck,
    LicenseCheckReport,
    LicenseConflict,
    UncheckableLicense,
)
from geofact.engine.licenses import check_licenses as _check_licenses
from geofact.engine.region import RegionInfo
from geofact.engine.outputs import SkippedOutput, WriteOutputsResult, write_outputs
from geofact.engine.run import (
    RunReport,
    ScenarioDocument,
    ScenarioSource,
    ValidationReport,
)

__all__ = [
    "CRS_PRESETS",
    "PLUGIN_PATH_ENV",
    "CancelToken",
    "ConfigError",
    "DataType",
    "ExecutionPlan",
    "ExecutionResult",
    "ExpansionReport",
    "Graph",
    "InstanceInfo",
    "LayerData",
    "LayerLoadError",
    "LayerProvenance",
    "LayerStore",
    "License",
    "LicenseCheck",
    "LicenseCheckReport",
    "LicenseConflict",
    "LineageNode",
    "NodeAttribution",
    "NodeOrigin",
    "OutputLicense",
    "OutputSkipped",
    "ParameterSpec",
    "PluginError",
    "PluginLoadWarning",
    "ProgressEvent",
    "RasterLayer",
    "RegionInfo",
    "Registry",
    "RunCancelled",
    "RunLineage",
    "RunReport",
    "RunWarning",
    "Scenario",
    "ScenarioDocument",
    "SkippedOutput",
    "StepExecutionError",
    "UncheckableLicense",
    "ValidationReport",
    "WriteOutputsResult",
    "check_licenses",
    "crs_label",
    "data_type_of",
    "default_registry",
    "discover",
    "events",
    "execute",
    "extension_catalog",
    "load_extensions",
    "load_scenario",
    "operation_catalog",
    "osm_backend_names",
    "osm_backend_option",
    "osm_backend_status",
    "output_attribution",
    "parameter_overrides",
    "plan",
    "registry",
    "run",
    "run_scenario",
    "scenario_json_schema",
    "source_types",
    "validate",
    "write_outputs",
]

_load_lock = threading.Lock()

# Plugin-Ordner, mit denen load_extensions() eine Registry gebaut hat (je
# Registry-Objekt): ein späterer Aufruf mit anderen Ordnern wird so erkannt,
# statt stillschweigend die alte Registry zu liefern.
_built_from: "weakref.WeakKeyDictionary[Registry, tuple[Path, ...]]" = (
    weakref.WeakKeyDictionary()
)


def _normalise_paths(paths: Sequence[str | Path]) -> tuple[Path, ...]:
    """Aufgelöste Plugin-Ordner in Angabereihenfolge, ohne Doppelte (so
    behandelt auch ``discover`` dieselben Ordner)."""
    unique: list[Path] = []
    for raw in paths:
        path = Path(raw)
        try:
            path = path.resolve()
        except OSError:
            pass
        if path not in unique:
            unique.append(path)
    return tuple(unique)


def _shown(paths: tuple[Path, ...]) -> str:
    return "[" + ", ".join(str(p) for p in paths) + "]" if paths else "keine"


def _check_same_plugin_paths(current: Registry, requested: tuple[Path, ...]) -> None:
    """Goldene Regel 7: Wer ausdrücklich andere Plugin-Ordner verlangt, als
    die installierte Registry hat, bekommt keine stillschweigend abweichende
    Registry, sondern einen Fehler mit dem Weg zum Ersetzen."""
    installed = _built_from.get(current)
    if installed is None:
        known = "nicht über load_extensions() installiert (Plugin-Ordner unbekannt)"
    elif installed == requested:
        return
    else:
        known = f"mit den Plugin-Ordnern {_shown(installed)} geladen"
    raise PluginError(
        f"Die Erweiterungen sind bereits {known}; angefordert wurden {_shown(requested)}. "
        "Die Registry gilt je Prozess einmal - load_extensions(plugin_paths=..., force=True) "
        "erkennt neu und ersetzt sie."
    )


def load_extensions(
    plugin_paths: Sequence[str | Path] | None = None, force: bool = False
) -> Registry:
    """Erkennt alle Bausteine und installiert die Standard-Registry.

    plugin_paths: Plugin-Ordner; None = GEOFACT_PLUGIN_PATH (os.pathsep-
    getrennt), eine leere Liste = keine Plugins. Ist bereits eine Registry
    installiert und force=False, wird sie zurückgegeben - je Prozess einmal.
    Ohne ``plugin_paths`` (None) ist das immer so; mit ``plugin_paths`` nur,
    wenn die Ordner denen der installierten Registry entsprechen, sonst
    ``PluginError`` (der Aufrufer bekäme eine Registry ohne seine Plugins).
    force=True erkennt neu und ersetzt die Registry. Thread-sicher: parallele
    Aufrufer warten auf dieselbe Erkennung."""
    with _load_lock:
        current = installed_registry()
        if current is not None and not force:
            if plugin_paths is not None:
                _check_same_plugin_paths(current, _normalise_paths(plugin_paths))
            return current
        paths = plugin_paths_from_env() if plugin_paths is None else list(plugin_paths)
        loaded = discover(paths)
        set_default_registry(loaded)
        _built_from[loaded] = _normalise_paths(paths)
        return loaded


def registry() -> Registry:
    """Die installierte Registry; lädt die Erweiterungen bei Bedarf."""
    return load_extensions()


# --- Anwendungsfall (FA44) ---


def load_scenario(
    source: Path | str | dict,
    base_dir: Path | str | None = None,
    registry: Registry | None = None,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> ScenarioDocument:
    """Lädt und validiert ein Szenario: ``Path`` = YAML-Datei, ``str`` =
    YAML-Text, ``dict`` = geparste Konfiguration. base_dir (FA4): Verzeichnis,
    gegen das relative Pfade im Szenario gelten; ohne Angabe bei einer Datei
    ihr Verzeichnis. parameters (FA59): Überschreibungen deklarierter
    Parameter (typisierte Werte; Texte vorher mit ``parameter_overrides``).
    Wirft ``ConfigError`` (Position + Meldung je Problem)."""
    return _use_case.load_scenario(
        source,
        base_dir,
        registry if registry is not None else load_extensions(),
        parameters=parameters,
    )


def validate(
    source: ScenarioSource,
    base_dir: Path | str | None = None,
    registry: Registry | None = None,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> ValidationReport:
    """Prüft ein Szenario (FA2) und liefert einen ``ValidationReport``; wirft
    nie wegen einer ungültigen Konfiguration. parameters wie bei
    ``load_scenario`` (FA59)."""
    return _use_case.validate(
        source,
        base_dir,
        registry if registry is not None else load_extensions(),
        parameters=parameters,
    )


def parameter_overrides(
    source: Path | str | dict, assignments: Sequence[str] | Mapping[str, Any]
) -> dict[str, Any]:
    """FA59 Nachbedingung 5: Überschreibungen als Text (``NAME=WERT`` von der
    Kommandozeile oder ``{name: text}`` aus einem Formular) -> typisierte
    Werte für ``load_scenario(parameters=...)``, gelesen nach den in
    ``source`` deklarierten Parametern (``engine/parameters.py``). Fehler:
    ``ConfigError`` an ``--param`` bzw. ``parameters -> <name>``."""
    if not assignments:
        return {}
    raw, _, _ = _use_case._read_source(source)
    return _parameters.parameter_overrides(raw, assignments)


def plan(document: ScenarioDocument, registry: Registry | None = None) -> ExecutionPlan:
    """Der Ausführungsplan eines geladenen Szenarios (FA9)."""
    return _use_case.plan(
        document, registry if registry is not None else load_extensions()
    )


def output_attribution(
    document: ScenarioDocument, execution_plan: ExecutionPlan | None = None
) -> NodeAttribution:
    """FA65: Vereinigung der Lizenzen aller Ausgabequellen (Reihenfolge der
    Ausgaben, ohne Duplikate) und der Layer ohne Lizenz - aus dem Plan, ohne
    etwas zu laden."""
    execution_plan = execution_plan if execution_plan is not None else plan(document)
    return _output_attribution(document.scenario, execution_plan)


def check_licenses(
    document: ScenarioDocument,
    *,
    refresh: bool = False,
    registry: Registry | None = None,
) -> LicenseCheckReport:
    """FA73: Kompatibilität der Lizenzen je Ausgabe (Quellen + Ausgabelizenz)
    laut DALICC. Netzgebunden und nur ausdrücklich aufgerufen; Antworten liegen
    danach unter ``GEOFACT_SNAPSHOT_DIR/dalicc`` (``refresh`` fragt neu). Ein
    nicht erreichbarer Dienst ist der Status ``unreachable``, keine Ausnahme."""
    execution_plan = plan(document, registry)
    return _check_licenses(document.scenario, execution_plan, refresh=refresh)


def run(
    document: ScenarioDocument,
    *,
    out_dir: Path | str | None = None,
    observer=None,
    cancel: CancelToken | None = None,
    store: LayerStore | None = None,
    refresh: bool = False,
    dump_intermediate: Path | str | None = None,
    connector_override=None,
    region_fetcher=None,
    registry: Registry | None = None,
    release_intermediates: bool = False,
    connector_options: Mapping[str, str] | None = None,
) -> RunReport:
    """Führt ein Szenario aus und schreibt - mit ``out_dir`` - seine
    deklarierten Ausgaben (FA44). release_intermediates (FA66): Zwischen-
    ergebnisse nach ihrem letzten Leser freigeben (opt-in; nicht zusammen mit
    ``dump_intermediate``). connector_options (FA82): Laufzeitoptionen der
    Konnektoren für diesen Lauf, z. B. ``{"osm.backend": "postgis"}``.
    Parameter und Fehler siehe ``geofact.engine.run.run``."""
    return _use_case.run(
        document,
        out_dir=out_dir,
        observer=observer,
        cancel=cancel,
        store=store,
        refresh=refresh,
        dump_intermediate=dump_intermediate,
        connector_override=connector_override,
        region_fetcher=region_fetcher,
        registry=registry if registry is not None else load_extensions(),
        release_intermediates=release_intermediates,
        connector_options=connector_options,
    )


# --- Kataloge und Betriebszustand ---


def extension_catalog() -> dict[str, list[dict[str, Any]]]:
    """Alle Erweiterungspunkte mit Name, Beschreibung und Herkunft (FA40)."""
    from geofact.engine import catalog

    return catalog.extension_catalog(load_extensions())


def operation_catalog() -> list[dict[str, Any]]:
    """Alle registrierten Operationen als sortierte Liste von Contracts."""
    from geofact.engine import catalog

    return catalog.operation_catalog(load_extensions())


def source_types() -> list[dict[str, str]]:
    """Alle registrierten Quellarten (Name + Beschreibung)."""
    from geofact.engine import catalog

    return catalog.source_types(load_extensions())


def scenario_json_schema() -> dict[str, Any]:
    """JSON-Schema des Scenario-Modells (Editor, LLM-Grounding)."""
    from geofact.engine import catalog

    return catalog.scenario_json_schema(load_extensions())


def osm_backend_status(timeout_s: float = 2.0) -> dict[str, Any]:
    """Zustand des OSM-Backends (GEOFACT_OSM_BACKEND): Name, ob PostGIS aktiv
    ist, ob es erreichbar ist (Verbindungstest mit kurzem Timeout) und
    Details. Wirft nie wegen eines Verbindungsfehlers - der Betriebszustand
    ist das Ergebnis (Health-Endpunkt der Web-Demo)."""
    from geofact.builtin.sources import osm

    return osm.backend_status(timeout_s)


def osm_backend_names() -> list[str]:
    """Die Namen der vorhandenen OSM-Backends (FA82), für Auswahllisten."""
    from geofact.builtin.sources import osm

    return osm.backend_names()


def osm_backend_option(name: str) -> dict[str, str]:
    """Die Konnektor-Optionen, mit denen ein Lauf das OSM-Backend ``name`` nutzt
    (FA82), für ``run(connector_options=...)``. Ein unbekannter Name ist ein
    ValueError, bevor ein Lauf startet."""
    from geofact.builtin.sources import osm

    if name not in osm.backend_names():
        raise ValueError(
            f"Unbekanntes OSM-Backend '{name}', verfügbar: {osm.backend_names()}"
        )
    return {osm.BACKEND_OPTION: name}
