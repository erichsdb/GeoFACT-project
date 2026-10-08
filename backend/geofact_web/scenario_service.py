"""Parsen + Validieren von Szenario-Konfigurationen für die Web-API.

Dünne Brücke zum Anwendungsfall des Kerns (``geofact.api``, FA44): nimmt YAML-Text
oder ein Dict und liefert das ``ScenarioDocument`` oder eine Fehlerliste (Position und
Meldung) für den Editor. Die Fehlermeldungen selbst kommen aus dem Kern.

Ursprung (FA4/FA44): relative Pfade gelten gegen das Verzeichnis der YAML-Datei. Da der
Editor nur Text schickt, steht der Ursprung als Kopfzeile ``# geofact-origin: <id>`` im
Text und überlebt Bearbeiten und Speichern. Text ohne Ursprung gilt gegen GEOFACT_WEB_BASE_DIR.

Parameter (FA59): Überschreibungen gehen an ``api.validate(parameters=...)``; der YAML-Text
bleibt unverändert. Die Antwort nennt die deklarierten Parameter auch bei ungültiger
Konfiguration (das Formular braucht sie gerade dann), die Expansion (FA59-FA62), die Lizenzen
der Ausgabequellen (FA65) und die gesetzten Ausgabelizenzen (FA73); ``check_licenses`` prüft
sie gegen DALICC (Netz).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

import yaml

from geofact import api

from .models import (
    AttributionOut,
    DroppedNode,
    ExpansionInfo,
    InstanceOut,
    LicenseCheckOut,
    LicenseCheckResponse,
    LicenseOut,
    NodeOriginOut,
    OutputLicenseInfo,
    ParameterInfo,
    PlannedStepOut,
    ValidateResponse,
    ValidationIssue,
)
from .settings import get_settings

ORIGIN_PREFIX = "geofact-origin:"
_ORIGIN_LINE = re.compile(r"^#\s*geofact-origin:\s*(\S+)\s*$")
_ORIGIN_SEARCH_LINES = 5


class OriginError(ValueError):
    """Die Ursprungs-Kopfzeile nennt kein mitgeliefertes Beispiel."""


def origin_header(example_id: str) -> str:
    """Die Kopfzeile, die der Beispiel-Lader dem Text voranstellt."""
    return (
        f"# {ORIGIN_PREFIX} {example_id}\n"
        "# (Ursprung: relative Pfade in diesem Szenario gelten gegen das Verzeichnis "
        "des Beispiels, FA4)\n"
    )


def origin_of(config_yaml: str) -> str | None:
    """Beispiel-id aus der Ursprungs-Kopfzeile (nur in den ersten Zeilen)."""
    for line in config_yaml.splitlines()[:_ORIGIN_SEARCH_LINES]:
        match = _ORIGIN_LINE.match(line.strip())
        if match:
            return match.group(1)
    return None


def example_path(example_id: str) -> Path | None:
    """YAML-Datei eines mitgelieferten Beispiels; None, wenn es keines dieser id
    gibt (auch nicht außerhalb von examples/ - kein Pfad-Ausbruch)."""
    examples_dir = get_settings().examples_dir.resolve()
    path = (examples_dir / example_id).with_suffix(".yaml").resolve()
    if examples_dir not in path.parents or not path.is_file():
        return None
    return path


def base_dir_for(config_yaml: str | None) -> Path:
    """FA4/FA44: Verzeichnis für relative Pfade dieses Szenarios - das der
    Beispiel-Datei, wenn der Text einen Ursprung nennt, sonst
    GEOFACT_WEB_BASE_DIR."""
    origin = origin_of(config_yaml) if config_yaml else None
    if origin is None:
        return get_settings().base_dir
    path = example_path(origin)
    if path is None:
        raise OriginError(
            f"Ursprung '{origin}' (Kopfzeile '# {ORIGIN_PREFIX}') ist kein mitgeliefertes "
            "Beispiel - Zeile entfernen oder korrigieren"
        )
    return path.parent


# --- Parameter, Expansion, Attribution (FA59-FA62, FA65) ---


def _raw_config(
    config_yaml: str | None, config: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Das rohe Dict für die Parameterliste; None, wenn der Text kein YAML-Mapping ist."""
    if config is not None:
        return config
    if "parameters" not in (config_yaml or ""):
        # Ohne das Wort gibt es keine Deklaration; ein zweites Parsen wäre vergebens.
        return None
    try:
        raw = yaml.safe_load(config_yaml or "")
    except yaml.YAMLError:
        return None
    return raw if isinstance(raw, dict) else None


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def parameter_infos(
    raw: Mapping[str, Any] | None, overrides: Mapping[str, Any] | None
) -> list[ParameterInfo]:
    """FA59: die deklarierten Parameter mit wirksamem Wert. Liest nur die Deklaration;
    die Prüfung macht der Kern, ein fehlerhafter Eintrag erscheint mit dem Lesbaren."""
    declared = raw.get("parameters") if isinstance(raw, Mapping) else None
    if not isinstance(declared, Mapping):
        return []
    overrides = overrides or {}
    infos: list[ParameterInfo] = []
    for name, body in declared.items():
        body = body if isinstance(body, Mapping) else {}
        choices = body.get("choices")
        default = body.get("default")
        overridden = name in overrides
        infos.append(
            ParameterInfo(
                name=str(name),
                type=_text_or_none(body.get("type")),
                items=_text_or_none(body.get("items")),
                default=default,
                description=_text_or_none(body.get("description")),
                choices=list(choices) if isinstance(choices, list) else None,
                required=default is None,
                value=overrides[name] if overridden else default,
                overridden=overridden,
            )
        )
    return infos


