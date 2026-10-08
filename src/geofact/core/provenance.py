"""Implements: FA65 (Lizenzen und Namensnennung durch den DAG), FA73 (Ausgabelizenz), FA74 (Herkunft eines Ergebnisses für den RDF-Export).

Die Attribution eines Knotens ist eine reine Funktion aus Konfiguration und Plan:
ein Layer trägt seine wirksame Lizenz (``LayerBase.effective_license``), ein
Schritt die Vereinigung seiner Eingänge (ohne Duplikate, in Reihenfolge des
ersten Auftretens, Eingänge nach Portnamen sortiert). Layer ohne Lizenz erscheinen
unter ``undeclared``, sichtbar statt still weggelassen. Ein Schritt, der Daten von
außen einbringt (``Step.brings_external_data``, z. B. op: rest), trägt zusätzlich
deren Lizenz bzw. steht selbst unter ``undeclared``. Nur benötigte Layer und
ausgeführte Schritte tragen bei (Lazy Loading).

FA73: ``OutputLicense`` ist die Lizenz, die der Nutzer auf ein abgeleitetes Ergebnis
legt (``output[i].license`` oder ``scenario.output_license``), samt Prüfvermerk;
eine Angabe auf eigenes Risiko, keine Rechtsauskunft.

FA74: ``RunLineage`` hält, woraus ein Lauf seine Ergebnisse gemacht hat: je
benötigtem Layer Quellart, Parameter, Lizenz und CRS-Provenienz (FA58), je
ausgeführtem Schritt Operation, Parameter und Eingänge, dazu die Laufmetadaten.
Reine Daten; Ausgabeformate lesen sie über ``OutputSpec.lineage``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Literal, Mapping, Sequence

from geofact.core.contracts import License
from geofact.core.types import LayerProvenance

if TYPE_CHECKING:
    from geofact.core.scenario import Scenario


@dataclass(frozen=True)
class NodeAttribution:
    """Lizenzen eines Knotens (Layer oder Schritt) und die Layer ohne Lizenz."""

    licenses: tuple[License, ...] = ()
    undeclared: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """Keine Lizenz bekannt (auch dann, wenn Layer ohne Lizenz beitragen)."""
        return not self.licenses

    def lines(self) -> list[str]:
        """Eine Zeile Namensnennung je Lizenz (``License.line``)."""
        return [license_.line() for license_ in self.licenses]

    def to_dict(self) -> dict[str, Any]:
        return {
            "licenses": [license_.to_dict() for license_ in self.licenses],
            "undeclared": list(self.undeclared),
            "lines": self.lines(),
        }


def _unique(items: Iterable[Any]) -> tuple[Any, ...]:
    seen: list[Any] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def union(parts: Iterable[NodeAttribution]) -> NodeAttribution:
    """Vereinigung ohne Duplikate in Reihenfolge des ersten Auftretens."""
    materialised = list(parts)
    return NodeAttribution(
        licenses=_unique(lic for part in materialised for lic in part.licenses),
        undeclared=_unique(layer for part in materialised for layer in part.undeclared),
    )


def attribution_of(
    scenario: "Scenario", order: Sequence[str], required_layers: Iterable[str]
) -> dict[str, NodeAttribution]:
    """Attribution je benötigtem Layer und je Schritt in ``order``. ``order``
    ist topologisch (jeder Eingang eines Schritts steht vor ihm oder ist ein
    Layer); unreferenzierte Layer und nicht ausgeführte Schritte fehlen."""
    layer_by_id = {layer.id: layer for layer in scenario.layers}
    step_by_id = {step.id: step for step in scenario.steps}
    result: dict[str, NodeAttribution] = {}
    for layer_id in sorted(required_layers):
        license_ = layer_by_id[layer_id].effective_license()
        result[layer_id] = (
            NodeAttribution(licenses=(license_,))
            if license_ is not None
            else NodeAttribution(undeclared=(layer_id,))
        )
    for step_id in order:
        step = step_by_id[step_id]
        parts = [
            result[source]
            for _port, source in sorted(step.inputs.items())
            if source in result
        ]
        if step.brings_external_data():
            # Externe Daten wie ein Layer: deklarierte Lizenz oder sichtbar ohne
            # Angabe, nie still nur die der Eingänge.
            external = step.external_license()
            parts.append(
                NodeAttribution(licenses=(external,))
                if external is not None
                else NodeAttribution(undeclared=(step_id,))
            )
        result[step_id] = union(parts)
    return result


# --- FA73: Lizenz des abgeleiteten Ergebnisses ---

OUTPUT_LICENSE_PREFIX = "Lizenz dieses Ergebnisses"
"""Anfang der Zeile, mit der jede Ausgabe ihre Ausgabelizenz nennt (FA73)."""

UNCHECKED = "ungeprüft"
"""Prüfvermerk ohne (zwischengespeicherte) Kompatibilitätsprüfung."""


@dataclass(frozen=True)
class OutputLicense:
    """Die Lizenz, die der Nutzer auf ein abgeleitetes Ergebnis legt (FA73).

    ``declared_at``: ``output`` (``output[i].license``) oder ``scenario``
    (Default ``scenario.output_license``). ``check``: Prüfvermerk, z. B.
    ``geprüft am 2026-10-03: vereinbar (DALICC)``; None = ungeprüft. GeoFACT
    prüft beim Lauf nie selbst über das Netz - der Vermerk stammt aus einer
    zuvor ausdrücklich ausgeführten und zwischengespeicherten Prüfung."""

    license: License
    declared_at: Literal["output", "scenario"] = "output"
    check: str | None = None

    @property
    def status(self) -> str:
        return self.check or UNCHECKED

    def line(self) -> str:
        """``Lizenz dieses Ergebnisses: CC-BY-4.0 (...) (vom Nutzer festgelegt, ungeprüft)``."""
        return (
            f"{OUTPUT_LICENSE_PREFIX}: {self.license.line()} "
            f"(vom Nutzer festgelegt, {self.status})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "license": self.license.to_dict(),
            "declared_at": self.declared_at,
            "check": self.check,
            "line": self.line(),
        }


# --- FA74: Herkunft eines Laufs (Lineage) ---


@dataclass(frozen=True)
class LineageNode:
    """Ein Knoten des Laufs: benötigter Layer oder ausgeführter Schritt.

    ``block``: Quellart (Layer) bzw. Operationsname (Schritt). ``parameters``:
    JSON-taugliche Parameter - beim Layer die deklarierten Felder ohne ``id``,
    ``source`` und ``license`` (z. B. OSM-Tags, Dateipfad wie in der YAML,
    URL), beim Schritt die geprüften ``params`` inklusive Defaults.
    ``inputs``: (Port, Knoten) nach Port sortiert (nur Schritte). ``license``:
    wirksame Lizenz des Layers bzw. eines von außen einbringenden Schritts.
    ``provenance``: CRS-Provenienz des geladenen Layers (FA58). ``warnings``:
    Warnungen, die beim Laden bzw. Ausführen dieses Knotens auftraten."""

    id: str
    kind: Literal["layer", "step"]
    block: str
    parameters: Mapping[str, Any]
    inputs: tuple[tuple[str, str], ...] = ()
    license: License | None = None
    provenance: LayerProvenance | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunLineage:
    """Herkunft eines Laufs (FA74): Knoten und Laufmetadaten.

    ``started_at``/``finished_at`` sind UTC-Zeitstempel (ISO 8601) - die
    einzigen Angaben (neben ``run_id``, der eindeutigen Laufkennung), die
    zwischen zwei gleichen Läufen abweichen."""

    scenario_name: str
    region: str
    working_crs: str | None
    started_at: str | None
    finished_at: str | None
    version: str
    nodes: Mapping[str, LineageNode]
    run_id: str | None = None

    def ancestors(self, node_id: str) -> tuple[str, ...]:
        """``node_id`` und alle Knoten, aus denen er entstand, sortiert nach
        Id (deterministisch). Ein unbekannter Knoten ist ein ``KeyError``."""
        if node_id not in self.nodes:
            raise KeyError(f"Knoten '{node_id}' ist nicht Teil des Laufs")
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            current = stack.pop()
            if current in seen or current not in self.nodes:
                continue
            seen.add(current)
            stack.extend(source for _port, source in self.nodes[current].inputs)
        return tuple(sorted(seen))


_LAYER_FIELDS_NOT_PARAMETERS = frozenset({"id", "source", "license"})


def layer_parameters(layer: Any) -> dict[str, Any]:
    """Deklarierte Felder eines Layers als JSON-taugliches Dict (FA74), ohne
    ``id``, ``source``, ``license`` und ohne Felder mit Wert None."""
    dumped = layer.model_dump(mode="json", exclude_none=True)
    return {
        key: value
        for key, value in dumped.items()
        if key not in _LAYER_FIELDS_NOT_PARAMETERS
    }


def lineage_nodes(
    scenario: "Scenario",
    order: Sequence[str],
    required_layers: Iterable[str],
    provenance: Mapping[str, LayerProvenance] | None = None,
    warnings_by_node: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, LineageNode]:
    """Knoten der Herkunft (FA74) für die benötigten Layer und die Schritte
    in ``order`` - dieselbe Auswahl wie ``attribution_of`` (Lazy Loading)."""
    provenance = provenance or {}
    warnings_by_node = warnings_by_node or {}
    layer_by_id = {layer.id: layer for layer in scenario.layers}
    step_by_id = {step.id: step for step in scenario.steps}
    nodes: dict[str, LineageNode] = {}
    for layer_id in sorted(required_layers):
        layer = layer_by_id[layer_id]
        nodes[layer_id] = LineageNode(
            id=layer_id,
            kind="layer",
            block=str(layer.source),
            parameters=layer_parameters(layer),
            license=layer.effective_license(),
            provenance=provenance.get(layer_id),
            warnings=tuple(warnings_by_node.get(layer_id, ())),
        )
    for step_id in order:
        step = step_by_id[step_id]
        nodes[step_id] = LineageNode(
            id=step_id,
            kind="step",
            block=step.op,
            parameters=dict(step.params or {}),
            inputs=tuple(sorted(step.inputs.items())),
            license=step.external_license() if step.brings_external_data() else None,
            warnings=tuple(warnings_by_node.get(step_id, ())),
        )
    return nodes
