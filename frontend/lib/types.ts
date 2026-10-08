// Gemeinsame Typen für die GeoFACT-Studio-Oberfläche.
//
// Diese Typen spiegeln die Pydantic-Modelle des Backends
// (backend/geofact_web/models.py) sowie die Serialisierung in
// backend/geofact_web/presentation/serialize.py und src/geofact/engine/catalog.py.
// Feldnamen und Optionalität müssen mit den echten JSON-Antworten
// übereinstimmen.

// --- Auth (FA24) -------------------------------------------------------

export interface LoginResponse {
  token: string;
  username: string;
  expires_in_s: number;
}

// --- Meta ------------------------------------------------------------

export interface Meta {
  osm_backend: string;
  llm_configured: boolean;
  llm_model: string;
  // FA81: Werte des Servers (Umgebungsvariablen) und Obergrenzen für die
  // Einstellungen der Erzeugung; optional für ein älteres Backend.
  generation_defaults?: { llm_deadline_s: number; trial_run: boolean; trial_run_s: number };
  generation_limits?: { llm_deadline_s: number; trial_run_s: number };
  // FA82/FA83: was der Server zur Wahl stellt (OSM-Quelle, Sprachmodell).
  osm_backends?: { default: string; available: string[] };
  llm_choices?: { id: string; model: string; providers: string[] }[];
  copernicus_dir: string;
}

// --- Katalog / Datenquellen -------------------------------------------

export type CatalogSourceKind = "osm_preset" | "copernicus" | "upload" | "sample";
export type CatalogDataType = "vector" | "raster";

export interface CatalogSource {
  id: string;
  kind: CatalogSourceKind;
  label: string;
  description?: string | null;
  data_type: CatalogDataType;
  // Einbaufertiges Layer-Fragment für die layers-Liste der Szenario-YAML.
  layer_template: Record<string, unknown>;
}

export interface Catalog {
  osm_backend: string;
  osm_presets: CatalogSource[];
  copernicus: CatalogSource[];
  uploads: CatalogSource[];
  // FA90: mitgelieferte Beispieldaten; fehlt bei einem älteren Backend.
  samples?: CatalogSource[];
}

export interface UploadResponse {
  source: CatalogSource;
}

// --- API-Katalog (FA36) --------------------------------------------------

export interface CatalogApiEndpoint {
  name: string;
  title?: string | null;
  method?: string | null;
  geometry?: string | null;
  attributes: string[];
  attribute_notes: Record<string, string>;
  notes?: string | null;
}

export interface CatalogApi {
  id: string;
  label: string;
  provider?: string | null;
  protocol: string;
  base_url: string;
  description?: string | null;
  license?: string | null;
  documentation_url?: string | null;
  spec_url?: string | null;
  spec_type?: string | null;
  auth?: string | null;
  data_type?: string | null;
  verified?: string | null;
  notes?: string | null;
  endpoints: CatalogApiEndpoint[];
  // Einbaufertiges Layer-Fragment (wo die Schnittstelle direkt als Layer
  // nutzbar ist) - analog CatalogSource.layer_template.
  layer_template?: Record<string, unknown> | null;
}

export interface ApiCatalogResponse {
  apis: CatalogApi[];
}

export interface ApiSpecResponse {
  api_id: string;
  spec_url: string;
  spec_type?: string | null;
  content: string;
  truncated: boolean;
}

// --- Operationen-Katalog ------------------------------------------------

export interface ParamSpec {
  type: "number" | "integer" | "string" | "boolean" | "enum" | "array" | "object" | string;
  required?: boolean;
  default?: unknown;
  description?: string;
  choices?: string[];
  minimum?: number;
  /** true: der Grenzwert `minimum` selbst ist nicht erlaubt (Wert > minimum). */
  exclusive_minimum?: boolean;
  item_type?: "number" | "string" | "boolean";
}

export interface OperationEntry {
  op: string;
  input_ports: Record<string, "vector" | "raster" | "graph" | string>;
  output_type: "vector" | "raster" | "graph" | string;
  required_params: string[];
  params: Record<string, ParamSpec>;
  doc?: string | null;
}

// --- Validierung / Generierung -----------------------------------------

export interface ValidationIssue {
  location: string;
  message: string;
  type?: string | null;
}

