"""Implements: FA40 (Registry, ein Lader, explizite Erkennung).

Contract-Tests für den Mechanismus hinter den Erweiterungspunkten:

- Registry (core/registry.py): eine Tabelle je Erweiterungsart, atomares
  Zusammenführen, eingefroren und ohne Sperre lesbar
- Markierung (register_*): prüft sofort, schreibt keinen globalen Zustand
- Erkennung (engine/discovery.py): Kernbausteine streng, Plugin-Dateien und
  -Pakete tolerant, Hilfsnamen mit '_' ausgelassen, transaktional je Modul
- api.load_extensions(): explizit, idempotent, thread-sicher

Jeder Test baut sich seine eigene Registry per ``discover(plugin_paths=[...])``
oder stellt die Standard-Registry der Sitzung in einem try/finally wieder her.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from geofact import api
from geofact.core.errors import PluginError, PluginLoadWarning
from geofact.core.registry import (
    NOT_LOADED_MESSAGE,
    Registry,
    TransformPlugin,
    default_registry,
    installed_registry,
    register_operation,
    register_source,
    set_default_registry,
)
from geofact.core.scenario import Scenario
from geofact.engine.discovery import discover

BBOX_REGION = "12.30,51.30,12.40,51.36"


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


@pytest.fixture
def plugin_dir(tmp_path) -> Path:
    directory = tmp_path / "plugins"
    directory.mkdir()
    return directory


_OP_HEADER = """
from typing import ClassVar, Literal

from geofact.plugin_api import DataType, register_operation, Step
"""


def _op_block(op: str, function: str) -> str:
    cls = op.title().replace("_", "") + "Step"
    return f'''

class {cls}(Step):
    op: Literal["{op}"] = "{op}"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {{"features": DataType.VECTOR}}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR


@register_operation({cls})
def {function}(inputs, params):
    return inputs["features"]
'''


def _op_plugin(op: str, function: str = "run") -> str:
    """Quelltext eines Plugins mit EINER Operation."""
    return _OP_HEADER + _op_block(op, function)


def _ops_plugin(*pairs: tuple[str, str]) -> str:
    """Quelltext eines Plugins mit mehreren Operationen (Name, Funktionsname)."""
    return _OP_HEADER + "".join(_op_block(op, fn) for op, fn in pairs)


# ---------------------------------------------------------------------------
# Transaktional: ein Plugin ist ganz oder gar nicht registriert
# ---------------------------------------------------------------------------

HALF_BROKEN_PLUGIN = """
from geofact.plugin_api import register_transform


@register_transform("halb_fertig")
def erste(layer, data, ctx):
    return data


