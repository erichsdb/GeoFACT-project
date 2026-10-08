"""Implements: FA12, FA13 (Ausgaben schreiben), FA2 (Dateiname über OutputSpec.target_name), FA58 (Provenienz an jede Ausgabe), FA65 (Attribution an jede Ausgabe, ATTRIBUTION.txt), FA73 (Ausgabelizenz an jede Ausgabe), FA74 (Herkunft des Laufs an jede Ausgabe).

Schreibt die im Scenario deklarierten output-Spezifikationen: aus einem
ExecutionResult entstehen alle Ausgaben in einem Zielverzeichnis; der Rückgabewert
nennt die erzeugten Pfade (Grundlage des Download-Endpunkts der Web-Schicht).

- FA40: Welche Formate es gibt, entscheidet die Tabelle 'output' der Registry; ein
  neues Ausgabeformat ist eine neue Datei mit @register_output.
- Isolation (FA12): jede Ausgabe wird für sich geschrieben. Scheitert ein Schreiber
  (mit welcher Ausnahme auch), wird genau diese Ausgabe als übersprungen mit Grund
  gemeldet, eine halb geschriebene Datei entfernt und mit den übrigen weitergemacht.
- Dateiname (FA2): ``OutputSpec.target_name(index, extension)``, dieselbe Regel, mit
  der ``validate_dag`` doppelte Namen ablehnt.
- Provenienz und Lizenzen (FA58/FA65/FA73/FA74): vor jedem ``plugin.write`` bindet
  diese Datei ``spec.provenance``, ``spec.attribution``, ``spec.output_license``
  (Prüfvermerk aus der Ablage, nie Netz) und ``spec.lineage`` an die Ausgabe; die
  Writer-Signatur ``(data, target, spec)`` bleibt. Danach entsteht ``ATTRIBUTION.txt``
  in ``out_dir``, nur wenn eine geschriebene Ausgabe eine Quell- oder Ausgabelizenz
  trägt; sie steht in ``WriteOutputsResult.attribution_file``, nicht in ``written``.
  Scheitert ihr Schreiben, ist das eine ``AttributionFileWarning``, kein Abbruch.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from geofact.core.errors import OutputSkipped
from geofact.core.provenance import (
    NodeAttribution,
    OutputLicense,
    RunLineage,
    attribution_of,
    lineage_nodes,
)
from geofact.core.registry import Registry, default_registry
from geofact.core.scenario import Scenario
from geofact.core.types import data_type_of
from geofact.engine.executor import ExecutionResult
from geofact.engine.licenses import output_license_for

_TYPE_LABELS = {"vector": "Vektor", "raster": "Raster", "graph": "Graph"}

ATTRIBUTION_FILE = "ATTRIBUTION.txt"
"""Name der Namensnennungsdatei eines Laufs (FA65)."""


class AttributionFileWarning(UserWarning):
    """``ATTRIBUTION.txt`` konnte nicht geschrieben werden (FA65-Nachbedingung 5)."""


@dataclass
class SkippedOutput:
    """Eine deklarierte Ausgabe, die NICHT geschrieben wurde (Regel 7:
    kein stiller Datenverlust - der Aufrufer soll das sehen können)."""

    index: int
    source: str | None
    type: str
    reason: str


@dataclass
class WriteOutputsResult:
    """Ergebnis von write_outputs(): geschriebene Pfade PLUS alle
    uebersprungenen/verworfenen Ausgaben mit Begründung, statt sie
    kommentarlos verschwinden zu lassen. ``attribution_file``: Pfad der
    ``ATTRIBUTION.txt`` (FA65) oder None (keine Lizenz, Schreibfehler)."""

    written: list[Path] = field(default_factory=list)
    skipped: list[SkippedOutput] = field(default_factory=list)
    attribution_file: Path | None = None


def _safe_name(spec, index: int, registry: Registry) -> str:
    """Dateiname einer Ausgabe nach ``OutputSpec.target_name`` (FA2)."""
    # Ein Format mit deklarationsabhängiger Endung (type: rest, FA43) führt sie als Option.
    ext = (
        getattr(spec.options, "extension", None) or registry.output(spec.type).extension
    )
    return spec.target_name(index, ext)


def _remove_partial(target: Path) -> None:
    """Entfernt eine halb geschriebene Ausgabedatei (nur Dateien); ein Fehler dabei
    darf die übrigen Ausgaben nicht verhindern (FA12)."""
    try:
        if target.is_file() or target.is_symlink():
            target.unlink()
    except OSError:
        pass


def _attributions(
    scenario: Scenario, result: ExecutionResult
) -> dict[str, NodeAttribution]:
    """Attribution je Knoten aus dem Plan des Laufs, ohne Plan aus Reihenfolge und
    geladenen Layern."""
    if result.plan is not None:
        return dict(result.plan.attribution)
    step_ids = {step.id for step in scenario.steps}
    order = [node for node in result.order if node in step_ids]
    return attribution_of(scenario, order, result.loaded_layers)


def geofact_version() -> str:
    """Installierte GeoFACT-Version (FA74) oder ``unbekannt``."""
    try:
        return metadata.version("geofact")
    except metadata.PackageNotFoundError:
        return "unbekannt"


def run_lineage(scenario: Scenario, result: ExecutionResult) -> RunLineage:
    """FA74: Herkunft des Laufs aus Szenario, Plan und Ergebnis (mit Warnungen je Knoten)."""
    warnings_by_node: dict[str, list[str]] = {}
    for warning in result.warnings:
        node = warning.layer or warning.step
        if node:
            warnings_by_node.setdefault(node, []).append(
                f"{warning.category}: {warning.message}"
            )
    step_ids = {step.id for step in scenario.steps}
    order = [node for node in result.order if node in step_ids]
    nodes = lineage_nodes(
        scenario,
        order,
        result.loaded_layers,
        provenance=result.provenance,
        warnings_by_node=warnings_by_node,
    )
    working_crs = None
    if result.region is not None:
        working_crs = str(result.region.to_dict().get("crs"))
    elif result.provenance:
        working_crs = next(iter(result.provenance.values())).target_crs
    return RunLineage(
        scenario_name=scenario.scenario.name,
        region=scenario.scenario.region,
        working_crs=working_crs,
        started_at=result.started_at,
        finished_at=result.finished_at,
        run_id=result.run_id,
        version=geofact_version(),
        nodes=nodes,
    )


AttributionEntry = tuple[Path, str, NodeAttribution, "OutputLicense | None"]


def _attribution_text(entries: list[AttributionEntry]) -> str:
    """Inhalt von ``ATTRIBUTION.txt``: je Ausgabe ein Block mit Dateiname, Quelle,
    Lizenzzeilen, Layern ohne Lizenz und, falls deklariert, der Ausgabelizenz (FA73)."""
    lines = ["Namensnennung und Lizenzen der Ausgaben dieses Laufs (GeoFACT, FA65)", ""]
    for target, source, attribution, output_license in entries:
        lines.append(f"{target.name} (Quelle: {source})")
        lines.extend(f"  {line}" for line in attribution.lines())
        if attribution.undeclared:
            lines.append(f"  ohne Lizenzangabe: {', '.join(attribution.undeclared)}")
        if output_license is not None:
            lines.append(f"  {output_license.line()}")
        lines.append("")
    return "\n".join(lines)


def _carries_license(entry: AttributionEntry) -> bool:
    return not entry[2].empty or entry[3] is not None


def _write_attribution_file(
    out_dir: Path, entries: list[AttributionEntry], written: list[Path]
) -> Path | None:
    """``ATTRIBUTION.txt`` (FA65): nur, wenn eine geschriebene Ausgabe eine Lizenz
    trägt; ein Fehler ist eine Warnung. Ohne Datei wird eine alte aus einem
    wiederverwendeten ``out_dir`` entfernt, sie nennte Lizenzen eines früheren Laufs."""
    target = out_dir / ATTRIBUTION_FILE
    if any(path.name.casefold() == ATTRIBUTION_FILE.casefold() for path in written):
        if any(_carries_license(entry) for entry in entries):
            warnings.warn(
                f"{ATTRIBUTION_FILE} nicht geschrieben: eine Ausgabe trägt bereits diesen "
                "Dateinamen - path dieser Ausgabe ändern",
                AttributionFileWarning,
                stacklevel=3,
            )
        return None  # die Datei ist eine Ausgabe dieses Laufs, nicht anfassen
    if not any(_carries_license(entry) for entry in entries):
        _discard_stale(target)
        return None
    try:
        target.write_text(_attribution_text(entries), encoding="utf-8")
    except OSError as exc:
        warnings.warn(
            f"{ATTRIBUTION_FILE} konnte nicht geschrieben werden ({type(exc).__name__}): {exc}",
            AttributionFileWarning,
            stacklevel=3,
        )
        _discard_stale(target)
        return None
    return target


def _discard_stale(target: Path) -> None:
    """Eine alte ``ATTRIBUTION.txt`` entfernen; scheitert das, eine Warnung."""
    if not target.is_file():
        return
    try:
        target.unlink()
    except OSError as exc:
        warnings.warn(
            f"veraltete {ATTRIBUTION_FILE} aus einem früheren Lauf konnte nicht entfernt werden "
            f"({type(exc).__name__}): {exc} - sie gilt nicht für die Ausgaben dieses Laufs",
            AttributionFileWarning,
            stacklevel=4,
        )


def write_outputs(
    scenario: Scenario,
    result: ExecutionResult,
    out_dir: str | Path,
    registry: Registry | None = None,
) -> WriteOutputsResult:
    """Schreibt alle deklarierten Ausgaben nach out_dir.

    Nachbedingung: `written` enthält die tatsächlich geschriebenen Pfade, `skipped`
    jede nicht geschriebene Ausgabe (fehlende Quelle im Store, falscher Datentyp,
    Schreibfehler) mit Grund, damit ein Aufrufer Unvollständiges nicht übersieht
    (Goldene Regel 7). registry (FA40): None = Standard-Registry."""
    registry = registry if registry is not None else default_registry()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    provenance = getattr(result, "provenance", None)
    attributions = _attributions(scenario, result)
    lineage = run_lineage(scenario, result)
    written: list[Path] = []
    skipped: list[SkippedOutput] = []
    attributed: list[AttributionEntry] = []
    for index, spec in enumerate(scenario.output):
        source_id = spec.source
        data = result.store.get(source_id) if source_id else None
        if data is None:
            # Quelle nicht im Store: überspringen statt abbrechen, aber sichtbar
            # protokollieren.
            skipped.append(
                SkippedOutput(
                    index=index,
                    source=source_id,
                    type=spec.type,
                    reason=f"Quelle '{source_id}' hat kein Ergebnis im Store.",
                )
            )
            continue

        plugin = registry.output(spec.type)
        try:
            actual = data_type_of(data)
        except TypeError as exc:
            skipped.append(
                SkippedOutput(
                    index=index,
                    source=source_id,
                    type=spec.type,
                    reason=f"Quelle '{source_id}' ist kein Layer: {exc}",
                )
            )
            continue
        if actual not in plugin.accepts:
            expected = " oder ".join(
                sorted(_TYPE_LABELS[t.value] for t in plugin.accepts)
            )
            skipped.append(
                SkippedOutput(
                    index=index,
                    source=source_id,
                    type=spec.type,
                    reason=f"Ausgabetyp '{spec.type}' erwartet einen {expected}-Layer, "
                    f"Quelle '{source_id}' ist {type(data).__name__}.",
                )
            )
            continue

        target = out_dir / _safe_name(spec, index, registry)
        # Provenienz und Lizenzen vor dem Schreiben binden (Writer-Signatur bleibt).
        attribution = attributions.get(source_id)
        output_license = output_license_for(scenario, result.plan, spec)
        spec.bind_provenance(provenance)
        spec.bind_attribution(attribution)
        spec.bind_output_license(output_license)
        spec.bind_lineage(lineage)
        try:
            plugin.write(data, target, spec)
        except OutputSkipped as exc:
            # Das Format kann dieses Ergebnis nicht schreiben (z. B. RDF ohne CRS):
            # nur diese Ausgabe entfällt, sichtbar (Goldene Regel 7).
            _remove_partial(target)
            skipped.append(
                SkippedOutput(
                    index=index,
                    source=source_id,
                    type=spec.type,
                    reason=str(exc),
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001 - jeder Schreibfehler isoliert diese eine Ausgabe
            _remove_partial(target)
            skipped.append(
                SkippedOutput(
                    index=index,
                    source=source_id,
                    type=spec.type,
                    reason=f"Schreiben fehlgeschlagen ({type(exc).__name__}): {exc}",
                )
            )
            continue
        written.append(target)
        if attribution is not None or output_license is not None:
            attributed.append(
                (
                    target,
                    source_id,
                    attribution or NodeAttribution(),
                    output_license,
                )
            )

    attribution_file = _write_attribution_file(out_dir, attributed, written)
    return WriteOutputsResult(
        written=written, skipped=skipped, attribution_file=attribution_file
    )