// --- Parameter, Expansion, Herkunft, Lizenzen (FA58/FA59/FA60/FA65) ---------
//
// Spiegeln die Web-Modelle aus W3-02 (backend/geofact_web/models.py) bzw. die
// to_dict()-Formen des Kerns: core/expansion.py (ParameterSpec, NodeOrigin),
// core/types.py (LayerProvenance), engine/region.py (RegionInfo),
// core/provenance.py (NodeAttribution). Alle neuen Felder sind optional:
// ein älteres Backend ohne sie bleibt bedienbar.

// Ein deklarierter Szenario-Parameter (FA59) - die Felder von ParameterSpec
// plus Name, wirksamer Wert und ob er überschrieben wurde.
export type ParameterType = "string" | "integer" | "float" | "boolean" | "list" | "mapping";

export interface ParameterInfo {
  name: string;
  type: ParameterType | string;
  items?: "string" | "integer" | "float" | "boolean" | "mapping" | string | null;
  default?: unknown;
  description?: string | null;
  choices?: unknown[] | null;
  // Wirksamer Wert nach Überschreibung (sonst der Default).
  value?: unknown;
  overridden?: boolean;
  // true: kein Default - der Wert muss gesetzt werden.
  required?: boolean;
}

// Herkunft eines Knotens im ausgedehnten Szenario (NodeOrigin.to_dict()).
// FA75: Herkunft eines Knotens (NodeOrigin.to_dict). module null = steht so in
// der YAML; sonst erzeugt aus Modul `module` in Instanz `instance` (Element
// `key`, null ohne foreach; Knoten `inner_id` im Modul).
export interface NodeOriginOut {
  section: string;
  index: number;
  module?: string | null;
  instance?: string | null;
  key?: string | null;
  inner_id?: string | null;
  location: string;
}

// FA75: eine Instanz mit ihren Schlüsseln (Listenreihenfolge) und den durch
// when entfernten Schlüsseln.
export interface InstanceOut {
  id: string;
  module: string;
  keys: string[];
  dropped_keys: string[];
}

export interface ExpansionInfo {
  node_count: number;
  overridden?: string[];
  // (Quellposition, when-Ausdruck) je durch when entferntem Element.
  dropped?: ([string, string] | { location: string; expression: string })[];
  instances?: InstanceOut[];
  node_origins?: Record<string, NodeOriginOut>;
}

export interface LicenseOut {
  name: string;
  attribution?: string | null;
  url?: string | null;
  // FA73: maschinenlesbare Kennung (DALICC-URI), nur wenn deklariert.
  id?: string | null;
}

// FA73: vom Nutzer festgelegte Ausgabelizenz einer Ausgabe (ungeprüft).
export interface OutputLicenseInfo {
  index: number;
  source: string;
  type: string;
  license: LicenseOut;
  declared_at: "output" | "scenario";
  line: string;
}

// FA73: Ergebnis der DALICC-Prüfung (POST /api/config/licenses).
export type LicenseStatus =
  | "compatible"
  | "conflicts"
  | "not_checkable"
  | "unreachable"
  | "no_licenses";

export interface LicenseCheckOut {
  index: number;
  source: string;
  type: string;
  output_license?: LicenseOut | null;
  declared_at?: "output" | "scenario" | null;
  source_licenses: LicenseOut[];
  undeclared: string[];
  status: LicenseStatus;
  message: string;
  conflicts: { kind: string; reason: string; message: string }[];
  uncheckable: { name: string; reason: string }[];
  checked_at?: string | null;
}

export interface LicenseCheckResponse {
  valid: boolean;
  issues: ValidationIssue[];
  status?: LicenseStatus | null;
  checks: LicenseCheckOut[];
}

// Lizenzen eines Knotens bzw. eines Laufs (NodeAttribution.to_dict()).
export interface AttributionOut {
  licenses: LicenseOut[];
  // Layer ohne Lizenzangabe, die zu diesem Knoten beitragen.
  undeclared: string[];
  lines?: string[];
}

// CRS-Herkunft eines geladenen Layers (LayerProvenance.to_dict(), FA58).
export interface LayerProvenanceOut {
  layer: string;
  source: string;
  source_crs: string | null;
  source_crs_label: string;
  crs_override: boolean;
  target_crs: string;
  target_crs_label: string;
  raw_bounds: [number, number, number, number] | null;
  bounds: [number, number, number, number] | null;
  feature_count: number | null;
  raw_sample?: [number, number][];
  sample?: [number, number][];
}

