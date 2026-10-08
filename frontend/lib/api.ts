// Dünner HTTP-Client für das GeoFACT-Web-Backend (FastAPI).
//
// Basis-URL kommt aus NEXT_PUBLIC_API_BASE (siehe .env.local.example);
// Default passt zum lokalen `uv run --extra web geofact-api`-Start.
// Siehe docs/web_demo.md für den vollständigen Endpunkt-Überblick.

import type {
  ApiCatalogResponse,
  ApiSpecResponse,
  Catalog,
  GenerateResponse,
  LicenseCheckResponse,
  LoginResponse,
  Meta,
  NodeResult,
  OperationEntry,
  RunCreated,
  RunHistory,
  RunOutputs,
  RunStatus,
  SavedScenario,
  ScenarioContent,
  UploadResponse,
  ValidateResponse,
} from "./types";
import { getToken, withTokenParam } from "./auth";
import type { GenerationOptions } from "./generationSettings";

// Zentrale Basis-URL-Auflösung - auch für Module außerhalb dieses
// Clients (z. B. map-view.tsx für Raster-Bild-URLs), damit nicht zwei
// Stellen mit leicht unterschiedlicher Fallback-Semantik (?? vs ||)
// denselben Wert herleiten.
export const API_BASE = (process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000").replace(/\/$/, "");
const BASE = API_BASE;

// HTTP-Fehler des Backends mit Statuscode. Die Oberfläche entscheidet am
// Status (z. B. 404 = Lauf unbekannt, FA54), nie am Text der Meldung - der
// Text darf sich ändern, der Status ist der Vertrag.
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function isNotFoundError(e: unknown): boolean {
  return e instanceof ApiError && e.status === 404;
}

// FA54: Läufe liegen im Arbeitsspeicher EINES Backend-Prozesses. Ein 404 für
// den aktuellen Lauf heißt deshalb meist: das Backend wurde neu gestartet
// (auch durch Auto-Reload) oder eine andere Backend-Instanz hat geantwortet.
export const RUN_GONE_MESSAGE =
  "Der Lauf ist dem Backend nicht mehr bekannt – z. B. nach einem Neustart des Backends " +
  "oder weil eine andere Backend-Instanz geantwortet hat. Bitte das Szenario erneut ausführen.";

// Wird von app-shell.tsx gesetzt: einmalig aufgerufen, wenn eine Anfrage
// mit 401 scheitert - egal ob ohne Token (GEOFACT_WEB_USERS erst gerade
// aktiviert) oder mit abgelaufenem Token. Zeigt den Login-Screen, ohne
// dass jede einzelne Aufrufstelle den Fall selbst behandeln muss.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

// Anfrage mit Token; eine Antwort außerhalb von 2xx ist ein ApiError.
async function send(path: string, init?: RequestInit): Promise<Response> {
  const token = getToken();
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (res.status === 401) {
    onUnauthorized?.();
  }
  if (!res.ok) {
    throw new ApiError(await extractError(res), res.status);
  }
  return res;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await send(path, init);
  if (res.status === 204) return undefined as T;
  try {
    return (await res.json()) as T;
  } catch {
    // res.ok war true, aber der Body ist kein gültiges JSON - z. B. eine
    // HTML-Fehlerseite eines vorgeschalteten Proxys. Ohne diesen Fang würde
    // hier eine kryptische "Unexpected token <" JSON-Parse-Exception
    // durchschlagen statt einer verständlichen Fehlermeldung.
    throw new Error(`Antwort von ${path} war kein gültiges JSON.`);
  }
}

async function extractError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && typeof detail.message === "string") {
      const issues = Array.isArray(detail.issues)
        ? detail.issues.map((i: { location?: string; message?: string }) => `${i.location}: ${i.message}`).join("; ")
        : "";
      return issues ? `${detail.message} (${issues})` : detail.message;
    }
    return `${res.status} ${res.statusText}`;
  } catch {
    return `${res.status} ${res.statusText}`;
  }
}

function json(body: unknown): RequestInit {
  return { method: "POST", body: JSON.stringify(body) };
}

// FA59: Parameter-Überschreibungen nur senden, wenn es welche gibt - ein
// Szenario ohne Parameter schickt denselben Body wie bisher.
function withParameters(parameters?: Record<string, unknown>): { parameters?: Record<string, unknown> } {
  return parameters && Object.keys(parameters).length > 0 ? { parameters } : {};
}

