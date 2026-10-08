"""Implements: FA40 (Einheitliche Erweiterungspunkte mit Plugin-Erkennung).

Eine Registry für alle sechs Erweiterungsarten, gleich für Kernbausteine und Plugins:

- source        Konnektor für eine Quellart (Layer-Feld ``source``)
- file_format   Dateiformat des Datei-Konnektors (Layer-Feld ``format``)
- table_format  Tabellenformat des Tabellen-Konnektors
- transform     Harmonisierung nach dem Laden (Standard: ``reproject``)
- operation     Verarbeitungsschritt (Step-Feld ``op``): Vertrag (Step-Klasse)
                und Ausführungsfunktion in einem Eintrag
- output        Ausgabeformat (Output-Feld ``type``)

Die ``register_*``-Dekoratoren markieren nur: sie prüfen sofort (Signatur,
Unterklasse, Namens-Default) und hängen den Eintrag an die Funktion
(``fn.__geofact_registrations__``), ohne globalen Zustand. Erst
``geofact.engine.discovery.discover()`` führt die Markierungen je Modul atomar
in eine Registry zusammen: ein Plugin, das nach dem ersten Dekorator scheitert,
hinterlässt nichts. Eine fertige Registry wird eingefroren (MappingProxyType)
und ist danach ohne Sperre von beliebig vielen Threads lesbar.

Die Standard-Registry des Prozesses setzt ``geofact.api.load_extensions()``;
ohne sie meldet ``default_registry()`` einen expliziten Fehler statt still eine
Erkennung anzustossen. Externe REST-Endpunkte sind keine eigene Erweiterungsart,
sondern die Kernbausteine ``rest`` (FA41-FA43).
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional

from pydantic import BaseModel, ValidationInfo

from geofact.core.contracts import (
    FileReader,
    LayerBase,
    OperationRunner,
    OutputWriter,
    ParamsBase,
    SourceLoader,
    Step,
    TableReader,
    Transform,
)
from geofact.core.errors import PluginError
from geofact.core.types import DataType

PLUGIN_PATH_ENV = "GEOFACT_PLUGIN_PATH"
"""Umgebungsvariable mit den Plugin-Ordnern (os.pathsep-getrennt)."""

MARK = "__geofact_registrations__"
"""Attribut einer markierten Funktion: Liste von (kind, Eintrag)."""

DEFAULT_TRANSFORM = "reproject"

KINDS = ("source", "file_format", "table_format", "transform", "operation", "output")

KIND_LABELS = {
    "source": "Quellart",
    "file_format": "Dateiformat",
    "table_format": "Tabellenformat",
    "transform": "Transformation",
    "operation": "Operation",
    "output": "Ausgabeformat",
}

NOT_LOADED_MESSAGE = (
    "Erweiterungen nicht geladen – geofact.api.load_extensions() aufrufen"
)


# --- Einträge der Verzeichnisse ---


@dataclass(frozen=True)
class SourcePlugin:
    name: str
    layer_model: type[LayerBase]
    load: SourceLoader
    # Name einer registrierten Transformation oder eine Funktion (layer, data, ctx) -> data.
    transform: str | Transform = DEFAULT_TRANSFORM
    description: str = ""
    origin: str = ""


@dataclass(frozen=True)
class FileFormatPlugin:
    name: str
    extensions: tuple[str, ...]
    data_type: DataType
    read: FileReader
    transform: str | Transform = DEFAULT_TRANSFORM
    # Endungen als Mitglied eines Zip-Containers (leer = nicht unterstützt).
    zip_extensions: tuple[str, ...] = ()
    # Formatspezifische Layer-Felder (z. B. ``layer`` bei GeoPackage), flach in der
    # YAML; der Datei-Konnektor prüft sie gegen dieses Modell. None = keine.
    options_model: Optional[type[BaseModel]] = None
    description: str = ""
    origin: str = ""


@dataclass(frozen=True)
class TableFormatPlugin:
    name: str
    extensions: tuple[str, ...]
    read: TableReader
    # Formatspezifische Layer-Felder (z. B. ``sheet`` bei Excel), wie bei FileFormatPlugin.
    options_model: Optional[type[BaseModel]] = None
    description: str = ""
    origin: str = ""


@dataclass(frozen=True)
class TransformPlugin:
    name: str
    apply: Transform
    description: str = ""
    origin: str = ""


@dataclass(frozen=True)
class OperationPlugin:
    """Eine Operation: Vertrag (step_model) und Ausführung (run) gehören
    zusammen - Validierung und Executor lesen denselben Eintrag."""

    name: str
    step_model: type[Step]
    run: OperationRunner
    description: str = ""
    origin: str = ""


@dataclass(frozen=True)
class OutputPlugin:
    name: str
    extension: str
    accepts: frozenset[DataType]
    write: OutputWriter
    # Modell für formatspezifische Felder der Output-Deklaration (z. B. color_field bei map).
    options_model: Optional[type[BaseModel]] = None
    description: str = ""
    origin: str = ""


# --- Registry ---


class Registry:
    """Sechs Tabellen (kind -> name -> Eintrag) plus die Liste der
    übersprungenen Plugins. Bis ``freeze()`` veränderbar (Aufbau durch
    die Erkennung), danach nur lesbar."""

    def __init__(self) -> None:
        self._tables: dict[str, Any] = {kind: {} for kind in KINDS}
        self._failed: Any = {}
        self._frozen = False

    # -- Aufbau ----------------------------------------------------------

    @property
    def frozen(self) -> bool:
        return self._frozen

    def _require_mutable(self) -> None:
        if self._frozen:
            raise RuntimeError(
                "Registry ist eingefroren - eine neue Registry per discover() aufbauen"
            )

    def merge(self, staged: Iterable[tuple[str, Any]]) -> None:
        """Führt die Einträge eines Moduls atomar zusammen: alle oder, bei doppeltem
        Namen oder kollidierender Dateiendung, keiner. Die Meldung nennt beide Herkünfte."""
        self._require_mutable()
        pending: dict[str, dict[str, Any]] = {kind: {} for kind in KINDS}
        for kind, entry in staged:
            if kind not in pending:
                raise PluginError(
                    f"Unbekannte Erweiterungsart '{kind}', erlaubt sind: {list(KINDS)}"
                )
            existing = self._tables[kind].get(entry.name) or pending[kind].get(
                entry.name
            )
            if existing is not None:
                raise PluginError(
                    f"{KIND_LABELS[kind]} '{entry.name}' ist doppelt registriert: "
                    f"{existing.origin} und {entry.origin}"
                )
            pending[kind][entry.name] = entry
        for kind in ("file_format", "table_format"):
            self._check_extension_collisions(kind, pending[kind])
        for kind in KINDS:
            self._tables[kind].update(pending[kind])

    def _check_extension_collisions(
        self, kind: str, new_entries: dict[str, Any]
    ) -> None:
        """Zwei Formate dürfen nicht dieselbe Dateiendung beanspruchen, sonst
        entschiede die Ladereihenfolge still, welches liest."""
        claimed: dict[str, Any] = {}
        for entry in (*self._tables[kind].values(), *new_entries.values()):
            for ext in entry.extensions:
                owner = claimed.get(ext)
                if owner is not None and owner.name != entry.name:
                    raise PluginError(
                        f"Dateiendung '.{ext}' wird von zwei {KIND_LABELS[kind]}en "
                        f"beansprucht: '{owner.name}' ({owner.origin}) und "
                        f"'{entry.name}' ({entry.origin})"
                    )
                claimed[ext] = entry

    def record_failure(self, origin: str, message: str) -> None:
        """Vermerkt ein übersprungenes Plugin (Herkunft -> Ursache)."""
        self._require_mutable()
        self._failed[origin] = message

    def remove_origin(self, origin: str) -> int:
        """Entfernt alle Einträge einer Herkunft; liefert deren Anzahl."""
        self._require_mutable()
        removed = 0
        for table in self._tables.values():
            for name in [n for n, e in table.items() if e.origin == origin]:
                del table[name]
                removed += 1
        return removed

    def check_references(self) -> list[tuple[str, str]]:
        """Prüft namentliche Verweise (Transformation einer Quellart oder eines
        Dateiformats). Liefert (Herkunft, Meldung) je Verstoß; die Erkennung
        entscheidet, ob das abbricht (Kernbaustein) oder das Plugin entfällt."""
        problems: list[tuple[str, str]] = []
        for kind in ("source", "file_format"):
            for entry in self.entries(kind):
                spec = entry.transform
                if isinstance(spec, str) and spec not in self._tables["transform"]:
                    problems.append(
                        (
                            entry.origin,
                            f"{KIND_LABELS[kind]} '{entry.name}': Transformation '{spec}' ist "
                            f"nicht registriert, registriert sind: {self.names('transform')}",
                        )
                    )
        return problems

    def freeze(self) -> "Registry":
        """Macht die Tabellen schreibgeschützt (ohne Sperre thread-sicher lesbar)."""
        self._tables = {
            kind: MappingProxyType(dict(table)) for kind, table in self._tables.items()
        }
        self._failed = MappingProxyType(dict(self._failed))
        self._frozen = True
        return self

    # -- Abfrage ---------------------------------------------------------

    @property
    def failed(self) -> Mapping[str, str]:
        """Übersprungene Plugins: Herkunft -> Fehlermeldung."""
        return self._failed

    def failed_hint(self) -> str:
        """Zusatz für "unbekannt"-Meldungen: der Name kann aus einem nicht geladenen Plugin stammen."""
        if not self._failed:
            return ""
        details = "; ".join(
            f"{origin}: {msg}" for origin, msg in sorted(self._failed.items())
        )
        return f" (nicht geladene Plugins: {details})"

    def names(self, kind: str) -> list[str]:
        return sorted(self._tables[kind])

    def entries(self, kind: str) -> list[Any]:
        """Alle Einträge einer Erweiterungsart, nach Namen sortiert."""
        table = self._tables[kind]
        return [table[name] for name in sorted(table)]

    def lookup(self, kind: str, name: str) -> Any:
        entry = self._tables[kind].get(name)
        if entry is None:
            raise ValueError(
                f"Unbekannte {KIND_LABELS[kind]} '{name}', registriert sind: "
                f"{self.names(kind)}{self.failed_hint()}"
            )
        return entry

    def source(self, name: str) -> SourcePlugin:
        return self.lookup("source", name)

    def file_format(self, name: str) -> FileFormatPlugin:
        return self.lookup("file_format", name)

    def table_format(self, name: str) -> TableFormatPlugin:
        return self.lookup("table_format", name)

    def transform(self, name: str) -> TransformPlugin:
        return self.lookup("transform", name)

    def operation(self, name: str) -> OperationPlugin:
        return self.lookup("operation", name)

    def output(self, name: str) -> OutputPlugin:
        return self.lookup("output", name)

    def file_format_for_extension(self, ext: str) -> FileFormatPlugin | None:
        ext = ext.lower()
        for plugin in self.entries("file_format"):
            if ext in plugin.extensions:
                return plugin
        return None

    def table_format_for_extension(self, ext: str) -> TableFormatPlugin | None:
        ext = ext.lower()
        for plugin in self.entries("table_format"):
            if ext in plugin.extensions:
                return plugin
        return None

    def zip_member_extensions(self) -> set[str]:
        return {e for p in self.entries("file_format") for e in p.zip_extensions}

    def resolve_transform(self, spec: str | Callable) -> Callable:
        """Transformationsangabe (Name oder Funktion) -> Funktion."""
        if callable(spec):
            return spec
        return self.transform(spec).apply


# --- Standard-Registry des Prozesses ---

_default: Registry | None = None
_default_lock = threading.Lock()


def default_registry() -> Registry:
    """Die installierte Registry; ohne ``geofact.api.load_extensions()`` ein
    expliziter Fehler, keine versteckte Erkennung beim ersten Zugriff."""
    registry = _default
    if registry is None:
        raise RuntimeError(NOT_LOADED_MESSAGE)
    return registry


def installed_registry() -> Registry | None:
    """Die installierte Registry oder None (kein Fehler)."""
    return _default


def set_default_registry(registry: Registry | None) -> Registry | None:
    """Installiert die Standard-Registry (None = entfernen), liefert die bisherige.
    Nur eingefrorene Registries, da sie ohne Sperre von Threads gelesen wird."""
    global _default
    if registry is not None and not registry.frozen:
        raise ValueError(
            "Nur eine eingefrorene Registry (freeze()) kann installiert werden"
        )
    with _default_lock:
        previous, _default = _default, registry
    return previous


def registry_from_context(info: ValidationInfo | None) -> Registry:
    """Registry einer Pydantic-Validierung: aus dem Kontext
    (``context={"registry": reg}``), sonst die Standard-Registry des Prozesses."""
    context = getattr(info, "context", None)
    if isinstance(context, dict) and context.get("registry") is not None:
        return context["registry"]
    return default_registry()


# --- Markierung (Dekoratoren) ---

# Parameternamen je Schnittstelle (Prüfung und Fehlermeldung).
_SIGNATURES: dict[type, tuple[str, ...]] = {
    SourceLoader: ("layer", "ctx"),
    FileReader: ("path", "layer", "ctx"),
    TableReader: ("layer", "path"),
    Transform: ("layer", "data", "ctx"),
    OutputWriter: ("data", "target", "spec"),
    OperationRunner: ("inputs", "params"),
}


def _check_signature(kind: str, name: str, fn: Any, interface: type) -> None:
    """Bricht ab, wenn fn nicht mit den Parametern der Schnittstelle aufrufbar ist
    (sonst fiele ein falsches Plugin erst im Lauf auf)."""
    params = _SIGNATURES[interface]
    if not callable(fn):
        raise PluginError(f"{KIND_LABELS[kind]} '{name}': {fn!r} ist nicht aufrufbar")
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return  # z. B. eingebaute Funktionen ohne lesbare Signatur
    try:
        signature.bind(*params)
    except TypeError as exc:
        raise PluginError(
            f"{KIND_LABELS[kind]} '{name}': {getattr(fn, '__name__', fn)}"
            f"{signature} passt nicht zur Schnittstelle {interface.__name__}"
            f"({', '.join(params)}) - {exc}"
        ) from None


def _check_transform_spec(kind: str, name: str, spec: str | Callable) -> None:
    """Eine Transformation als Funktion muss Transform erfüllen; ein Name wird erst
    nach der Erkennung geprüft (``Registry.check_references``)."""
    if not isinstance(spec, str):
        _check_signature(kind, name, spec, Transform)


def _check_options_model(
    kind: str, name: str, model: Optional[type[BaseModel]]
) -> None:
    """Das Optionsmodell eines Formats lehnt unbekannte Felder ab (``extra='forbid'``),
    damit ein Tippfehler in der flachen YAML nicht still ignoriert wird."""
    if model is None:
        return
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise PluginError(
            f"{KIND_LABELS[kind]} '{name}': options_model {model!r} ist kein Pydantic-Modell"
        )
    if model.model_config.get("extra") != "forbid":
        raise PluginError(
            f"{KIND_LABELS[kind]} '{name}': options_model {model.__name__} muss "
            "extra='forbid' setzen (model_config = ConfigDict(extra='forbid'))"
        )


LEGACY_PARAM_ATTRIBUTES = ("REQUIRED_PARAMS", "PARAMS")


def _check_params_contract(step_model: type[Step], name: str) -> None:
    """Parameter sind eine verschachtelte ``class Params(ParamsBase)``;
    REQUIRED_PARAMS und PARAMS werden nicht gelesen und deshalb abgelehnt, statt
    still ignoriert zu werden (Goldene Regel 7)."""
    legacy = [attr for attr in LEGACY_PARAM_ATTRIBUTES if hasattr(step_model, attr)]
    if legacy:
        raise PluginError(
            f"Operation '{name}': {step_model.__name__} deklariert {' und '.join(legacy)} - "
            "Parameter als class Params(ParamsBase) deklarieren (Typen, Grenzen und Defaults "
            "stehen dort; REQUIRED_PARAMS/PARAMS werden nicht mehr gelesen)"
        )
    params = step_model.Params
    if not (isinstance(params, type) and issubclass(params, ParamsBase)):
        raise PluginError(
            f"Operation '{name}': {step_model.__name__}.Params muss eine Unterklasse von "
            f"ParamsBase sein, ist aber {params!r}"
        )
    if params.model_config.get("extra") != "forbid":
        raise PluginError(
            f"Operation '{name}': {params.__name__} muss unbekannte Parameter ablehnen "
            "(model_config extra='forbid', von ParamsBase geerbt)"
        )


def _mark(kind: str, name: str, fn: Any, entry: Any) -> None:
    """Hängt den Eintrag an die Funktion (unter mehreren Namen möglich)."""
    try:
        marks = fn.__dict__.setdefault(MARK, [])
    except AttributeError:
        raise PluginError(
            f"{KIND_LABELS[kind]} '{name}': {fn!r} kann nicht markiert werden "
            "(erwartet wird eine Funktion auf Modulebene)"
        ) from None
    marks.append((kind, entry))


def register_source(
    name: str,
    layer_model: type[LayerBase],
    *,
    transform: str | Callable = DEFAULT_TRANSFORM,
    description: str = "",
) -> Callable:
    """Markiert die Ladefunktion (layer, ctx) -> LayerData einer Quellart.
    layer_model (Unterklasse von LayerBase) muss ``source=name`` als Default tragen."""
    if not (isinstance(layer_model, type) and issubclass(layer_model, LayerBase)):
        raise PluginError(
            f"Quellart '{name}': {layer_model!r} ist keine Unterklasse von LayerBase"
        )
    source_field = layer_model.model_fields.get("source")
    if source_field is None or source_field.default != name:
        raise PluginError(
            f"Quellart '{name}': Layer-Modell {layer_model.__name__} muss "
            f"source='{name}' als Default deklarieren"
        )
    _check_transform_spec("source", name, transform)

    def decorator(load_fn: Callable) -> Callable:
        _check_signature("source", name, load_fn, SourceLoader)
        _mark(
            "source",
            name,
            load_fn,
            SourcePlugin(
                name=name,
                layer_model=layer_model,
                load=load_fn,
                transform=transform,
                description=description,
            ),
        )
        return load_fn

    return decorator


def register_file_format(
    name: str,
    *,
    extensions: tuple[str, ...],
    data_type: DataType,
    transform: str | Callable = DEFAULT_TRANSFORM,
    zip_extensions: tuple[str, ...] = (),
    options_model: Optional[type[BaseModel]] = None,
    description: str = "",
) -> Callable:
    """Markiert die Lesefunktion (path, layer, ctx) -> LayerData eines Dateiformats
    (source: file). options_model beschreibt formatspezifische Layer-Felder
    (lesbar aus ``layer.options``)."""
    _check_transform_spec("file_format", name, transform)
    _check_options_model("file_format", name, options_model)

    def decorator(read_fn: Callable) -> Callable:
        _check_signature("file_format", name, read_fn, FileReader)
        _mark(
            "file_format",
            name,
            read_fn,
            FileFormatPlugin(
                name=name,
                extensions=tuple(e.lower() for e in extensions),
                data_type=data_type,
                read=read_fn,
                transform=transform,
                zip_extensions=tuple(e.lower() for e in zip_extensions),
                options_model=options_model,
                description=description,
            ),
        )
        return read_fn

    return decorator


def register_table_format(
    name: str,
    *,
    extensions: tuple[str, ...],
    options_model: Optional[type[BaseModel]] = None,
    description: str = "",
) -> Callable:
    """Markiert die Lesefunktion (layer, path) -> pandas.DataFrame eines Tabellenformats
    (source: table); das Spalten- und Geometrie-Mapping bleibt im Konnektor.
    options_model beschreibt formatspezifische Layer-Felder (``layer.options``)."""
    _check_options_model("table_format", name, options_model)

    def decorator(read_fn: Callable) -> Callable:
        _check_signature("table_format", name, read_fn, TableReader)
        _mark(
            "table_format",
            name,
            read_fn,
            TableFormatPlugin(
                name=name,
                extensions=tuple(e.lower() for e in extensions),
                read=read_fn,
                options_model=options_model,
                description=description,
            ),
        )
        return read_fn

    return decorator


def register_transform(name: str, *, description: str = "") -> Callable:
    """Markiert eine Harmonisierung (layer, data, ctx) -> data, auf die Quellen und
    Dateiformate per Namen verweisen."""

    def decorator(apply_fn: Callable) -> Callable:
        _check_signature("transform", name, apply_fn, Transform)
        _mark(
            "transform",
            name,
            apply_fn,
            TransformPlugin(
                name=name,
                apply=apply_fn,
                description=description,
            ),
        )
        return apply_fn

    return decorator


def register_operation(step_model: type[Step], *, description: str = "") -> Callable:
    """Markiert die Ausführungsfunktion (inputs, params) -> LayerData einer Operation.
    step_model (Unterklasse von Step) trägt den op-Namen als Literal-Default,
    INPUT_PORTS, OUTPUT_TYPE und Params. Vertrag und Ausführung sind ein Eintrag."""
    if not (isinstance(step_model, type) and issubclass(step_model, Step)):
        raise PluginError(f"Operation: {step_model!r} ist keine Unterklasse von Step")
    op_field = step_model.model_fields.get("op")
    name = op_field.default if op_field is not None else None
    if not isinstance(name, str) or not name:
        raise PluginError(
            f"Operation: Step-Modell {step_model.__name__} muss 'op' als Literal mit "
            "dem Operationsnamen als Default deklarieren"
        )
    _check_params_contract(step_model, name)

    def decorator(run_fn: Callable) -> Callable:
        _check_signature("operation", name, run_fn, OperationRunner)
        _mark(
            "operation",
            name,
            run_fn,
            OperationPlugin(
                name=name,
                step_model=step_model,
                run=run_fn,
                description=description or (step_model.__doc__ or "").strip(),
            ),
        )
        return run_fn

    return decorator


def register_output(
    name: str,
    *,
    extension: str,
    accepts: tuple[DataType, ...] = (DataType.VECTOR,),
    options_model: Optional[type[BaseModel]] = None,
    description: str = "",
) -> Callable:
    """Markiert die Schreibfunktion (data, target_path, spec) eines Ausgabeformats.
    accepts nennt die schreibbaren Datentypen (andere Quellen lehnt die
    Konfigurationsprüfung ab, FA2); options_model die Felder in ``spec.options``."""
    _check_options_model("output", name, options_model)

    def decorator(write_fn: Callable) -> Callable:
        _check_signature("output", name, write_fn, OutputWriter)
        _mark(
            "output",
            name,
            write_fn,
            OutputPlugin(
                name=name,
                extension=extension.lstrip("."),
                accepts=frozenset(accepts),
                write=write_fn,
                options_model=options_model,
                description=description,
            ),
        )
        return write_fn

    return decorator
