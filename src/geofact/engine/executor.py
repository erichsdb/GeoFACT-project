"""Implements: FA9 (DAG-Executor mit Lazy Loading), FA40 (Aufruf über Verzeichnisse), FA55 (Arbeits-CRS, region_ready), FA57 (CRS-Konsistenz je Schritt), FA58 (Provenienz im Ergebnis), FA65 (Attribution in plan/run_done), FA66 (Freigabe nach dem letzten Leser), FA74 (Start- und Endzeit des Laufs).

Führt ein Scenario nach seinem ExecutionPlan (core/plan.py) aus: Schritte in
topologischer Reihenfolge, jeder Layer erst unmittelbar vor dem ersten Schritt,
der ihn braucht (``plan.loads``); Layer, die nur ein Output liest, nach dem letzten
Schritt (``plan.final_loads``); nie referenzierte Layer werden nie geladen
(Lazy Loading by design). Das Laden selbst macht engine/loading.py. Schritt-Outputs
landen ebenfalls im LayerStore, unter dem Step-Namen.

Vertrag:

- Nachbedingung je Schritt: ``data_type_of(Ergebnis) == step.output_type()``,
  sonst ``StepExecutionError``. Fehler in Schritt oder Layer werden als
  ``StepExecutionError`` bzw. ``LayerLoadError`` mit Ursache per ``from`` weitergegeben.
- Abbruch: ein ``CancelToken`` wird an jeder Layer- und Schrittgrenze und nach der
  letzten Ladung geprüft (``RunCancelled``, ``RUN_CANCELLED``).
- Invariante am Ende: geladen == benötigt (Plan), jeder Layer genau einmal.
- Warnungen werden je Layer/Schritt abgefangen (engine/events.py), als Ereignisse
  gemeldet und im Ergebnis gesammelt.
- Arbeits-CRS (FA55): ``target_crs`` oder ohne Angabe die UTM-Zone der Region (FA5).
  Mit ``region_info`` folgen vor dem Plan ``REGION_READY`` und je Regionswarnung
  ``REGION_WARNING``.
- CRS-Konsistenz (FA57): alle Eingänge einer Operation liegen in einem CRS
  (Eingänge ohne CRS zählen nicht), sonst ``StepExecutionError``; weicht es vom
  Arbeits-CRS ab, gibt es eine ``ForeignCrsWarning``.
- Provenienz (FA58): ``ExecutionResult.provenance`` deckt genau die geladenen Layer ab.
- Attribution (FA65): in ``PLAN.detail`` je Knoten, in ``RUN_DONE.detail`` die
  Vereinigung der Ausgabequellen.
- Freigabe (FA66, opt-in ``release_intermediates``): nach jedem Schritt werden die
  Knoten aus ``plan.releases[step]`` aus dem Store entfernt.

FA40: Der Executor kennt weder Quellarten noch Operationen; er schlägt sie in der
Registry nach, Kernbausteine und Plugins gleich. Welche Bausteine sie enthält,
entscheidet die explizite Erkennung (``geofact.api.load_extensions()``).
"""

from __future__ import annotations

import time
import uuid
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import networkx as nx
import pandas as pd
from geopandas import GeoDataFrame
from pyproj import CRS

from geofact.core.contracts import LoadContext
from geofact.core.errors import LayerLoadError, RunCancelled, StepExecutionError
from geofact.core.plan import ExecutionPlan
from geofact.core.provenance import NodeAttribution, union
from geofact.core.registry import Registry, default_registry
from geofact.core.scenario import Scenario
from geofact.core.store import LayerStore
from geofact.core.types import LayerProvenance, data_type_of
from geofact.engine import events as _ev
from geofact.engine import region as region_module
from geofact.engine.events import (
    CancelToken,
    ProgressCallback,
    ProgressEvent,
    RunWarning,
)
from geofact.engine.loading import (
    ConnectorOverride,
    layer_loaded_detail,
    layer_loading_detail,
    load_layer,
    warning_severity,
)

