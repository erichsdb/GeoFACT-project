"""Implements: FA40 (Einheitliche Erweiterungspunkte mit Plugin-Erkennung).

Ein Lader für alle Bausteine. ``discover(plugin_paths)`` baut eine frische
Registry (core/registry.py) und friert sie ein:

1. Kernbausteine: alle Module des Pakets geofact.builtin (BUILTIN_PACKAGES), streng:
   ein Fehler in einem Kernmodul bricht die Erkennung ab.
2. Plugins: in jedem Ordner aus ``plugin_paths`` (Standard: GEOFACT_PLUGIN_PATH)
   alle ``*.py``-Dateien und Pakete, tolerant: ein defektes Plugin wird mit einer
   PluginLoadWarning übersprungen, in ``Registry.failed`` vermerkt und in jeder
   "Unbekannte ..."-Meldung genannt, verschwindet also nie still.

Gemeinsame Regeln:

- Namen mit führendem Unterstrich sind Hilfsmodule und werden nie gescannt.
- Gesammelt werden nur Markierungen von Objekten, die im gescannten Modul selbst
  definiert sind (``obj.__module__ == module.__name__``), so registriert sich ein
  importiertes Objekt nicht zweimal.
- Die Reihenfolge ist deterministisch (Kernmodule nach Namen, Plugin-Ordner in
  Angabereihenfolge, darin nach Namen): die Registry ist eine reine Funktion von
  (Code, Pfaden).
- Transaktional je Modul: Markierungen werden erst nach sauberem Import gesammelt
  und atomar zusammengeführt. Ein Plugin kann keinen Kernbaustein überschreiben,
  weil Kernmodule zuerst laden und ein doppelter Name abgelehnt wird.

Eine neue Erweiterung ist genau eine neue Datei (NFA4). ``discover`` verändert
keinen globalen Zustand; installiert wird erst durch ``geofact.api.load_extensions()``.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil
import sys
import threading
import warnings
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, Sequence

from geofact.core.contracts import Step
from geofact.core.errors import PluginError, PluginLoadWarning
from geofact.core.registry import MARK, PLUGIN_PATH_ENV, Registry

# Das Kernpaket, dessen Module Bausteine tragen. Nur sein Name steht hier; die
# Engine importiert geofact.builtin nie selbst (FA45).
BUILTIN_PACKAGES: tuple[str, ...] = ("geofact.builtin",)

# Ergebnis eines Imports: die gescannten Module samt Herkunft je Modul.
_Imported = list[tuple[ModuleType, str]]

# Plugin-Module liegen während der Erkennung in sys.modules (Pydantic und relative
# Importe brauchen das); die Sperre verhindert, dass zwei discover()-Aufrufe sich
# gegenseitig Module austauschen.
_discover_lock = threading.RLock()


def plugin_paths_from_env() -> list[Path]:
    """Ordner aus GEOFACT_PLUGIN_PATH (os.pathsep-getrennt)."""
    raw = os.environ.get(PLUGIN_PATH_ENV, "")
    return [Path(p) for p in raw.split(os.pathsep) if p.strip()]


# --- Sammeln ---


def _marks_of(obj: Any) -> list[tuple[str, Any]]:
    try:
        return list(vars(obj).get(MARK, ()))
    except Exception:  # noqa: BLE001 - Objekt ohne __dict__ oder mit exotischem Zugriff
        return []


def collect(module: ModuleType, origin: str) -> list[tuple[str, Any]]:
    """Markierungen aller im Modul selbst definierten Objekte, mit gestempelter
    Herkunft; auch ein Name mit führendem Unterstrich darf markiert sein."""
    staged: list[tuple[str, Any]] = []
    seen: set[int] = set()
    for _name, obj in sorted(vars(module).items()):
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        try:
            defined_in = getattr(obj, "__module__", None)
        except Exception:  # noqa: BLE001 - exotische Objekte ohne lesbares __module__
            continue
        if defined_in != module.__name__:
            continue
        for kind, entry in _marks_of(obj):
            staged.append((kind, replace(entry, origin=origin)))
    return staged


def _check_contracts_registered(
    modules: Sequence[ModuleType], staged: Sequence[tuple[str, Any]]
) -> None:
    """Eine Step-Klasse mit Operationsnamen ohne @register_operation-Funktion ist ein
    unvollständiges Plugin; ohne diese Prüfung bliebe der Fehler stumm."""
    registered = {entry.step_model for kind, entry in staged if kind == "operation"}
    for module in modules:
        for _name, obj in sorted(vars(module).items()):
            if not (
                isinstance(obj, type) and issubclass(obj, Step) and obj is not Step
            ):
                continue
            if obj.__module__ != module.__name__ or obj in registered:
                continue
            op_field = obj.model_fields.get("op")
            op_name = op_field.default if op_field is not None else None
            if isinstance(op_name, str) and op_name:
                raise PluginError(
                    f"Operation '{op_name}' hat einen Vertrag ({obj.__name__}), aber keine "
                    "Ausführungsfunktion (@register_operation fehlt)"
                )


def _module_names(package_name: str) -> list[str]:
    """Alle Nicht-Hilfsmodule eines Pakets (rekursiv), nach Namen sortiert."""
    package = importlib.import_module(package_name)
    names: list[str] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        full = f"{package_name}.{info.name}"
        names.append(full)
        if info.ispkg:
            names.extend(_module_names(full))
    return names


# --- Import ---


def _modules_of(prefix: str) -> dict[str, ModuleType]:
    return {
        n: m
        for n, m in sys.modules.items()
        if n == prefix or n.startswith(prefix + ".")
    }


def _replace_modules(prefix: str, saved: dict[str, ModuleType]) -> None:
    """Entfernt alle Module unter prefix aus sys.modules und setzt ``saved``
    wieder ein (leer = nur entfernen)."""
    for name in _modules_of(prefix):
        del sys.modules[name]
    sys.modules.update(saved)


def _exec_plugin_module(module_name: str, path: Path, *, package: bool) -> ModuleType:
    """Importiert eine Plugin-Datei bzw. ein Plugin-Paket frisch unter
    ``geofact_plugin_<name>``. Module gleichen Namens aus einer früheren Erkennung
    werden vorher entfernt, damit ein Paket keine veralteten Untermodule erbt;
    scheitert der Import, kehrt sys.modules in den Stand davor zurück."""
    target = path / "__init__.py" if package else path
    kwargs = {"submodule_search_locations": [str(path)]} if package else {}
    spec = importlib.util.spec_from_file_location(module_name, target, **kwargs)
    if spec is None or spec.loader is None:
        raise PluginError(f"Plugin '{path}' ist kein ladbares Python-Modul")
    saved = _modules_of(module_name)
    _replace_modules(module_name, {})
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except PluginError:
        _replace_modules(module_name, saved)
        raise
    except Exception as exc:
        _replace_modules(module_name, saved)
        kind = "Plugin-Paket" if package else "Plugin-Datei"
        raise PluginError(
            f"{kind} '{path}' konnte nicht geladen werden: {type(exc).__name__}: {exc}"
        ) from exc
    return module


def _import_plugin_file(path: Path) -> _Imported:
    module = _exec_plugin_module(f"geofact_plugin_{path.stem}", path, package=False)
    return [(module, str(path))]


def _import_plugin_package(directory: Path) -> _Imported:
    """Ein Plugin-Paket: das Paket und alle seine Nicht-Hilfs-Untermodule tragen Bausteine bei."""
    package_name = f"geofact_plugin_{directory.name}"
    saved = _modules_of(package_name)
    package = _exec_plugin_module(package_name, directory, package=True)
    imported: _Imported = [(package, str(directory / "__init__.py"))]
    try:
        for name in _module_names(package_name):
            module = importlib.import_module(name)
            imported.append((module, str(getattr(module, "__file__", None) or name)))
    except PluginError:
        _replace_modules(package_name, saved)
        raise
    except Exception as exc:
        _replace_modules(package_name, saved)
        raise PluginError(
            f"Plugin-Paket '{directory}' konnte nicht geladen werden: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return imported


def _import_builtin(module_name: str) -> _Imported:
    module = importlib.import_module(module_name)
    return [(module, module_name)]


# --- Laden ---


def _skip_plugin(registry: Registry, origin: str, exc: BaseException) -> None:
    message = str(exc)
    registry.record_failure(origin, message)
    warnings.warn(
        f"Plugin '{origin}' wurde übersprungen: {message}",
        PluginLoadWarning,
        stacklevel=4,
    )


def _load(
    registry: Registry,
    origin: str,
    importer: Callable[[], _Imported],
    *,
    strict: bool,
) -> list[str]:
    """Importiert eine Einheit (Modul, Datei oder Paket), sammelt ihre Markierungen
    und führt sie atomar zusammen. strict=True (Kernbaustein): jeder Fehler bricht
    ab; strict=False (Plugin): vermerkt und übersprungen. Liefert die Herkünfte
    der übernommenen Module (leer bei Übersprungen)."""
    try:
        staged: list[tuple[str, Any]] = []
        origins: list[str] = []
        imported = importer()
        for module, module_origin in imported:
            staged.extend(collect(module, module_origin))
            origins.append(module_origin)
        _check_contracts_registered([module for module, _ in imported], staged)
        registry.merge(staged)
    except Exception as exc:  # noqa: BLE001 - je nach Herkunft abbrechen oder melden
        if strict:
            raise PluginError(
                f"Kernbaustein '{origin}': {type(exc).__name__}: {exc}"
            ) from exc
        _skip_plugin(registry, origin, exc)
        return []
    return origins


def _plugin_units(directory: Path) -> list[tuple[str, Path, bool]]:
    """(Modulname, Pfad, ist_Paket) je Plugin-Datei und -Paket eines Ordners, nach
    Namen sortiert, ohne Hilfsnamen (``_x``) und ``__pycache__``."""
    units: list[tuple[str, Path, bool]] = []
    for path in sorted(directory.iterdir(), key=lambda p: p.name):
        if path.name.startswith("_"):
            continue
        if path.is_file() and path.suffix == ".py":
            units.append((f"geofact_plugin_{path.stem}", path, False))
        elif path.is_dir() and (path / "__init__.py").is_file():
            units.append((f"geofact_plugin_{path.name}", path, True))
    return units


def _load_plugin_dirs(
    registry: Registry, plugin_paths: Iterable[str | Path]
) -> dict[str, list[str]]:
    """Lädt alle Plugin-Dateien und -Pakete; liefert je übernommener Einheit
    ihre Modul-Herkünfte (für die Verweisprüfung)."""
    members: dict[str, list[str]] = {}
    claimed: dict[str, Path] = {}  # Modulname -> Quelle, nur innerhalb dieses Aufrufs
    seen_dirs: set[Path] = set()
    for raw in plugin_paths:
        directory = Path(raw)
        try:
            resolved = directory.resolve()
        except OSError:
            resolved = directory
        if resolved in seen_dirs:
            continue  # derselbe Ordner zweimal angegeben
        seen_dirs.add(resolved)
        if not directory.is_dir():
            _skip_plugin(
                registry,
                str(directory),
                PluginError(f"Plugin-Ordner '{directory}' existiert nicht"),
            )
            continue
        for module_name, path, is_package in _plugin_units(directory):
            other = claimed.get(module_name)
            if other is not None:
                _skip_plugin(
                    registry,
                    str(path),
                    PluginError(
                        f"Plugin '{path}' hat denselben Namen wie '{other}' - Plugins müssen "
                        "über alle Plugin-Ordner eindeutig benannt sein"
                    ),
                )
                continue
            claimed[module_name] = path
            importer = (
                (lambda p=path: _import_plugin_package(p))
                if is_package
                else (lambda p=path: _import_plugin_file(p))
            )
            origins = _load(registry, str(path), importer, strict=False)
            if origins:
                members[str(path)] = origins
            else:
                _replace_modules(
                    module_name, {}
                )  # ein übersprungenes Plugin bleibt nicht importiert
    return members


def _resolve_references(
    registry: Registry, core_origins: set[str], members: dict[str, list[str]]
) -> None:
    """Namentliche Verweise (Transformationen) prüfen. Ein Verstoß in einem
    Kernbaustein bricht ab, bei einem Plugin entfällt es ganz. Wiederholt, weil
    der Wegfall eines Plugins die Verweise eines anderen brechen kann."""
    unit_of = {origin: unit for unit, origins in members.items() for origin in origins}
    while True:
        problems = registry.check_references()
        if not problems:
            return
        for origin, message in problems:
            if origin in core_origins:
                raise PluginError(f"Kernbaustein '{origin}': {message}")
        done: set[str] = set()
        for origin, message in problems:
            unit = unit_of.get(origin, origin)
            if unit in done:
                continue
            done.add(unit)
            for member in members.get(unit, [origin]):
                registry.remove_origin(member)
            _skip_plugin(registry, unit, PluginError(message))


def discover(
    plugin_paths: Sequence[str | Path] = (),
    *,
    builtin_packages: Sequence[str] | None = None,
) -> Registry:
    """Baut eine frische, eingefrorene Registry aus Kernpaketen und Plugin-Ordnern.
    Verändert keinen globalen Zustand außer sys.modules der Plugin-Module."""
    with _discover_lock:
        registry = Registry()
        packages = (
            BUILTIN_PACKAGES if builtin_packages is None else tuple(builtin_packages)
        )

        core_origins: set[str] = set()
        for package_name in packages:
            for module_name in _module_names(package_name):
                core_origins.add(module_name)
                _load(
                    registry,
                    module_name,
                    lambda n=module_name: _import_builtin(n),
                    strict=True,
                )
        members = _load_plugin_dirs(registry, plugin_paths)
        _resolve_references(registry, core_origins, members)
        return registry.freeze()