def expansion_of(document: api.ScenarioDocument | None) -> Any | None:
    """Der ``ExpansionReport`` eines Dokuments oder None."""
    return getattr(document, "expansion", None) if document is not None else None


def node_origins(document: api.ScenarioDocument | None) -> dict[str, dict[str, Any]]:
    """FA75: Herkunft je Knoten-id (``NodeOrigin.to_dict``)."""
    report = expansion_of(document)
    origins = getattr(report, "node_origins", None) or {}
    return {node: origin.to_dict() for node, origin in origins.items()}


def _expansion_info(document: api.ScenarioDocument | None) -> ExpansionInfo | None:
    report = expansion_of(document)
    if report is None or report.is_trivial():
        return None
    return ExpansionInfo(
        node_count=report.node_count,
        overridden=list(report.overridden),
        dropped=[
            DroppedNode(location=" -> ".join(map(str, loc)), expression=expr)
            for loc, expr in report.dropped
        ],
        instances=[
            InstanceOut(
                id=info.id,
                module=info.module,
                keys=list(info.keys),
                dropped_keys=list(info.dropped_keys),
            )
            for info in report.instances
        ],
        node_origins={
            node: NodeOriginOut.model_validate(origin)
            for node, origin in node_origins(document).items()
        },
    )


def attribution_out(attribution: Any | None) -> AttributionOut | None:
    """FA65: ``NodeAttribution`` (oder dessen ``to_dict``) als Antwortmodell;
    None, wenn weder eine Lizenz noch ein Layer ohne Lizenz beiträgt."""
    if attribution is None:
        return None
    raw = attribution if isinstance(attribution, Mapping) else attribution.to_dict()
    if not raw.get("licenses") and not raw.get("undeclared"):
        return None
    return AttributionOut.model_validate(raw)


def output_license_infos(document: api.ScenarioDocument) -> list[OutputLicenseInfo]:
    """FA73: Ausgabelizenzen je Ausgabe (``output[i].license`` vor ``scenario.output_license``), ungeprüft, ohne Netz."""
    scenario = document.scenario
    infos: list[OutputLicenseInfo] = []
    for index, spec in enumerate(scenario.output):
        license_, declared_at = scenario.output_license_of(spec)
        if license_ is None:
            continue
        infos.append(
            OutputLicenseInfo(
                index=index,
                source=spec.source,
                type=spec.type,
                license=LicenseOut.model_validate(license_.to_dict()),
                declared_at=declared_at,  # type: ignore[arg-type]
                line=api.OutputLicense(license_).line(),
            )
        )
    return infos