raise RuntimeError("nach dem ersten Dekorator abgebrochen")
"""


def test_fa40_half_broken_plugin_leaves_nothing_registered(plugin_dir):
    """Vorbedingung: ein Plugin scheitert NACH seinem ersten Dekorator.
    Nachbedingung: nichts davon ist registriert (Markierungen werden erst nach
    einem sauberen Import gesammelt), das Plugin steht in Registry.failed."""
    broken = _write(plugin_dir, "halb.py", HALF_BROKEN_PLUGIN)
    with pytest.warns(PluginLoadWarning, match="nach dem ersten Dekorator abgebrochen"):
        registry = discover(plugin_paths=[plugin_dir])

    assert "halb_fertig" not in registry.names("transform")
    assert str(broken) in registry.failed
    assert "RuntimeError" in registry.failed[str(broken)]
    assert registry.names("transform") == default_registry().names("transform")


def test_fa40_failed_plugin_does_not_hide_the_good_one_next_to_it(plugin_dir):
    _write(plugin_dir, "a_halb.py", HALF_BROKEN_PLUGIN)
    _write(plugin_dir, "b_gut.py", _op_plugin("gut_operation"))
    with pytest.warns(PluginLoadWarning):
        registry = discover(plugin_paths=[plugin_dir])
    assert "gut_operation" in registry.names("operation")
    assert "halb_fertig" not in registry.names("transform")


def test_fa40_duplicate_across_plugin_files_names_both_origins(plugin_dir):
    """Zwei Plugin-Dateien mit demselben Operationsnamen: die zweite wird
    abgelehnt, die Meldung nennt BEIDE Dateien; die erste bleibt voll nutzbar."""
    first = _write(plugin_dir, "a_erste.py", _op_plugin("doppelt", "run_a"))
    second = _write(plugin_dir, "b_zweite.py", _op_plugin("doppelt", "run_b"))
    with pytest.warns(PluginLoadWarning) as caught:
        registry = discover(plugin_paths=[plugin_dir])

    message = registry.failed[str(second)]
    assert "Operation 'doppelt' ist doppelt registriert" in message
    assert str(first) in message and str(second) in message
    assert any("doppelt registriert" in str(w.message) for w in caught)
    assert registry.operation("doppelt").run.__name__ == "run_a"
    assert registry.operation("doppelt").origin == str(first)


def test_fa40_duplicate_rejects_the_whole_second_module(plugin_dir):
    """Atomar: die zweite Datei bringt neben dem doppelten Namen noch einen
    eigenen mit - auch der darf nicht übrig bleiben."""
    _write(plugin_dir, "a_erste.py", _op_plugin("doppelt"))
    _write(
        plugin_dir,
        "b_zweite.py",
        _ops_plugin(("doppelt", "run_b"), ("eigene_zweite", "run_c")),
    )
    with pytest.warns(PluginLoadWarning):
        registry = discover(plugin_paths=[plugin_dir])
    assert "eigene_zweite" not in registry.names("operation")


def test_fa40_file_format_extension_collision_names_both_formats(plugin_dir):
    """Zwei Formate mit derselben Dateiendung würden still nach Ladereihenfolge
    entscheiden: abgelehnt, mit beiden Namen und Herkünften."""
    plugin = _write(
        plugin_dir,
        "json_format.py",
        """
        from geofact.plugin_api import DataType, register_file_format


        @register_file_format("json2", extensions=("geojson",), data_type=DataType.VECTOR)
        def read_json2(path, layer, ctx):
            raise AssertionError("darf nie aufgerufen werden")
    """,
    )
    with pytest.warns(PluginLoadWarning, match="Dateiendung '.geojson'"):
        registry = discover(plugin_paths=[plugin_dir])
    message = registry.failed[str(plugin)]
    assert "'geojson'" in message and "'json2'" in message
    assert "json2" not in registry.names("file_format")


# ---------------------------------------------------------------------------
# Markierung prüft sofort
# ---------------------------------------------------------------------------


def test_fa40_operation_with_wrong_run_signature_is_rejected(plugin_dir):
    """Die Ausführungsfunktion muss (inputs, params) annehmen - sonst fällt das
    Plugin beim Laden auf statt erst im Lauf (Schnittstelle OperationRunner)."""
    wrong = _op_plugin("falsche_signatur").replace(
        "def run(inputs, params):", "def run(inputs):"
    )
    plugin = _write(plugin_dir, "falsch.py", wrong)
    with pytest.warns(PluginLoadWarning, match="OperationRunner\\(inputs, params\\)"):
        registry = discover(plugin_paths=[plugin_dir])
    assert "falsche_signatur" not in registry.names("operation")
    assert str(plugin) in registry.failed


def test_fa40_register_operation_rejects_a_contract_that_is_not_a_step():
    class PlainModel:
        op = "plain"

    with pytest.raises(PluginError, match="keine Unterklasse von Step"):
        register_operation(PlainModel)


def test_fa40_register_operation_needs_the_op_literal_default():
    from geofact.core.contracts import Step

    class NoOpName(Step):
        pass

    with pytest.raises(PluginError, match="'op' als Literal"):
        register_operation(NoOpName)


def test_fa40_register_source_rejects_a_model_that_is_not_a_layer_base():
    from pydantic import BaseModel

    class NotALayer(BaseModel):
        source: str = "x"

    with pytest.raises(PluginError, match="keine Unterklasse von LayerBase"):
        register_source("x", NotALayer)


def test_fa40_marking_writes_no_global_state():
    """Die Dekoratoren markieren nur: die Standard-Registry ändert sich durch
    eine Markierung nicht."""
    from geofact.core.registry import MARK, register_transform

    before = default_registry().names("transform")

    @register_transform("nur_markiert")
    def fn(layer, data, ctx):
        return data

    assert default_registry().names("transform") == before
    assert getattr(fn, MARK)[0][0] == "transform"


# ---------------------------------------------------------------------------
# Namentliche Verweise
# ---------------------------------------------------------------------------

BAD_TRANSFORM_PLUGIN = """
from typing import Literal

