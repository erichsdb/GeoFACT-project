"""Implements: FA9 (Ausführungsplan: Reihenfolge und bedarfsgesteuertes Laden), FA6 (Referenz-Layer der räumlichen Prüfung), FA65 (Attribution je Knoten), FA66 (Freigabe nach dem letzten Leser).

Der eine Plan, den Executor und Web-Schicht gemeinsam lesen: aus einem
validierten Scenario wird einmal abgeleitet,

- ``order``         welche Schritte laufen (nur die vom Output aus erreichbaren),
                    topologisch geordnet,
- ``loads``         welche Layer unmittelbar vor welchem Schritt geladen werden
                    (erst bei Bedarf, nie beim Start),
- ``final_loads``   Layer, die nur ein Output liest (nach dem letzten Schritt),
- ``required_*`` / ``unused_*``  was gebraucht wird und was nie geladen wird,
- ``attribution``   Lizenzen je benötigtem Layer und Schritt (FA65),
- ``releases``      nur mit ``release_intermediates=True`` (FA66): je Schritt die
                    Knoten, die danach niemand mehr liest; ``order`` ist dann
                    tiefensuchend statt Kahn (``full_order`` bleibt Kahn).

Invariante (``of()`` prüft sie): jeder benötigte Layer wird genau einmal
geplant geladen und kein anderer. Ein Layer, auf den eine räumliche Prüfung
(``spatial_check.within``) eines benötigten Layers verweist, ist ebenfalls
benötigt und wird vor dem verweisenden Layer geladen.

``Scenario.execution_order()``, ``required_sources()`` und ``load_schedule()``
delegieren hierher. Dieses Modul kennt keinen konkreten Baustein; die Registry
dient nur der Vorabprüfung, damit ein fehlender Baustein vor dem ersten
Ladevorgang auffällt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping

from geofact.core.contracts import SCENARIO_REGION
from geofact.core.errors import ConfigError
from geofact.core.provenance import NodeAttribution, attribution_of

if TYPE_CHECKING:
    from geofact.core.registry import Registry
    from geofact.core.scenario import Scenario


def _depth_first_order(
    scenario: "Scenario", step_by_id: Mapping, needed: set[str]
) -> tuple[str, ...]:
    """FA66: Post-Order einer Tiefensuche von den Ausgaben (Deklarationsreihenfolge)
    über die Eingänge (Ports alphabetisch): ein Zweig endet, bevor der nächste
    beginnt, so sind seine Zwischenergebnisse früh frei. Iterativ, damit tiefe
    Ketten kein Rekursionslimit treffen."""
    order: list[str] = []
    done: set[str] = set()
    for out in scenario.output:
        if out.source not in step_by_id or out.source in done:
            continue
        stack: list[tuple[str, bool]] = [(out.source, False)]
        while stack:
            node, expanded = stack.pop()
            if node in done:
                continue
            if expanded:
                done.add(node)
                order.append(node)
                continue
            stack.append((node, True))
            inputs = sorted(step_by_id[node].inputs.items())
            for _port, source in reversed(inputs):
                if source in step_by_id and source not in done:
                    stack.append((source, False))
    if set(order) != needed:
        raise RuntimeError(
            f"Ausführungsplan inkonsistent: Tiefensuche {sorted(order)} != benötigt {sorted(needed)}"
        )
    return tuple(order)


def _last_readers(
    scenario: "Scenario",
    order: tuple[str, ...],
    loads: Mapping[str, tuple[str, ...]],
    final_loads: tuple[str, ...],
    layer_by_id: Mapping,
    step_by_id: Mapping,
) -> dict[str, tuple[str, ...]]:
    """FA66: je Schritt die Knoten, deren letzter Leser er ist. Leser sind
    Schritte mit dem Knoten als Eingang und die Ladung eines prüfenden Layers
    (für dessen Referenz-Layer). Ausgabequellen und Referenzen einer späten
    Ladung werden nie freigegeben; dass kein Leser nach der Freigabe liest, wird
    geprüft."""
    position = {step_id: index for index, step_id in enumerate(order)}
    last: dict[str, int] = {}

    def read(node: str, at: int) -> None:
        last[node] = max(last.get(node, -1), at)

    for step_id in order:
        for source in step_by_id[step_id].inputs.values():
            read(source, position[step_id])
        for layer_id in loads.get(step_id, ()):
            for reference in layer_dependencies(layer_by_id[layer_id]):
                read(reference, position[step_id])
    kept = {out.source for out in scenario.output}
    for layer_id in final_loads:
        kept.update(layer_dependencies(layer_by_id[layer_id]))

    releases: dict[str, list[str]] = {}
    for node, at in last.items():
        if node not in kept:
            releases.setdefault(order[at], []).append(node)
    ordered = {
        step_id: tuple(sorted(releases[step_id]))
        for step_id in order
        if step_id in releases
    }

    # Invariante: nach der Freigabe liest kein Schritt, keine Ladung und
    # keine Ausgabe den Knoten.
    for step_id, nodes in ordered.items():
        for later in order[position[step_id] + 1 :]:
            readers = set(step_by_id[later].inputs.values())
            for layer_id in loads.get(later, ()):
                readers.update(layer_dependencies(layer_by_id[layer_id]))
            clash = readers & set(nodes)
            if clash:
                raise RuntimeError(
                    f"Freigabeplan inkonsistent: {sorted(clash)} nach '{step_id}' freigegeben, "
                    f"aber von '{later}' gelesen"
                )
        if set(nodes) & kept:
            raise RuntimeError(
                f"Freigabeplan inkonsistent: Ausgabequelle nach '{step_id}' freigegeben"
            )
    return ordered


def topological_order(
    edges: Mapping[str, set[str]], step_ids: Iterable[str]
) -> list[str]:
    """Kahn-Algorithmus über die Schritte. edges: Schritt -> Quellen (nur
    Schritte erzeugen Kanten). Gleichrangige Schritte erscheinen alphabetisch;
    ein Zyklus ist ein Fehler mit den beteiligten Schritten."""
    step_set = set(step_ids)
    in_degree = {sid: 0 for sid in step_set}
    dependents: dict[str, list[str]] = {sid: [] for sid in step_set}
    for sid, sources in edges.items():
        for src in sources:
            if src in step_set:
                in_degree[sid] += 1
                dependents[src].append(sid)
    queue = sorted(sid for sid in step_set if in_degree[sid] == 0)
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for dep in sorted(dependents[node]):
            in_degree[dep] -= 1
            if in_degree[dep] == 0:
                queue.append(dep)
    if len(order) != len(step_set):
        cyclic = step_set - set(order)
        raise ValueError(f"Zyklus im Datenflussgraphen erkannt, beteiligt: {cyclic}")
    return order


def layer_dependencies(layer) -> list[str]:
    """Layer-IDs, die vor ``layer`` geladen sein müssen: der Referenz-Layer einer
    räumlichen Prüfung (FA6); ``scenario_region`` ist kein Layer und zählt nicht."""
    validation = getattr(layer, "validation", None)
    if validation is None or validation.spatial_check is None:
        return []
    reference = validation.spatial_check.within
    return [] if reference == SCENARIO_REGION else [reference]


def _empty_mapping() -> Mapping:
    return MappingProxyType({})


@dataclass(frozen=True)
class ExecutionPlan:
    """Unveränderlicher Ausführungsplan eines Szenarios (siehe Modul-Docstring)."""

    full_order: tuple[str, ...]
    order: tuple[str, ...]
    loads: Mapping[str, tuple[str, ...]]
    final_loads: tuple[str, ...]
    required_layers: frozenset[str]
    required_steps: frozenset[str]
    unused_layers: frozenset[str]
    unused_steps: frozenset[str]
    releases: Mapping[str, tuple[str, ...]] = field(default_factory=_empty_mapping)
    attribution: Mapping[str, "NodeAttribution"] = field(default_factory=_empty_mapping)

    @property
    def load_order(self) -> tuple[str, ...]:
        """Alle geplanten Ladungen in der Reihenfolge, in der sie stattfinden."""
        planned: list[str] = []
        for step_id in self.order:
            planned.extend(self.loads.get(step_id, ()))
        planned.extend(self.final_loads)
        return tuple(planned)

    def required(self) -> dict:
        """Erreichbarkeitsanalyse in der Form von ``Scenario.required_sources()``."""
        return {
            "layers": set(self.required_layers),
            "steps": set(self.required_steps),
            "unused_layers": set(self.unused_layers),
            "unused_steps": set(self.unused_steps),
        }

    def schedule(self) -> dict[str, list[str]]:
        """Ladeplan in der Form von ``Scenario.load_schedule()`` (nur Schritte mit Ladung)."""
        return {step_id: list(layer_ids) for step_id, layer_ids in self.loads.items()}

    @classmethod
    def of(
        cls,
        scenario: "Scenario",
        registry: "Registry | None" = None,
        *,
        release_intermediates: bool = False,
    ) -> "ExecutionPlan":
        """Leitet den Plan ab. registry (optional): prüft vorab, dass jede benötigte
        Quellart, Operation und jedes Ausgabeformat registriert ist (ConfigError
        mit Position). release_intermediates (FA66): tiefensuchende ``order`` und
        gefüllte ``releases``; sonst Kahn-Reihenfolge und leere ``releases``."""
        layer_by_id = {layer.id: layer for layer in scenario.layers}
        step_by_id = {step.id: step for step in scenario.steps}

        full_order = topological_order(
            {step.id: set(step.inputs.values()) for step in scenario.steps}, step_by_id
        )

        # Erreichbarkeit vom Output rückwärts
        needed_steps: set[str] = set()
        needed_layers: set[str] = set()
        stack = [out.source for out in scenario.output if out.source]
        while stack:
            node = stack.pop()
            if node in layer_by_id:
                needed_layers.add(node)
            elif node in step_by_id and node not in needed_steps:
                needed_steps.add(node)
                stack.extend(step_by_id[node].inputs.values())

        # Referenz-Layer räumlicher Prüfungen (Fixpunkt, da Referenzen verkettet sein können)
        pending = list(needed_layers)
        while pending:
            layer = layer_by_id[pending.pop()]
            for reference in layer_dependencies(layer):
                if reference not in layer_by_id:
                    raise ConfigError(
                        [
                            (
                                f"layers -> {layer.id} -> validation -> spatial_check -> within",
                                f"verweist auf unbekannten Layer '{reference}'",
                            )
                        ]
                    )
                if reference not in needed_layers:
                    needed_layers.add(reference)
                    pending.append(reference)

        if release_intermediates:
            order = _depth_first_order(scenario, step_by_id, needed_steps)
        else:
            order = tuple(sid for sid in full_order if sid in needed_steps)

        # Ladeplan: Layer unmittelbar vor dem ersten Schritt, der ihn braucht; Referenz-Layer zuerst.
        loaded: set[str] = set()

        def plan_layer(
            layer_id: str, into: list[str], visiting: tuple[str, ...] = ()
        ) -> None:
            if layer_id in loaded:
                return
            if layer_id in visiting:
                chain = " -> ".join((*visiting, layer_id))
                raise ConfigError(
                    [
                        (
                            f"layers -> {layer_id} -> validation -> spatial_check -> within",
                            f"zyklische Referenz: {chain}",
                        )
                    ]
                )
            for reference in layer_dependencies(layer_by_id[layer_id]):
                plan_layer(reference, into, (*visiting, layer_id))
            into.append(layer_id)
            loaded.add(layer_id)

        loads: dict[str, tuple[str, ...]] = {}
        for step_id in order:
            into: list[str] = []
            for source_id in step_by_id[step_id].inputs.values():
                if source_id in layer_by_id:
                    plan_layer(source_id, into)
            if into:
                loads[step_id] = tuple(into)

        # Layer, die nur ein Output liest: nach dem letzten Schritt laden.
        final: list[str] = []
        for out in scenario.output:
            if out.source and out.source in layer_by_id:
                plan_layer(out.source, final)

        if loaded != needed_layers:
            raise RuntimeError(
                "Ausführungsplan inkonsistent: benötigte Layer "
                f"{sorted(needed_layers)} != geplante Ladungen {sorted(loaded)}"
            )

        releases: dict[str, tuple[str, ...]] = {}
        if release_intermediates:
            releases = _last_readers(
                scenario, order, loads, tuple(final), layer_by_id, step_by_id
            )

        plan = cls(
            full_order=tuple(full_order),
            order=order,
            loads=MappingProxyType(loads),
            final_loads=tuple(final),
            required_layers=frozenset(needed_layers),
            required_steps=frozenset(needed_steps),
            unused_layers=frozenset(layer_by_id) - frozenset(needed_layers),
            unused_steps=frozenset(step_by_id) - frozenset(needed_steps),
            releases=MappingProxyType(releases),
            attribution=MappingProxyType(
                attribution_of(scenario, order, needed_layers)
            ),
        )
        if registry is not None:
            plan._check_registered(scenario, registry)
        return plan

    def _check_registered(self, scenario: "Scenario", registry: "Registry") -> None:
        issues: list[tuple[str, str]] = []
        for index, layer in enumerate(scenario.layers):
            if layer.id in self.required_layers:
                try:
                    registry.source(layer.source)
                except ValueError as exc:
                    issues.append(
                        (f"layers -> {index} ({layer.id}) -> source", str(exc))
                    )
        for index, step in enumerate(scenario.steps):
            if step.id in self.required_steps:
                try:
                    registry.operation(step.op)
                except ValueError as exc:
                    issues.append((f"steps -> {index} ({step.id}) -> op", str(exc)))
        for index, out in enumerate(scenario.output):
            try:
                registry.output(out.type)
            except ValueError as exc:
                issues.append((f"output -> {index} -> type", str(exc)))
        if issues:
            raise ConfigError(issues)
