"""Implements: FA45 (Paketstruktur core / support / engine / builtin, Fassade plugin_api), FA40.

Contract-Tests für die Paketstruktur nach dem Umbau (Architektur B, Stufe 4):

- Die alten Pakete (config, integration, operations, output, plugins) und die
  Module extensions, progress, catalog gibt es nicht mehr.
- Alle mitgelieferten Bausteine liegen in geofact.builtin/{sources, formats,
  transforms, operations, outputs}; die Erkennung scannt nur dieses Paket und
  überspringt Hilfsmodule (führender Unterstrich).
- Jede Quellart hat genau einen Einstieg load(layer, ctx).
- geofact.plugin_api ist die Autorenfassade: Kernbausteine und externe Plugins
  importieren dieselben Namen von dort.
- Code, Beispiele, Skripte und Frontend-Kommentare nennen keinen alten Modul-
  pfad mehr (Härtung; Dokumentation, die Geschichte beschreibt, ist ausgenommen).

Die Importregeln selbst (wer wen importieren darf) prüft
tests/unit/test_architecture_layers.py.
"""

from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

import geofact
from geofact import plugin_api
from geofact.core import contracts, errors, registry as core_registry, types
from geofact.core.registry import KINDS, default_registry
from geofact.engine import discovery

GEOFACT_ROOT = Path(geofact.__file__).resolve().parent
BUILTIN_ROOT = GEOFACT_ROOT / "builtin"
REPO_ROOT = GEOFACT_ROOT.parents[1]

REMOVED_MODULES = [
    "geofact.config",
    "geofact.integration",
    "geofact.integration.osm",
    "geofact.integration.base",
    "geofact.operations",
    "geofact.operations.executor",
    "geofact.operations.registry",
    "geofact.operations.vector",
    "geofact.output",
    "geofact.output.run_outputs",
    "geofact.plugins",
    "geofact.plugins.power_grid",
    "geofact.extensions",
    "geofact.progress",
    "geofact.catalog",
]

KIND_PACKAGES = {
    "source": "geofact.builtin.sources.",
    "file_format": "geofact.builtin.formats.",
    "table_format": "geofact.builtin.formats.",
    "transform": "geofact.builtin.transforms.",
    "operation": "geofact.builtin.operations.",
    "output": "geofact.builtin.outputs.",
}


def _fresh_process(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
    )


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_fa45_old_module_is_gone(module):
    """Keine Kompatibilitäts-Shims: der alte Importpfad existiert nicht mehr."""
    try:
        spec = importlib.util.find_spec(module)
    except ModuleNotFoundError:  # das übergeordnete Paket fehlt bereits
        spec = None
    assert spec is None


def test_fa45_builtin_package_has_exactly_the_five_kind_packages():
    packages = {
        p.name for p in BUILTIN_ROOT.iterdir() if p.is_dir() and p.name != "__pycache__"
    }
    assert packages == {"sources", "formats", "transforms", "operations", "outputs"}
    top_level_modules = {p.name for p in BUILTIN_ROOT.glob("*.py")}
    # direkt in builtin/ liegen nur Hilfsmodule (führender Unterstrich) und __init__
    assert {n for n in top_level_modules if not n.startswith("_")} == set()


def test_fa45_block_origins_follow_the_package_layout():
    registry = default_registry()
    for kind in KINDS:
        entries = registry.entries(kind)
        assert entries, f"keine Einträge der Art {kind}"
        for entry in entries:
            assert entry.origin.startswith(KIND_PACKAGES[kind]), (
                f"{kind} '{entry.name}' stammt aus {entry.origin}, "
                f"erwartet unter {KIND_PACKAGES[kind]}"
            )


def test_fa45_discovery_scans_only_geofact_builtin():
    assert discovery.BUILTIN_PACKAGES == ("geofact.builtin",)


def test_fa45_discovery_skips_helper_modules():
    """Hilfsmodule (_tabular, _rest_client, _geometry, _hexgeom) sind nie Herkunft
    eines Eintrags - sie tragen keine Markierung und werden nicht gescannt."""
    registry = default_registry()
    scanned = {entry.origin for kind in KINDS for entry in registry.entries(kind)}
    assert scanned
    assert not any(
        part.startswith("_") for origin in scanned for part in origin.split(".")
    )
    found = discovery._module_names("geofact.builtin")
    assert "geofact.builtin.operations.buffer" in found
    assert not any(part.startswith("_") for name in found for part in name.split("."))


def test_fa45_vector_operations_have_one_module_each():
    expected = {
        "buffer",
        "nearest_distance",
        "spatial_join",
        "overlay",
        "clip",
        "dissolve",
        "zonal_stats",
        "classify",
        "filter",
        "ranking",
    }
    registry = default_registry()
    for op in expected:
        assert registry.operation(op).origin == f"geofact.builtin.operations.{op}"
    # vector.py ist aufgeteilt: das gemeinsame Hilfsmodul ist ein Unterstrich-Modul
    assert importlib.util.find_spec("geofact.builtin.operations._geometry") is not None