// Aufgelöste Region und Arbeits-CRS des Laufs (RegionInfo.to_dict(), FA55/FA56).
export interface RegionInfoOut {
  crs: string;
  crs_label: string;
  crs_mode: "auto" | "declared" | string;
  crs_spec: string;
  bbox: [number, number, number, number];
  area_km2: number;
  display_name?: string | null;
  osm_class?: string | null;
  osm_type?: string | null;
  warnings: string[];
}

export type WarningSeverity = "info" | "warning";

// Eine Warnung mit Schwere (RunWarning): info = Hinweis (Kategorie endet auf
// "Notice"), warning = Warnung. Lauf-Ebene (Region) ohne step/layer.
export interface RunWarningOut {
  message: string;
  category?: string | null;
  severity?: WarningSeverity | string | null;
  layer?: string | null;
  step?: string | null;
}

export interface ValidateResponse {
  valid: boolean;
  issues: ValidationIssue[];
  execution_order?: string[] | null;
  required_layers?: string[] | null;
  unused_layers?: string[] | null;
  parameters?: ParameterInfo[];
  expansion?: ExpansionInfo | null;
  attribution?: AttributionOut | null;
  // FA73: Ausgabelizenzen (ohne Prüfung).
  output_licenses?: OutputLicenseInfo[];
  // FA75: Schritte des ausgedehnten Szenarios für den Graphen vor dem Lauf.
  planned_steps?: PlannedStepOut[] | null;
}

// FA75: ein Schritt des Plans vor dem Lauf (Eingänge port -> Layer-/Step-id).
export interface PlannedStepOut {
  id: string;
  op: string;
  inputs: Record<string, string>;
}

// FA78: Probelauf der erzeugten Konfiguration ("skipped" = nicht ausgeführt,
// "timeout" = nach der Frist nicht fertig, also ungeprüft).
export interface TrialRunOut {
  status: "ok" | "failed" | "timeout" | "skipped";
  error?: string | null;
  warnings: string[];
  repair_rounds: number;
}

// FA87: ein Datensatz, den die Frage braucht und den das Sprachmodell in keiner
// Quelle findet ("hint" = was der Nutzer hochladen kann, "alternative" = eine
// umformulierte Frage, die sich mit den verfügbaren Daten beantworten lässt).
export interface MissingDataset {
  name: string;
  reason?: string | null;
  hint?: string | null;
  alternative?: string | null;
}

export interface GenerateResponse {
  config_yaml: string;
  validation: ValidateResponse;
  model: string;
  notes?: string | null;
  trial_run?: TrialRunOut;
  // FA87: nicht leer = dem Modell fehlen Daten; config_yaml ist dann leer.
  missing_data?: MissingDataset[];
}

// --- Szenarien (speichern/laden) ----------------------------------------

export interface SavedScenario {
  // Eindeutige Lade-Referenz (bei "user" der Dateiname, bei "example" der
  // Pfad relativ zu examples/ ohne Endung, z. B. "leipzig/leipzig10_erreichbarkeit").
  id: string;
  // Menschenlesbarer Anzeigename (scenario.name aus dem YAML bei Beispielen).
  name: string;
  source: "user" | "example";
  description?: string | null;
}

export interface ScenarioContent {
  name: string;
  config_yaml: string;
}

// --- Ausführung / Run-Status --------------------------------------------

export type StepStatus = "pending" | "loading" | "running" | "done" | "error" | "cancelled";
export type RunStatusValue = "pending" | "running" | "done" | "error" | "cancelled";

export interface StepState {
  id: string;
  op?: string | null;
  output_type?: string | null;
  // port -> Layer-/Step-id, das diesen Eingang liefert.
  inputs: Record<string, string>;
  status: StepStatus;
  duration_ms?: number | null;
  error?: string | null;
  warnings: string[];
  // Schwere je Warnung (gleiche Reihenfolge wie warnings, soweit bekannt) -
  // aus detail.severity der Ereignisse step_warning/layer_warning.
  warning_details?: RunWarningOut[];
}