def _response(
    report: api.ValidationReport, parameters: list[ParameterInfo]
) -> ValidateResponse:
    if not report.valid:
        return ValidateResponse(
            valid=False,
            issues=[
                ValidationIssue(location=loc, message=msg) for loc, msg in report.issues
            ],
            parameters=parameters,
        )
    assert report.plan is not None and report.document is not None
    return ValidateResponse(
        valid=True,
        execution_order=list(report.plan.full_order),
        required_layers=sorted(report.plan.required_layers),
        unused_layers=sorted(report.plan.unused_layers),
        parameters=parameters,
        expansion=_expansion_info(report.document),
        attribution=attribution_out(
            api.output_attribution(report.document, report.plan)
        ),
        output_licenses=output_license_infos(report.document),
        planned_steps=[
            PlannedStepOut(id=step.id, op=step.op, inputs=dict(step.inputs))
            for step in report.document.scenario.steps
        ],
    )


def _validate(
    source: str | dict[str, Any],
    base_dir: Path,
    parameters: Mapping[str, Any] | None,
) -> api.ValidationReport:
    """``api.validate`` mit Überschreibungen (FA59). Formularwerte dürfen Texte sein;
    ``api.parameter_overrides`` liest sie nach dem deklarierten Typ, ein unpassender
    Wert ist ein Problem an ``parameters -> <name>``."""
    if not parameters:
        return api.validate(source, base_dir=base_dir)
    try:
        values = api.parameter_overrides(source, dict(parameters))
    except api.ConfigError as exc:
        return api.ValidationReport(valid=False, issues=list(exc.issues))
    return api.validate(source, base_dir=base_dir, parameters=values)


def validate_config(
    config_yaml: str | None,
    config: dict[str, Any] | None,
    *,
    parameters: Mapping[str, Any] | None = None,
) -> tuple[api.ScenarioDocument | None, ValidateResponse]:
    """Prüft einen Text oder ein Dict und liefert (Dokument oder None, Antwort).
    Das Dokument trägt base_dir und Original-Text, die Lauf und ZIP-Download brauchen.
    parameters (FA59): Überschreibungen; ein unbekannter Name ist ein Problem an
    ``parameters -> <name>``."""
    if config is None and config_yaml is None:
        return None, ValidateResponse(
            valid=False,
            issues=[
                ValidationIssue(
                    location="(YAML)",
                    message="Weder config_yaml noch config angegeben.",
                )
            ],
        )
    try:
        base_dir = base_dir_for(config_yaml if config is None else None)
    except OriginError as exc:
        return None, ValidateResponse(
            valid=False,
            issues=[ValidationIssue(location="(Ursprung)", message=str(exc))],
        )
    source: str | dict[str, Any] = config if config is not None else config_yaml  # type: ignore[assignment]
    raw = _raw_config(config_yaml, config)
    report = _validate(source, base_dir, parameters)
    return report.document, _response(report, parameter_infos(raw, parameters))


def check_licenses(
    config_yaml: str | None,
    config: dict[str, Any] | None,
    *,
    parameters: Mapping[str, Any] | None = None,
    refresh: bool = False,
) -> LicenseCheckResponse:
    """FA73: Lizenzkompatibilität je Ausgabe laut DALICC (Netz; ein nicht erreichbarer
    Dienst ist der Status ``unreachable``). Eine ungültige Konfiguration liefert
    ``valid=False`` mit den Problemen von ``validate_config`` und prüft nichts."""
    document, validation = validate_config(config_yaml, config, parameters=parameters)
    if document is None or not validation.valid:
        return LicenseCheckResponse(valid=False, issues=validation.issues)
    report = api.check_licenses(document, refresh=refresh)
    return LicenseCheckResponse(
        valid=True,
        status=report.status,
        checks=[
            LicenseCheckOut.model_validate(check.to_dict()) for check in report.checks
        ],
    )