export const api = {
  // --- Auth (FA24) ----------------------------------------------------
  // Login selbst läuft ohne Token (siehe request() - kein gespeichertes
  // Token vorhanden, kein Authorization-Header gesendet).
  login: (username: string, password: string) =>
    request<LoginResponse>("/api/auth/login", json({ username, password })),

  // --- Meta ---------------------------------------------------------
  meta: () => request<Meta>("/api/meta"),
  operations: () => request<OperationEntry[]>("/api/operations"),
  schema: () => request<Record<string, unknown>>("/api/schema"),

  // --- Katalog / Uploads ---------------------------------------------
  // Uploads sind seit FA28 pro Nutzer isoliert - catalog() zeigt nur
  // eigene Uploads, deleteUpload() löscht nur, was der eigene Katalog
  // auch anzeigt (fremde source_id -> 404, siehe routers/uploads.py).
  catalog: () => request<Catalog>("/api/catalog"),
  upload: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<UploadResponse>("/api/uploads", { method: "POST", body: form });
  },
  deleteUpload: (sourceId: string) =>
    request<void>(`/api/uploads/${encodeURIComponent(sourceId)}`, { method: "DELETE" }),

  // --- Konfiguration ----------------------------------------------------
  // parameters (FA59): Überschreibungen der deklarierten Szenario-Parameter;
  // leer/undefined = Defaults (dann wird das Feld gar nicht gesendet).
  validate: (configYaml: string, parameters?: Record<string, unknown>) =>
    request<ValidateResponse>(
      "/api/config/validate",
      json({ config_yaml: configYaml, ...withParameters(parameters) })
    ),
  // FA73: Lizenzen je Ausgabe gegen DALICC prüfen (Netz, nur auf Knopfdruck).
  checkLicenses: (configYaml: string, parameters?: Record<string, unknown>) =>
    request<LicenseCheckResponse>(
      "/api/config/licenses",
      json({ config_yaml: configYaml, ...withParameters(parameters) })
    ),
  generate: (prompt: string, sourceIds: string[], region?: string) =>
    request<GenerateResponse>(
      "/api/config/generate",
      json({ prompt, source_ids: sourceIds, region: region ?? null })
    ),
  // SSE-Streaming-Variante: liefert YAML-Text-Deltas während der LLM-Antwort
  // über Callbacks, danach das validierte Endergebnis (oder einen Fehler).
  // onThinking/onPhase (FA31) sind optional: Reasoning-Text läuft auf einem
  // eigenen Kanal (er gehört NICHT in die YAML), phase meldet den Wechsel
  // von der Generierung zur Validierung/Reparatur - ohne das wirkte die UI
  // während einer langen Denkphase oder Reparatur-Runde hängend.
  generateStream: async (
    prompt: string,
    sourceIds: string[],
    region: string | undefined,
    onToken: (delta: string) => void,
    signal?: AbortSignal,
    onThinking?: (delta: string) => void,
    onPhase?: (phase: string) => void,
    // FA81: Fristen/Probelauf dieser Anfrage; leer = Werte des Servers.
    options?: GenerationOptions
  ): Promise<GenerateResponse> => {
    const token = getToken();
    const res = await fetch(`${BASE}/api/config/generate/stream`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({
        prompt,
        source_ids: sourceIds,
        region: region ?? null,
        ...(options && Object.keys(options).length > 0 ? { options } : {}),
      }),
      signal,
    });
    if (res.status === 401) {
      onUnauthorized?.();
    }
    if (!res.ok || !res.body) {
      throw new ApiError(await extractError(res), res.status);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let result: GenerateResponse | null = null;
    let streamError: string | null = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split("\n\n");
      buffer = events.pop() ?? "";
      for (const raw of events) {
        const eventLine = raw.split("\n").find((l) => l.startsWith("event:"));
        const dataLine = raw.split("\n").find((l) => l.startsWith("data:"));
        if (!eventLine || !dataLine) continue;
        const event = eventLine.slice("event:".length).trim();
        const data = JSON.parse(dataLine.slice("data:".length).trim());
        if (event === "token") onToken(data.delta as string);
        else if (event === "thinking") onThinking?.(data.delta as string);
        else if (event === "phase") onPhase?.(data.phase as string);
        else if (event === "done") result = data as GenerateResponse;
        else if (event === "error") streamError = data.error as string;
      }
    }
    if (streamError) throw new Error(streamError);
    if (!result) throw new Error("Stream endete ohne Ergebnis.");
    return result;
  },

  // --- API-Katalog (FA36) -------------------------------------------
  listApis: () => request<ApiCatalogResponse>("/api/apis"),
  // Spezifikation wird bewusst erst auf Anfrage geladen: der Katalog
  // selbst bleibt offline nutzbar, und ein langsamer Fremd-Endpunkt darf
  // das Laden der Oberfläche nicht blockieren.
  apiSpec: (apiId: string) =>
    request<ApiSpecResponse>(`/api/apis/${encodeURIComponent(apiId)}/spec`),

  // --- Szenarien ----------------------------------------------------
  listScenarios: () => request<SavedScenario[]>("/api/scenarios"),
  saveScenario: (name: string, configYaml: string) =>
    request<SavedScenario>("/api/scenarios", json({ name, config_yaml: configYaml })),
  // Eigene Szenarien liegen unter /api/scenarios/{id}, die eingecheckten
  // Beispiele (schreibgeschützt) unter /api/scenarios/examples/{id} - id
  // ist bei Beispielen ein Pfad (z. B. "leipzig/leipzig10_erreichbarkeit"),
  // daher segmentweise statt komplett über encodeURIComponent kodieren
  // (sonst würden die Slashes mit escaped).
  loadScenario: (scenario: SavedScenario) =>
    scenario.source === "example"
      ? request<ScenarioContent>(`/api/scenarios/examples/${scenario.id.split("/").map(encodeURIComponent).join("/")}`)
      : request<ScenarioContent>(`/api/scenarios/${encodeURIComponent(scenario.id)}`),
  deleteScenario: (name: string) =>
    request<{ name: string; deleted: boolean }>(`/api/scenarios/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  // --- Ausführung ----------------------------------------------------
  createRun: (
    configYaml: string,
    refreshSnapshots = false,
    parameters?: Record<string, unknown>,
    // FA82: Einstellungen dieses Laufs (OSM-Quelle); leer = Werte des Servers.
    options?: { osm_backend?: string }
  ) =>
    request<RunCreated>(
      "/api/runs",
      json({
        config_yaml: configYaml,
        refresh_snapshots: refreshSnapshots,
        ...withParameters(parameters),
        ...(options && Object.keys(options).length > 0 ? { options } : {}),
      })
    ),
  runStatus: (runId: string) => request<RunStatus>(`/api/runs/${runId}`),
  // FA54: ist der Lauf dem Backend (noch) bekannt? Nur ein 404 der Statusabfrage
  // sagt "nein". Netzfehler und 5xx beweisen das nicht (das Backend kann gerade
  // neu starten) und gelten als "nicht verloren" - die ursprüngliche Meldung
  // bleibt dann stehen. Auch ein 404 am Knoten-Endpunkt kann zwei Dinge heißen
  // (Lauf unbekannt oder Knoten ohne Ergebnis) - diese Probe trennt beides.
  runIsGone: async (runId: string): Promise<boolean> => {
    try {
      await request<RunStatus>(`/api/runs/${runId}`);
      return false;
    } catch (e) {
      return isNotFoundError(e);
    }
  },
  cancelRun: (runId: string) =>
    request<{ run_id: string; cancelling: boolean }>(`/api/runs/${runId}/cancel`, { method: "POST" }),
  // EventSource/<img>/<a> können keinen Authorization-Header setzen -
  // withTokenParam() hängt das Token als ?token= an (siehe lib/auth.ts
  // und die passende, auf vier Routen begrenzte Server-Ausnahme in
  // backend/geofact_web/auth.py::require_auth_middleware).
  eventsUrl: (runId: string) => withTokenParam(`${BASE}/api/runs/${runId}/events`),
  // limit: gewünschte Objektzahl der Ansicht; ohne Angabe entscheidet
  // der Server (schneller Default). Die Antwort meldet in feature_limit /
  // max_feature_limit, was tatsächlich gilt.
  nodeResult: (runId: string, nodeId: string, limit?: number) =>
    request<NodeResult>(
      `/api/runs/${runId}/nodes/${encodeURIComponent(nodeId)}` +
        (limit ? `?limit=${limit}` : "")
    ),
  nodeRasterPngUrl: (runId: string, nodeId: string) =>
    withTokenParam(`${BASE}/api/runs/${runId}/nodes/${encodeURIComponent(nodeId)}/raster.png`),
  downloadUrl: (runId: string) => withTokenParam(`${BASE}/api/runs/${runId}/download`),
  // FA88: die Dateien des Laufs (dieselbe Aufzählung wie das ZIP) und eine davon.
  runOutputs: (runId: string) => request<RunOutputs>(`/api/runs/${runId}/outputs`),
  runFileUrl: (runId: string, name: string) =>
    withTokenParam(`${BASE}/api/runs/${runId}/outputs/${encodeURIComponent(name)}`),
  runFileText: async (runId: string, name: string): Promise<string> =>
    (await send(`/api/runs/${runId}/outputs/${encodeURIComponent(name)}`)).text(),
  // FA89: die Läufe des Nutzers, die das Backend noch hält (neuester zuerst).
  listRuns: () => request<RunHistory>("/api/runs"),
};
