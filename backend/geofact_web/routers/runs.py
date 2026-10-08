"""Ausführung: Lauf starten, Live-Fortschritt (SSE), Per-Node-Ergebnisse, Download.

Implements: FA44 (Lauf und ZIP), FA88 (Dateiliste und Einzelabruf), FA89 (Liste der Läufe)

Parameter (FA59): ``RunRequest.parameters`` überschreibt deklarierte Parameter;
der Download enthält sie als ``parameters.yaml`` (nur bei Überschreibungen),
damit der Lauf mit ``scenario.yaml`` reproduzierbar bleibt. Lizenzen (FA65): das
Knoten-Ergebnis trägt ``attribution``, der Download ``ATTRIBUTION.txt``, wenn der
Lauf sie geschrieben hat.

Dateien (FA88): ``_run_files`` zählt die Dateien eines Laufs EINMAL auf; die
Liste (``/outputs``), der Einzelabruf (``/outputs/{name}``) und das ZIP
(``/download``) lesen alle daraus, damit sie nie auseinanderlaufen."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse

from .. import options as option_module
from .. import scenario_service
from ..auth import AuthenticatedUser, require_user
from ..models import (
    NodeResult,
    RejectedRequestResponse,
    RunCancelResponse,
    RunCreated,
    RunFileOut,
    RunHistory,
    RunOutputs,
    RunRequest,
    RunStatus,
    RunSummary,
    SkippedOutputOut,
)
from ..openapi import (
    EventStreamResponse,
    FileDownloadResponse,
    PngResponse,
    ZipResponse,
    errors,
)
from ..presentation import serialize, view_columns
from ..raster_png import render_png
from ..run_manager import _SENTINEL, RunLimitExceeded, RunState, manager
from ..settings import get_settings

router = APIRouter(prefix="/api/runs", tags=["runs"])

# Obergrenze für Vektor-/Graph-Features im Node-Ergebnis, nur für die Ansicht:
# Der Lauf rechnet immer über alle Objekte, Ausgaben und ZIP-Download sind vollständig.
# Der Erstaufruf bleibt schnell, danach bietet die Oberfläche "mehr laden" bis zum Maximum.
_DEFAULT_NODE_FEATURES = 10_000
_MAX_NODE_FEATURES = 200_000

# Attributspalten je Feature in der Ansicht (siehe view_columns): breite OSM-Layer haben
# über 800 meist leere Spalten, das Budget begrenzt die Antwortgröße an dieser Ursache.
_VIEW_COLUMN_BUDGET = 10


def _run_not_found(run_id: str) -> str:
    """Meldung für einen unbekannten Lauf (FA54), gleich für fremde und unbekannte (FA24).
    Läufe liegen nur im Arbeitsspeicher eines Backend-Prozesses; nach einem Neustart
    oder bei einer anderen Instanz ist die run_id dort unbekannt."""
    return (
        f"Lauf '{run_id}' nicht gefunden - Läufe liegen nur im Arbeitsspeicher des Backends; "
        "nach einem Neustart bitte erneut ausführen."
    )


def _require_run(run_id: str, user: AuthenticatedUser) -> RunState:
    # Fremder Lauf meldet dasselbe 404 wie ein unbekannter (FA24), damit eine
    # geratene run_id nichts verrät.
    state = manager.get_for_owner(run_id, user.username)
    if state is None:
        raise HTTPException(status_code=404, detail=_run_not_found(run_id))
    return state


_NO_RUN = (
    "Lauf unbekannt oder gehört einem anderen Nutzer. Läufe liegen nur im Arbeitsspeicher; "
    "nach einem Neustart des Backends sind sie weg."
)


@router.post(
    "",
    response_model=RunCreated,
    summary="Lauf starten",
    description="Prüft die Konfiguration und startet den Lauf im Hintergrund. Fortschritt "
    "über GET /api/runs/{run_id} oder den Ereignisstrom.",
    responses={
        422: {
            "model": RejectedRequestResponse,
            "description": "Konfiguration ungültig (detail.issues) oder Anfrage formal ungültig.",
        },
        **errors({429: "Zu viele gleichzeitige Läufe dieses Nutzers."}),
    },
)
def create_run(
    request: RunRequest, user: AuthenticatedUser = Depends(require_user)
) -> RunCreated:
    document, validation = scenario_service.validate_config(
        request.config_yaml, request.config, parameters=request.parameters
    )
    if document is None:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Konfiguration ungültig.",
                "issues": [i.model_dump() for i in validation.issues],
            },
        )
    try:
        # FA82: die OSM-Quelle dieses Laufs; eine Wahl außerhalb der Auswahl
        # des Servers ist ein 422, bevor der Lauf angelegt wird.
        connector_options = option_module.osm_connector_options(
            get_settings(), request.options.osm_backend if request.options else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        state = manager.create(
            document,
            request.refresh_snapshots,
            owner=user.username,
            parameters=request.parameters,
            connector_options=connector_options,
        )
    except RunLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    manager.start(state)
    return RunCreated(run_id=state.run_id, status=state.status)


@router.get(
    "",
    response_model=RunHistory,
    summary="Läufe auflisten",
    description="Die Läufe des Nutzers, die das Backend noch hält, neuester zuerst. Läufe liegen "
    "nur im Arbeitsspeicher: nach einem Neustart ist die Liste leer, und von den abgeschlossenen "
    "bleiben höchstens max_retained.",
)
def list_runs(user: AuthenticatedUser = Depends(require_user)) -> RunHistory:
    return RunHistory(
        runs=[_summary(state) for state in manager.list_for_owner(user.username)],
        max_retained=manager.max_retained_runs,
    )


def _summary(state: RunState) -> RunSummary:
    report = state.report
    outputs = report.outputs if report is not None else None
    return RunSummary(
        run_id=state.run_id,
        status=state.status,  # type: ignore[arg-type]
        scenario_name=state.scenario.scenario.name,
        created_at=_utc(state.created_at),
        finished_at=_utc(state.finished_at) if state.finished_at is not None else None,
        step_count=len(state.order),
        output_count=len(outputs.written) if outputs is not None else 0,
        skipped_count=len(report.skipped_outputs) if report is not None else 0,
        parameters=state.parameters,
    )


def _utc(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


@router.get(
    "/{run_id}",
    response_model=RunStatus,
    summary="Lauf-Status",
    description="Zustand des Laufs und jedes Schritts, mit Warnungen, Ladezeiten, Region, "
    "CRS-Herkunft der Layer und Lizenzen.",
    responses=errors({404: _NO_RUN}),
)
def run_status(
    run_id: str, user: AuthenticatedUser = Depends(require_user)
) -> RunStatus:
    return _require_run(run_id, user).to_status()


@router.post(
    "/{run_id}/cancel",
    response_model=RunCancelResponse,
    summary="Lauf abbrechen",
    description="Der Lauf gilt sofort als abgebrochen (Status cancelled, Ereignis run_cancelled, "
    "Ende des Ereignisstroms). Ein gerade laufender Baustein endet im Hintergrund an der nächsten "
    "Schrittgrenze; sein Ergebnis wird verworfen. cancelling=false: der Lauf war schon beendet.",
    responses=errors({404: _NO_RUN}),
)
def cancel_run(run_id: str, user: AuthenticatedUser = Depends(require_user)) -> dict:
    _require_run(run_id, user)
    cancelled = manager.cancel(run_id)
    return {"run_id": run_id, "cancelling": cancelled}


@router.get(
    "/{run_id}/events",
    response_class=EventStreamResponse,
    summary="Live-Fortschritt (Ereignisstrom)",
    description="Server-Sent Events: zuerst status (Lauf-Status vor dem ersten Ereignis), dann "
    "je Ereignis des Laufs ein progress, zum Schluss status (Endstand) und end.",
    responses={
        200: {"description": "Ereignisstrom bis end."},
        **errors({404: _NO_RUN}),
    },
)
async def run_events(
    run_id: str, user: AuthenticatedUser = Depends(require_user)
) -> StreamingResponse:
    state = _require_run(run_id, user)
    subscription = state.subscribe()
    loop = asyncio.get_event_loop()

    async def event_stream():
        # Stand vor dem ersten Ereignis, damit der Client den DAG sofort zeichnen kann;
        # die Subscription liefert die bisherigen Ereignisse nach (sonst doppelte Warnungen).
        initial = state.initial_status or state.to_status()
        yield _sse("status", initial.model_dump())
        while True:
            event = await loop.run_in_executor(None, subscription.get)
            if event is _SENTINEL:
                yield _sse("status", state.to_status().model_dump())
                yield _sse("end", {"status": state.status})
                break
            yield _sse("progress", event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get(
    "/{run_id}/nodes/{node_id}",
    response_model=None,
    summary="Knoten-Ergebnis",
    description="Ergebnis eines Layers oder Schritts für Karte und Tabelle: GeoJSON, Graph oder "
    "Raster-Metadaten. Die Ansicht ist begrenzt (Objekte, Attributspalten); das vollständige "
    "Ergebnis steht im Download.",
    responses={
        200: {"model": NodeResult, "description": "Ergebnis des Knotens."},
        422: {
            "model": RejectedRequestResponse,
            "description": "limit ist keine Zahl >= 1.",
        },
        **errors({404: _NO_RUN + " Oder der Knoten hat (noch) kein Ergebnis."}),
    },
)
def node_result(
    run_id: str,
    node_id: str,
    limit: int | None = Query(
        None,
        description="Gewünschte Objektzahl der Ansicht (Standard 10.000, höchstens 200.000). "
        "Der angewendete Wert steht in feature_limit.",
    ),
    user: AuthenticatedUser = Depends(require_user),
) -> dict:
    """limit: gewünschte Objektzahl der Ansicht.

    Ohne Angabe gilt _DEFAULT_NODE_FEATURES (schneller Erstaufruf). Die
    Oberfläche fordert mit höherem limit nach ("mehr laden"), bis
    _MAX_NODE_FEATURES erreicht ist. Der Payload meldet in
    'feature_limit', welcher Wert tatsächlich angewendet wurde - der
    Client soll den Deckel nicht raten müssen."""
    state = _require_run(run_id, user)
    data = state.store.get(node_id)
    if data is None:
        if node_id in state.released:
            # FA66: nur wenn release_intermediates gesetzt ist; die Web-Demo setzt es nie.
            raise HTTPException(
                status_code=404,
                detail=f"Knoten '{node_id}' wurde nach seinem letzten Leser freigegeben (FA66).",
            )
        raise HTTPException(
            status_code=404,
            detail=f"Knoten '{node_id}' hat (noch) kein Ergebnis.",
        )
    if limit is not None and limit < 1:
        raise HTTPException(
            status_code=422,
            detail=f"limit muss >= 1 sein, nicht {limit}.",
        )
    applied_limit = min(limit or _DEFAULT_NODE_FEATURES, _MAX_NODE_FEATURES)
    payload = serialize.serialize_result(
        data,
        max_features=applied_limit,
        pinned_columns=view_columns.required_columns(state.scenario),
        column_budget=_VIEW_COLUMN_BUDGET,
    )
    payload["feature_limit"] = applied_limit
    payload["max_feature_limit"] = _MAX_NODE_FEATURES
    payload["node_id"] = node_id
    # FA65: Lizenzen dieses Knotens (Layer: seine Lizenz, Schritt: Vereinigung).
    attribution = state.node_attribution.get(node_id)
    payload["attribution"] = (
        attribution.model_dump() if attribution is not None else None
    )
    if serialize.result_kind(data) == serialize.RASTER:
        # Die Karte nutzt die URL unverändert, daher die id als Pfadsegment kodieren.
        payload["raster_png_url"] = (
            f"/api/runs/{run_id}/nodes/{quote(node_id, safe='')}/raster.png"
        )
    return payload


@router.get(
    "/{run_id}/nodes/{node_id}/raster.png",
    response_class=PngResponse,
    summary="Raster-Vorschau",
    description="Das Raster-Ergebnis eines Knotens als eingefärbtes PNG.",
    responses={
        200: {"description": "PNG-Bild."},
        **errors({404: _NO_RUN + " Oder der Knoten hat kein Raster-Ergebnis."}),
    },
)
def node_raster_png(
    run_id: str, node_id: str, user: AuthenticatedUser = Depends(require_user)
) -> Response:
    state = _require_run(run_id, user)
    data = state.store.get(node_id)
    if data is None or serialize.result_kind(data) != serialize.RASTER:
        raise HTTPException(
            status_code=404, detail="Kein Raster-Ergebnis für diesen Knoten."
        )
    png = render_png(data)
    return Response(content=png, media_type="image/png")


@dataclass(frozen=True)
class _RunFile:
    """Eine Datei eines Laufs (FA88): entweder ein Pfad im Laufverzeichnis oder
    ein Text, den die Web-Schicht selbst erzeugt (scenario.yaml, parameters.yaml)."""

    name: str
    role: str  # output | companion
    path: Path | None = None
    text: str | None = None
    type: str | None = None
    source: str | None = None

    def content(self) -> bytes:
        if self.path is not None:
            return self.path.read_bytes()
        return (self.text or "").encode("utf-8")

    def size(self) -> int:
        if self.path is not None:
            return self.path.stat().st_size
        return len((self.text or "").encode("utf-8"))


def _run_files(state: RunState) -> list[_RunFile]:
    """Die Dateien des Laufs in der Reihenfolge des ZIP: geschriebene Ausgaben,
    dann scenario.yaml (der Original-Text des Laufs, nicht ein Dump des
    Modells), ATTRIBUTION.txt (FA65, wenn der Lauf Lizenzen kennt) und
    parameters.yaml (FA59, nur bei Überschreibungen). Vor dem Abschluss und nach
    einem Fehler gibt es keine Ausgaben, nur die Begleitdateien aus dem Zustand."""
    files: list[_RunFile] = []
    report = state.report
    outputs = report.outputs if report is not None else None
    if outputs is not None:
        written = list(outputs.written)
        # write_outputs schreibt in der Reihenfolge der Deklaration und meldet
        # jede nicht geschriebene Ausgabe mit ihrem Index: die übrigen
        # Deklarationen gehören der Reihe nach zu den Dateien. Geht das nicht
        # auf, bleiben Format und Quelle leer statt geraten.
        skipped = {s.index for s in outputs.skipped}
        declared = [
            spec
            for index, spec in enumerate(state.scenario.output)
            if index not in skipped
        ]
        specs = declared if len(declared) == len(written) else [None] * len(written)
        for path, spec in zip(written, specs):
            files.append(
                _RunFile(
                    name=path.name,
                    role="output",
                    path=path,
                    type=spec.type if spec is not None else None,
                    source=spec.source if spec is not None else None,
                )
            )
    # Die ausgeführte Konfiguration mitliefern (Reproduzierbarkeit): der Text,
    # wie er gelaufen ist. Szenarien aus einem dict haben keinen Text - dann
    # der Modell-Dump.
    files.append(
        _RunFile(name="scenario.yaml", role="companion", text=_scenario_text(state))
    )
    attribution_file = report.attribution_file if report is not None else None
    if attribution_file is not None and Path(attribution_file).is_file():
        files.append(
            _RunFile(
                name="ATTRIBUTION.txt", role="companion", path=Path(attribution_file)
            )
        )
    if state.parameters:
        files.append(
            _RunFile(
                name="parameters.yaml",
                role="companion",
                text=_parameters_text(state.parameters),
            )
        )
    return files


@router.get(
    "/{run_id}/outputs",
    response_model=RunOutputs,
    summary="Dateien des Laufs",
    description="Die geschriebenen Ausgaben mit Format und Quellknoten, die Begleitdateien "
    "(scenario.yaml, ATTRIBUTION.txt, parameters.yaml) und die übersprungenen Ausgaben mit Grund. "
    "Dieselben Dateien wie im ZIP. Vor dem Abschluss und nach einem Fehler nur die Begleitdateien.",
    responses=errors({404: _NO_RUN}),
)
def run_outputs(
    run_id: str, user: AuthenticatedUser = Depends(require_user)
) -> RunOutputs:
    state = _require_run(run_id, user)
    try:
        files = [
            RunFileOut(
                name=f.name,
                role=f.role,  # type: ignore[arg-type]
                size_bytes=f.size(),
                type=f.type,
                source=f.source,
            )
            for f in _run_files(state)
        ]
    except OSError as exc:
        # Das Laufverzeichnis ist weg (der Lauf wurde gerade verdrängt).
        raise HTTPException(status_code=404, detail=_run_not_found(run_id)) from exc
    report = state.report
    return RunOutputs(
        run_id=run_id,
        status=state.status,  # type: ignore[arg-type]
        files=files,
        skipped=[
            SkippedOutputOut(
                index=s.index, type=s.type, source=s.source, reason=s.reason
            )
            for s in (report.skipped_outputs if report is not None else [])
        ],
    )


@router.get(
    "/{run_id}/outputs/{name}",
    response_class=FileDownloadResponse,
    summary="Einzelne Datei herunterladen",
    description="Eine Datei aus der Liste GET /api/runs/{run_id}/outputs als Anhang, mit denselben "
    "Bytes wie im ZIP.",
    responses={
        200: {"description": "Die Datei."},
        **errors({404: _NO_RUN + " Oder der Lauf hat keine Datei dieses Namens."}),
    },
)
def run_output_file(
    run_id: str, name: str, user: AuthenticatedUser = Depends(require_user)
) -> Response:
    state = _require_run(run_id, user)
    # Der Name wird nur mit der Aufzählung verglichen, nie als Pfad benutzt.
    entry = next((f for f in _run_files(state) if f.name == name), None)
    if entry is None:
        raise HTTPException(
            status_code=404, detail=f"Lauf '{run_id}' hat keine Datei '{name}'."
        )
    if entry.path is not None:
        if not entry.path.is_file():
            raise HTTPException(status_code=404, detail=_run_not_found(run_id))
        return FileResponse(
            entry.path, media_type=FileDownloadResponse.media_type, filename=entry.name
        )
    return Response(
        content=entry.content(),
        media_type=FileDownloadResponse.media_type,
        headers={"Content-Disposition": f'attachment; filename="{entry.name}"'},
    )


@router.get(
    "/{run_id}/download",
    response_class=ZipResponse,
    summary="Ergebnis herunterladen",
    description="ZIP mit den geschriebenen Ausgaben und scenario.yaml, dazu ATTRIBUTION.txt, "
    "parameters.yaml und SKIPPED_OUTPUTS.txt, soweit vorhanden.",
    responses={
        200: {"description": "ZIP-Archiv."},
        **errors({404: _NO_RUN, 409: "Lauf ist noch nicht abgeschlossen."}),
    },
)
def download(
    run_id: str,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(require_user),
) -> FileResponse:
    """ZIP mit den Dateien des Laufs (``_run_files``, FA88) und - falls Ausgaben
    übersprungen wurden - SKIPPED_OUTPUTS.txt."""
    state = _require_run(run_id, user)
    report = state.report
    if state.result is None or report is None:
        raise HTTPException(
            status_code=409, detail="Lauf ist noch nicht abgeschlossen."
        )
    skipped = report.skipped_outputs

    tmp_dir = Path(tempfile.mkdtemp(prefix=f"geofact_{run_id}_"))
    zip_path = tmp_dir / f"geofact_{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in _run_files(state):
            if entry.path is not None:
                archive.write(entry.path, arcname=entry.name)
            else:
                archive.writestr(entry.name, entry.text or "")
        if skipped:
            archive.writestr(
                "SKIPPED_OUTPUTS.txt",
                "Folgende deklarierten Ausgaben fehlen in dieser ZIP-Datei:\n\n"
                + "\n".join(
                    f"- output[{s.index}] (type={s.type}, source={s.source}): {s.reason}"
                    for s in skipped
                )
                + "\n",
            )

    # Temp-Verzeichnis erst nach dem Versand aufräumen, sonst fehlt die Datei beim Streamen.
    background_tasks.add_task(shutil.rmtree, tmp_dir, ignore_errors=True)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=zip_path.name,
        background=background_tasks,
    )


def _parameters_text(parameters: dict) -> str:
    import yaml

    return (
        "# Parameter-Überschreibungen dieses Laufs (FA59) - zusammen mit scenario.yaml\n"
        "# reproduzierbar: geofact run scenario.yaml --param NAME=WERT ...\n"
        + yaml.safe_dump(parameters, allow_unicode=True, sort_keys=False)
    )


def _scenario_text(state: RunState) -> str:
    if state.document.source_text is not None:
        return state.document.source_text
    import yaml

    return yaml.safe_dump(
        state.scenario.model_dump(mode="json"), allow_unicode=True, sort_keys=False
    )