from geofact.plugin_api import DataType, LayerBase, register_operation, register_source


class BadLayer(LayerBase):
    source: Literal["bad_ref"] = "bad_ref"
    data_type: DataType = DataType.VECTOR


@register_source("bad_ref", BadLayer, transform="gibt_es_nicht")
def load(layer, ctx):
    raise AssertionError("darf nie aufgerufen werden")
"""


def test_fa40_bad_transform_name_is_reported(plugin_dir):
    """Eine Quelle verweist per Namen auf eine Transformation, die es nicht
    gibt: das Plugin entfällt mit einer Meldung, die den Namen und die
    registrierten Transformationen nennt - nicht erst im Lauf."""
    plugin = _write(plugin_dir, "bad_ref.py", BAD_TRANSFORM_PLUGIN)
    _write(plugin_dir, "good.py", _op_plugin("gut_operation"))
    with pytest.warns(PluginLoadWarning, match="Transformation 'gibt_es_nicht'"):
        registry = discover(plugin_paths=[plugin_dir])
    assert "bad_ref" not in registry.names("source")
    assert "reproject" in registry.failed[str(plugin)]
    assert "gut_operation" in registry.names("operation")


def test_fa40_transform_reference_resolves_across_plugin_files(plugin_dir):
    """Der Verweis wird nach dem Laden ALLER Plugins geprüft: die Transformation
    darf in einer Datei stehen, die später als die Quelle geladen wird."""
    _write(
        plugin_dir,
        "a_quelle.py",
        BAD_TRANSFORM_PLUGIN.replace("gibt_es_nicht", "spaeter"),
    )
    _write(
        plugin_dir,
        "z_transform.py",
        """
        from geofact.plugin_api import register_transform


        @register_transform("spaeter")
        def spaeter(layer, data, ctx):
            return data
    """,
    )
    registry = discover(plugin_paths=[plugin_dir])
    assert "bad_ref" in registry.names("source")
    assert not registry.failed


# ---------------------------------------------------------------------------
# Plugin-Dateien, Hilfsnamen, Plugin-Pakete
# ---------------------------------------------------------------------------


def test_fa40_files_with_leading_underscore_are_helpers_and_not_scanned(plugin_dir):
    _write(plugin_dir, "_helfer.py", _op_plugin("versteckt"))
    _write(plugin_dir, "sichtbar.py", _op_plugin("sichtbar_op"))
    registry = discover(plugin_paths=[plugin_dir])
    assert "sichtbar_op" in registry.names("operation")
    assert "versteckt" not in registry.names("operation")


def test_fa40_marks_are_collected_only_from_the_defining_module(plugin_dir):
    """Komposition bleibt erlaubt: ein Plugin darf eine bereits markierte
    Funktion importieren, ohne sie ein zweites Mal zu registrieren."""
    imports = (
        "from geofact.builtin.operations.buffer import run_buffer  # anderswo definiert und markiert\n"
        "from geofact.builtin.operations.buffer import BufferStep\n"
    )
    _write(plugin_dir, "komposition.py", imports + _op_plugin("eigene_op"))
    registry = discover(plugin_paths=[plugin_dir])
    assert not registry.failed
    assert registry.operation("buffer").origin == "geofact.builtin.operations.buffer"
    assert registry.operation("eigene_op").origin.endswith("komposition.py")


PACKAGE_INIT = """
from ._helfer import FAKTOR
"""

PACKAGE_OPS = """
from typing import ClassVar, Literal

from geofact.plugin_api import DataType, register_operation, Step

from ._helfer import FAKTOR, versteckte_funktion  # noqa: F401
from . import _helfer


class PaketStep(Step):
    op: Literal["paket_op"] = "paket_op"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR


@register_operation(PaketStep)
def run_paket(inputs, params):
    return FAKTOR, _helfer.FAKTOR
"""

PACKAGE_HELPER = """
from geofact.plugin_api import register_transform

FAKTOR = 42


@register_transform("aus_hilfsmodul")
def versteckte_funktion(layer, data, ctx):
    return data