def test_fa40_power_grid_topology_is_a_builtin_block():
    """Das mitgelieferte Stromnetz-Beispiel bleibt Kernbaustein (Entscheidung zum
    Umbau): ein Baustein in geofact.builtin lädt wie ein Plugin, nur streng."""
    entry = default_registry().operation("power_grid_topology")
    assert entry.origin == "geofact.builtin.operations.power_grid_topology"


def test_fa45_every_source_has_the_single_entry_point_load_layer_ctx():
    """Pro Konnektor genau ein Einstieg load(layer, ctx); die alten Signaturen
    load(layer, region, refresh) und load(layer, window_bounds, base_dir) sind weg."""
    registry = default_registry()
    for source in registry.entries("source"):
        module = importlib.import_module(source.origin)
        assert source.load is module.load, (
            f"{source.name}: registrierte Funktion ist nicht load()"
        )
        assert list(inspect.signature(module.load).parameters) == ["layer", "ctx"], (
            source.name
        )


def test_fa45_formats_and_connectors_are_split():
    """Datei- und Tabellenformate stehen in builtin/formats, der Konnektor kennt
    nur die Registry."""
    registry = default_registry()
    for name in ("geojson", "shp", "gml", "gpkg"):
        assert registry.file_format(name).origin == "geofact.builtin.formats.vector"
    assert registry.file_format("tif").origin == "geofact.builtin.formats.raster"
    for name in ("csv", "fwf", "sql", "sqlite", "xlsx"):
        assert registry.table_format(name).origin == "geofact.builtin.formats.tables"
    assert registry.source("file").origin == "geofact.builtin.sources.file"
    assert registry.source("table").origin == "geofact.builtin.sources.table"


def test_fa45_osm_snapshot_wrapper_lives_with_the_osm_source():
    """support/snapshot.py ist Infrastruktur ohne OSM-Wissen; der OSM-Schlüssel
    (Payload-Form unverändert, siehe test_fa16_snapshot_key_matches_frozen_hash)
    und der dünne Wrapper gehören zum OSM-Konnektor."""
    from geofact.builtin.sources import osm
    from geofact.support import snapshot

    for name in ("snapshot_key", "fetch_with_snapshot", "OsmBackend"):
        assert hasattr(osm, name), name
    for name in ("snapshot_key", "fetch_with_snapshot", "_canonical_tags"):
        assert not hasattr(snapshot, name), name
    for name in ("payload_key", "fetch_with_snapshot_key", "binary_path"):
        assert hasattr(snapshot, name), name


def test_fa45_region_services_live_in_the_engine():
    """Auflösung der Region und UTM-Zone sind feste Kerndienste der Engine,
    keine Bausteine; die Quellart 'region' liefert die Region nur als Layer."""
    from geofact.engine import region

    assert callable(region.resolve_region)
    assert callable(region.utm_zone_epsg)
    registry = default_registry()
    assert registry.source("region").origin == "geofact.builtin.sources.region"
    transforms = registry.names("transform")
    assert transforms == ["none", "reproject"]


# =====================================================================
# Fassade plugin_api
# =====================================================================

AUTHOR_SURFACE = {
    "DataType",
    "LayerData",
    "RasterLayer",
    "Graph",
    "LayerBase",
    "LayerValidation",
    "Step",
    "ParamsBase",
    "LoadContext",
    "register_source",
    "register_file_format",
    "register_table_format",
    "register_transform",
    "register_operation",
    "register_output",
    "OutputSkipped",
    "PluginError",
    "http",
    "snapshot",
}


def test_fa45_plugin_api_exposes_the_block_author_surface():
    assert AUTHOR_SURFACE <= set(plugin_api.__all__)
    for name in AUTHOR_SURFACE:
        assert hasattr(plugin_api, name), name


def test_fa45_plugin_api_names_are_the_core_objects():
    """Kein zweiter Satz Klassen: die Fassade reicht die Objekte des Kerns durch,
    damit isinstance/issubclass für Kernbausteine und Plugins gleich gelten."""
    assert plugin_api.Step is contracts.Step
    assert plugin_api.LayerBase is contracts.LayerBase
    assert plugin_api.LayerValidation is contracts.LayerValidation
    assert plugin_api.ParamsBase is contracts.ParamsBase
    assert plugin_api.LoadContext is contracts.LoadContext
    assert plugin_api.DataType is types.DataType
    assert plugin_api.RasterLayer is types.RasterLayer
    assert plugin_api.Graph is types.Graph
    assert plugin_api.OutputSkipped is errors.OutputSkipped
    assert plugin_api.register_operation is core_registry.register_operation
    assert plugin_api.register_source is core_registry.register_source


def test_fa45_plugin_api_import_loads_neither_engine_nor_builtin():
    done = _fresh_process(
        "import sys, geofact.plugin_api\n"
        "bad = [m for m in sys.modules if m.startswith(('geofact.engine', 'geofact.builtin'))]\n"
        "assert not bad, bad\n"
    )
    assert done.returncode == 0, done.stderr