export interface RunStatus {
  run_id: string;
  status: RunStatusValue;
  order: string[];
  steps: Record<string, StepState>;
  layers: string[];
  loaded_layers: string[];
  // Ladezeit je Layer (Millisekunden) - gehört zur Quelle, nicht zum
  // ersten Schritt, der sie braucht.
  layer_load_ms: Record<string, number>;
  error?: string | null;
  // FA60: Herkunft erzeugter Knoten (foreach/use) je Knoten-id.
  origins?: Record<string, NodeOriginOut>;
  // FA58: CRS-Herkunft je geladenem Layer.
  layer_provenance?: Record<string, LayerProvenanceOut>;
  // FA55/FA56: Region und Arbeits-CRS (Ereignis region_ready).
  region?: RegionInfoOut | null;
  // FA65: Lizenzen der Ausgabequellen des Laufs (run_done).
  attribution?: AttributionOut | null;
  // FA65: Lizenzen je Knoten aus dem Plan (plan.detail.attribution).
  node_attribution?: Record<string, AttributionOut | null>;
  // FA56: Warnungen auf Lauf-Ebene (Regionswarnungen).
  warnings?: (RunWarningOut | string)[];
  // FA66: freigegebene Knoten - in der Web-Demo immer leer (FA33).
  released?: string[];
}

export interface RunCreated {
  run_id: string;
  status: string;
}

// --- Knoten-Ergebnis (presentation/serialize.py) --------------------------------------

export interface RasterStats {
  min: number;
  max: number;
  mean: number;
  p2: number;
  p98: number;
  valid_count: number;
}

export interface DescribeVector {
  kind: "vector";
  feature_count: number;
  geometry_types: string[];
  crs: string | null;
  columns: Record<string, string>;
  numeric_fields: string[];
  bounds: [number, number, number, number] | null;
}

export interface DescribeRaster {
  kind: "raster";
  shape: [number, number];
  dtype: string;
  crs: string | null;
  bounds: [number, number, number, number] | null;
  stats: RasterStats | null;
  path?: string | null;
  numeric_fields?: undefined;
}

export interface DescribeGraph {
  kind: "graph";
  node_count: number;
  edge_count: number;
  node_attributes: string[];
  crs: string | null;
  numeric_fields?: undefined;
}

export type NodeDescribe = DescribeVector | DescribeRaster | DescribeGraph;

export interface GraphNode {
  id: string | number;
  lon: number | null;
  lat: number | null;
  [key: string]: unknown;
}

export interface GraphEdge {
  source: string | number;
  target: string | number;
  [key: string]: unknown;
}

export interface NodeLinkGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface NodeResult {
  node_id: string;
  describe: NodeDescribe;
  // Nur bei Vektor-Ergebnissen vorhanden.
  geojson?: GeoJSON.FeatureCollection;
  // Nur bei Graph-Ergebnissen vorhanden.
  graph?: NodeLinkGraph;
  // Nur bei Raster-Ergebnissen vorhanden (separater Binärendpunkt).
  raster_png_url?: string;
  // Anzahl Attributspalten, die die Ansicht nicht ausliefert (breite
  // OSM-Layer haben hunderte fast leere Spalten). Vollständig im
  // ZIP-Download.
  dropped_columns?: number;
  // Tatsächlich angewendete Objektgrenze dieser Antwort und der vom
  // Server erlaubte Höchstwert - Grundlage für "mehr laden".
  feature_limit?: number;
  max_feature_limit?: number;
  // FA65: Lizenzen der Layer, aus denen dieser Knoten entstand.
  attribution?: AttributionOut | null;
}

// --- Dateien eines Laufs (FA88) und Liste der Läufe (FA89) ---------------------

export interface RunFile {
  name: string;
  // output = geschriebene Ausgabe, companion = scenario.yaml, ATTRIBUTION.txt, parameters.yaml
  role: "output" | "companion";
  size_bytes: number;
  // Nur für Ausgaben, und nur wenn das Backend sie output[i] zuordnen konnte.
  type?: string | null;
  source?: string | null;
}

export interface SkippedOutput {
  index: number;
  type: string;
  source?: string | null;
  reason: string;
}

export interface RunOutputs {
  run_id: string;
  status: RunStatusValue;
  files: RunFile[];
  skipped: SkippedOutput[];
}

export interface RunSummary {
  run_id: string;
  status: RunStatusValue;
  scenario_name: string;
  created_at: string;
  finished_at?: string | null;
  step_count: number;
  output_count: number;
  skipped_count: number;
  parameters?: Record<string, unknown> | null;
}

export interface RunHistory {
  runs: RunSummary[];
  max_retained: number;
}