"""


def test_fa40_plugin_package_with_helper_module_loads(plugin_dir):
    """Ein Plugin-Paket (Ordner mit __init__.py) bringt eigene Hilfsmodule mit
    (relative Importe); Nicht-Hilfsmodule tragen Bausteine bei, ``_helfer``
    bleibt ungescannt, auch wenn dort markiert wurde."""
    package = plugin_dir / "mein_paket"
    _write(package, "__init__.py", PACKAGE_INIT)
    _write(package, "ops.py", PACKAGE_OPS)
    _write(package, "_helfer.py", PACKAGE_HELPER)

    registry = discover(plugin_paths=[plugin_dir])

    assert not registry.failed
    entry = registry.operation("paket_op")
    assert entry.run({}, {}) == (42, 42)
    assert entry.origin.endswith("ops.py")
    assert "aus_hilfsmodul" not in registry.names("transform")


def test_fa40_broken_plugin_package_is_skipped_as_a_whole(plugin_dir):
    package = plugin_dir / "kaputtes_paket"
    _write(package, "__init__.py", "")
    _write(package, "a_ok.py", _op_plugin("paket_ok"))
    _write(package, "b_kaputt.py", "raise ImportError('Hilfsbibliothek fehlt')\n")
    with pytest.warns(PluginLoadWarning, match="Hilfsbibliothek fehlt"):
        registry = discover(plugin_paths=[plugin_dir])
    assert "paket_ok" not in registry.names("operation")
    assert str(package) in registry.failed


def test_fa40_same_plugin_name_in_two_directories_is_reported(tmp_path):
    first, second = tmp_path / "eins", tmp_path / "zwei"
    a = _write(first, "gleich.py", _op_plugin("op_eins"))
    b = _write(second, "gleich.py", _op_plugin("op_zwei"))
    with pytest.warns(PluginLoadWarning, match="denselben Namen"):
        registry = discover(plugin_paths=[first, second])
    assert "op_eins" in registry.names("operation")
    assert "op_zwei" not in registry.names("operation")
    assert str(b) in registry.failed and str(a) not in registry.failed


def test_fa40_same_directory_twice_loads_once(plugin_dir):
    _write(plugin_dir, "einmal.py", _op_plugin("einmal_op"))
    registry = discover(plugin_paths=[plugin_dir, plugin_dir])
    assert not registry.failed
    assert "einmal_op" in registry.names("operation")


def test_fa40_discover_is_deterministic(plugin_dir):
    _write(plugin_dir, "b.py", _op_plugin("op_b"))
    _write(plugin_dir, "a.py", _op_plugin("op_a"))
    first = discover(plugin_paths=[plugin_dir])
    second = discover(plugin_paths=[plugin_dir])
    for kind in (
        "source",
        "file_format",
        "table_format",
        "transform",
        "operation",
        "output",
    ):
        assert [e.name for e in first.entries(kind)] == [
            e.name for e in second.entries(kind)
        ]
        assert first.names(kind) == sorted(first.names(kind))
        assert [e.origin for e in first.entries(kind)] == [
            e.origin for e in second.entries(kind)
        ]


def test_fa40_broken_core_module_aborts_discovery(tmp_path, monkeypatch):
    """Ein Kernbaustein, der sich nicht laden lässt, ist ein Fehler im
    Framework: die Erkennung bricht ab (streng), anders als bei Plugins."""
    package = tmp_path / "kern_paket_kaputt"
    _write(package, "__init__.py", "")
    _write(package, "gut.py", "")
    _write(package, "schlecht.py", "raise ImportError('Kernmodul kaputt')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(
        PluginError, match="Kernbaustein 'kern_paket_kaputt.schlecht'.*Kernmodul kaputt"
    ):
        discover(builtin_packages=["kern_paket_kaputt"])


# ---------------------------------------------------------------------------
# Registry: eingefroren, atomar, ohne Sperre lesbar
# ---------------------------------------------------------------------------


def test_fa40_frozen_registry_is_read_only():
    registry = default_registry()
    assert registry.frozen
    with pytest.raises(RuntimeError, match="eingefroren"):
        registry.merge([])
    with pytest.raises(RuntimeError, match="eingefroren"):
        registry.record_failure("x", "y")
    with pytest.raises(TypeError):
        registry._tables["source"]["neu"] = object()  # MappingProxyType


def test_fa40_only_a_frozen_registry_can_be_installed():
    with pytest.raises(ValueError, match="eingefroren"):
        set_default_registry(Registry())


def test_fa40_merge_is_all_or_nothing():
    registry = Registry()

    def entry(name: str, origin: str) -> TransformPlugin:
        return TransformPlugin(
            name=name, apply=lambda layer, data, ctx: data, origin=origin
        )

    registry.merge([("transform", entry("erste", "modul_a"))])
    with pytest.raises(PluginError, match="modul_a und modul_b"):
        registry.merge(
            [
                ("transform", entry("neu", "modul_b")),
                ("transform", entry("erste", "modul_b")),
            ]
        )
    assert registry.names("transform") == ["erste"]


def test_fa40_concurrent_reads_of_the_frozen_registry_need_no_lock():
    """Die installierte Registry ist eingefroren: viele Threads lesen parallel
    (Validierung und Ausführung der ersten parallelen Web-Requests) und
    sehen immer denselben, vollständigen Stand."""
    registry = default_registry()
    expected_ops = registry.names("operation")
    barrier = threading.Barrier(16)

    def reader(_: int) -> bool:
        barrier.wait()
        for _round in range(200):
            assert registry.names("operation") == expected_ops
            assert registry.operation("buffer").step_model.__name__ == "BufferStep"
            assert registry.source("osm").layer_model.__name__ == "OsmLayer"
            assert registry.file_format_for_extension("GEOJSON").name == "geojson"
            assert "gpkg" in registry.zip_member_extensions()
        return True

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert all(pool.map(reader, range(16)))


def test_fa40_concurrent_validation_against_the_frozen_registry():
    raw = {
        "scenario": {"name": "parallel", "region": BBOX_REGION},
        "layers": [{"id": "r", "source": "region"}],
        "steps": [
            {
                "id": "b",
                "op": "buffer",
                "inputs": {"geometry": "r"},
                "params": {"radius_km": 1},
            }
        ],
        "output": [{"type": "geojson", "source": "b"}],
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: Scenario(**raw).execution_order(), range(40)))
    assert results == [["b"]] * 40


# ---------------------------------------------------------------------------
# Standard-Registry und load_extensions
# ---------------------------------------------------------------------------


def test_fa40_default_registry_without_load_raises_the_explicit_message():
    """Es gibt keine versteckte Erkennung: ohne load_extensions() ein
    expliziter Fehler - auch beim Validieren eines Szenarios."""
    previous = set_default_registry(None)
    try:
        assert installed_registry() is None
        with pytest.raises(RuntimeError) as excinfo:
            default_registry()
        assert str(excinfo.value) == (
            "Erweiterungen nicht geladen – geofact.api.load_extensions() aufrufen"
        )
        assert str(excinfo.value) == NOT_LOADED_MESSAGE
        with pytest.raises(RuntimeError, match="load_extensions"):
            Scenario(
                **{
                    "scenario": {"name": "x", "region": BBOX_REGION},
                    "layers": [{"id": "r", "source": "region"}],
                    "steps": [
                        {
                            "id": "b",
                            "op": "buffer",
                            "inputs": {"geometry": "r"},
                            "params": {"radius_km": 1},
                        }
                    ],
                    "output": [{"type": "geojson", "source": "b"}],
                }
            )
    finally:
        set_default_registry(previous)


def test_fa40_load_extensions_is_idempotent():
    first = api.load_extensions(plugin_paths=[])
    assert api.load_extensions() is first
    assert (
        api.load_extensions(plugin_paths=[]) is first
    )  # dieselben Ordner: je Prozess einmal
    assert api.registry() is first


def test_fa40_load_extensions_with_different_plugin_paths_is_an_explicit_error(
    plugin_dir,
):
    """Goldene Regel 7: wer andere Plugin-Ordner verlangt, als die installierte
    Registry hat, bekommt keine stillschweigend abweichende Registry."""
    first = api.load_extensions(plugin_paths=[])
    with pytest.raises(PluginError) as excinfo:
        api.load_extensions(plugin_paths=[plugin_dir])
    message = str(excinfo.value)
    assert str(plugin_dir.resolve()) in message  # angefordert
    assert "keine" in message  # installiert: keine Plugin-Ordner
    assert "force=True" in message  # Weg zum Ersetzen
    assert api.load_extensions() is first  # nichts wurde verändert
    assert installed_registry() is first


def test_fa40_load_extensions_same_plugin_paths_stay_idempotent_in_any_spelling(
    plugin_dir,
):
    _write(plugin_dir, "stabil.py", _op_plugin("stabil_op"))
    previous = installed_registry()
    try:
        forced = api.load_extensions(plugin_paths=[plugin_dir], force=True)
        assert api.load_extensions(plugin_paths=[str(plugin_dir)]) is forced
        assert (
            api.load_extensions(plugin_paths=[plugin_dir, plugin_dir / "."]) is forced
        )
        assert api.load_extensions() is forced  # None: ohne Prüfung
        with pytest.raises(PluginError, match="Plugin-Ordnern"):
            api.load_extensions(plugin_paths=[])
    finally:
        set_default_registry(previous)
    assert (
        api.load_extensions(plugin_paths=[]) is previous
    )  # zurückgesetzt: wieder ohne Plugins


def test_fa40_load_extensions_with_paths_on_a_foreign_registry_is_an_explicit_error(
    plugin_dir,
):
    """Eine nicht über load_extensions() installierte Registry kennt ihre
    Plugin-Ordner nicht - ein Aufruf mit Ordnern kann nicht bestätigt werden."""
    previous = set_default_registry(discover(plugin_paths=[]))
    try:
        with pytest.raises(PluginError, match="nicht über load_extensions"):
            api.load_extensions(plugin_paths=[])
    finally:
        set_default_registry(previous)


def test_fa40_load_extensions_force_discovers_again(plugin_dir):
    _write(plugin_dir, "neu.py", _op_plugin("neu_op"))
    previous = installed_registry()
    try:
        forced = api.load_extensions(plugin_paths=[plugin_dir], force=True)
        assert forced is not previous
        assert "neu_op" in default_registry().names("operation")
    finally:
        set_default_registry(previous)
    assert "neu_op" not in default_registry().names("operation")


def test_fa40_load_extensions_reads_the_plugin_path_environment(
    plugin_dir, monkeypatch
):
    _write(plugin_dir, "aus_env.py", _op_plugin("env_op"))
    monkeypatch.setenv(api.PLUGIN_PATH_ENV, str(plugin_dir))
    previous = installed_registry()
    try:
        api.load_extensions(force=True)
        assert "env_op" in default_registry().names("operation")
    finally:
        set_default_registry(previous)


def test_fa40_parallel_load_extensions_discovers_exactly_once(monkeypatch):
    """Thread-sicher: parallele Erstaufrufer warten auf dieselbe Erkennung."""
    calls: list[int] = []
    real_discover = api.discover

    def counting_discover(paths):
        calls.append(1)
        return real_discover(paths)

    monkeypatch.setattr(api, "discover", counting_discover)
    previous = set_default_registry(None)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            registries = list(
                pool.map(lambda _: api.load_extensions(plugin_paths=[]), range(8))
            )
    finally:
        set_default_registry(previous)
    assert len(calls) == 1
    assert all(r is registries[0] for r in registries)


def test_fa40_import_of_core_registers_and_loads_nothing():
    """Importieren von geofact.core und geofact.api lädt weder Bausteine noch
    die Engine-Kernbausteine; erst load_extensions() tut das."""
    script = (
        "import sys\n"
        "import geofact.core.registry, geofact.core.types, geofact.core.contracts\n"
        "assert not any(m.startswith('geofact.builtin') for m in sys.modules), 'builtin geladen'\n"
        "assert 'networkx' not in sys.modules, 'networkx im Kern importiert'\n"
        "assert 'rasterio' not in sys.modules, 'rasterio im Kern importiert'\n"
        "from geofact.core.registry import installed_registry\n"
        "assert installed_registry() is None\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr


def test_fa40_validation_error_is_raised_for_unknown_operation_in_fresh_registry(
    plugin_dir,
):
    """Die Registry kommt aus dem Validierungskontext: eine Operation, die nur
    eine andere Registry kennt, ist hier unbekannt."""
    _write(plugin_dir, "nur_hier.py", _op_plugin("nur_hier_op"))
    registry = discover(plugin_paths=[plugin_dir])
    raw = {
        "scenario": {"name": "x", "region": BBOX_REGION},
        "layers": [{"id": "r", "source": "region"}],
        "steps": [{"id": "s", "op": "nur_hier_op", "inputs": {"features": "r"}}],
        "output": [{"type": "geojson", "source": "s"}],
    }
    assert Scenario.model_validate(
        raw, context={"registry": registry}
    ).execution_order() == ["s"]
    with pytest.raises(ValidationError, match="Unbekannte Operation 'nur_hier_op'"):
        Scenario(**raw)
