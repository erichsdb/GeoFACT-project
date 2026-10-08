"""Implements: FA40 (Vertrag liegt bei der Implementierung).

Contract-Tests für die Kolokation: das Layer-Modell einer Quellart steht in
der Datei ihrer Ladefunktion, die Step-Klasse einer Operation in der Datei
ihrer Ausführungsfunktion - für Kernbausteine und Plugins gleich. Der Kern
(geofact/core) kennt nur die Basisklassen, keinen konkreten Baustein; die
früher zentrale Datei ``config/schema.py`` gibt es nicht mehr.

Die Regel ist prüfbar, weil jeder Registry-Eintrag beide Teile trägt:
``OperationPlugin.step_model`` / ``.run`` und ``SourcePlugin.layer_model`` /
``.load``. ``_colocation_violations`` liest nur die Registry, es kennt keinen
Baustein beim Namen.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import textwrap
from pathlib import Path

import pytest

import geofact.core
from geofact.core.contracts import LayerBase, Step
from geofact.core.registry import (
    OperationPlugin,
    Registry,
    SourcePlugin,
    default_registry,
)
from geofact.engine.discovery import discover


def _colocation_violations(registry: Registry) -> list[str]:
    """Einträge, deren Vertrag nicht in der Datei der Implementierung steht."""
    problems: list[str] = []
    for plugin in registry.entries("operation"):
        if plugin.step_model.__module__ != plugin.run.__module__:
            problems.append(
                f"operation '{plugin.name}': Step-Klasse {plugin.step_model.__name__} in "
                f"{plugin.step_model.__module__}, Ausfuehrungsfunktion in {plugin.run.__module__}"
            )
    for plugin in registry.entries("source"):
        if plugin.layer_model.__module__ != plugin.load.__module__:
            problems.append(
                f"source '{plugin.name}': Layer-Modell {plugin.layer_model.__name__} in "
                f"{plugin.layer_model.__module__}, Ladefunktion in {plugin.load.__module__}"
            )
    return problems


def _subclasses(base: type) -> set[type]:
    found: set[type] = set()
    stack = [base]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub not in found:
                found.add(sub)
                stack.append(sub)
    return found


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


PLUGIN_OPERATION = """
from typing import ClassVar, Literal

from geofact.plugin_api import DataType, register_operation, Step


class KolokationStep(Step):
    op: Literal["kolokation_op"] = "kolokation_op"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR


@register_operation(KolokationStep)
def run_kolokation(inputs, params):
    return inputs["features"]
"""

PLUGIN_SOURCE = """
from typing import Literal

from geopandas import GeoDataFrame

from geofact.plugin_api import DataType, LayerBase, LoadContext, register_source


class KolokationLayer(LayerBase):
    source: Literal["kolokation_quelle"] = "kolokation_quelle"
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR


@register_source("kolokation_quelle", KolokationLayer)
def load(layer: KolokationLayer, ctx: LoadContext) -> GeoDataFrame:
    return GeoDataFrame({"geometry": []}, crs="EPSG:4326")
