"""Implements: FA45 (checked layering rule: rings core / support / engine / builtin + facades).

Target picture of architecture B (docs/architektur.md):

    core     ring 0, pure: contracts, types, registry. Imports from geofact only
             geofact.core and no infrastructure library (rasterio, networkx,
             folium, rdflib, requests, sqlalchemy, openpyxl).
    support  infrastructure (HTTP, snapshots), registers nothing.
    engine   application layer; knows core + support, NEVER geofact.builtin.
    builtin  shipped blocks; knows core + support, NEVER geofact.engine.
    plugin_api / api   facades for block authors / for delivery code.
    cli, scripts, backend/geofact_web   delivery; import only geofact.api (the
             backend additionally geofact.plugin_api and geofact.support).

The rules are checked with `ast` over the source files (no extra dependency
such as import-linter). Imports under `if TYPE_CHECKING:` do not count (they
do not exist at runtime).

State: every rule is a real assert, no xfail mark is left. During the
restructuring a rule whose package was not finished yet was `xfail(strict=True)`;
the stage that finished a package removed the mark in the same commit. The core
and engine rules came with B step 1/2, support, builtin and plugin_api with the
package layout (B step 4), the cli rule and the backend rule with the application
use case (B step 6/8). Stage 7 added the third-party whitelist of core (a new
library in ring 0 must be a conscious decision, not an oversight of the blacklist)
and the completeness check of the registry: every scanned builtin module
contributes at least one entry, so a block whose registration got lost cannot
disappear silently.

The self-tests at the end check the scanner itself on synthetic directory
trees and always run - otherwise a broken scanner could let every rule pass.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from geofact.core.registry import KINDS, default_registry
from geofact.engine import discovery

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
GEOFACT_ROOT = SRC_ROOT / "geofact"
BACKEND_ROOT = REPO_ROOT / "backend" / "geofact_web"

INFRASTRUCTURE_LIBS = frozenset(
    {"rasterio", "networkx", "folium", "rdflib", "requests", "sqlalchemy", "openpyxl"}
)

# The only third-party libraries ring 0 (core) may import: the validation
# framework and the data types of the contracts (frames, CRS, affine, arrays).
# Everything else is infrastructure and belongs to support/engine/builtin.
CORE_THIRD_PARTY = frozenset(
    {"pydantic", "pydantic_core", "geopandas", "pyproj", "numpy", "affine"}
)


# =====================================================================
# Scanner
# =====================================================================


@dataclass(frozen=True)
class Import:
    """One import statement: file (relative), line and the absolute module
    names it addresses. `import a.b` -> (a.b,); `from X import a, b` ->
    (X, X.a, X.b), because `a` may be a submodule (`from geofact import engine`)."""

    file: str
    line: int
    candidates: tuple[str, ...]


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _package_of(path: Path, source_root: Path) -> list[str]:
    """Package path of a file (for relative imports): an __init__.py belongs to
    its own folder, every other module to the folder above."""
    parts = list(path.relative_to(source_root).with_suffix("").parts)
    return parts[:-1]


class _ImportCollector(ast.NodeVisitor):
    """Collects all imports except those under `if TYPE_CHECKING:` (the else
    branch exists at runtime and counts)."""

    def __init__(self, package: list[str], file: str) -> None:
        self.package = package
        self.file = file
        self.found: list[Import] = []

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_guard(node):
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.found.append(Import(self.file, node.lineno, (alias.name,)))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            base = self.package[: len(self.package) - (node.level - 1)]
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
        else:
            module = node.module or ""
        aliases = tuple(f"{module}.{a.name}" for a in node.names if a.name != "*")
        self.found.append(Import(self.file, node.lineno, (module, *aliases)))


def _imports_in_source(source: str, package: list[str], file: str) -> list[Import]:
    collector = _ImportCollector(package, file)
    collector.visit(ast.parse(source, filename=file))
    return collector.found


def _python_files(scope: Path) -> list[Path]:
    if scope.is_file():
        return [scope]
    return sorted(p for p in scope.rglob("*.py") if "__pycache__" not in p.parts)


def collect_imports(scope: Path, source_root: Path) -> list[Import]:
    imports: list[Import] = []
    for path in _python_files(scope):
        package = _package_of(path, source_root)
        imports.extend(
            _imports_in_source(
                path.read_text(encoding="utf-8"),
                package,
                path.relative_to(source_root).as_posix(),
            )
        )
    return imports


def _geofact_subpackage(module: str) -> str | None:
    """'geofact.engine.run' -> 'engine'; bare 'geofact' and foreign modules -> None."""
    parts = module.split(".")
    if parts[0] != "geofact" or len(parts) < 2:
        return None
    return parts[1]


@dataclass(frozen=True)
class Rule:
    """One layering rule. `allowed_internal` is a whitelist (only these geofact
    submodules may be imported), `forbidden_internal` a blacklist;
    `forbidden_external` lists forbidden top-level third-party modules."""

    scope: str  # path relative to src/geofact, or "backend" / "scripts" (repo level)
    allowed_internal: frozenset[str] | None = None
    forbidden_internal: frozenset[str] = frozenset()
    forbidden_external: frozenset[str] = frozenset()
    # whitelist of third-party top-level modules (stdlib and geofact itself are
    # always fine); None = no whitelist
    allowed_external: frozenset[str] | None = None


def _is_third_party(top_level: str) -> bool:
    return top_level != "geofact" and top_level not in sys.stdlib_module_names


def find_violations(rule: Rule, *, geofact_root: Path, backend_root: Path) -> list[str]:
    if rule.scope == "backend":
        scope, source_root = backend_root, backend_root.parent
    elif rule.scope == "scripts":
        scope = geofact_root.parents[1] / "scripts"
        source_root = scope.parent
    else:
        scope, source_root = geofact_root / rule.scope, geofact_root.parent
    assert scope.exists(), f"{scope.parent.name}/{scope.name} does not exist yet"
    violations: list[str] = []
    for imp in collect_imports(scope, source_root):
        for candidate in imp.candidates:
            sub = _geofact_subpackage(candidate)
            if sub is not None and (
                (rule.allowed_internal is not None and sub not in rule.allowed_internal)
                or sub in rule.forbidden_internal
            ):
                violations.append(f"{imp.file}:{imp.line} imports {candidate}")
                if candidate == imp.candidates[0]:
                    break  # the module itself violates - its alias candidates share the violation
        top_level = imp.candidates[0].split(".")[0]
        if top_level in rule.forbidden_external:
            violations.append(f"{imp.file}:{imp.line} imports {imp.candidates[0]}")
        elif (
            rule.allowed_external is not None
            and _is_third_party(top_level)
            and top_level not in rule.allowed_external
        ):
            violations.append(
                f"{imp.file}:{imp.line} imports {imp.candidates[0]} (not whitelisted)"
            )
    return sorted(set(violations))


# =====================================================================
# The rules of the target picture
# =====================================================================

RULES: dict[str, tuple[Rule, pytest.MarkDecorator | None]] = {
    # core imports from geofact only geofact.core ... (enforced since B step 1/2)
    "core-imports-only-core": (
        Rule("core", allowed_internal=frozenset({"core"})),
        None,
    ),
    # ... and no infrastructure library (networkx only under TYPE_CHECKING)
    "core-imports-no-infrastructure-library": (
        Rule("core", forbidden_external=INFRASTRUCTURE_LIBS),
        None,
    ),
    # ... and from the third-party world only the whitelisted libraries
    # (stage 7: a blacklist forgets the library nobody thought of)
    "core-third-party-whitelist": (
        Rule("core", allowed_external=CORE_THIRD_PARTY),
        None,
    ),
    # enforced since B step 4 (geofact/support: http, snapshot)
    "support-imports-no-engine-builtin-or-facades": (
        Rule(
            "support",
            forbidden_internal=frozenset(
                {"engine", "builtin", "api", "plugin_api", "cli"}
            ),
        ),
        None,
    ),
    # enforced since B step 2 (geofact/engine/discovery.py reaches the blocks
    # only through package NAMES, never through an import)
    "engine-does-not-import-builtin": (
        Rule("engine", forbidden_internal=frozenset({"builtin", "api", "cli"})),
        None,
    ),
    # enforced since B step 4 (geofact/builtin: every shipped block imports
    # geofact.plugin_api / core / support, never the engine)
    "builtin-does-not-import-engine": (
        Rule("builtin", forbidden_internal=frozenset({"engine", "api", "cli"})),
        None,
    ),
    # enforced since B step 4 (geofact/plugin_api.py is the block-author facade)
    "plugin-api-imports-only-core-and-support": (
        Rule("plugin_api.py", allowed_internal=frozenset({"core", "support"})),
        None,
    ),
    # enforced since B step 6 (geofact/cli.py is delivery code on the application use case)
    "cli-imports-only-api": (
        Rule("cli.py", allowed_internal=frozenset({"api"})),
        None,
    ),
    # delivery scripts (scripts/run_demo.py, ...) are delivery code like the CLI: only geofact.api
    "scripts-import-only-api": (
        Rule("scripts", allowed_internal=frozenset({"api"})),
        None,
    ),
    # enforced since B step 8 (backend/geofact_web reads the core only through geofact.api;
    # support.http for the endpoint probes of the API catalog)
    "backend-imports-only-api-plugin-api-support": (
        Rule("backend", allowed_internal=frozenset({"api", "plugin_api", "support"})),
        None,
    ),
}


@pytest.mark.parametrize(
    "rule_id",
    [
        pytest.param(rule_id, marks=marker if marker is not None else (), id=rule_id)
        for rule_id, (_, marker) in RULES.items()
    ],
)
def test_fa45_layering_rule(rule_id):
    rule, _ = RULES[rule_id]
    violations = find_violations(
        rule, geofact_root=GEOFACT_ROOT, backend_root=BACKEND_ROOT
    )
    assert not violations, "layering rule violated:\n  " + "\n  ".join(violations)


def test_fa45_package_init_has_no_side_effects():
    """`geofact/__init__.py` imports nothing from geofact (docstring/version
    only): `import geofact.core` must not pull in the engine or any blocks.
    True today and a precondition of the target picture."""
    init = GEOFACT_ROOT / "__init__.py"
    internal = [
        imp
        for imp in collect_imports(init, GEOFACT_ROOT.parent)
        if imp.candidates[0].split(".")[0] == "geofact"
    ]
    assert not internal, f"geofact/__init__.py has side-effect imports: {internal}"


# =====================================================================
# Completeness: every scanned builtin module contributes a registry entry
# =====================================================================


def scanned_builtin_modules(
    builtin_root: Path, prefix: str = "geofact.builtin"
) -> set[str]:
    """Module names of all block modules below `builtin_root`, derived from the
    file system with the same rule discovery uses: a file or folder whose name
    starts with an underscore is a helper (`_tabular.py`, `_hexgeom.py`,
    `__init__.py`) and is never scanned."""
    modules: set[str] = set()
    for path in sorted(builtin_root.rglob("*.py")):
        relative = path.relative_to(builtin_root).with_suffix("")
        if (
            any(part.startswith("_") for part in relative.parts)
            or "__pycache__" in path.parts
        ):
            continue
        modules.add(".".join([prefix, *relative.parts]))
    return modules


def modules_without_entries(scanned: set[str], origins: set[str]) -> list[str]:
    return sorted(scanned - origins)


def _registry_origins() -> set[str]:
    registry = default_registry()
    return {entry.origin for kind in KINDS for entry in registry.entries(kind)}


def test_fa45_every_scanned_builtin_module_contributes_a_registry_entry():
    """A module under geofact/builtin that registers nothing is a block that
    silently does not exist (forgotten or broken decorator, misspelled marker).
    The module list comes from the file system, the origins from the registry
    that discovery built: two independent views that must agree."""
    scanned = scanned_builtin_modules(GEOFACT_ROOT / "builtin")
    assert len(scanned) >= 30, (
        f"suspiciously few builtin modules found: {sorted(scanned)}"
    )
    missing = modules_without_entries(scanned, _registry_origins())
    assert not missing, "builtin modules without any registry entry:\n  " + "\n  ".join(
        missing
    )


def test_fa45_no_registry_entry_comes_from_an_unscanned_module():
    """The reverse direction: every origin of the default registry (builtin
    only, see tests/conftest.py) is a scanned block module - no entry from a
    helper module or from a module outside geofact.builtin."""
    scanned = scanned_builtin_modules(GEOFACT_ROOT / "builtin")
    assert sorted(_registry_origins() - scanned) == []


def test_fa45_file_system_scan_matches_what_discovery_imports():
    """The scan of this test and `discovery._module_names` name the same block
    modules (discovery additionally lists the packages themselves)."""
    scanned = scanned_builtin_modules(GEOFACT_ROOT / "builtin")
    discovered = set(discovery._module_names("geofact.builtin"))
    packages = {name for name in discovered if name not in scanned}
    assert scanned <= discovered
    assert packages and all(
        (GEOFACT_ROOT.parent / Path(*name.split("."))).is_dir() for name in packages
    )


# =====================================================================
# Self-tests of the scanner (synthetic trees, always run)
# =====================================================================


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    geofact_root = tmp_path / "src" / "geofact"
    backend_root = tmp_path / "backend" / "geofact_web"
    backend_root.mkdir(parents=True)
    return geofact_root, backend_root


def _violations(rule: Rule, tree) -> list[str]:
    geofact_root, backend_root = tree
    return find_violations(rule, geofact_root=geofact_root, backend_root=backend_root)


def test_fa45_scanner_flags_absolute_import_of_forbidden_package(tree):
    _write(tree[0], "core/types.py", "from geofact.engine.run import run\n")
    found = _violations(Rule("core", allowed_internal=frozenset({"core"})), tree)
    assert found == ["geofact/core/types.py:1 imports geofact.engine.run"]


def test_fa45_scanner_flags_relative_import_of_forbidden_package(tree):
    _write(tree[0], "core/deep/inner.py", "from ...engine import run\n")
    found = _violations(Rule("core", forbidden_internal=frozenset({"engine"})), tree)
    assert found == ["geofact/core/deep/inner.py:1 imports geofact.engine"]


def test_fa45_scanner_allows_relative_import_inside_own_package(tree):
    _write(
        tree[0],
        "core/types.py",
        "from .store import LayerStore\nfrom . import registry\n",
    )
    assert _violations(Rule("core", allowed_internal=frozenset({"core"})), tree) == []


def test_fa45_scanner_flags_from_package_import_submodule(tree):
    """`from geofact import engine` is an import of geofact.engine."""
    _write(
        tree[0],
        "builtin/x.py",
        "from geofact import engine\nfrom geofact import core\n",
    )
    found = _violations(Rule("builtin", forbidden_internal=frozenset({"engine"})), tree)
    assert found == ["geofact/builtin/x.py:1 imports geofact.engine"]


def test_fa45_scanner_flags_infrastructure_library_but_not_under_type_checking(tree):
    _write(
        tree[0],
        "core/types.py",
        "from typing import TYPE_CHECKING\n"
        "import numpy\n"
        "if TYPE_CHECKING:\n"
        "    import networkx\n"
        "def f():\n"
        "    import rasterio\n",
    )
    found = _violations(Rule("core", forbidden_external=INFRASTRUCTURE_LIBS), tree)
    assert found == ["geofact/core/types.py:6 imports rasterio"]


def test_fa45_scanner_counts_else_branch_of_type_checking_guard(tree):
    _write(
        tree[0],
        "core/types.py",
        "import typing\n"
        "if typing.TYPE_CHECKING:\n"
        "    import networkx\n"
        "else:\n"
        "    import requests\n",
    )
    found = _violations(Rule("core", forbidden_external=INFRASTRUCTURE_LIBS), tree)
    assert found == ["geofact/core/types.py:5 imports requests"]


def test_fa45_scanner_checks_single_file_scope_and_backend(tree):
    _write(
        tree[0],
        "cli.py",
        "from geofact import api\nfrom geofact.core.scenario import Scenario\n",
    )
    _write(
        tree[1],
        "run_manager.py",
        "from geofact.api import run\n"
        "from geofact.support.http import get\n"
        "from geofact.operations import registry\n"
        "from .models import X\n",
    )
    assert _violations(Rule("cli.py", allowed_internal=frozenset({"api"})), tree) == [
        "geofact/cli.py:2 imports geofact.core.scenario",
    ]
    backend_rule = Rule(
        "backend", allowed_internal=frozenset({"api", "plugin_api", "support"})
    )
    assert _violations(backend_rule, tree) == [
        "geofact_web/run_manager.py:3 imports geofact.operations",
    ]


def test_fa45_scanner_reports_missing_package_instead_of_passing(tree):
    """A package that does not exist must not count as 'rule satisfied' -
    otherwise the xfail would say nothing."""
    with pytest.raises(AssertionError, match="does not exist yet"):
        _violations(Rule("core", allowed_internal=frozenset({"core"})), tree)


def test_fa45_scanner_flags_unlisted_third_party_library_but_not_stdlib_or_geofact(
    tree,
):
    _write(
        tree[0],
        "core/types.py",
        "from __future__ import annotations\n"
        "import threading\n"
        "from pydantic import BaseModel\n"
        "from geofact.core.store import LayerStore\n"
        "from . import registry\n"
        "import pandas\n"
        "from yaml import safe_load\n",
    )
    found = _violations(Rule("core", allowed_external=frozenset({"pydantic"})), tree)
    assert found == [
        "geofact/core/types.py:6 imports pandas (not whitelisted)",
        "geofact/core/types.py:7 imports yaml (not whitelisted)",
    ]


def test_fa45_scanner_whitelist_ignores_type_checking_imports(tree):
    _write(
        tree[0],
        "core/types.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import networkx\n",
    )
    assert _violations(Rule("core", allowed_external=frozenset()), tree) == []


def test_fa45_core_whitelist_is_enforced_on_the_real_core():
    """The whitelist rule is not vacuous: the real core imports at least one
    whitelisted library, and dropping the whitelist would flag it."""
    core_third_party = {
        imp.candidates[0].split(".")[0]
        for imp in collect_imports(GEOFACT_ROOT / "core", GEOFACT_ROOT.parent)
        if _is_third_party(imp.candidates[0].split(".")[0])
    }
    assert core_third_party and core_third_party <= CORE_THIRD_PARTY
    violations = find_violations(
        Rule("core", allowed_external=frozenset()),
        geofact_root=GEOFACT_ROOT,
        backend_root=BACKEND_ROOT,
    )
    assert violations


def test_fa45_block_scan_skips_helpers_and_lists_nested_modules(tmp_path):
    for relative in (
        "sources/osm.py",
        "sources/__init__.py",
        "_tabular.py",
        "operations/buffer.py",
        "operations/_geometry.py",
        "_private/inner.py",
    ):
        _write(tmp_path, relative, "")
    assert scanned_builtin_modules(tmp_path, "pkg.builtin") == {
        "pkg.builtin.sources.osm",
        "pkg.builtin.operations.buffer",
    }


def test_fa45_completeness_check_reports_a_module_without_entries():
    scanned = {"geofact.builtin.sources.osm", "geofact.builtin.operations.forgotten"}
    origins = {"geofact.builtin.sources.osm"}
    assert modules_without_entries(scanned, origins) == [
        "geofact.builtin.operations.forgotten"
    ]


def test_fa45_scanner_checks_the_scripts_scope_at_repo_level(tree):
    scripts = tree[0].parents[1] / "scripts"
    scripts.mkdir()
    (scripts / "run_demo.py").write_text(
        "from geofact import api\nfrom geofact.engine.run import run\n",
        encoding="utf-8",
    )
    found = _violations(Rule("scripts", allowed_internal=frozenset({"api"})), tree)
    assert found == ["scripts/run_demo.py:2 imports geofact.engine.run"]