def test_fa45_engine_import_loads_no_builtin_block():
    """Die Engine kennt das Paket builtin nur dem NAMEN nach (Erkennung)."""
    done = _fresh_process(
        "import sys\n"
        "import geofact.engine.discovery, geofact.engine.executor, geofact.engine.outputs\n"
        "import geofact.engine.region, geofact.engine.validate, geofact.engine.catalog\n"
        "bad = [m for m in sys.modules if m.startswith('geofact.builtin')]\n"
        "assert not bad, bad\n"
    )
    assert done.returncode == 0, done.stderr


def _geofact_imports(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == "geofact":
                found.append((node.module, tuple(a.name for a in node.names)))
    return found


def test_fa45_builtin_blocks_take_the_author_surface_from_plugin_api():
    """Ein Kernbaustein importiert Vertragsklassen und Marker aus der Fassade,
    nicht aus core/contracts|types|errors - sonst wäre die Fassade nur für
    externe Plugins da und der Kern bekäme eine Sonderrolle."""
    offenders = []
    for path in sorted(BUILTIN_ROOT.rglob("*.py")):
        for module, names in _geofact_imports(path):
            if module in {
                "geofact.core.contracts",
                "geofact.core.types",
                "geofact.core.errors",
            }:
                direct = [n for n in names if n in plugin_api.__all__]
                if direct:
                    offenders.append(
                        f"{path.relative_to(GEOFACT_ROOT).as_posix()}: {module} {direct}"
                    )
    assert offenders == []


def test_fa45_example_plugins_use_plugin_api_only():
    plugin_files = sorted((REPO_ROOT / "examples").rglob("plugins/*.py"))
    assert plugin_files, "kein Beispiel-Plugin gefunden"
    for path in plugin_files:
        modules = {m for m, _ in _geofact_imports(path)}
        assert modules == {"geofact.plugin_api"}, f"{path.name}: {sorted(modules)}"


# =====================================================================
# Keine Spuren der alten Struktur im Code (Härtung)
# =====================================================================

# Modul- und Dateipfade der aufgelösten Struktur (geofact.config, geofact/operations/...).
# Dokumentation, die Geschichte beschreibt (docs/), ist ausgenommen; Code,
# Beispiele, Skripte, Frontend-Kommentare und Konfiguration dürfen sie nicht
# mehr nennen - weder als Import noch in Kommentaren und Docstrings.
_OLD_PATH = re.compile(
    r"geofact[./](?:config|integration|operations|output|extensions|plugins|progress|catalog"
    r"|serialize|view_columns|api_catalog)\b"
)
_CODE_SCOPES = (
    "src",
    "tests",
    "backend",
    "scripts",
    "examples",
    "frontend/lib",
    "frontend/components",
    "frontend/app",
    "Dockerfile.backend",
    "Dockerfile.frontend",
    "docker-compose.yml",
    "pyproject.toml",
    "README.md",
)
_CODE_SUFFIXES = {".py", ".yaml", ".yml", ".ts", ".tsx", ".toml", ".md", ".txt", ""}
# Tests, die ausdrücklich prüfen, dass die alten Module NICHT mehr existieren (oder
# das Muster selbst), nennen sie absichtlich.
_NAMES_OLD_PATHS_ON_PURPOSE = {
    "tests/unit/test_fa45_package_layout.py",
    "tests/unit/test_fa40_contract_colocation.py",
    "tests/unit/test_architecture_layers.py",
}
_SKIPPED_PARTS = {"__pycache__", "node_modules", ".next", ".venv"}


def _code_files() -> list[Path]:
    files: list[Path] = []
    for scope in _CODE_SCOPES:
        path = REPO_ROOT / scope
        candidates = (
            [path]
            if path.is_file()
            else sorted(path.rglob("*"))
            if path.is_dir()
            else []
        )
        files.extend(
            f
            for f in candidates
            if f.is_file()
            and f.suffix in _CODE_SUFFIXES
            and not _SKIPPED_PARTS & set(f.parts)
        )
    return files


def test_fa45_no_old_module_path_is_left_in_code():
    files = _code_files()
    assert len(files) > 100, "Dateiliste verdächtig klein - Suchpfade stimmen nicht"
    offenders = []
    for path in files:
        if path.relative_to(REPO_ROOT).as_posix() in _NAMES_OLD_PATHS_ON_PURPOSE:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, start=1):
            if _OLD_PATH.search(line):
                offenders.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{number}: {line.strip()[:100]}"
                )
    assert offenders == []


def test_fa45_old_path_pattern_matches_what_it_should():
    """Das Muster selbst: alte Pfade treffen, die heutigen nicht."""
    assert _OLD_PATH.search("from geofact.config.schema import Scenario")
    assert _OLD_PATH.search("src/geofact/operations/executor.py")
    assert _OLD_PATH.search("import geofact.extensions")
    assert not _OLD_PATH.search("from geofact.engine.catalog import operation_catalog")
    assert not _OLD_PATH.search("src/geofact/builtin/operations/buffer.py")
    assert not _OLD_PATH.search("examples/plugin_demo/plugins/flatgeobuf.py")
