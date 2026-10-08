"""HTTP-Request/Response-Modelle (getrennt von geofact.core.scenario).

Die Szenario-Validierung läuft über das Kern-Modell; hier stehen nur die
Hüllen für die Web-API, damit HTTP-Belange nicht in den Kern lecken.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)


class ValidateRequest(BaseModel):
    # Entweder YAML-Text oder ein Dict.
    config_yaml: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    # FA59: Überschreibungen deklarierter Parameter (name -> Wert); die YAML bleibt unverändert.
    parameters: Optional[dict[str, Any]] = None


class ValidationIssue(BaseModel):
    location: str
    message: str
    type: Optional[str] = None


class ParameterInfo(BaseModel):
    """FA59: ein deklarierter Szenario-Parameter für das Web-Formular. Wird
    auch bei ungültiger Konfiguration geliefert (z. B. fehlt die Überschreibung
    eines Pflichtparameters - genau dann braucht das Formular die Liste)."""

    name: str
    type: Optional[str] = None
    items: Optional[str] = None
    default: Any = None
    description: Optional[str] = None
    choices: Optional[list[Any]] = None
    required: bool = False
    # wirksamer Wert: Überschreibung, sonst default (None = fehlt noch)
    value: Any = None
    overridden: bool = False


class DroppedNode(BaseModel):
    location: str
    expression: str


class NodeOriginOut(BaseModel):
    """FA75: Herkunft eines Knotens im ausgedehnten Szenario
    (NodeOrigin.to_dict); module None = Knoten steht so in der YAML, sonst
    erzeugt aus Modul ``module`` in Instanz ``instance`` (Element ``key``,
    Knoten ``inner_id`` im Modul)."""

    section: str
    index: int
    module: Optional[str] = None
    instance: Optional[str] = None
    key: Optional[str] = None
    inner_id: Optional[str] = None
    location: str


class InstanceOut(BaseModel):
    """FA75: eine Instanz mit ihren Schlüsseln (Listenreihenfolge, leer ohne
    foreach) und den durch when entfernten Schlüsseln."""

    id: str
    module: str
    keys: list[str] = Field(default_factory=list)
    dropped_keys: list[str] = Field(default_factory=list)


class ExpansionInfo(BaseModel):
    """FA59/FA62/FA75: Kennzahlen der Expansion (nur wenn das Szenario
    Parameter, Instanzen oder when nutzt)."""

    node_count: int = 0
    overridden: list[str] = Field(default_factory=list)
    dropped: list[DroppedNode] = Field(default_factory=list)
    instances: list[InstanceOut] = Field(default_factory=list)
    # FA75: Herkunft je Knoten, gleiche Gruppierung wie RunStatus.origins.
    node_origins: dict[str, NodeOriginOut] = Field(default_factory=dict)


class LicenseOut(BaseModel):
    name: str
    attribution: Optional[str] = None
    url: Optional[str] = None
    # FA73: maschinenlesbare Kennung (DALICC-URI), nur wenn deklariert.
    id: Optional[str] = None

    @model_serializer(mode="wrap")
    def _omit_missing_id(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, Any]:
        """Lässt ``id`` weg, wenn es fehlt (kein ``"id": null``)."""
        data = handler(self)
        if data.get("id") is None:
            data.pop("id", None)
        return data


class OutputLicenseInfo(BaseModel):
    """FA73: vom Nutzer festgelegte Ausgabelizenz einer Ausgabe (ungeprüft)."""

    index: int
    source: str
    type: str
    license: LicenseOut
    declared_at: Literal["output", "scenario"]
    line: str


class AttributionOut(BaseModel):
    """FA65: Lizenzen eines Knotens bzw. eines Laufs (NodeAttribution.to_dict)."""

    licenses: list[LicenseOut] = Field(default_factory=list)
    undeclared: list[str] = Field(default_factory=list)
    lines: list[str] = Field(default_factory=list)


class PlannedStepOut(BaseModel):
    """FA75: ein Schritt des Plans vor dem Lauf (id, op, Eingänge)."""

    id: str
    op: str
    inputs: dict[str, str] = Field(default_factory=dict)


class ValidateResponse(BaseModel):
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    # Bei Gültigkeit: abgeleitete Ausführungsinformationen (für Editor-Feedback).
    execution_order: Optional[list[str]] = None
    required_layers: Optional[list[str]] = None
    unused_layers: Optional[list[str]] = None
    # FA59: deklarierte Parameter (auch bei ungültiger Konfiguration).
    parameters: list[ParameterInfo] = Field(default_factory=list)
    # FA59-FA62: nur bei gültiger Konfiguration mit Expansion.
    expansion: Optional[ExpansionInfo] = None
    # FA65: Vereinigung der Lizenzen aller Ausgabequellen (ohne Lauf).
    attribution: Optional[AttributionOut] = None
    # FA73: Ausgabelizenzen (output[i].license / scenario.output_license), ohne Prüfung.
    output_licenses: list[OutputLicenseInfo] = Field(default_factory=list)
    # FA75: Schritte des ausgedehnten Szenarios (op, Eingänge port -> id) für
    # den Graphen vor dem Lauf - dieselben Kanten wie später StepState.inputs.
    # Die YAML allein reicht nicht: "puffer[*].x" ist ein Selektor, kein Knoten.
    planned_steps: Optional[list[PlannedStepOut]] = None


class LicenseCheckRequest(ValidateRequest):
    """FA73: Lizenzprüfung gegen DALICC; ``refresh`` fragt neu statt aus der Ablage."""

    refresh: bool = False


class LicenseConflictOut(BaseModel):
    kind: str
    reason: str
    statement_1: list[str] = Field(default_factory=list)
    statement_2: list[str] = Field(default_factory=list)
    message: str


class UncheckableLicenseOut(BaseModel):
    name: str
    reason: str


LicenseStatus = Literal[
    "compatible", "conflicts", "not_checkable", "unreachable", "no_licenses"
]


class LicenseCheckOut(BaseModel):
    """FA73: Ergebnis je Ausgabe (``engine/licenses.py::LicenseCheck.to_dict``)."""

    index: int
    source: str
    type: str
    output_license: Optional[LicenseOut] = None
    declared_at: Optional[Literal["output", "scenario"]] = None
    source_licenses: list[LicenseOut] = Field(default_factory=list)
    undeclared: list[str] = Field(default_factory=list)
    status: LicenseStatus
    message: str
    conflicts: list[LicenseConflictOut] = Field(default_factory=list)
    uncheckable: list[UncheckableLicenseOut] = Field(default_factory=list)
    checked_at: Optional[str] = None


class LicenseCheckResponse(BaseModel):
    """FA73: ``valid`` false = Konfiguration ungültig (``issues``), dann keine Prüfung."""

    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    status: Optional[LicenseStatus] = None
    checks: list[LicenseCheckOut] = Field(default_factory=list)


# FA81: Obergrenzen der je Anfrage einstellbaren Fristen (Sekunden); sie
# verhindern, dass eine Anfrage den Server beliebig lange bindet.
MAX_LLM_DEADLINE_S = 1800.0
MAX_TRIAL_RUN_S = 600.0


class GenerationOptions(BaseModel):
    """FA81: Einstellungen einer Erzeugung, die der Nutzer je Anfrage setzt.
    Ein fehlendes Feld bedeutet: es gilt der Wert des Servers (Umgebungsvariable)."""

    model_config = ConfigDict(extra="forbid")

    # Frist je Modellanfrage (Default GEOFACT_LLM_DEADLINE_S)
    llm_deadline_s: Optional[float] = Field(default=None, gt=0, le=MAX_LLM_DEADLINE_S)
    # Probelauf an/aus (Default GEOFACT_LLM_TRIAL_RUN)
    trial_run: Optional[bool] = None
    # Frist des Probelaufs (Default GEOFACT_LLM_TRIAL_RUN_S)
    trial_run_s: Optional[float] = Field(default=None, gt=0, le=MAX_TRIAL_RUN_S)
    # FA82: OSM-Quelle des Probelaufs (Default GEOFACT_OSM_BACKEND); nur Namen aus /api/meta.
    osm_backend: Optional[str] = None
    # FA83: Sprachmodell (Default GEOFACT_LLM_MODEL); nur ids aus /api/meta.
    llm_choice: Optional[str] = None


class GenerateRequest(BaseModel):
    prompt: str
    # Katalog- oder Upload-IDs, die das Modell in die Konfiguration aufnehmen soll.
    source_ids: list[str] = Field(default_factory=list)
    region: Optional[str] = None
    # FA81: Fristen und Probelauf dieser Anfrage; ohne Angabe die Werte des Servers.
    options: Optional[GenerationOptions] = None


class TrialRunOut(BaseModel):
    """FA78: Probelauf der erzeugten Konfiguration. ``skipped``: nicht
    ausgeführt (ungültig oder GEOFACT_LLM_TRIAL_RUN aus); ``timeout``: nach
    der Frist nicht fertig, also ungeprüft."""

    status: Literal["ok", "failed", "timeout", "skipped"] = "skipped"
    error: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    # Anfragen an das Modell nach der Erzeugung (Reparatur + Durchsicht).
    repair_rounds: int = 0


class MissingDatasetOut(BaseModel):
    """FA87: ein Datensatz, den die Frage braucht und den das Modell in keiner
    Quelle findet. ``hint``: was der Nutzer hochladen kann. ``alternative``: eine
    umformulierte Frage, die sich mit den verfügbaren Daten beantworten lässt."""

    name: str
    reason: Optional[str] = None
    hint: Optional[str] = None
    alternative: Optional[str] = None


class GenerateResponse(BaseModel):
    config_yaml: str
    validation: ValidateResponse
    model: str
    notes: Optional[str] = None
    trial_run: TrialRunOut = Field(default_factory=TrialRunOut)
    # FA87: nicht leer = dem Modell fehlen Daten; dann ist config_yaml leer und
    # es gibt keine Konfiguration zu übernehmen.
    missing_data: list[MissingDatasetOut] = Field(default_factory=list)


class RunOptions(BaseModel):
    """FA82: Einstellungen eines Laufs, die der Nutzer je Anfrage setzt. Ein
    fehlendes Feld bedeutet: es gilt der Wert des Servers."""

    model_config = ConfigDict(extra="forbid")

    # OSM-Quelle dieses Laufs (Default GEOFACT_OSM_BACKEND); nur Namen aus /api/meta.
    osm_backend: Optional[str] = None


class RunRequest(BaseModel):
    config_yaml: Optional[str] = None
    config: Optional[dict[str, Any]] = None
    refresh_snapshots: bool = False
    # FA59: Überschreibungen deklarierter Parameter (siehe ValidateRequest).
    parameters: Optional[dict[str, Any]] = None
    # FA82: Einstellungen dieses Laufs; ohne Angabe die Werte des Servers.
    options: Optional[RunOptions] = None


class RunCreated(BaseModel):
    run_id: str
    status: str


class RunWarningOut(BaseModel):
    message: str
    category: Optional[str] = None
    severity: Literal["info", "warning"] = "warning"
    layer: Optional[str] = None
    step: Optional[str] = None


class StepState(BaseModel):
    id: str
    op: Optional[str] = None
    output_type: Optional[str] = None
    # Eingänge (port -> Layer-/Step-id), damit das Frontend die DAG-Kanten zeichnen kann.
    inputs: dict[str, str] = Field(default_factory=dict)
    status: Literal["pending", "loading", "running", "done", "error", "cancelled"] = (
        "pending"
    )
    duration_ms: Optional[float] = None
    error: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)
    # Dieselben Warnungen mit Kategorie und Schweregrad, parallel zu warnings.
    warning_details: list[RunWarningOut] = Field(default_factory=list)


class RegionInfoOut(BaseModel):
    """FA55/FA56: Region und Arbeits-CRS des Laufs (RegionInfo.to_dict)."""

    crs: str
    crs_label: Optional[str] = None
    crs_mode: Optional[str] = None
    crs_spec: Optional[str] = None
    bbox: list[float] = Field(default_factory=list)
    area_km2: Optional[float] = None
    display_name: Optional[str] = None
    osm_class: Optional[str] = None
    osm_type: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)


class LayerProvenanceOut(BaseModel):
    """FA58: CRS-Herkunft eines geladenen Layers (LayerProvenance.to_dict)."""

    layer: str
    source: str
    source_crs: Optional[str] = None
    source_crs_label: Optional[str] = None
    crs_override: bool = False
    target_crs: Optional[str] = None
    target_crs_label: Optional[str] = None
    raw_bounds: Optional[list[float]] = None
    bounds: Optional[list[float]] = None
    feature_count: Optional[int] = None
    raw_sample: list[list[float]] = Field(default_factory=list)
    sample: list[list[float]] = Field(default_factory=list)


class RunStatus(BaseModel):
    run_id: str
    status: Literal["pending", "running", "done", "error", "cancelled"]
    order: list[str] = Field(default_factory=list)
    steps: dict[str, StepState] = Field(default_factory=dict)
    layers: list[str] = Field(default_factory=list)
    loaded_layers: list[str] = Field(default_factory=list)
    # Ladezeit je Layer in ms; gehört zur Quelle, nicht zum ersten Schritt, der sie braucht.
    layer_load_ms: dict[str, float] = Field(default_factory=dict)
    error: Optional[str] = None
    # FA60/FA61: Herkunft je Knoten-id (aus der Expansion; leer ohne Expansion).
    origins: dict[str, NodeOriginOut] = Field(default_factory=dict)
    # FA58: CRS-Herkunft je geladenem Layer (aus layer_loaded).
    layer_provenance: dict[str, LayerProvenanceOut] = Field(default_factory=dict)
    # FA55/FA56: Region und Arbeits-CRS (aus region_ready).
    region: Optional[RegionInfoOut] = None
    # FA65: Lizenzen je Knoten (Plan), auch nach einem Neuladen verfügbar.
    node_attribution: dict[str, Optional[AttributionOut]] = Field(default_factory=dict)
    # FA65: Lizenzen der Ausgabequellen (Plan, bestätigt durch run_done).
    attribution: Optional[AttributionOut] = None
    # Warnungen auf Lauf-Ebene (region_warning), nicht einem Schritt zugeordnet.
    warnings: list[RunWarningOut] = Field(default_factory=list)
    # FA66: freigegebene Knoten; leer, solange niemand release_intermediates setzt (FA33).
    released: list[str] = Field(default_factory=list)


class CatalogSource(BaseModel):
    id: str
    kind: Literal["osm_preset", "copernicus", "upload", "sample"]
    label: str
    description: Optional[str] = None
    data_type: Literal["vector", "raster"]
    # Ein einbaufertiges Layer-Fragment für die Szenario-YAML.
    layer_template: dict[str, Any]


# --- API-Katalog (FA36) ---


class CatalogApiEndpoint(BaseModel):
    """Ein Endpunkt/Feature-Type einer Schnittstelle. attributes sind die
    in der Praxis relevanten Felder (nicht die vollständige Liste - die
    steht in der Spezifikation); attribute_notes erklärt einzelne davon,
    damit ein Mensch den Endpunkt ohne Spec-Studium einordnen kann."""

    name: str
    title: Optional[str] = None
    method: Optional[str] = None
    geometry: Optional[str] = None
    attributes: list[str] = Field(default_factory=list)
    attribute_notes: dict[str, str] = Field(default_factory=dict)
    notes: Optional[str] = None


class CatalogApi(BaseModel):
    """Eintrag des kuratierten API-Katalogs (geofact_web/data/api_catalog.yaml).
    layer_template ist - wo vorhanden - ein einbaufertiges Fragment für
    die layers-Liste der Szenario-YAML, analog CatalogSource."""

    id: str
    label: str
    provider: Optional[str] = None
    protocol: str
    base_url: str
    description: Optional[str] = None
    license: Optional[str] = None
    documentation_url: Optional[str] = None
    spec_url: Optional[str] = None
    spec_type: Optional[str] = None
    auth: Optional[str] = None
    data_type: Optional[str] = None
    verified: Optional[str] = None
    notes: Optional[str] = None
    endpoints: list[CatalogApiEndpoint] = Field(default_factory=list)
    layer_template: Optional[dict[str, Any]] = None


class ApiCatalogResponse(BaseModel):
    apis: list[CatalogApi] = Field(default_factory=list)


class ApiSpecResponse(BaseModel):
    """Auf Anfrage geladene Spezifikation einer Schnittstelle. truncated
    meldet, dass das Dokument am Limit abgeschnitten wurde - statt einen
    stillen Teilinhalt auszugeben (Goldene Regel 7)."""

    api_id: str
    spec_url: str
    spec_type: Optional[str] = None
    content: str
    truncated: bool = False


class CatalogResponse(BaseModel):
    osm_backend: str
    osm_presets: list[CatalogSource]
    copernicus: list[CatalogSource]
    uploads: list[CatalogSource]
    # FA90: mitgelieferte Beispieldaten (ohne Upload nutzbar).
    samples: list[CatalogSource] = Field(default_factory=list)


class UploadResponse(BaseModel):
    source: CatalogSource


class SaveScenarioRequest(BaseModel):
    name: str
    config_yaml: str


class SavedScenario(BaseModel):
    # id: Lade-Referenz (user: Dateiname ohne Endung; example: Pfad relativ zu
    # examples/ ohne Endung, kollidiert so nie mit eigenen Namen). name: Anzeigename.
    id: str
    name: str
    source: Literal["user", "example"] = "user"
    description: Optional[str] = None


class ScenarioContent(BaseModel):
    name: str
    config_yaml: str


# --- Weitere Antworten (FA85) ---


class ErrorResponse(BaseModel):
    """Fehlerantwort (``HTTPException``): ``detail`` ist die Meldung."""

    detail: str


class InvalidConfigDetail(BaseModel):
    message: str
    issues: list[ValidationIssue] = Field(default_factory=list)


class RejectedRequestResponse(BaseModel):
    """Antwort 422. ``detail`` ist je nach Ursache eine Meldung, die Prüffehler
    einer ungültigen Konfiguration (``message`` + ``issues``) oder die
    Feldfehler einer formal ungültigen Anfrage (Liste, von FastAPI)."""

    detail: str | InvalidConfigDetail | list[dict[str, Any]]


class PostgisCheck(BaseModel):
    enabled: bool
    # None, solange GEOFACT_OSM_BACKEND nicht postgis ist.
    reachable: Optional[bool] = None
    detail: Optional[str] = None


class CopernicusCheck(BaseModel):
    dir: str
    exists: bool
    raster_count: int


class LlmCheck(BaseModel):
    configured: bool
    model: Optional[str] = None


class HealthChecks(BaseModel):
    postgis: PostgisCheck
    copernicus: CopernicusCheck
    llm: LlmCheck


class HealthResponse(BaseModel):
    # degraded: PostGIS ist als Backend gewählt, aber nicht erreichbar.
    status: Literal["ok", "degraded"]
    checks: HealthChecks


class GenerationDefaults(BaseModel):
    """Werte des Servers für die einstellbaren Felder der Erzeugung (FA81)."""

    llm_deadline_s: float
    trial_run: bool
    trial_run_s: float


class GenerationLimits(BaseModel):
    """Obergrenzen der Fristen in Sekunden (FA81)."""

    llm_deadline_s: float
    trial_run_s: float


class OsmBackendChoices(BaseModel):
    default: str
    available: list[str]


class LLMChoiceOut(BaseModel):
    id: str
    model: str
    providers: list[str] = Field(default_factory=list)


class MetaResponse(BaseModel):
    osm_backend: str
    llm_configured: bool
    llm_model: Optional[str] = None
    # FA81: Vorbelegung und Obergrenzen des Einstellungsdialogs.
    generation_defaults: GenerationDefaults
    generation_limits: GenerationLimits
    # FA82: wählbare OSM-Quellen; "default" ist GEOFACT_OSM_BACKEND.
    osm_backends: OsmBackendChoices
    # FA83: wählbare Sprachmodelle, das Modell des Servers zuerst.
    llm_choices: list[LLMChoiceOut] = Field(default_factory=list)
    copernicus_dir: str
    copernicus_dir_exists: bool
    # FA40: übersprungene Plugins (Herkunft -> Ursache).
    failed_plugins: dict[str, str] = Field(default_factory=dict)


class OperationContract(BaseModel):
    """Vertrag einer registrierten Operation (``api.operation_catalog``). Die
    Form eines Eintrags in ``params`` bestimmt der Kern (type, required,
    default, description, choices, minimum, ...)."""

    op: str
    input_ports: dict[str, str]
    output_type: str
    required_params: list[str] = Field(default_factory=list)
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    doc: Optional[str] = None


class RunFileOut(BaseModel):
    """FA88: eine Datei eines Laufs. ``output`` = geschriebene Ausgabe,
    ``companion`` = Begleitdatei (scenario.yaml, ATTRIBUTION.txt, parameters.yaml)."""

    name: str
    role: Literal["output", "companion"]
    size_bytes: int
    # Nur für role=output und nur, wenn die Zuordnung zu output[i] eindeutig ist.
    type: Optional[str] = None
    source: Optional[str] = None


class SkippedOutputOut(BaseModel):
    """FA88: eine deklarierte Ausgabe, die der Lauf nicht geschrieben hat."""

    index: int
    type: str
    source: Optional[str] = None
    reason: str


class RunOutputs(BaseModel):
    """FA88: die Dateien eines Laufs - dieselbe Aufzählung, aus der das ZIP entsteht."""

    run_id: str
    status: Literal["pending", "running", "done", "error", "cancelled"]
    files: list[RunFileOut] = Field(default_factory=list)
    skipped: list[SkippedOutputOut] = Field(default_factory=list)


class RunSummary(BaseModel):
    """FA89: ein Lauf in der Liste der Läufe."""

    run_id: str
    status: Literal["pending", "running", "done", "error", "cancelled"]
    scenario_name: str
    created_at: datetime
    # None, solange der Lauf nicht abgeschlossen ist.
    finished_at: Optional[datetime] = None
    step_count: int
    output_count: int
    skipped_count: int
    # FA59: Überschreibungen deklarierter Parameter (None = keine).
    parameters: Optional[dict[str, Any]] = None


class RunHistory(BaseModel):
    """FA89: die Läufe des Nutzers, die das Backend noch hält (neuester zuerst)."""

    runs: list[RunSummary] = Field(default_factory=list)
    # GEOFACT_WEB_MAX_RETAINED_RUNS: so viele abgeschlossene Läufe bleiben.
    max_retained: int


class RunCancelResponse(BaseModel):
    run_id: str
    # false: der Lauf war schon beendet, es gibt nichts abzubrechen.
    cancelling: bool


class ScenarioDeleted(BaseModel):
    name: str
    # false: es gab kein Szenario dieses Namens.
    deleted: bool


class NodeDescription(BaseModel):
    """Zusammenfassung eines Knoten-Ergebnisses (``serialize.describe``). Außer
    ``kind`` und ``crs`` hängen die Felder von der Art ab: Vektor
    ``feature_count``, ``geometry_types``, ``columns``, ``numeric_fields``,
    ``bounds``; Raster ``shape``, ``dtype``, ``bounds``, ``stats``, ``path``;
    Graph ``node_count``, ``edge_count``, ``node_attributes``."""

    model_config = ConfigDict(extra="allow")

    kind: Literal["vector", "raster", "graph"]
    crs: Optional[str] = None


class NodeResult(BaseModel):
    """Ergebnis eines Knotens für die Ansicht. Je nach ``describe.kind`` ist
    genau eines vorhanden: ``geojson`` (Vektor), ``graph`` (Graph) oder
    ``raster_png_url`` (Raster)."""

    node_id: str
    describe: NodeDescription
    # Vektor: FeatureCollection in EPSG:4326, höchstens feature_limit Objekte.
    geojson: Optional[dict[str, Any]] = None
    # Vektor: Zahl der in der Ansicht weggelassenen Attributspalten.
    dropped_columns: Optional[int] = None
    # Graph: {"nodes": [{id, lon, lat, ...}], "edges": [{source, target, ...}]}.
    graph: Optional[dict[str, Any]] = None
    # Raster: Pfad des Vorschaubilds (GET .../raster.png).
    raster_png_url: Optional[str] = None
    # Angewendete Obergrenze der Objektzahl und ihr höchster zulässiger Wert.
    feature_limit: int
    max_feature_limit: int
    # FA65: Lizenzen dieses Knotens.
    attribution: Optional[AttributionOut] = None
