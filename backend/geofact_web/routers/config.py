"""Konfiguration: LLM-Generierung aus Prompt + Validierung + Lizenzprüfung (FA73).

Implements: FA78 (beide Erzeugungsendpunkte laufen durch ``generation.refine``), FA79
(``_grounding`` gibt je lokalem Raster Kennwerte mit), FA81 (``request.options``
überschreibt Fristen und Probelauf je Anfrage) und FA87 (``missing_data`` der Antwort:
dem Modell fehlen Daten, es gibt keine Konfiguration)."""

from __future__ import annotations

import asyncio
import json
import queue
import warnings

import yaml
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import api_catalog, catalog_service, generation, scenario_service
from .. import options as option_module

from ..auth import AuthenticatedUser, require_user
from ..llm import LLMClient, LLMError
from ..openapi import EventStreamResponse, errors
from ..models import (
    GenerateRequest,
    GenerateResponse,
    LicenseCheckRequest,
    LicenseCheckResponse,
    ValidateRequest,
    ValidateResponse,
)
from ..settings import get_settings

router = APIRouter(prefix="/api/config", tags=["config"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _grounding(
    settings, request: GenerateRequest, owner: str
) -> tuple[list[dict], list[str]]:
    """Layer-Templates + Schema-Hinweise für vorausgewaehlte/lokale Quellen (Grounding).

    owner (FA28): ein vorausgewählter Upload wird nur aufgelöst, wenn er dem
    anfragenden Nutzer gehört, sonst flösse eine fremde source_id ins Grounding.
    """
    templates = []
    data_hints: list[str] = []
    selected_ids = set(request.source_ids)
    for source_id in request.source_ids:
        source = catalog_service.find_source(settings, source_id, owner)
        if source is not None:
            templates.append(source.layer_template)
            hint = catalog_service.source_schema_hint(source)
            if hint:
                data_hints.append(hint)

    # FA36: vorausgewählte API-Katalog-Einträge wie Katalogquellen behandeln
    # (example_layer als Template, Endpunkte und Attribute als Hinweis).
    api_ids = [
        sid for sid in request.source_ids if api_catalog.find_api(sid) is not None
    ]
    templates.extend(api_catalog.layer_templates(api_ids))
    if api_ids:
        data_hints.append(api_catalog.prompt_text(api_ids))

    # FA90: mitgelieferte Beispieldaten immer nennen (wie die lokalen Raster), damit
    # eine Frage nach BIP, Unfällen oder Wahlergebnissen ohne Vorauswahl beantwortbar ist.
    for source in catalog_service.sample_sources(settings):
        if source.id in selected_ids:
            continue
        hint = (
            f"Mitgelieferte Beispieldaten (nicht vorausgewählt, bei Bedarf mit genau diesem "
            f"Template übernehmen): {source.label} - {source.description or ''} -> "
            f"{source.layer_template}"
        )
        columns = catalog_service.source_schema_hint(source)
        data_hints.append(f"{hint} | {columns}" if columns else hint)

    # Lokale Rasterkataloge immer zeigen, sonst erfindet das LLM einen Dateipfad.
    for source in catalog_service.copernicus_catalog(settings):
        if source.id not in selected_ids:
            data_hints.append(
                f"Verfügbare Rasterquelle (nicht vorausgewählt, bei Bedarf als "
                f"file-Layer mit diesem Template übernehmen): "
                f"{source.label} -> {source.layer_template}"
            )
        # FA79: Zellgröße und Wertebereich, damit Schwellwerte zur Skala passen.
        profile = catalog_service.raster_profile(source)
        if profile:
            data_hints.append(profile)
    return templates, data_hints


_NO_LLM = "Kein LLM-API-Key konfiguriert."


def _generation_setup(request: GenerateRequest):
    """FA81-FA83: Einstellungen dieser Erzeugung (Serverwerte, überschrieben von
    ``request.options``) und Konnektor-Optionen des Probelaufs. Eine Wahl außerhalb
    der Serverauswahl ist ein 422 vor der ersten Modellanfrage."""
    settings = get_settings()
    try:
        effective = generation.apply_options(settings, request.options)
        connector_options = option_module.osm_connector_options(
            settings, request.options.osm_backend if request.options else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return effective, connector_options


@router.post(
    "/validate",
    response_model=ValidateResponse,
    summary="Konfiguration prüfen",
    description="Prüft Schema, DAG und Porttypen, ohne Daten zu laden. Eine ungültige "
    "Konfiguration ist kein HTTP-Fehler: die Antwort trägt valid=false und die Meldungen.",
)
def validate(request: ValidateRequest) -> ValidateResponse:
    _scenario, response = scenario_service.validate_config(
        request.config_yaml, request.config, parameters=request.parameters
    )
    return response


@router.post(
    "/licenses",
    response_model=LicenseCheckResponse,
    summary="Lizenzen prüfen",
    description="Prüft je Ausgabe die Lizenzen der Quellen gegen die Ausgabelizenz (FA73). "
    "Fragt den externen Dienst DALICC.",
)
def check_licenses(
    request: LicenseCheckRequest, user: AuthenticatedUser = Depends(require_user)
) -> LicenseCheckResponse:
    """FA73: Lizenzen je Ausgabe (Quellen + Ausgabelizenz) gegen DALICC prüfen.
    Fragt einen externen Dienst - deshalb nur für angemeldete Nutzer (FA24) und
    nur auf ausdrückliche Anfrage, nie bei ``/validate``."""
    return scenario_service.check_licenses(
        request.config_yaml,
        request.config,
        parameters=request.parameters,
        refresh=request.refresh,
    )


@router.post(
    "/generate",
    response_model=GenerateResponse,
    summary="Konfiguration erzeugen",
    description="Erzeugt aus einer Frage in natürlicher Sprache eine Konfiguration, prüft "
    "sie, führt sie zur Probe aus und lässt sie bei Fehlern reparieren (FA78). Braucht die "
    "Frage einen Datensatz, den keine Quelle liefert, ist missing_data nicht leer und "
    "config_yaml leer (FA87).",
    responses=errors(
        {502: "Der LLM-Anbieter hat nicht oder fehlerhaft geantwortet.", 503: _NO_LLM}
    ),
)
def generate(
    request: GenerateRequest, user: AuthenticatedUser = Depends(require_user)
) -> GenerateResponse:
    settings = get_settings()
    if not settings.llm_configured:
        raise HTTPException(
            status_code=503,
            detail="Kein LLM-API-Key konfiguriert (OPENAI_API_KEY / OPENROUTER_API_KEY).",
        )

    # FA81-FA83: Einstellungen dieser Anfrage; ohne Angabe die Werte des Servers.
    settings, connector_options = _generation_setup(request)
    templates, data_hints = _grounding(settings, request, user.username)

    client = LLMClient(settings)
    context = generation.GenerationContext(
        prompt=request.prompt,
        region=request.region,
        templates=templates,
        data_hints=data_hints,
        connector_options=connector_options,
    )
    try:
        config_yaml = client.generate_config(
            request.prompt, templates, request.region, data_hints
        )
        # FA78: Prüfung, Probelauf und Reparatur; die Phasen braucht nur der Strom.
        *_phases, outcome = generation.refine(client, config_yaml, context, settings)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    assert isinstance(outcome, generation.Outcome)
    return _response(outcome, client.model)


def _response(outcome: generation.Outcome, model: str) -> GenerateResponse:
    """Antwort aus dem Endzustand der Schleife. Das YAML wird nur kosmetisch neu formatiert;
    scheitert das, bleibt der geprüfte Text, mit Warnung (Regel 7)."""
    config_yaml = outcome.config_yaml
    try:
        reparsed = yaml.safe_load(config_yaml)
        if isinstance(reparsed, dict):
            config_yaml = yaml.safe_dump(reparsed, allow_unicode=True, sort_keys=False)
    except yaml.YAMLError as exc:
        warnings.warn(
            f"YAML-Reformat nach Generierung fehlgeschlagen, behalte Original: {exc}"
        )
    return GenerateResponse(
        config_yaml=config_yaml,
        validation=outcome.validation,
        model=model,
        notes=outcome.notes,
        trial_run=outcome.trial_run,
        missing_data=outcome.missing_data,
    )


@router.post(
    "/generate/stream",
    response_class=EventStreamResponse,
    summary="Konfiguration erzeugen (Ereignisstrom)",
    description="Wie /generate, als Server-Sent Events: thinking {delta} (Reasoning-Text), "
    "token {delta} (YAML-Text), phase {phase} (validating, repairing, trial_run, reviewing), "
    "zum Schluss done (Endergebnis in der Form von /generate) oder error {error}.",
    responses={
        200: {"description": "Ereignisstrom bis done oder error."},
        **errors({503: _NO_LLM}),
    },
)
async def generate_stream(
    request: GenerateRequest, user: AuthenticatedUser = Depends(require_user)
) -> StreamingResponse:
    """SSE-Variante von /generate: streamt YAML-Text-Deltas während der LLM-Antwort,
    danach die Phasen der Schleife (validating, repairing, trial_run, reviewing -
    FA78) und ein 'done'-Event mit dem Endergebnis. Auch Reparatur und Durchsicht
    streamen: nach ihrem phase-Ereignis folgen wieder thinking- und token-Ereignisse
    (die Tokens gehören dann zur korrigierten Fassung, nicht zur ersten)."""
    settings = get_settings()
    if not settings.llm_configured:
        raise HTTPException(
            status_code=503,
            detail="Kein LLM-API-Key konfiguriert (OPENAI_API_KEY / OPENROUTER_API_KEY).",
        )

    # FA81-FA83: Einstellungen dieser Anfrage; ohne Angabe die Werte des Servers.
    settings, connector_options = _generation_setup(request)
    templates, data_hints = _grounding(settings, request, user.username)
    client = LLMClient(settings)

    async def event_stream():
        chunks: list[str] = []
        q: queue.Queue = queue.Queue()
        loop = asyncio.get_event_loop()

        def produce():
            try:
                # Chunks sind bereits getaggt ("token" | "thinking", FA31).
                for kind, delta in client.stream_generate_config(
                    request.prompt, templates, request.region, data_hints
                ):
                    q.put((kind, delta))
            except LLMError as exc:
                q.put(("error", str(exc)))
            finally:
                q.put(("eof", None))

        loop.run_in_executor(None, produce)

        while True:
            kind, payload = await loop.run_in_executor(None, q.get)
            if kind == "token":
                chunks.append(payload)
                yield _sse("token", {"delta": payload})
            elif kind == "thinking":
                # Reasoning gehört nicht in die erzeugte YAML (FA31).
                yield _sse("thinking", {"delta": payload})
            elif kind == "error":
                yield _sse("error", {"error": payload})
                return
            else:  # eof
                break

        config_yaml = _strip_stream_fences("".join(chunks))
        # Ohne die phase-Ereignisse wirkte das UI während Probelauf und Reparatur hängend (FA31/FA78).
        context = generation.GenerationContext(
            prompt=request.prompt,
            region=request.region,
            templates=templates,
            data_hints=data_hints,
            connector_options=connector_options,
        )
        steps = generation.refine(client, config_yaml, context, settings, stream=True)
        outcome: generation.Outcome | None = None
        try:
            while outcome is None:
                # Jeder Schritt blockiert (LLM, Lauf), daher im Executor.
                item = await loop.run_in_executor(None, next, steps)
                if isinstance(item, generation.Phase):
                    yield _sse("phase", {"phase": item.name})
                elif isinstance(item, generation.Delta):
                    yield _sse(item.kind, {"delta": item.text})
                else:
                    outcome = item
        except LLMError as exc:
            yield _sse("error", {"error": str(exc)})
            return

        yield _sse("done", _response(outcome, client.model).model_dump())

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _strip_stream_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