if TYPE_CHECKING:
    from geofact.engine.region import RegionInfo

REGION_WARNING_CATEGORY = "RegionWarning"


class ForeignCrsWarning(UserWarning):
    """Ein Eingang eines Schritts liegt nicht im Arbeits-CRS des Laufs (FA57)."""


@dataclass
class ExecutionResult:
    store: LayerStore
    order: list[str]
    loaded_layers: list[str] = field(default_factory=list)
    plan: ExecutionPlan | None = None
    warnings: list[RunWarning] = field(default_factory=list)
    provenance: dict[str, LayerProvenance] = field(default_factory=dict)
    region: "RegionInfo | None" = None
    released: list[str] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    run_id: str | None = None


def utc_now() -> str:
    """UTC-Zeitstempel (ISO 8601, Sekunden) für Start und Ende eines Laufs (FA74)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def crs_of(data: object) -> CRS | None:
    """CRS eines Layer-Objekts (FA57): ``GeoDataFrame.crs``, ``RasterLayer.crs``,
    ``Graph.graph["crs"]``; Tabellen und Objekte ohne CRS liefern None."""
    if isinstance(data, GeoDataFrame):
        raw: Any = data.crs
    elif isinstance(data, nx.Graph):
        raw = data.graph.get("crs")
    elif isinstance(data, pd.DataFrame):
        return None  # Tabelle ohne Geometrie (eine Spalte 'crs' ist kein CRS)
    else:
        raw = getattr(data, "crs", None)
    if raw is None:
        return None
    try:
        return raw if isinstance(raw, CRS) else CRS.from_user_input(raw)
    except Exception:  # noqa: BLE001 - ein unlesbares CRS wird nicht verglichen
        return None


def _crs_text(crs: CRS) -> str:
    authority = crs.to_authority()
    return f"{authority[0]}:{authority[1]}" if authority else crs.to_string()


def check_input_crs(inputs: dict[str, object], working_crs: CRS | None) -> None:
    """FA57: alle Eingänge mit CRS liegen in einem CRS, sonst ``TypeError`` (der
    Executor macht daraus vor dem Aufruf einen ``StepExecutionError``). Ein
    abweichendes Arbeits-CRS ist eine ``ForeignCrsWarning``."""
    found: dict[str, CRS] = {}
    for port, data in inputs.items():
        crs = crs_of(data)
        if crs is not None:
            found[port] = crs
    if not found:
        return
    distinct: list[CRS] = []
    for crs in found.values():
        if not any(crs == seen for seen in distinct):
            distinct.append(crs)
    if len(distinct) > 1:
        listing = ", ".join(
            f"{port}={_crs_text(crs)}" for port, crs in sorted(found.items())
        )
        raise TypeError(
            f"Eingänge liegen in verschiedenen CRS: {listing} - FA5-Nachbedingung verletzt "
            "(Quelle mit Transformation 'none' oder Plugin-Ergebnis in eigenem CRS?)"
        )
    if working_crs is not None and distinct[0] != working_crs:
        ports = ", ".join(sorted(found))
        warnings.warn(
            f"Eingang {ports} liegt in {_crs_text(distinct[0])}, das Arbeits-CRS des Laufs "
            f"ist {_crs_text(working_crs)} - die Operation rechnet in den Einheiten des "
            "fremden CRS",
            ForeignCrsWarning,
            stacklevel=2,
        )


def output_attribution(scenario: Scenario, plan: ExecutionPlan) -> NodeAttribution:
    """FA65: Vereinigung der Attribution aller Ausgabequellen (Reihenfolge der
    Ausgaben, ohne Duplikate)."""
    return union(
        plan.attribution[out.source]
        for out in scenario.output
        if out.source and out.source in plan.attribution
    )


def execute(
    scenario: Scenario,
    plan: ExecutionPlan,
    *,
    registry: Registry,
    region: GeoDataFrame,
    observer: ProgressCallback | None = None,
    cancel: CancelToken | None = None,
    store: LayerStore | None = None,
    base_dir: Path | None = None,
    refresh_snapshots: bool = False,
    connector_override: ConnectorOverride | None = None,
    target_crs: CRS | None = None,
    region_info: "RegionInfo | None" = None,
    release_intermediates: bool = False,
    region_fetcher: region_module.Fetcher | None = None,
    connector_options: Mapping[str, str] | None = None,
) -> ExecutionResult:
    """Führt ``plan`` aus (Vertrag: Modul-Docstring). region: aufgelöste Region
    (EPSG:4326). target_crs: Arbeits-CRS (FA55); None = ``region_info.crs``, sonst
    die UTM-Zone der Region. release_intermediates (FA66): der Plan muss mit
    ``release_intermediates=True`` abgeleitet sein. region_fetcher: Nahtstelle für
    Layer mit eigener Region (FA64)."""
    emit = observer or _ev._noop
    started_at = utc_now()
    run_id = uuid.uuid4().hex
    layer_by_id = {layer.id: layer for layer in scenario.layers}
    step_by_id = {step.id: step for step in scenario.steps}
    store = store if store is not None else LayerStore()
    loaded_layers: list[str] = []
    run_warnings: list[RunWarning] = []
    provenance: dict[str, LayerProvenance] = {}
    released: list[str] = []

    if target_crs is None:
        target_crs = (
            region_info.crs
            if region_info is not None
            else region_module.utm_zone_epsg(region)
        )

    # Ein Kontext je Lauf, nicht je Layer.
    ctx = LoadContext(
        region=region,
        target_crs=target_crs,
        refresh=refresh_snapshots,
        base_dir=base_dir,
        registry=registry,
        densify_km=getattr(scenario.scenario, "densify_km", None),
        # FA82: Laufzeitoptionen der Konnektoren, unverändert.
        options=dict(connector_options or {}),
    )

    def check_cancel() -> None:
        if cancel is not None and cancel.cancelled:
            emit(ProgressEvent(type=_ev.RUN_CANCELLED))
            raise RunCancelled("Lauf abgebrochen")

    def load_into_store(layer_id: str, step_id: str | None) -> None:
        check_cancel()
        layer = layer_by_id[layer_id]
        emit(
            ProgressEvent(
                type=_ev.LAYER_LOADING,
                layer=layer_id,
                step=step_id,
                detail=layer_loading_detail(layer),
            )
        )
        load_started = time.perf_counter()

        def observe(event: ProgressEvent) -> None:
            if event.type == _ev.LAYER_WARNING:
                run_warnings.append(
                    RunWarning(
                        message=event.message or "",
                        category=event.detail.get("category", ""),
                        layer=layer_id,
                        step=step_id,
                        severity=event.detail.get("severity", "warning"),
                    )
                )
            emit(event)

        try:
            store[layer_id] = load_layer(
                layer,
                ctx,
                registry,
                observe,
                step=step_id,
                override=(connector_override or {}).get(layer_id),
                store=store,
                provenance_sink=provenance,
                region_fetcher=region_fetcher,
            )
        except LayerLoadError as exc:
            emit(
                ProgressEvent(
                    type=_ev.RUN_ERROR,
                    layer=layer_id,
                    step=step_id,
                    error=str(exc),
                )
            )
            raise
        loaded_layers.append(layer_id)
        loaded_event = ProgressEvent(
            type=_ev.LAYER_LOADED,
            layer=layer_id,
            step=step_id,
            duration_ms=(time.perf_counter() - load_started) * 1000,
        )
        loaded_event.detail.update(layer_loaded_detail(layer_id, provenance))
        emit(loaded_event)

    def run_step(step_id: str) -> None:
        check_cancel()
        step = step_by_id[step_id]
        emit(ProgressEvent(type=_ev.STEP_RUNNING, step=step_id, op=step.op))
        started = time.perf_counter()
        failure: StepExecutionError | None = None
        origin = ""
        with _ev.collect_warnings() as caught:
            try:
                entry = registry.operation(step.op)
                origin = entry.origin
                inputs = {
                    port: store[source_id] for port, source_id in step.inputs.items()
                }
                # FA57: gemischte CRS scheitern VOR dem Aufruf der Operation.
                check_input_crs(inputs, ctx.target_crs)
                result = entry.run(inputs, step.params)
                # Nachbedingung (FA9): sonst scheitert erst der nächste Schritt
                # oder das Schreiben mit einer unverständlichen Meldung.
                actual = data_type_of(result)
                expected = step.output_type()
                if actual != expected:
                    raise TypeError(
                        f"Ergebnis ist {actual.value}, der Vertrag der Operation "
                        f"verspricht {expected.value}"
                    )
                store[step_id] = result
            except Exception as exc:  # noqa: BLE001 - wird gemeldet und als StepExecutionError weitergereicht
                failure = StepExecutionError(step_id, step.op, exc, origin=origin)
                failure.__cause__ = exc
        duration_ms = (time.perf_counter() - started) * 1000

        # Warnungen der Operation als Ereignisse melden (auch bei einem Fehler),
        # statt sie stumm auf stderr zu verlieren.
        for message, category in caught:
            severity = warning_severity(category)
            run_warnings.append(
                RunWarning(
                    message=message,
                    category=category,
                    step=step_id,
                    severity=severity,
                )
            )
            emit(
                ProgressEvent(
                    type=_ev.STEP_WARNING,
                    step=step_id,
                    op=step.op,
                    message=message,
                    detail={"category": category, "severity": severity},
                )
            )

        if failure is not None:
            cause = failure.cause
            emit(
                ProgressEvent(
                    type=_ev.STEP_ERROR,
                    step=step_id,
                    op=step.op,
                    error=f"{type(cause).__name__}: {cause}",
                    duration_ms=duration_ms,
                )
            )
            emit(ProgressEvent(type=_ev.RUN_ERROR, step=step_id, error=str(failure)))
            raise failure

        # FA66: Knoten ohne späteren Leser verlassen den Store.
        released_now: list[str] = []
        if release_intermediates:
            for node in plan.releases.get(step_id, ()):
                if node in store:
                    store.pop(node)
                    released_now.append(node)
            released.extend(released_now)
        emit(
            ProgressEvent(
                type=_ev.STEP_DONE,
                step=step_id,
                op=step.op,
                output_type=step.output_type().value,
                duration_ms=duration_ms,
                detail={"released": released_now} if released_now else {},
            )
        )

    # FA55/FA56: Region und Arbeits-CRS, bevor der Plan beginnt.
    if region_info is not None:
        emit(ProgressEvent(type=_ev.REGION_READY, detail=region_info.to_dict()))
        for message in region_info.warnings:
            run_warnings.append(
                RunWarning(message=message, category=REGION_WARNING_CATEGORY)
            )
            emit(
                ProgressEvent(
                    type=_ev.REGION_WARNING,
                    message=message,
                    detail={"category": REGION_WARNING_CATEGORY, "severity": "warning"},
                )
            )

    emit(
        ProgressEvent(
            type=_ev.PLAN,
            detail={
                "order": list(plan.order),
                "steps": {
                    sid: {
                        "op": step_by_id[sid].op,
                        "inputs": dict(step_by_id[sid].inputs),
                        "output_type": step_by_id[sid].output_type().value,
                    }
                    for sid in plan.order
                },
                "layers": sorted(plan.required_layers),
                "unused_layers": sorted(plan.unused_layers),
                "load_schedule": plan.schedule(),
                "final_loads": list(plan.final_loads),
                "attribution": {
                    node: attribution.to_dict()
                    for node, attribution in plan.attribution.items()
                },
            },
        )
    )

    for step_id in plan.order:
        for layer_id in plan.loads.get(step_id, ()):
            load_into_store(layer_id, step_id)
        run_step(step_id)
    # Layer, die nur ein Output liest: erst jetzt, aber vor den Ausgaben.
    for layer_id in plan.final_loads:
        load_into_store(layer_id, None)
    # FA9: Abbruch im letzten Schritt oder der letzten Ladung endet als Abbruch.
    check_cancel()

    # Invariante: geladen == benötigt. Explizit statt assert, damit sie auch
    # mit "python -O" gilt (Goldene Regel 7).
    if set(loaded_layers) != set(plan.required_layers) or len(loaded_layers) != len(
        set(loaded_layers)
    ):
        raise RuntimeError(
            f"Lauf inkonsistent: geladen {loaded_layers}, benötigt {sorted(plan.required_layers)}"
        )
    # FA58: Provenienz genau der geladenen Layer.
    if set(provenance) != set(plan.required_layers):
        raise RuntimeError(
            f"Provenienz inkonsistent: erfasst {sorted(provenance)}, "
            f"geladen {sorted(plan.required_layers)}"
        )

    emit(
        ProgressEvent(
            type=_ev.RUN_DONE,
            detail={
                "order": list(plan.order),
                "loaded_layers": loaded_layers,
                "attribution": output_attribution(scenario, plan).to_dict(),
            },
        )
    )
    return ExecutionResult(
        store=store,
        order=list(plan.order),
        loaded_layers=loaded_layers,
        plan=plan,
        warnings=run_warnings,
        provenance=provenance,
        region=region_info,
        released=released,
        started_at=started_at,
        finished_at=utc_now(),
        run_id=run_id,
    )


def run_scenario(
    scenario: Scenario,
    refresh_snapshots: bool = False,
    connector_override: ConnectorOverride | None = None,
    region_fetcher: region_module.Fetcher | None = None,
    on_event: ProgressCallback | None = None,
    store: LayerStore | None = None,
    base_dir: Path | None = None,
    registry: Registry | None = None,
    cancel: CancelToken | None = None,
) -> ExecutionResult:
    """Führt ein Scenario aus: Plan ableiten, Region auflösen, ``execute()``.
    on_event empfängt den Layer-/Schritt-Lebenszyklus (ohne Callback läuft der
    Executor still). store erlaubt, Zwischenergebnisse während des Laufs zu lesen.
    base_dir (FA4): Verzeichnis der Szenario-YAML für relative Dateipfade (None:
    Prozess-CWD). registry (FA40): None = Standard-Registry. cancel: CancelToken.
    Arbeits-CRS und Regionsprüfung wie ``engine/run.py::run`` (FA55/FA56), aber
    ohne Pfad-Vorabcheck und ohne Ausgaben; der volle Anwendungsfall ist ``run``."""
    registry = registry if registry is not None else default_registry()
    # Plan zuerst: ein unregistrierter Baustein fällt vor Netzzugriff und Laden auf.
    plan = ExecutionPlan.of(scenario, registry)

    # Arbeits-CRS aus scenario.crs (FA55) und Regionsprüfung (FA56) wie im
    # vollen Anwendungsfall, sonst rechnete dieser Einstieg still in UTM.
    region_gdf, meta = region_module.resolve_region_with_meta(
        scenario.scenario.region, fetcher=region_fetcher, refresh=refresh_snapshots
    )
    region_info = region_module.describe_region(region_gdf, scenario.scenario.crs, meta)

    return execute(
        scenario,
        plan,
        registry=registry,
        region=region_gdf,
        observer=on_event,
        cancel=cancel,
        store=store,
        base_dir=base_dir,
        refresh_snapshots=refresh_snapshots,
        connector_override=connector_override,
        region_fetcher=region_fetcher,
        target_crs=region_info.crs,
        region_info=region_info,
    )