"""


def test_fa40_contract_lives_with_implementation(tmp_path):
    """Nachbedingung: für jede Operation steht die Step-Klasse im Modul der
    Ausführungsfunktion, für jede Quellart das Layer-Modell im Modul der
    Ladefunktion - in der Kern-Registry und in einer Registry mit Plugin."""
    plugins = tmp_path / "plugins"
    _write(plugins, "kolokation_op.py", PLUGIN_OPERATION)
    _write(plugins, "kolokation_quelle.py", PLUGIN_SOURCE)
    with_plugin = discover(plugin_paths=[plugins])
    core = default_registry()

    # Nicht leer: die Regel darf nicht an einer leeren Registry "gelten".
    assert len(core.entries("operation")) >= 27
    assert len(core.entries("source")) >= 8
    assert "kolokation_op" in with_plugin.names("operation")
    assert "kolokation_quelle" in with_plugin.names("source")

    assert _colocation_violations(core) == []
    assert _colocation_violations(with_plugin) == []


def test_fa40_colocation_check_reports_a_contract_in_a_foreign_module():
    """Die Prüfung selbst ist wirksam: ein Eintrag, dessen Step-Klasse
    woanders steht als die Ausführungsfunktion, und eine Quellart, deren
    Layer-Modell woanders steht als die Ladefunktion, werden benannt."""
    core = default_registry()
    buffer_entry: OperationPlugin = core.operation("buffer")
    osm_entry: SourcePlugin = core.source("osm")

    def foreign_run(inputs, params):
        return inputs["geometry"]

    def foreign_load(layer, ctx):
        return None

    registry = Registry()
    registry.merge(
        [
            (
                "operation",
                OperationPlugin("buffer_fremd", buffer_entry.step_model, foreign_run),
            ),
            ("source", SourcePlugin("osm_fremd", osm_entry.layer_model, foreign_load)),
        ]
    )
    problems = _colocation_violations(registry)

    assert len(problems) == 2
    assert any(
        "operation 'buffer_fremd'" in p and "geofact.builtin.operations.buffer" in p
        for p in problems
    )
    assert any(
        "source 'osm_fremd'" in p and "geofact.builtin.sources.osm" in p
        for p in problems
    )


def test_fa40_core_defines_no_concrete_step_or_layer_model():
    """Nachbedingung: geofact/core definiert nur Basisklassen. Jede Unterklasse
    von Step oder LayerBase stammt aus einem Baustein, keine aus dem Kern -
    sonst wäre der Kern wieder der zentrale Ort der Verträge."""
    for info in pkgutil.walk_packages(geofact.core.__path__, prefix="geofact.core."):
        importlib.import_module(info.name)
    # Die Bausteine importieren, damit ihre Klassen in __subclasses__ stehen.
    default_registry()

    in_core = sorted(
        f"{cls.__module__}.{cls.__qualname__}"
        for cls in _subclasses(Step) | _subclasses(LayerBase)
        if cls.__module__.startswith("geofact.core")
    )
    assert in_core == []
    assert len(_subclasses(Step)) >= 27
    assert len(_subclasses(LayerBase)) >= 8


def test_fa40_central_schema_module_is_gone():
    """Nachbedingung: es gibt weder ``geofact.config`` noch eine Re-Export-
    Schicht konkreter Klassen im Kern (sie erzeugte eine Kante Kern -> Baustein)."""
    assert importlib.util.find_spec("geofact.config") is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("geofact.config.schema")
    scenario = importlib.import_module("geofact.core.scenario")
    contracts = importlib.import_module("geofact.core.contracts")
    for module in (scenario, contracts):
        for name in (
            "BufferStep",
            "OsmLayer",
            "FileLayer",
            "TableLayer",
            "ZonalStatsStep",
        ):
            assert not hasattr(module, name), f"{module.__name__} re-exportiert {name}"


def test_fa40_every_builtin_step_and_layer_module_is_an_extension_module():
    """Die Bausteine, in die die Verträge gewandert sind, sind genau die
    Module, die die Erkennung scannt: ihr Modulname steht als Herkunft im
    Registry-Eintrag (kein Vertrag in einem ungescannten Hilfsmodul)."""
    registry = default_registry()
    for plugin in registry.entries("operation"):
        assert plugin.origin == plugin.step_model.__module__, plugin.name
    for plugin in registry.entries("source"):
        assert plugin.origin == plugin.layer_model.__module__, plugin.name


@pytest.mark.parametrize(
    ("module", "names"),
    [
        ("geofact.builtin.sources.osm", ["OsmLayer"]),
        ("geofact.builtin.sources.file", ["FileLayer"]),
        ("geofact.builtin.sources.table", ["TableLayer"]),
        ("geofact.builtin.sources.wfs", ["WfsLayer"]),
        ("geofact.builtin.sources.ckan", ["CkanLayer"]),
        ("geofact.builtin.sources.gtfs", ["GtfsLayer"]),
        ("geofact.builtin.sources.region", ["RegionLayer"]),
        ("geofact.builtin.sources.rest", ["RestLayer"]),
        ("geofact.builtin._tabular", ["GeometryMapping", "ColumnSpec"]),
        ("geofact.builtin.operations.buffer", ["BufferStep"]),
        ("geofact.builtin.operations.nearest_distance", ["NearestDistanceStep"]),
        ("geofact.builtin.operations.spatial_join", ["SpatialJoinStep"]),
        ("geofact.builtin.operations.overlay", ["OverlayStep"]),
        ("geofact.builtin.operations.clip", ["ClipStep"]),
        ("geofact.builtin.operations.dissolve", ["DissolveStep"]),
        ("geofact.builtin.operations.zonal_stats", ["ZonalStatsStep"]),
        ("geofact.builtin.operations.classify", ["ClassifyStep"]),
        ("geofact.builtin.operations.filter", ["FilterStep"]),
        ("geofact.builtin.operations.ranking", ["RankingStep"]),
        ("geofact.builtin.operations.to_points", ["ToPointsStep"]),
        ("geofact.builtin.operations.aggregate", ["AggregateStep"]),
        ("geofact.builtin.operations.hexbin", ["HexbinStep"]),
        ("geofact.builtin.operations.hex_grid", ["HexGridStep"]),
        (
            "geofact.builtin.operations.graph",
            ["BuildNetworkStep", "CentralityStep", "GraphToVectorStep"],
        ),
        ("geofact.builtin.operations.network_nodes", ["NetworkNodesStep"]),
        ("geofact.builtin.operations.homogenize", ["HomogenizeGeometryStep"]),
        ("geofact.builtin.operations.vector_union", ["VectorUnionStep"]),
        ("geofact.builtin.operations.repair", ["RepairGeometryStep"]),
        (
            "geofact.builtin.operations.reachability",
            ["ShortestPathStep", "IsochroneStep"],
        ),
        ("geofact.builtin.operations.attribute_join", ["AttributeJoinStep"]),
        ("geofact.builtin.operations.top_n", ["TopNStep"]),
        ("geofact.builtin.operations.rest", ["RestStep"]),
        ("geofact.builtin.operations.power_grid_topology", ["PowerGridTopologyStep"]),
    ],
)
def test_fa40_moved_contracts_are_defined_in_their_block_module(module, names):
    """Die aus schema.py verschobenen Klassen (23 Step-Klassen, 7 Layer-Modelle,
    GeometryMapping/ColumnSpec) sind dort definiert, wo ihre Implementierung
    steht - nicht nur von dort importiert."""
    loaded = importlib.import_module(module)
    for name in names:
        cls = getattr(loaded, name)
        assert cls.__module__ == module, (
            f"{name} ist in {cls.__module__} definiert, nicht in {module}"
        )
