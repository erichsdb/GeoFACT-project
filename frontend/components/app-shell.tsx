"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { load as loadYaml } from "js-yaml";
import { Save, FolderOpen, Link2, ChevronRight, Square, Play, ArrowLeft, Waypoints, Loader2, LogOut, Sparkles, PencilLine, FilePlus2 } from "lucide-react";
import { BLANK_TEMPLATE, canInsertTemplate } from "@/lib/blankTemplate";
import { cancelRunStatus } from "@/lib/runCancel";
import { api, isNotFoundError, RUN_GONE_MESSAGE } from "@/lib/api";
import { clearAuth, getUsername } from "@/lib/auth";
import { insertLayer, insertStep } from "@/lib/configEdit";
import {
  ENTRY_GUIDE,
  ENTRY_MODES,
  ENTRY_MODE_STORAGE_KEY,
  canResume,
  forgetEntryMode,
  initialEntryMode,
  initialEntryView,
  showsEditor,
  viewForMode,
  type EntryMode,
  type EntryView,
} from "@/lib/entryMode";
import {
  CONFIG_ORIGIN_STORAGE_KEY,
  configGuideText,
  originNotes,
  parseOriginRecord,
  promptValues,
  serializeOriginRecord,
  type OriginRecord,
} from "@/lib/configOrigin";
import {
  RUN_BASIS_STORAGE_KEY,
  isStaleRun,
  parseRunBasis,
  serializeRunBasis,
  type RunBasis,
} from "@/lib/runBasis";
import { cn } from "@/lib/utils";
import {
  GENERATION_SETTINGS_STORAGE_KEY,
  parseStoredOptions,
  runOptions,
  serverSettings,
  type GenerationOptions,
} from "@/lib/generationSettings";
import GenerationSettings from "@/components/generation-settings";
import { notify } from "@/lib/toast";
import { formatCount, formatDecimal } from "@/lib/format";
import {
  buildShareUrl,
  linkErrorAction,
  parseScenarioParam,
  planLinkOpen,
  refKey,
  urlWithScenario,
  type ScenarioRef,
} from "@/lib/shareLink";
import type {
  AttributionOut,
  Catalog,
  CatalogSource,
  LayerProvenanceOut,
  Meta,
  OperationEntry,
  ParameterInfo,
  RegionInfoOut,
  RunStatus,
  RunSummary,
  RunWarningOut,
  SavedScenario,
  StepState,
  ValidateResponse,
} from "@/lib/types";
import SourcePanel from "@/components/source-panel";
import OperationsPanel from "@/components/operations-panel";
import ApiPanel from "@/components/api-panel";
import PromptPanel from "@/components/prompt-panel";
import ConfigEditor from "@/components/config-editor";
import LicenseCheckPanel, { licenseCheckKey } from "@/components/license-check";
import RunGraph from "@/components/run-graph";
import NodeInspector from "@/components/node-inspector";
import Stepper, { type WizardStep } from "@/components/stepper";
import ParameterPanel from "@/components/parameter-panel";
import OriginBar from "@/components/origin-bar";
import { RunFilesButton, RunHistoryDialog, type RunDownloadHandler } from "@/components/run-outputs";

// Seitenleisten-Reiter: Quellenkatalog, API-Katalog (FA36), Operationen.
type SidebarTab = "sources" | "apis" | "operations";
import SaveScenarioDialog from "@/components/save-scenario-dialog";
import ScenarioPicker from "@/components/scenario-picker";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Info, TriangleAlert } from "lucide-react";

const CONFIG_YAML_STORAGE_KEY = "geofact:configYaml";
const STEP_STORAGE_KEY = "geofact:step";
const RUN_ID_STORAGE_KEY = "geofact:runId";
const SELECTED_STORAGE_KEY = "geofact:selectedSources";
// FA86: Prompt und Region der Erzeugung (gehören der Seite, nicht dem Panel).
const PROMPT_STORAGE_KEY = "geofact:prompt";
const REGION_STORAGE_KEY = "geofact:promptRegion";

function readLocalStorage(key: string): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(key);
}

// FA72: Tab-weite Marke "die App hat diese Referenz selbst in die Adresse
// geschrieben" - unterscheidet Neuladen des eigenen Tabs von einem Link.
const OWN_LINK_STORAGE_KEY = "geofact:ownScenarioLink";

function readOwnLinkKey(): string | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage.getItem(OWN_LINK_STORAGE_KEY);
  } catch {
    return null;
  }
}

// FA80: der Einstieg gilt je Tab-Sitzung - eine neue Sitzung fragt wieder.
function readSessionEntryMode(): string | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage.getItem(ENTRY_MODE_STORAGE_KEY);
  } catch {
    return null;
  }
}

export default function AppShell() {
  // FA72: ein Link `?scenario=...` gewinnt gegen den wiederhergestellten
  // Browserzustand (YAML, Schritt, Lauf) - nur im Browser lesbar (ssr: false).
  const [urlScenario] = useState(() =>
    typeof window === "undefined" ? ({ kind: "none" } as const) : parseScenarioParam(window.location.search)
  );
  // FA72: Vorrang vor localStorage gilt nur für einen geöffneten Link ("open"),
  // nicht für das Neuladen des eigenen Tabs ("restore": die Adresse hat die App
  // selbst gesetzt) und nicht für einen unlesbaren Parameter.
  const [linkMode] = useState(() => planLinkOpen(urlScenario, readOwnLinkKey()));
  const [meta, setMeta] = useState<Meta | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  // Nur gesetzt, wenn tatsächlich ein Login stattfand (FA24) - ohne
  // konfigurierte GEOFACT_WEB_USERS bleibt das null, kein Logout-Button.
  const [username] = useState<string | null>(() => getUsername());
  const [selected, setSelected] = useState<Set<string>>(() => {
    try {
      const raw = readLocalStorage(SELECTED_STORAGE_KEY);
      return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
    } catch {
      return new Set();
    }
  });
  const [configYaml, setConfigYaml] = useState(() => readLocalStorage(CONFIG_YAML_STORAGE_KEY) ?? "");
  // FA72: zuletzt geladenes Szenario samt geladenem Text. Der Link gilt nur,
  // solange der Editor genau diesen Text zeigt (siehe `shareRef`).
  const [loadedScenario, setLoadedScenario] = useState<(ScenarioRef & { yaml: string }) | null>(() =>
    linkMode === "restore" && urlScenario.kind === "ok"
      ? { ...urlScenario.ref, yaml: readLocalStorage(CONFIG_YAML_STORAGE_KEY) ?? "" }
      : null
  );
  const loadedScenarioRef = useRef(loadedScenario);
  useEffect(() => {
    loadedScenarioRef.current = loadedScenario;
  }, [loadedScenario]);
  const configYamlRef = useRef(configYaml);
  useEffect(() => {
    configYamlRef.current = configYaml;
  }, [configYaml]);
  const linkLoadStarted = useRef(false);
  // FA86: Prompt und Region der Erzeugung - über Schrittwechsel und Neuladen hinweg.
  const [prompt, setPrompt] = useState(() => readLocalStorage(PROMPT_STORAGE_KEY) ?? "");
  const [region, setRegion] = useState(() => readLocalStorage(REGION_STORAGE_KEY) ?? "");
  // FA86: wo der Editortext herkommt (samt Text von damals). Unlesbar oder nie
  // gesetzt = null = unbekannte Herkunft. Beim Neuladen des eigenen Tabs mit
  // Szenario-Link gilt das verlinkte Szenario.
  const [originRecord, setOriginRecord] = useState<OriginRecord | null>(() => {
    const stored = parseOriginRecord(readLocalStorage(CONFIG_ORIGIN_STORAGE_KEY));
    if (linkMode === "restore" && urlScenario.kind === "ok") {
      const { id, source } = urlScenario.ref;
      if (stored?.origin.kind === "load" && stored.origin.id === id && stored.origin.source === source) return stored;
      return {
        origin: { kind: "load", id, source, name: id },
        yaml: readLocalStorage(CONFIG_YAML_STORAGE_KEY) ?? "",
      };
    }
    return stored;
  });
  // FA86: womit der gemerkte Lauf gestartet wurde (YAML + wirksame Parameter).
  const [runBasis, setRunBasis] = useState<RunBasis | null>(() =>
    parseRunBasis(readLocalStorage(RUN_BASIS_STORAGE_KEY))
  );
  // FA86: läuft gerade eine Erzeugung? Dann überschreibt "Prompt anpassen" den Prompt nicht.
  const generatingRef = useRef(false);
  const onGeneratingChange = useCallback((busy: boolean) => {
    generatingRef.current = busy;
  }, []);
  const [validation, setValidation] = useState<ValidateResponse | null>(null);
  const [validating, setValidating] = useState(false);
  // FA59: Überschreibungen der Szenario-Parameter (Name -> Wert). Nur Namen,
  // die die YAML aktuell deklariert, gehen an validate/run (effectiveParams).
  const [paramOverrides, setParamOverrides] = useState<Record<string, unknown>>({});

  const [runId, setRunId] = useState<string | null>(() => readLocalStorage(RUN_ID_STORAGE_KEY));
  // FA54: der Lauf, den das Backend nicht mehr kennt (Neustart, andere
  // Backend-Instanz). Solange gesetzt, steht ein Hinweis mit "Erneut ausführen"
  // über der Ansicht; der Lauf selbst ist dann schon aus runId/localStorage entfernt.
  const [goneRunId, setGoneRunId] = useState<string | null>(null);
  // Spiegel von runId für asynchrone Rückrufe (Knoten-Abruf, Ereignisstrom,
  // Download-Probe): sie sollen nur den AKTUELLEN Lauf verwerfen, nie einen
  // später gestarteten, und ein zweiter Rückruf zum selben Lauf ist wirkungslos.
  const runIdRef = useRef<string | null>(runId);
  useEffect(() => {
    runIdRef.current = runId;
  }, [runId]);
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [nodeVersion, setNodeVersion] = useState(0);
  const esRef = useRef<EventSource | null>(null);
  // B10: POST /api/runs läuft (Ref gegen Doppelklick im selben Render, State für die Anzeige).
  const startingRef = useRef(false);
  const [starting, setStarting] = useState(false);

  const [sidebarTab, setSidebarTab] = useState<SidebarTab>("sources");
  const [scenarios, setScenarios] = useState<SavedScenario[]>([]);
  const [saveModalOpen, setSaveModalOpen] = useState(false);

  // FA81-FA83: Einstellungen (Fristen, Probelauf, OSM-Quelle, Sprachmodell), wie
  // der Nutzer sie in diesem Browser gewählt hat; leer = Werte des Servers.
  const [generationOptions, setGenerationOptions] = useState<GenerationOptions>(() =>
    parseStoredOptions(readLocalStorage(GENERATION_SETTINGS_STORAGE_KEY))
  );
  useEffect(() => {
    window.localStorage.setItem(GENERATION_SETTINGS_STORAGE_KEY, JSON.stringify(generationOptions));
  }, [generationOptions]);
  // Werte, Grenzen und Auswahl des Servers (null, solange /api/meta fehlt).
  const settingsServer = useMemo(() => serverSettings(meta), [meta]);
  // Was der Server heute nicht mehr annimmt (Grenzen, Auswahl), fällt beim Senden weg.
  const sentGenerationOptions = useMemo(
    () => parseStoredOptions(JSON.stringify(generationOptions), settingsServer),
    [generationOptions, settingsServer]
  );

  // FA80: der gewählte Einstieg in Schritt 1 (null = die Frage ist offen) und,
  // für "generate"/"load", ob die Eingabe oder die Konfiguration zu sehen ist.
  // Nur ein geöffneter Link setzt ihn vorab; beim Neuladen des eigenen Tabs
  // gilt die Wahl der Sitzung.
  const [entryMode, setEntryMode] = useState<EntryMode | null>(() =>
    initialEntryMode(readSessionEntryMode(), linkMode === "open")
  );
  const [entryView, setEntryView] = useState<EntryView>(() =>
    // Ein geöffneter Link ersetzt den gemerkten Text; bis er geladen ist, zeigt
    // die Auswahl den verlinkten Eintrag (openLink schaltet danach um).
    linkMode === "open" ? "input" : initialEntryView(entryMode, readLocalStorage(CONFIG_YAML_STORAGE_KEY) ?? "")
  );
  const chooseEntryMode = useCallback((mode: EntryMode) => {
    setEntryMode(mode);
    setEntryView(mode === "write" ? "config" : "input");
  }, []);
  // FA86: Einstieg wechseln (Kopfzeile): es gilt die zuletzt gezeigte Ansicht
  // dieses Einstiegs, der Editortext bleibt.
  const lastViewsRef = useRef<Partial<Record<EntryMode, EntryView>>>({});
  const originKindRef = useRef(originRecord?.origin.kind ?? null);
  useEffect(() => {
    originKindRef.current = originRecord?.origin.kind ?? null;
  }, [originRecord]);
  useEffect(() => {
    if (entryMode) lastViewsRef.current[entryMode] = entryView;
  }, [entryMode, entryView]);
  const changeEntryMode = useCallback((mode: EntryMode) => {
    setEntryMode(mode);
    setEntryView(viewForMode(mode, lastViewsRef.current, configYamlRef.current, originKindRef.current));
  }, []);

  const [step, setStep] = useState<WizardStep>(() => {
    if (linkMode === "open") return "prompt";
    // FA80: ohne Einstieg (neue Sitzung, neue Anmeldung) steht die Frage vor allem anderen.
    if (entryMode === null) return "prompt";
    // FA86: ohne Konfiguration gibt es weder Ausführung noch Ergebnis.
    if (!(readLocalStorage(CONFIG_YAML_STORAGE_KEY) ?? "").trim()) return "prompt";
    const raw = readLocalStorage(STEP_STORAGE_KEY);
    return raw === "execution" || raw === "result" ? raw : "prompt";
  });
  // FA86: Spiegel für asynchrone Rückrufe (eine Erzeugung endet evtl. woanders).
  const stepRef = useRef(step);
  const entryModeRef = useRef(entryMode);
  const entryViewRef = useRef(entryView);
  useEffect(() => {
    stepRef.current = step;
    entryModeRef.current = entryMode;
    entryViewRef.current = entryView;
  });

  // FA72: Anzeigename für die Erfolgsmeldung nach dem Öffnen eines Links; die
  // Liste kann beim Öffnen noch nicht geladen sein (Ref statt State-Closure).
  const scenariosRef = useRef(scenarios);
  useEffect(() => {
    scenariosRef.current = scenarios;
  }, [scenarios]);

  const refreshScenarios = useCallback(() => {
    api.listScenarios().then(setScenarios).catch(() => setScenarios([]));
  }, []);

  const refreshCatalog = useCallback(() => {
    api.catalog().then(setCatalog).catch(() => setCatalog(null));
  }, []);

  // FA54: das Backend hat auf eine Anfrage zum Lauf `id` mit 404 geantwortet
  // und die Rückfrage (runIsGone) bestätigt: es kennt den Lauf nicht mehr.
  // Der Lauf wird verworfen (State UND localStorage - der Schlüssel fällt mit
  // runId weg), die Ansicht kehrt zur Ausführung zurück (die Ergebnisansicht
  // braucht einen Lauf), und der Hinweis bietet erneutes Ausführen an.
  const handleRunGone = useCallback((id: string) => {
    if (runIdRef.current !== id) return;
    runIdRef.current = null;
    esRef.current?.close();
    setGoneRunId(id);
    setRunId(null);
    setRunBasis(null);
    setStatus(null);
    setSelectedNode(null);
    setStep((cur) => (cur === "result" ? "execution" : cur));
  }, []);

  useEffect(() => {
    api.meta().then(setMeta).catch(() => setMeta(null));
    refreshCatalog();
    refreshScenarios();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    window.localStorage.setItem(CONFIG_YAML_STORAGE_KEY, configYaml);
  }, [configYaml]);

  useEffect(() => {
    window.localStorage.setItem(STEP_STORAGE_KEY, step);
  }, [step]);
  useEffect(() => {
    try {
      if (entryMode) window.sessionStorage.setItem(ENTRY_MODE_STORAGE_KEY, entryMode);
      else window.sessionStorage.removeItem(ENTRY_MODE_STORAGE_KEY);
    } catch {
      // sessionStorage nicht verfügbar: die Frage kommt dann bei jedem Laden.
    }
  }, [entryMode]);
  useEffect(() => {
    window.localStorage.setItem(SELECTED_STORAGE_KEY, JSON.stringify(Array.from(selected)));
  }, [selected]);
  useEffect(() => {
    window.localStorage.setItem(PROMPT_STORAGE_KEY, prompt);
  }, [prompt]);
  useEffect(() => {
    window.localStorage.setItem(REGION_STORAGE_KEY, region);
  }, [region]);
  useEffect(() => {
    if (originRecord) window.localStorage.setItem(CONFIG_ORIGIN_STORAGE_KEY, serializeOriginRecord(originRecord));
    else window.localStorage.removeItem(CONFIG_ORIGIN_STORAGE_KEY);
  }, [originRecord]);
  useEffect(() => {
    if (runBasis) window.localStorage.setItem(RUN_BASIS_STORAGE_KEY, serializeRunBasis(runBasis));
    else window.localStorage.removeItem(RUN_BASIS_STORAGE_KEY);
  }, [runBasis]);
  useEffect(() => {
    if (runId) window.localStorage.setItem(RUN_ID_STORAGE_KEY, runId);
    else window.localStorage.removeItem(RUN_ID_STORAGE_KEY);
  }, [runId]);

  // FA86: eine Änderung wirft den Lauf nicht mehr weg - er bleibt in Schritt 3
  // einsehbar und wird dort als "früherer Fassung" gekennzeichnet (staleRun).
  // Beginnt der Nutzer in einem leeren Editor, ist der Text "selbst geschrieben";
  // Erzeugen und Laden setzen ihre Herkunft danach selbst (letzter Aufruf gewinnt).
  const applyConfigYaml = useCallback((next: string | ((prev: string) => string)) => {
    const prevText = configYamlRef.current;
    const text = typeof next === "function" ? next(prevText) : next;
    configYamlRef.current = text;
    setConfigYaml(text);
    if (!prevText.trim() && text.trim()) setOriginRecord({ origin: { kind: "write" }, yaml: text });
  }, []);

  // FA59: deklarierte Parameter direkt aus der YAML (`parameters:`). Die Namen
  // filtern die Überschreibungen (eine nicht mehr deklarierte wäre ein
  // Validierungsfehler); die Felder dienen als Formular, solange das Backend
  // keine ParameterInfo liefert.
  const yamlParameters = useMemo(() => parseDeclaredParameters(configYaml), [configYaml]);
  const declaredParameters = useMemo<ParameterInfo[]>(
    () => (validation?.parameters && validation.parameters.length > 0 ? validation.parameters : yamlParameters),
    [validation, yamlParameters]
  );
  const effectiveParams = useMemo(() => {
    const names = new Set(yamlParameters.map((p) => p.name));
    return Object.fromEntries(Object.entries(paramOverrides).filter(([k]) => names.has(k)));
  }, [paramOverrides, yamlParameters]);
  const effectiveParamsKey = JSON.stringify(effectiveParams);

  useEffect(() => {
    const handle = setTimeout(async () => {
      if (!configYaml.trim()) {
        setValidation(null);
        return;
      }
      setValidating(true);
      try {
        setValidation(await api.validate(configYaml, effectiveParams));
      } catch {
        setValidation(null);
      } finally {
        setValidating(false);
      }
    }, 500);
    return () => clearTimeout(handle);
    // effectiveParams ist über seinen Schlüssel abgedeckt (stabil bei gleichem Inhalt).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [configYaml, effectiveParamsKey]);

  const staticStatus = useMemo<RunStatus | null>(() => {
    if (!validation?.valid || !validation.execution_order) return null;
    let doc: Record<string, unknown>;
    try {
      const parsed = loadYaml(configYaml);
      if (!parsed || typeof parsed !== "object") return null;
      doc = parsed as Record<string, unknown>;
    } catch {
      return null;
    }
    const yamlLayers = (Array.isArray(doc.layers) ? doc.layers : [])
      .map((l) => (l && typeof l === "object" ? (l as { id?: unknown }).id : undefined))
      .filter((id): id is string => typeof id === "string");
    // FA60: nach foreach/use stehen die erzeugten Layer nicht wörtlich in der
    // YAML - dann gelten die Layer, die die Validierung nennt.
    const validatedLayers = [...(validation.required_layers ?? []), ...(validation.unused_layers ?? [])];
    const layers = validatedLayers.length > 0 ? validatedLayers : yamlLayers;
    const stepList = Array.isArray(doc.steps) ? doc.steps : [];
    const steps: Record<string, StepState> = {};
    for (const raw of stepList) {
      if (!raw || typeof raw !== "object") continue;
      const s = raw as { id?: unknown; op?: unknown; inputs?: unknown };
      if (typeof s.id !== "string") continue;
      const inputs =
        s.inputs && typeof s.inputs === "object"
          ? Object.fromEntries(
              Object.entries(s.inputs as Record<string, unknown>).filter(
                (e): e is [string, string] => typeof e[1] === "string"
              )
            )
          : {};
      steps[s.id] = {
        id: s.id,
        op: typeof s.op === "string" ? s.op : null,
        inputs,
        status: "pending",
        warnings: [],
      };
    }
    // FA75: Kanten aus dem ausgedehnten Plan - ein Selektor wie
    // "stadt[*].anteil" ist in der YAML kein Knoten, die Eingänge der
    // erzeugten Schritte stehen dort gar nicht. Ohne planned_steps (älteres
    // Backend) bleibt es bei den wörtlichen YAML-Eingängen.
    for (const planned of validation.planned_steps ?? []) {
      steps[planned.id] = {
        id: planned.id,
        op: planned.op,
        inputs: planned.inputs,
        status: "pending",
        warnings: [],
      };
    }
    // Erzeugte Schritte ohne wörtlichen YAML-Eintrag: Knoten ohne bekannte Kanten.
    for (const sid of validation.execution_order) {
      if (!steps[sid]) steps[sid] = { id: sid, inputs: {}, status: "pending", warnings: [] };
    }
    return {
      run_id: "preview",
      status: "pending",
      order: validation.execution_order,
      steps,
      layers,
      loaded_layers: [],
      layer_load_ms: {},
      origins: validation.expansion?.node_origins,
      attribution: validation.attribution ?? null,
    };
  }, [configYaml, validation]);

  const graphStatus = status ?? staticStatus;

  // Szenario-Output kann je Knoten ein color_field deklarieren (YAML
  // output[].source + .color_field, nur für type: map relevant) - ohne
  // diese Zuordnung fällt NodeInspector auf eine reine Spaltenreihenfolge-
  // Heuristik zurück, die z. B. bei spatial_join-Ergebnissen (q_left/
  // r_left aus Spaltenkollisionen) das falsche Feld wählt statt des vom
  // Szenario ausdrücklich gewählten.
  const outputColorFields = useMemo(() => {
    const fields: Record<string, string> = {};
    try {
      const parsed = loadYaml(configYaml);
      if (!parsed || typeof parsed !== "object") return fields;
      const outputs = (parsed as Record<string, unknown>).output;
      if (!Array.isArray(outputs)) return fields;
      for (const raw of outputs) {
        if (!raw || typeof raw !== "object") continue;
        const o = raw as { type?: unknown; source?: unknown; color_field?: unknown };
        if (o.type !== "map") continue;
        if (typeof o.source !== "string" || typeof o.color_field !== "string") continue;
        // Erster map-Output je source gewinnt (mehrere map-Outputs für
        // denselben Knoten sind unüblich; falls doch, ist "erster in
        // YAML-Reihenfolge" die nachvollziehbarste Regel).
        if (!(o.source in fields)) fields[o.source] = o.color_field;
      }
    } catch {
      return fields;
    }
    return fields;
  }, [configYaml]);

  const saveScenario = async (name: string) => {
    setSaveModalOpen(false);
    try {
      await api.saveScenario(name, configYaml);
      refreshScenarios();
      notify.success(`Szenario „${name}“ gespeichert.`);
    } catch (e) {
      notify.error(e instanceof Error ? e.message : String(e));
    }
  };

  const loadScenario = async (scenario: SavedScenario): Promise<boolean> => {
    try {
      const { config_yaml } = await api.loadScenario(scenario);
      setLoadedScenario({ id: scenario.id, source: scenario.source, yaml: config_yaml });
      applyConfigYaml(config_yaml);
      setOriginRecord({
        origin: { kind: "load", id: scenario.id, source: scenario.source, name: scenario.name },
        yaml: config_yaml,
      });
      setEntryView("config");
      notify.success(`Szenario „${scenario.name}“ geladen.`);
      return true;
    } catch (e) {
      notify.error(e instanceof Error ? e.message : String(e));
      return false;
    }
  };

  // FA72: Adresszeile ändern, ohne fremde Parameter oder den Hash zu verlieren;
  // die Marke "selbst gesetzt" (Tab-weit) unterscheidet später ein Neuladen
  // des eigenen Tabs von einem geöffneten Link.
  const setUrlScenario = useCallback((ref: ScenarioRef | null) => {
    window.history.replaceState(null, "", urlWithScenario(window.location, ref));
    try {
      if (ref) window.sessionStorage.setItem(OWN_LINK_STORAGE_KEY, refKey(ref));
      else window.sessionStorage.removeItem(OWN_LINK_STORAGE_KEY);
    } catch {
      // sessionStorage nicht verfügbar: ein Neuladen gilt dann als geöffneter Link.
    }
  }, []);

  // FA72: Link öffnen - das Szenario aus `?scenario=` laden (auch nach dem
  // Login, `AuthGate` mountet AppShell erst danach). Unbekannte ID oder
  // unlesbarer Parameter: ausdrückliche Fehlermeldung, Parameter entfernen,
  // kein stiller Rückfall. Andere Fehler (401, Netzwerk) lassen den Parameter
  // stehen. Der Ref-Guard verhindert den doppelten Aufruf unter StrictMode;
  // wurde der Editor währenddessen geändert, wird das Ergebnis verworfen.
  const openLink = async (ref: ScenarioRef) => {
    const startYaml = configYamlRef.current;
    try {
      const { config_yaml } = await api.loadScenario({ ...ref, name: ref.id });
      // Anzeigename aus der Szenarienliste, Rückfall auf die ID.
      const list = scenariosRef.current.length
        ? scenariosRef.current
        : await api.listScenarios().catch(() => [] as SavedScenario[]);
      const displayName = list.find((s) => s.source === ref.source && s.id === ref.id)?.name ?? ref.id;
      if (configYamlRef.current !== startYaml) {
        notify.error(`Szenario „${ref.id}“ nicht geladen: der Editor wurde währenddessen geändert.`);
        if (!loadedScenarioRef.current) setUrlScenario(null);
        return;
      }
      setLoadedScenario({ ...ref, yaml: config_yaml });
      applyConfigYaml(config_yaml);
      setOriginRecord({ origin: { kind: "load", ...ref, name: displayName }, yaml: config_yaml });
      // Ein geöffneter Link hat Vorrang vor dem gemerkten Lauf.
      runIdRef.current = null;
      esRef.current?.close();
      setRunId(null);
      setRunBasis(null);
      setGoneRunId(null);
      setStatus(null);
      setSelectedNode(null);
      setStep("prompt");
      setEntryMode("load");
      setEntryView("config");
      notify.success(`Szenario „${displayName}“ geladen.`);
    } catch (e) {
      notify.error(e instanceof Error ? e.message : String(e));
      if (linkErrorAction(isNotFoundError(e)) === "remove" && !loadedScenarioRef.current) setUrlScenario(null);
    }
  };

  useEffect(() => {
    if (linkMode === "none" || linkMode === "restore" || linkLoadStarted.current) return;
    linkLoadStarted.current = true;
    if (urlScenario.kind === "invalid") {
      notify.error(
        `Ungültiger Szenario-Link „${urlScenario.raw}“ (erwartet: example:<id> oder user:<id>).`
      );
      setUrlScenario(null);
    } else if (urlScenario.kind === "ok") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      void openLink(urlScenario.ref);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // FA72: nur solange der Editor das geladene Szenario unverändert zeigt,
  // gibt es einen Link. Jede Abweichung (LLM-Erzeugung, eingefügte
  // Ebene/Operation, Handedit) entfernt den Parameter - Textvergleich statt
  // Aufzählung der Änderungswege.
  const shareRef = loadedScenario && loadedScenario.yaml === configYaml ? loadedScenario : null;
  useEffect(() => {
    if (shareRef) setUrlScenario(shareRef);
    else if (loadedScenario) setUrlScenario(null);
  }, [shareRef, loadedScenario, setUrlScenario]);

  const copyShareLink = async () => {
    if (!shareRef) return;
    try {
      await navigator.clipboard.writeText(
        buildShareUrl(window.location.origin, window.location.pathname, shareRef)
      );
      // Q13: der Link nennt nur das Szenario (FA72) - geänderte Formularwerte
      // reisen nicht mit. Das sagen, statt den Empfänger still auf die
      // Standardwerte zu setzen.
      const changed = Object.keys(effectiveParams);
      if (changed.length > 0) {
        notify.info(
          `Link kopiert. Er enthält nur das Szenario: ${changed.join(", ")} gilt beim Empfänger mit dem Standardwert.`
        );
      } else {
        notify.success("Link kopiert.");
      }
    } catch (e) {
      notify.error(`Link konnte nicht kopiert werden: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const handleInsertStep = (op: OperationEntry) => applyConfigYaml((prev) => insertStep(prev, op));

  const cancelRun = async () => {
    // Während des Anlegens zeigt runId noch auf den vorigen Lauf (B10).
    if (!runId || startingRef.current) return;
    // FA93: der Abbruch gilt sofort - die Ansicht wartet weder auf die Antwort
    // noch auf das Ereignis des Backends (beide bestätigen denselben Zustand).
    setStatus((prev) => (prev && prev.run_id === runId ? cancelRunStatus(prev) : prev));
    try {
      await api.cancelRun(runId);
    } catch (e) {
      // Der Abbruch-Endpunkt antwortet nur für einen unbekannten Lauf mit 404
      // - dort ist keine Rückfrage nötig (FA54).
      if (isNotFoundError(e)) {
        handleRunGone(runId);
        return;
      }
      notify.error(e instanceof Error ? e.message : String(e));
    }
  };

  const toggleSource = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const handleInsertLayer = (source: CatalogSource) => applyConfigYaml((prev) => insertLayer(prev, source));

  // FA36: ein API-Layer-Fragment wird wie eine Katalogquelle eingefügt -
  // insertLayer erwartet eine CatalogSource, daher die Hülle.
  const handleInsertApiLayer = (template: Record<string, unknown>) =>
    applyConfigYaml((prev) =>
      insertLayer(prev, {
        id: String(template.id ?? "api_layer"),
        kind: "osm_preset",
        label: String(template.id ?? "api_layer"),
        data_type: "vector",
        layer_template: template,
      } as CatalogSource)
    );

  const handleUploaded = (source: CatalogSource) => {
    refreshCatalog();
    handleInsertLayer(source);
  };

  const startRun = async () => {
    // Zweiter Klick während des Anlegens (Antwort von POST /api/runs steht
    // noch aus) startet keinen zweiten Lauf (Browsercheck 03.10., B10).
    if (startingRef.current) return;
    startingRef.current = true;
    setStarting(true);
    esRef.current?.close();
    setStatus(null);
    setSelectedNode(null);
    // FA86: Stand, mit dem dieser Lauf beginnt (der Editor kann sich während des
    // Anlegens noch ändern).
    const startYaml = configYaml;
    const startParams = effectiveParams;
    try {
      // FA59: mit Parameter-Überschreibungen; ohne bleibt es der bisherige Aufruf.
      // FA82: mit der gewählten OSM-Quelle, falls sie vom Server-Wert abweicht.
      const runSettings = runOptions(sentGenerationOptions);
      const { run_id } =
        Object.keys(effectiveParams).length > 0 || Object.keys(runSettings).length > 0
          ? await api.createRun(configYaml, false, effectiveParams, runSettings)
          : await api.createRun(configYaml);
      runIdRef.current = run_id;
      setRunId(run_id);
      setRunBasis({ runId: run_id, yaml: startYaml, params: startParams });
      setGoneRunId(null);
      subscribe(run_id);
    } catch (e) {
      finishStarting();
      notify.error(e instanceof Error ? e.message : String(e));
    }
  };

  // Das Anlegen endet erst mit dem ersten Status des Ereignisstroms (oder
  // einem Fehler): zwischen der Antwort von POST /api/runs und dem ersten
  // SSE-Status stand der Knopf sonst kurz wieder auf "Ausführen" (B10).
  const finishStarting = () => {
    startingRef.current = false;
    setStarting(false);
  };

  const subscribe = (id: string) => {
    const es = new EventSource(api.eventsUrl(id));
    esRef.current = es;

    es.addEventListener("status", (ev) => {
      let parsed: RunStatus;
      try {
        parsed = JSON.parse((ev as MessageEvent).data);
      } catch (e) {
        console.error("[AppShell] SSE 'status' event: invalid JSON", e, (ev as MessageEvent).data);
        return;
      }
      setStatus(parsed);
      finishStarting();
    });

    es.addEventListener("progress", (ev) => {
      let event: Record<string, unknown>;
      try {
        event = JSON.parse((ev as MessageEvent).data);
      } catch (e) {
        console.error("[AppShell] SSE 'progress' event: invalid JSON", e, (ev as MessageEvent).data);
        return;
      }
      setStatus((prev) => (prev ? applyEvent(prev, event) : prev));
      if (event.type === "step_done") {
        setSelectedNode((cur) => {
          if (cur === event.step) setNodeVersion((v) => v + 1);
          return cur;
        });
      }
    });

    es.addEventListener("end", () => {
      es.close();
      setStatus((prev) => {
        if (prev) {
          // Bei Erfolg der letzte Knoten, sonst der letzte, der noch ein
          // Ergebnis hat bzw. der fehlgeschlagene selbst (FA33) - genau
          // dort will man nach einem Fehler hinsehen.
          setSelectedNode((cur) => cur ?? lastInspectableNode(prev));
          setNodeVersion((v) => v + 1);
          // Auch fehlgeschlagene/abgebrochene Läufe führen in die
          // Ergebnisansicht: die Zwischenergebnisse der fertigen Schritte
          // sind der Weg zur Fehlerursache (FA33). Vorher blieb der
          // Nutzer auf Schritt 2 ohne Inspector zurück.
          setStep("result");
        }
        return prev;
      });
    });

    es.onerror = () => {
      es.close();
      finishStarting();
      // EventSource nennt den HTTP-Status nicht: ob die Verbindung abbrach oder
      // das Backend den Lauf nicht kennt (404), zeigt erst eine Rückfrage (FA54).
      void api.runIsGone(id).then((gone) => {
        if (esRef.current !== es) return; // inzwischen läuft ein neuer Lauf
        if (gone) {
          handleRunGone(id);
          return;
        }
        // Ohne dies friert die UI beim Verbindungsabbruch still ein: der
        // "läuft…"-Button bliebe hängen und niemand erfährt, warum sich
        // nichts mehr tut. Nur bei einem noch laufenden Run relevant - ein
        // bereits abgeschlossener/fehlgeschlagener Lauf braucht keine
        // Korrektur. Keine automatische Rekonnektion (bewusst außerhalb des
        // Scopes) - nur den Ausfall sichtbar machen.
        setStatus((prev) => {
          if (!prev || (prev.status !== "running" && prev.status !== "pending")) return prev;
          return { ...prev, status: "error", error: "Verbindung zum Server unterbrochen." };
        });
      });
    };
  };

  // Nach einem Reload lebt `status` (Graph/Knoten der Ausführung) nur noch
  // als persistierte `runId` weiter - der Lauf selbst liegt im
  // Backend-Prozessspeicher (siehe docs/web_demo.md) und muss aktiv
  // nachgeladen werden. Schlägt das fehl (Backend inzwischen neugestartet)
  // oder gibt es gar keine `runId` (Reload auf Schritt 3 ohne je einen
  // Lauf gestartet zu haben), zurück auf Schritt 1 statt auf einer leeren
  // Ansicht hängen zu bleiben.
  useEffect(() => {
    if (step === "prompt") return;
    if (!runId) {
      // Initialer Mount-Redirect (kein Lauf persistiert) - kein
      // wiederholtes Rendern/Kaskadieren, läuft nur einmal beim Laden. Nur
      // die Ergebnisansicht braucht einen Lauf; Schritt 2 zeigt den Plan aus
      // der gemerkten YAML und bleibt (R15).
      // eslint-disable-next-line react-hooks/set-state-in-effect
      if (step === "result") setStep("prompt");
      return;
    }
    api
      .runStatus(runId)
      .then((s) => {
        setStatus(s);
        if (s.status === "running" || s.status === "pending") {
          // Neu laden während eines Laufs: wieder am Ereignisstrom anmelden,
          // sonst bliebe die Ansicht beim Schnappschuss "läuft…" stehen und
          // wechselte nie in die Ergebnisansicht (Nachtreview 02.10., Runde 2).
          subscribe(runId);
        } else {
          // Auch nach einem Fehler/Abbruch gibt es etwas anzuzeigen: das
          // Backend hält die Ergebnisse aller fertigen Schritte weiter
          // vor (FA33). Vorher landete der Nutzer hier auf Schritt 1 und
          // kam an die Zwischenergebnisse gar nicht mehr heran.
          setSelectedNode((cur) => cur ?? lastInspectableNode(s));
        }
      })
      .catch((e) => {
        if (isNotFoundError(e)) {
          // Das Backend kennt den gemerkten Lauf nicht mehr (Neustart): erklären
          // statt still auf Schritt 1 zu springen (FA54).
          handleRunGone(runId);
          return;
        }
        setRunId(null);
        setStatus(null);
        setStep("prompt");
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => () => esRef.current?.close(), []);

  // Jeder Knoten ist auswählbar - auch ein fehlgeschlagener oder noch
  // nicht gelaufener (FA33). Vorher wurde die Auswahl stillschweigend
  // verworfen: ein Klick auf einen roten Knoten pannte nur die Ansicht
  // und zeigte nichts, obwohl das Backend Fehlertext und die Ergebnisse
  // aller fertigen Schritte weiterhin vorhält. Was zu einem Knoten
  // anzuzeigen ist, entscheidet der Inspector anhand von status/error.
  const onSelectNode = (id: string) => {
    setSelectedNode(id);
    setNodeVersion((v) => v + 1);
  };

  const isDone = status?.status === "done";
  // Ein Lauf gilt ab dem Klick als laufend: während POST /api/runs, solange
  // das Backend ihn als "pending" führt und während "running" - vorher war
  // "Erneut ausführen" bei "pending" schon wieder aktiv (B10).
  const isRunning = runInProgress(status, starting);

  // Jeder Abruf (ZIP oder einzelne Datei, FA88) ist ein gewöhnlicher Link: ein
  // 404 trüge den Browser auf eine JSON-Fehlerseite. Deshalb vorher prüfen, ob
  // das Backend den Lauf noch kennt (FA54); sonst läuft der Download wie bisher
  // (Content-Disposition: attachment lässt die Seite stehen). Mit Zusatztasten
  // (neuer Tab) greift der Browser selbst. Ein anderer als der geöffnete Lauf
  // (Liste der Läufe, FA89) wird nur gemeldet - die Ansicht bleibt.
  const onDownloadClick = async (e: React.MouseEvent<HTMLElement>, id: string, url: string) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    if (await api.runIsGone(id)) {
      if (runIdRef.current === id) handleRunGone(id);
      else notify.error(RUN_GONE_MESSAGE);
      return;
    }
    window.location.assign(url);
  };
  const downloadFromRun: RunDownloadHandler = (e, id, url) => void onDownloadClick(e, id, url);

  // FA89: einen früheren Lauf in Schritt 3 öffnen. Der Editor bleibt: weicht sein
  // Text von der Konfiguration des Laufs ab, gilt der Lauf als frühere Fassung
  // (staleRun) und der Hinweis bietet deren Übernahme an. Nur in einen leeren
  // Editor wird sie sofort gesetzt - ohne Konfiguration ist Schritt 3 gesperrt (FA86).
  const openRun = async (run: RunSummary): Promise<boolean> => {
    if (startingRef.current) return false;
    const id = run.run_id;
    try {
      const [s, yaml] = await Promise.all([api.runStatus(id), api.runFileText(id, "scenario.yaml")]);
      const params = run.parameters ?? {};
      esRef.current?.close();
      runIdRef.current = id;
      setRunId(id);
      setRunBasis({ runId: id, yaml, params });
      setGoneRunId(null);
      setStatus(s);
      setSelectedNode(lastInspectableNode(s));
      setNodeVersion((v) => v + 1);
      if (!configYamlRef.current.trim()) {
        applyConfigYaml(yaml);
        setParamOverrides(params);
      }
      if (s.status === "running" || s.status === "pending") {
        subscribe(id);
        setStep("execution");
      } else {
        setStep("result");
      }
      return true;
    } catch (e) {
      notify.error(isNotFoundError(e) ? RUN_GONE_MESSAGE : e instanceof Error ? e.message : String(e));
      return false;
    }
  };

  // FA89: die Konfiguration des gezeigten Laufs in den Editor übernehmen.
  const adoptRunConfig = () => {
    if (!runBasis) return;
    applyConfigYaml(runBasis.yaml);
    setParamOverrides(runBasis.params);
  };

  const rerunAfterLoss = () => {
    setStep("execution");
    void startRun();
  };

  const entryQuestionOpen = step === "prompt" && entryMode === null;
  // FA80: Wechsel des Einstiegs aus der Kopfzeile, von jedem Schritt aus. Der
  // Editortext bleibt; der schon gewählte Einstieg führt nur zurück zu Schritt 1.
  // FA86: ein anderer Einstieg zeigt seine zuletzt gezeigte Ansicht (changeEntryMode).
  const switchEntryMode = (mode: EntryMode) => {
    if (mode !== entryMode) changeEntryMode(mode);
    setStep("prompt");
  };

  // FA86: gehört der gemerkte Lauf zu einer früheren Fassung der Konfiguration
  // (YAML oder wirksame Parameter)? Ohne bekannte Basis: nein (nichts behaupten).
  const staleRun = isStaleRun(runBasis, runId, configYaml, effectiveParams);
  // Ein noch laufender Lauf zeigt weiter seinen Fortschritt; sonst zeigt Schritt 2
  // den Plan der AKTUELLEN Konfiguration.
  const showRunInExecution = !staleRun || isRunning;
  const executionGraphStatus = showRunInExecution ? graphStatus : staticStatus;

  // FA86: Ergebnis einer Erzeugung. Steht der Nutzer in der Eingabe der Erzeugung,
  // geht es wie bisher zur Konfiguration; sonst (anderer Schritt, anderer Einstieg)
  // wird nichts umgeschaltet, sondern gemeldet.
  const handleGenerated = (
    yaml: string,
    genNotes: string | null | undefined,
    used: { prompt: string; region: string }
  ) => {
    applyConfigYaml(yaml);
    setOriginRecord({
      origin: { kind: "generate", prompt: used.prompt, region: used.region, notes: genNotes ?? null },
      yaml,
    });
    if (stepRef.current === "prompt" && entryModeRef.current === "generate" && entryViewRef.current === "input") {
      setEntryView("config");
    } else {
      notify.success("Konfiguration erzeugt – sie steht jetzt im Editor von Schritt 1 und ersetzt dort den bisherigen Text.");
    }
  };

  // FA86: Direktsprünge zur Herkunft der Konfiguration (Herkunftsleiste).
  const jumpAdjustPrompt = () => {
    const values = promptValues(originRecord);
    if (values && !generatingRef.current) {
      setPrompt(values.prompt);
      setRegion(values.region);
    }
    setEntryMode("generate");
    setEntryView("input");
    setStep("prompt");
  };
  const jumpPickScenario = () => {
    setEntryMode("load");
    setEntryView("input");
    setStep("prompt");
  };
  const jumpEditConfig = () => {
    // Der Einstieg bleibt, wie er ist; nur die Konfigurationsansicht wird geöffnet.
    if (entryMode === null) setEntryMode("write");
    setEntryView("config");
    setStep("prompt");
  };

  // FA86: ohne Konfiguration (noch kein Szenario erzeugt, geladen oder geschrieben)
  // ist auch das Ergebnis eines gemerkten Laufs nicht erreichbar - Schritt 2 und 3
  // sind dann beide gesperrt. Mit Konfiguration bleibt ein Lauf einsehbar, auch
  // wenn sie inzwischen geändert oder ungültig ist (staleRun).
  const hasConfig = configYaml.trim().length > 0;
  const reachable: Record<WizardStep, boolean> = {
    prompt: true,
    execution: !!validation?.valid,
    result: !!runId && hasConfig,
  };
  const completed: Partial<Record<WizardStep, boolean>> = {
    prompt: !!validation?.valid,
    execution: !!isDone && !staleRun,
  };

  return (
    <div className="flex min-h-screen flex-1 flex-col">
      <header className="sticky top-0 z-20 flex flex-wrap items-center gap-x-6 gap-y-2 border-b bg-background/95 px-4 py-2.5 backdrop-blur supports-backdrop-filter:bg-background/80 sm:px-6">
        {/* Links die Marke, daneben der Einstieg (FA80), rechts nur globale Aktionen.
            OSM-Quelle und Sprachmodell stehen im Einstellungsdialog; die
            Kopfzeile meldet sich dazu nur, wenn etwas fehlt. */}
        <div className="flex items-center gap-2">
          <Waypoints className="size-5 text-primary" />
          <h1 className="text-base font-bold tracking-tight">GeoFACT Studio</h1>
        </div>
        {entryMode !== null && (
          <EntrySwitch
            mode={entryMode}
            llmConfigured={meta ? meta.llm_configured : null}
            onSwitch={switchEntryMode}
          />
        )}
        <div className="ml-auto flex items-center gap-1">
          {meta && !meta.llm_configured && (
            <Badge
              variant="outline"
              className="mr-1 border-amber-400/40 text-amber-600 dark:text-amber-400"
              title="Ohne Sprachmodell lässt sich kein Szenario generieren. Laden und selbst schreiben funktionieren."
            >
              <span className="hidden sm:inline">Kein Sprachmodell konfiguriert</span>
              <span className="sm:hidden">Kein LLM</span>
            </Badge>
          )}
          {/* FA88: die Dateien eines Laufs stehen in Schritt 3 (RunFilesButton), nicht mehr
              als Knopf hier; FA89: die Läufe, die das Backend noch hält. */}
          <RunHistoryDialog currentRunId={runId} onOpenRun={openRun} onDownload={downloadFromRun} />
          <GenerationSettings
            server={settingsServer}
            value={sentGenerationOptions}
            onChange={setGenerationOptions}
          />
          {username && (
            <Button
              variant="ghost"
              size="sm"
              title={`Als ${username} angemeldet – abmelden`}
              aria-label={`${username} abmelden`}
              onClick={() => {
                clearAuth();
                forgetEntryMode();
                window.location.reload();
              }}
            >
              <span className="hidden max-w-32 truncate lg:inline">{username}</span>
              <LogOut className="size-4" />
            </Button>
          )}
        </div>
      </header>

      {/* FA80: solange die Frage offen ist, lenkt nichts von ihr ab. */}
      {!entryQuestionOpen && (
        <Stepper current={step} reachable={reachable} completed={completed} onSelect={setStep} />
      )}

      <div className="mx-auto w-full max-w-[1800px] flex-1 px-6 py-6 2xl:px-10">
        {goneRunId && step !== "prompt" && (
          <RunGoneNotice
            runId={goneRunId}
            canRerun={!!validation?.valid}
            onRerun={rerunAfterLoss}
            onDismiss={() => setGoneRunId(null)}
          />
        )}

        {/* FA86: Schritt 1 bleibt eingehängt (nur ausgeblendet), damit Prompt,
            Gedankengang und eine laufende Erzeugung einen Schrittwechsel
            überstehen. Ausführung und Ergebnis nicht: Karte und Graph
            messen sich falsch, solange sie verborgen sind. */}
        {step !== "prompt" && (
          <OriginBar
            record={originRecord}
            yaml={configYaml}
            onAdjustPrompt={jumpAdjustPrompt}
            onPickScenario={jumpPickScenario}
            onEditConfig={jumpEditConfig}
            className="mb-4"
          />
        )}
        {step === "result" && staleRun && (
          <StaleResultNotice
            canRerun={!!validation?.valid && !isRunning}
            invalid={!validation?.valid}
            onRerun={rerunAfterLoss}
            onAdopt={adoptRunConfig}
          />
        )}

        <div className={cn(step !== "prompt" && "hidden")}>
          <PromptStep
            catalog={catalog}
            selected={selected}
            onToggle={toggleSource}
            onInsertLayer={handleInsertLayer}
            onInsertApiLayer={handleInsertApiLayer}
            onUploaded={handleUploaded}
            onUploadedForPrompt={(source) => {
              refreshCatalog();
              setSelected((prev) => new Set(prev).add(source.id));
            }}
            onDeleted={refreshCatalog}
            entryMode={entryMode}
            entryView={entryView}
            onChooseEntryMode={chooseEntryMode}
            prompt={prompt}
            setPrompt={setPrompt}
            region={region}
            setRegion={setRegion}
            onGeneratingChange={onGeneratingChange}
            originRecord={originRecord}
            onAdjustPrompt={jumpAdjustPrompt}
            onPickScenario={jumpPickScenario}
            generationOptions={sentGenerationOptions}
            username={username}
            setEntryView={setEntryView}
            sidebarTab={sidebarTab}
            setSidebarTab={setSidebarTab}
            meta={meta}
            onInsertStep={handleInsertStep}
            configYaml={configYaml}
            setConfigYaml={applyConfigYaml}
            onGenerated={handleGenerated}
            validation={validation}
            validating={validating}
            scenarios={scenarios}
            saveScenario={() => setSaveModalOpen(true)}
            loadScenario={loadScenario}
            shareRef={shareRef}
            copyShareLink={copyShareLink}
            linkOmitsParameters={Object.keys(effectiveParams).length > 0}
            onContinue={() => setStep("execution")}
            parameters={declaredParameters}
            parameterValues={effectiveParams}
            onParametersChange={setParamOverrides}
          />
        </div>

        {step === "execution" && (
          <ExecutionStep
            validation={validation}
            startRun={startRun}
            isRunning={isRunning}
            graphStatus={executionGraphStatus}
            selectedNode={selectedNode}
            onSelectNode={(id) => {
              // Ein Knotenklick führt immer an dieselbe Stelle: in die
              // Ergebnisansicht, in der Graph und Inspector zusammen
              // stehen (FA32). Vorher zeigte ein Klick hier nichts an.
              onSelectNode(id);
              setStep("result");
            }}
            cancelRun={cancelRun}
            status={showRunInExecution ? status : null}
            onViewResult={() => setStep("result")}
            runId={runId}
            parameters={declaredParameters}
            parameterValues={effectiveParams}
            onParametersChange={setParamOverrides}
          />
        )}

        {step === "result" && (
          <ResultStep
            key={runId ?? "no-run"}
            runId={runId}
            selectedNode={selectedNode}
            nodeVersion={nodeVersion}
            status={status}
            graphStatus={graphStatus}
            onSelectNode={onSelectNode}
            onBackToExecution={() => setStep("execution")}
            outputColorFields={outputColorFields}
            onRunGone={handleRunGone}
            onDownload={downloadFromRun}
          />
        )}
      </div>

      <SaveScenarioDialog
        open={saveModalOpen}
        onConfirm={saveScenario}
        onCancel={() => setSaveModalOpen(false)}
      />
    </div>
  );
}

// --- Hinweis: Ergebnis gehört zu einer früheren Fassung (FA86) ---------------

function StaleResultNotice({
  canRerun,
  invalid,
  onRerun,
  onAdopt,
}: {
  canRerun: boolean;
  invalid: boolean;
  onRerun: () => void;
  // FA89: die Konfiguration, mit der dieser Lauf lief, in den Editor setzen.
  onAdopt: () => void;
}) {
  return (
    <Alert data-slot="stale-result" className="mb-4 border-amber-400/40 bg-amber-400/10 text-amber-700 dark:text-amber-300">
      <TriangleAlert className="size-4" />
      <AlertTitle>Dieses Ergebnis gehört zu einer früheren Fassung der Konfiguration</AlertTitle>
      <AlertDescription className="text-amber-700 dark:text-amber-300 [&_p:not(:last-child)]:mb-2">
        <p>
          Der Editor zeigt eine andere Konfiguration oder andere Parameter als die, mit denen dieser Lauf lief – sie
          wurden seither geändert, oder der Lauf stammt aus der Liste der Läufe. Das Ergebnis lässt sich weiter ansehen.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button
            onClick={onRerun}
            disabled={!canRerun}
            title={invalid ? "Die Konfiguration ist nicht gültig." : undefined}
          >
            <Play className="size-4" />
            Erneut ausführen
          </Button>
          <Button
            variant="outline"
            onClick={onAdopt}
            title="Ersetzt den Text im Editor durch die Konfiguration dieses Laufs"
          >
            Konfiguration dieses Laufs übernehmen
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}

// --- Hinweis: Lauf nicht mehr bekannt (FA54) -----------------------------------

function RunGoneNotice({
  runId,
  canRerun,
  onRerun,
  onDismiss,
}: {
  runId: string;
  canRerun: boolean;
  onRerun: () => void;
  onDismiss: () => void;
}) {
  return (
    <Alert variant="destructive" className="mb-4">
      <TriangleAlert className="size-4" />
      <AlertTitle>Lauf nicht mehr vorhanden</AlertTitle>
      <AlertDescription className="[&_p:not(:last-child)]:mb-2">
        <p>
          {RUN_GONE_MESSAGE} (Lauf <span className="font-mono">{runId}</span>)
        </p>
        <div className="flex flex-wrap gap-2">
          <Button
            onClick={onRerun}
            disabled={!canRerun}
            title={canRerun ? undefined : "Die Konfiguration ist nicht gültig."}
          >
            <Play className="size-4" />
            Erneut ausführen
          </Button>
          <Button variant="ghost" onClick={onDismiss}>
            Schließen
          </Button>
        </div>
      </AlertDescription>
    </Alert>
  );
}

// --- Step 1: Szenario (generieren, laden oder selbst schreiben; FA80) --------

const ENTRY_MODE_ICON: Record<EntryMode, typeof Sparkles> = {
  generate: Sparkles,
  load: FolderOpen,
  write: PencilLine,
};

/** FA80: die Frage nach der Anmeldung - was möchte der Nutzer tun? Drei
 * gleichrangige Wege; ein Editortext aus einer früheren Sitzung lässt sich
 * darunter fortsetzen. Solange die Frage offen ist, steht sonst nichts auf der Seite. */
function EntryChooser({
  username,
  llmConfigured,
  resumable,
  onChoose,
  onResume,
}: {
  username: string | null;
  // null: noch nicht bekannt (Meta-Abruf läuft).
  llmConfigured: boolean | null;
  resumable: boolean;
  onChoose: (mode: EntryMode) => void;
  onResume: () => void;
}) {
  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-8 py-8 sm:py-14">
      <div className="text-center">
        {username && <p className="mb-1 text-sm text-muted-foreground">Willkommen, {username}</p>}
        <h2 className="text-2xl font-semibold tracking-tight sm:text-3xl">Was möchten Sie tun?</h2>
        <p className="mt-2 text-sm text-muted-foreground sm:text-base">
          Wählen Sie einen Weg. Jeder führt zu einer Szenario-Konfiguration, die Sie danach ausführen.
        </p>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        {ENTRY_MODES.map((m) => {
          const Icon = ENTRY_MODE_ICON[m.id];
          // Ohne Sprachmodell führt "generieren" nirgends hin: sperren und sagen, warum.
          const unavailable = m.id === "generate" && llmConfigured === false;
          return (
            <button
              key={m.id}
              type="button"
              onClick={() => onChoose(m.id)}
              disabled={unavailable}
              className="group flex flex-col items-start gap-3 rounded-xl border bg-card p-6 text-left transition-colors hover:border-primary hover:bg-accent focus-visible:border-primary focus-visible:outline-none disabled:pointer-events-none disabled:opacity-60"
            >
              <span className="flex size-11 items-center justify-center rounded-lg bg-primary/10 text-primary">
                <Icon className="size-5" />
              </span>
              <span className="text-lg font-semibold">{m.label}</span>
              <span className="text-sm text-muted-foreground">{m.description}</span>
              <span className="mt-auto flex w-full items-center justify-between gap-2 pt-3 text-sm">
                <span className="text-muted-foreground">
                  {unavailable ? "Nicht verfügbar: kein Sprachmodell konfiguriert" : m.hint}
                </span>
                {!unavailable && (
                  <span className="flex items-center gap-1 font-medium text-primary">
                    Auswählen
                    <ChevronRight className="size-4 transition-transform group-hover:translate-x-0.5" />
                  </span>
                )}
              </span>
            </button>
          );
        })}
      </div>
      {resumable && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-dashed px-5 py-4">
          <div>
            <p className="text-sm font-medium">Sie haben zuletzt an einer Konfiguration gearbeitet.</p>
            <p className="text-sm text-muted-foreground">Sie ist in diesem Browser noch vorhanden.</p>
          </div>
          <Button variant="outline" onClick={onResume}>
            Dort weitermachen
            <ChevronRight className="size-4" />
          </Button>
        </div>
      )}
    </div>
  );
}

/** FA80: der Einstieg in der Kopfzeile - die drei Wege stehen nebeneinander,
 * der gewählte ist hervorgehoben, ein Klick wechselt von jedem Schritt aus. */
function EntrySwitch({
  mode,
  llmConfigured,
  onSwitch,
}: {
  mode: EntryMode;
  llmConfigured: boolean | null;
  onSwitch: (mode: EntryMode) => void;
}) {
  return (
    <div role="group" aria-label="Einstieg" className="flex items-center gap-0.5 rounded-lg bg-muted p-0.5">
      {ENTRY_MODES.map((m) => {
        const Icon = ENTRY_MODE_ICON[m.id];
        const active = m.id === mode;
        // Wie in der Frage: ohne Sprachmodell führt "generieren" nirgends hin.
        const unavailable = m.id === "generate" && llmConfigured === false;
        return (
          <button
            key={m.id}
            type="button"
            aria-pressed={active}
            aria-label={m.label}
            disabled={unavailable}
            title={unavailable ? "Nicht verfügbar: kein Sprachmodell konfiguriert" : m.label}
            onClick={() => onSwitch(m.id)}
            className={cn(
              "flex h-7 items-center gap-1.5 rounded-md px-2.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-ring disabled:pointer-events-none disabled:opacity-50",
              active ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
            )}
          >
            <Icon className="size-4" />
            <span className="hidden md:inline">{m.short}</span>
          </button>
        );
      })}
    </div>
  );
}

/** FA80: Kopf des gewählten Wegs - welcher Einstieg, welcher Schritt und was
 * als Nächstes zu tun ist. Der Wechsel des Einstiegs steht in der Kopfzeile. */
function EntryGuide({
  mode,
  view,
  text,
  extra,
}: {
  mode: EntryMode;
  view: EntryView;
  // FA86: Anweisung, die der Herkunft des Textes folgt (ersetzt den Standardtext).
  text?: string;
  extra?: React.ReactNode;
}) {
  const Icon = ENTRY_MODE_ICON[mode];
  const guide = ENTRY_GUIDE[mode][view];
  const label = ENTRY_MODES.find((m) => m.id === mode)?.label ?? mode;
  return (
    <div data-slot="entry-guide" className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
      <div className="flex min-w-0 items-start gap-3">
        <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Icon className="size-5" />
        </span>
        <div className="min-w-0">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {label}
            {guide.of > 1 && ` · Schritt ${guide.step} von ${guide.of}`}
          </p>
          <h2 className="text-lg font-semibold tracking-tight">{guide.title}</h2>
          <p className="text-sm text-muted-foreground">{text ?? guide.text}</p>
        </div>
      </div>
      {extra && <div className="flex flex-wrap items-center gap-2">{extra}</div>}
    </div>
  );
}

function PromptStep({
  catalog,
  selected,
  onToggle,
  onInsertLayer,
  onInsertApiLayer,
  onUploaded,
  onUploadedForPrompt,
  onDeleted,
  entryMode,
  entryView,
  onChooseEntryMode,
  prompt,
  setPrompt,
  region,
  setRegion,
  onGeneratingChange,
  originRecord,
  onAdjustPrompt,
  onPickScenario,
  generationOptions,
  username,
  setEntryView,
  sidebarTab,
  setSidebarTab,
  meta,
  onInsertStep,
  configYaml,
  setConfigYaml,
  onGenerated,
  validation,
  validating,
  scenarios,
  saveScenario,
  loadScenario,
  shareRef,
  copyShareLink,
  linkOmitsParameters,
  onContinue,
  parameters,
  parameterValues,
  onParametersChange,
}: {
  catalog: Catalog | null;
  selected: Set<string>;
  onToggle: (id: string) => void;
  onInsertLayer: (s: CatalogSource) => void;
  onInsertApiLayer: (template: Record<string, unknown>) => void;
  onUploaded: (s: CatalogSource) => void;
  // FA80: ein Upload im Einstieg "generate" wird für den Prompt vorgemerkt,
  // nicht in die (dort unsichtbare) Konfiguration eingefügt.
  onUploadedForPrompt: (s: CatalogSource) => void;
  onDeleted?: () => void;
  entryMode: EntryMode | null;
  entryView: EntryView;
  onChooseEntryMode: (mode: EntryMode) => void;
  prompt: string;
  setPrompt: (v: string) => void;
  region: string;
  setRegion: (v: string) => void;
  onGeneratingChange: (busy: boolean) => void;
  originRecord: OriginRecord | null;
  onAdjustPrompt: () => void;
  onPickScenario: () => void;
  generationOptions: GenerationOptions;
  username: string | null;
  setEntryView: (view: EntryView) => void;
  sidebarTab: SidebarTab;
  setSidebarTab: (t: SidebarTab) => void;
  meta: Meta | null;
  onInsertStep: (op: OperationEntry) => void;
  configYaml: string;
  setConfigYaml: (v: string) => void;
  onGenerated: (yaml: string, notes: string | null | undefined, used: { prompt: string; region: string }) => void;
  validation: ValidateResponse | null;
  validating: boolean;
  scenarios: SavedScenario[];
  saveScenario: () => void;
  loadScenario: (scenario: SavedScenario) => void;
  shareRef: ScenarioRef | null;
  copyShareLink: () => void;
  linkOmitsParameters: boolean;
  onContinue: () => void;
  parameters: ParameterInfo[];
  parameterValues: Record<string, unknown>;
  onParametersChange: (next: Record<string, unknown>) => void;
}) {
  const hasConfig = configYaml.trim().length > 0;
  const promptVisible = entryMode === "generate" && entryView === "input";
  const pickerVisible = entryMode === "load" && entryView === "input";
  const editorVisible = showsEditor(entryMode, entryView);
  // Im Einstieg "generate" dient der Katalog nur der Vorauswahl für den
  // Prompt; Operationen lassen sich dort nicht vormerken.
  const promptSidebarTab: SidebarTab = sidebarTab === "operations" ? "sources" : sidebarTab;
  // Zähler an den Tabs: Vorgemerktes bleibt sichtbar, auch wenn der andere Tab offen ist.
  // Quellen-ids tragen ein Präfix (osm./copernicus./upload.), API-ids nicht.
  const catalogSourceIds = new Set(
    [...(catalog?.uploads ?? []), ...(catalog?.osm_presets ?? []), ...(catalog?.copernicus ?? [])].map((s) => s.id)
  );
  const selectedIds = Array.from(selected);
  const selectedSourceCount = selectedIds.filter((id) => catalogSourceIds.has(id)).length;
  const selectedApiCount = selectedIds.filter((id) => !/^(osm|copernicus|upload)\./.test(id)).length;

  const showConfigButton = hasConfig && (
    <Button variant="ghost" size="sm" onClick={() => setEntryView("config")}>
      Aktuelle Konfiguration ansehen
      <ChevronRight className="size-4" />
    </Button>
  );

  return (
    <div className="flex flex-col gap-6">
      {entryMode === null ? (
        <EntryChooser
          username={username}
          llmConfigured={meta ? meta.llm_configured : null}
          resumable={canResume(configYaml)}
          onChoose={onChooseEntryMode}
          onResume={() => onChooseEntryMode("write")}
        />
      ) : (
        <EntryGuide
          mode={entryMode}
          view={entryView}
          // FA86: der Text der Konfigurationsansicht folgt der Herkunft des Textes.
          text={
            entryView === "config" && entryMode !== "write" ? configGuideText(originRecord, configYaml) : undefined
          }
          extra={(promptVisible || pickerVisible) && showConfigButton}
        />
      )}

      {/* Einstieg "generate": Prompt und Vorauswahl. Bleibt eingehängt (nur
          ausgeblendet), damit Prompttext und eine laufende Erzeugung einen
          Wechsel der Ansicht überstehen. */}
      <div
        data-entry="generate"
        className={cn("grid grid-cols-1 gap-6 lg:grid-cols-[1fr_22rem]", !promptVisible && "hidden")}
      >
        <div className="flex min-w-0 flex-col gap-4 rounded-xl border bg-card p-6">
          {meta && !meta.llm_configured && (
            <Alert>
              <Info className="size-4" />
              <AlertTitle>Kein Sprachmodell konfiguriert</AlertTitle>
              <AlertDescription>
                Die Erzeugung braucht einen LLM-Zugang im Backend (siehe docs/web_demo.md). Ein Szenario laden
                oder selbst schreiben geht ohne.
              </AlertDescription>
            </Alert>
          )}
          <PromptPanel
            prompt={prompt}
            onPromptChange={setPrompt}
            region={region}
            onRegionChange={setRegion}
            onBusyChange={onGeneratingChange}
            selectedIds={Array.from(selected)}
            llmConfigured={meta?.llm_configured ?? false}
            options={generationOptions}
            onGenerated={onGenerated}
          />
        </div>
        <div className="flex flex-col rounded-xl border bg-card p-6">
          <h3 className="text-base font-semibold">Daten vormerken (optional)</h3>
          <p className="mb-3 text-sm text-muted-foreground">
            Angehakte Quellen und APIs bekommt das Sprachmodell als Vorgabe.
          </p>
          <Tabs
            value={promptSidebarTab}
            onValueChange={(v) => setSidebarTab(v as SidebarTab)}
            className="mb-3"
          >
            <TabsList className="w-full">
              <TabsTrigger value="sources" className="flex-1">
                Quellen
                {selectedSourceCount > 0 && <Badge className="ml-1.5 px-1.5">{selectedSourceCount}</Badge>}
              </TabsTrigger>
              <TabsTrigger value="apis" className="flex-1">
                APIs
                {selectedApiCount > 0 && <Badge className="ml-1.5 px-1.5">{selectedApiCount}</Badge>}
              </TabsTrigger>
            </TabsList>
          </Tabs>
          {promptSidebarTab === "sources" && (
            <SourcePanel
              catalog={catalog}
              selected={selected}
              onToggle={onToggle}
              onUploaded={onUploadedForPrompt}
              onDeleted={onDeleted}
            />
          )}
          {promptSidebarTab === "apis" && <ApiPanel selected={selected} onToggle={onToggle} />}
        </div>
      </div>

      {pickerVisible && (
        <div data-entry="load" className="rounded-xl border bg-card p-6">
          <ScenarioPicker scenarios={scenarios} onSelect={loadScenario} preselect={shareRef} />
        </div>
      )}

      {editorVisible && (
        <div
          data-entry="config"
          className={cn("grid grid-cols-1 gap-6", entryMode === "write" && "lg:grid-cols-[1fr_22rem]")}
        >
          <div className="flex min-w-0 flex-col rounded-xl border bg-card p-6">
            {/* FA86: Herkunft und Weg zurück dorthin (Prompt bzw. Szenarioauswahl);
                die Anmerkungen des Modells gehören zur erzeugten Fassung. */}
            <OriginBar
              record={originRecord}
              yaml={configYaml}
              onAdjustPrompt={onAdjustPrompt}
              onPickScenario={onPickScenario}
              onEditConfig={() => setEntryView("config")}
              hideEdit
              mode={entryMode}
              className="mb-3"
            />
            {originNotes(originRecord) && (
              <p className="mb-3 rounded-lg bg-muted px-3 py-2 text-sm text-foreground/80">
                {originNotes(originRecord)}
              </p>
            )}
            <ConfigEditor
              value={configYaml}
              onChange={setConfigYaml}
              onRun={onContinue}
              running={false}
              validation={validation}
              validating={validating}
              runLabel={validation?.valid ? "Weiter zur Ausführung" : undefined}
              footer={
                <LicenseCheckPanel
                  key={licenseCheckKey(configYaml, parameterValues)}
                  configYaml={configYaml}
                  parameters={parameterValues}
                  outputLicenses={validation?.output_licenses ?? []}
                />
              }
              toolbar={
                <>
                  {/* FA91: Blanko-Vorlage, nur beim Selbstschreiben und nur in einen leeren Editor. */}
                  {entryMode === "write" && (
                    <Button
                      variant="ghost"
                      size="sm"
                      data-action="insert-template"
                      onClick={() => setConfigYaml(BLANK_TEMPLATE)}
                      disabled={!canInsertTemplate(configYaml)}
                      title={
                        canInsertTemplate(configYaml)
                          ? "Vorlage mit allen Abschnitten und Platzhaltern einfügen"
                          : "Die Vorlage ersetzt keinen vorhandenen Text – dafür den Editor leeren"
                      }
                    >
                      <FilePlus2 className="size-4" />
                      Vorlage
                    </Button>
                  )}
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={saveScenario}
                    disabled={!hasConfig}
                    title="Szenario speichern"
                  >
                    <Save className="size-4" />
                    Speichern
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={copyShareLink}
                    disabled={!shareRef}
                    title={
                      !shareRef
                        ? "Nur für ein geladenes, unverändertes Szenario verfügbar"
                        : linkOmitsParameters
                          ? "Link zu diesem Szenario kopieren – geänderte Parameter sind nicht enthalten"
                          : "Link zu diesem Szenario kopieren"
                    }
                  >
                    <Link2 className="size-4" />
                    Link kopieren
                  </Button>
                </>
              }
            />
            {parameters.length > 0 && (
              // FA59: das Formular auch hier - ein Parameter ohne Default macht
              // die Konfiguration ungültig, und die Ausführung (mit dem
              // Formular) ist erst bei gültiger Konfiguration erreichbar
              // (Nachtreview 02.10., Runde 2).
              <div className="mt-4">
                <ParameterPanel parameters={parameters} values={parameterValues} onChange={onParametersChange} />
              </div>
            )}
          </div>

          {entryMode === "write" && (
            <div className="flex flex-col rounded-xl border bg-card p-6">
              <Tabs
                value={sidebarTab}
                onValueChange={(v) => setSidebarTab(v as SidebarTab)}
                className="mb-3"
              >
                <TabsList className="w-full">
                  <TabsTrigger value="sources" className="flex-1">
                    Quellen
                  </TabsTrigger>
                  <TabsTrigger value="apis" className="flex-1">
                    APIs
                  </TabsTrigger>
                  <TabsTrigger value="operations" className="flex-1">
                    Operationen
                  </TabsTrigger>
                </TabsList>
              </Tabs>
              {sidebarTab === "sources" && (
                <SourcePanel
                  catalog={catalog}
                  selected={selected}
                  onInsertLayer={onInsertLayer}
                  onUploaded={onUploaded}
                  onDeleted={onDeleted}
                />
              )}
              {sidebarTab === "apis" && (
                <ApiPanel selected={selected} onInsertLayer={onInsertApiLayer} />
              )}
              {sidebarTab === "operations" && <OperationsPanel onInsert={onInsertStep} />}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** B10: läuft ein Lauf (oder wird er gerade angelegt)? "pending" zählt mit. */
function runInProgress(status: RunStatus | null, starting: boolean): boolean {
  return starting || status?.status === "pending" || status?.status === "running";
}

// --- Step 2: Ausführung -----------------------------------------------------

function ExecutionStep({
  validation,
  startRun,
  isRunning,
  graphStatus,
  selectedNode,
  onSelectNode,
  cancelRun,
  status,
  onViewResult,
  parameters,
  parameterValues,
  onParametersChange,
}: {
  validation: ValidateResponse | null;
  startRun: () => void;
  isRunning: boolean;
  graphStatus: RunStatus | null;
  selectedNode: string | null;
  onSelectNode: (id: string) => void;
  cancelRun: () => void;
  status: RunStatus | null;
  onViewResult: () => void;
  runId: string | null;
  // FA59: deklarierte Parameter und ihre Überschreibungen.
  parameters: ParameterInfo[];
  parameterValues: Record<string, unknown>;
  onParametersChange: (next: Record<string, unknown>) => void;
}) {
  const isDone = status?.status === "done";
  const failedSteps = status
    ? Object.values(status.steps).filter((s) => s.status === "error" && s.error)
    : [];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
        if (validation?.valid && !isRunning) {
          e.preventDefault();
          startRun();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [validation?.valid, isRunning, startRun]);

  return (
    <div className="flex flex-col gap-6">
      <ParameterPanel parameters={parameters} values={parameterValues} onChange={onParametersChange} />
      <section className="flex flex-col rounded-xl border bg-card p-6">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
          <div>
            <h3 className="text-base font-semibold">Ausführungsgraph</h3>
            <p className="text-xs text-muted-foreground">
              Knoten anklicken, um sein Zwischenergebnis zu sehen
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {isRunning && (
              <Button variant="destructive" onClick={cancelRun}>
                <Square className="size-3.5" />
                Abbrechen
              </Button>
            )}
            {isDone && (
              <Button
                onClick={onViewResult}
                className="bg-emerald-600 text-white hover:bg-emerald-500 dark:bg-emerald-600 dark:hover:bg-emerald-500"
              >
                Ergebnis ansehen
                <ChevronRight className="size-4" />
              </Button>
            )}
            <Button onClick={startRun} disabled={!validation?.valid || isRunning} title="Strg/Cmd+Enter">
              {isRunning ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
              {isRunning ? "läuft…" : status ? "Erneut ausführen" : "Ausführen"}
            </Button>
          </div>
        </div>
        {status?.error && (
          <Alert variant="destructive" className="mb-3">
            <TriangleAlert className="size-4" />
            <AlertTitle>Lauf fehlgeschlagen</AlertTitle>
            <AlertDescription>{status.error}</AlertDescription>
          </Alert>
        )}
        <RunWarnings warnings={status?.warnings} className="mb-3" />
        {failedSteps.length > 0 && (
          <Alert variant="destructive" className="mb-3">
            <TriangleAlert className="size-4" />
            <AlertTitle>
              {failedSteps.length === 1 ? "Ein Schritt ist fehlgeschlagen" : `${failedSteps.length} Schritte sind fehlgeschlagen`}
            </AlertTitle>
            <AlertDescription>
              <ul className="max-h-40 list-disc overflow-y-auto pl-4">
                {failedSteps.map((s) => (
                  <li key={s.id}>
                    <span className="font-mono">{s.id}</span>: {s.error}
                  </li>
                ))}
              </ul>
            </AlertDescription>
          </Alert>
        )}
        <div className="h-[60vh] overflow-hidden rounded-lg border bg-muted/20 sm:h-168">
          {graphStatus ? (
            <RunGraph status={graphStatus} selected={selectedNode} onSelect={onSelectNode} />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              Gültige Konfiguration eingeben, um den Graphen zu sehen.
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

// --- Step 3: Ergebnis ---------------------------------------------------

function ResultStep({
  runId,
  selectedNode,
  nodeVersion,
  status,
  graphStatus,
  onSelectNode,
  onBackToExecution,
  outputColorFields,
  onRunGone,
  onDownload,
}: {
  runId: string | null;
  selectedNode: string | null;
  nodeVersion: number;
  status: RunStatus | null;
  graphStatus: RunStatus | null;
  onSelectNode: (id: string) => void;
  onBackToExecution: () => void;
  // Vom Szenario deklariertes output[].color_field je Knoten (nur
  // type: map) - siehe NodeInspector.configuredColorField.
  outputColorFields: Record<string, string>;
  // FA54: der Inspector meldet, dass das Backend den Lauf nicht mehr kennt.
  onRunGone: (runId: string) => void;
  // FA88: Abruf einer Datei des Laufs (fragt vorher das Backend, FA54).
  onDownload: RunDownloadHandler;
}) {
  // FA67: genau eine Ansicht zur Zeit. `key={runId}` am Aufrufer setzt sie bei
  // einem neuen Lauf auf `result` zurück.
  const [view, setView] = useState<"result" | "graph">("result");

  const selectFromGraph = (id: string) => {
    onSelectNode(id);
    setView("result");
  };

  // Kürzel `g`: Ergebnis <-> Vollbild-Graph (nicht beim Tippen, nicht mit Modifier).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "g" || e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      setView((v) => (v === "graph" ? "result" : "graph"));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (!runId) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        Noch kein Lauf gestartet.
      </div>
    );
  }

  const failedSteps = status
    ? Object.values(status.steps).filter((s) => s.status === "error" && s.error)
    : [];
  const runFailed = status?.status === "error" || status?.status === "cancelled";
  const selectedStep = selectedNode ? status?.steps[selectedNode] : undefined;
  const finalNode = status?.order[status.order.length - 1] ?? null;
  const showFinal =
    finalNode !== null &&
    status?.steps[finalNode]?.status === "done" &&
    (view === "graph" || selectedNode !== finalNode);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 rounded-xl border bg-card px-4 py-3">
        <Button variant="ghost" onClick={onBackToExecution} className="text-muted-foreground">
          <ArrowLeft className="size-4" />
          Zurück zur Ausführung
        </Button>
        {/* Lauf-Attribution (FA65/FA67): Region/Arbeits-CRS und die Lizenzen der Ausgabequellen */}
        <div data-slot="run-attribution" className="min-w-0 flex-1">
          <RunAttribution attribution={status?.attribution} region={status?.region} />
        </div>
        {showFinal && finalNode && (
          <Button
            variant="outline"
            onClick={() => {
              setView("result");
              onSelectNode(finalNode);
            }}
          >
            Endergebnis anzeigen
          </Button>
        )}
        <Button
          variant="outline"
          onClick={() => setView((v) => (v === "graph" ? "result" : "graph"))}
          disabled={!graphStatus}
          title="Taste g"
        >
          {view === "graph" ? "Zurück zum Ergebnis" : "Ausführungsgraph"}
        </Button>
        {/* FA88: was der Lauf geschrieben hat - jede Datei einzeln, alle als ZIP.
            Als Dialog, damit die Liste der Karte keinen Platz nimmt. Erst nach
            dem Abschluss: vorher gibt es keine Ausgaben. */}
        {status?.status === "done" && (
          <RunFilesButton runId={runId} onDownload={onDownload} onGone={onRunGone} refreshKey={status.status} />
        )}
      </div>

      {/* Lauf-Fehler bleiben in der Ergebnisansicht sichtbar - die
          Zwischenergebnisse daneben sind der Weg zur Ursache (FA33). */}
      {status?.error && (
        <Alert variant="destructive">
          <TriangleAlert className="size-4" />
          <AlertTitle>
            {status.status === "cancelled" ? "Lauf abgebrochen" : "Lauf fehlgeschlagen"}
          </AlertTitle>
          <AlertDescription>{status.error}</AlertDescription>
        </Alert>
      )}
      {runFailed && (
        <Alert>
          <TriangleAlert className="size-4" />
          <AlertTitle>Zwischenergebnisse bleiben verfügbar</AlertTitle>
          <AlertDescription>
            Die Schritte, die vor dem Abbruch fertig wurden, sind im Graphen
            weiterhin anklickbar - so lässt sich nachsehen, welche Daten in den
            fehlgeschlagenen Schritt liefen.
          </AlertDescription>
        </Alert>
      )}
      <RunWarnings warnings={status?.warnings} />
      {failedSteps.length > 0 && (
        <Alert variant="destructive">
          <TriangleAlert className="size-4" />
          <AlertTitle>
            {failedSteps.length === 1
              ? "Ein Schritt ist fehlgeschlagen"
              : `${failedSteps.length} Schritte sind fehlgeschlagen`}
          </AlertTitle>
          <AlertDescription>
            <ul className="max-h-40 list-disc overflow-y-auto pl-4">
              {failedSteps.map((s) => (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => selectFromGraph(s.id)}
                    className="font-mono underline underline-offset-2"
                  >
                    {s.id}
                  </button>
                  : {s.error}
                </li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}

      {/* Entweder Vollbild-Graph ODER Ergebnis, nie beides (FA67). Knotenklick
          im Graphen wählt den Knoten und kehrt zum Ergebnis zurück (FA32). */}
      {view === "graph" ? (
        <section className="h-[calc(100vh-14rem)] min-h-[32rem] overflow-hidden rounded-xl border bg-card">
          {graphStatus ? (
            <RunGraph status={graphStatus} selected={selectedNode} onSelect={selectFromGraph} />
          ) : (
            <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
              Kein Graph verfügbar.
            </div>
          )}
        </section>
      ) : (
        <section className="min-w-0 rounded-xl border bg-card p-4 sm:p-6">
          {selectedStep?.status === "error" ? (
            // Fehlerhafter Knoten: hier gibt es kein Ergebnis, aber der
            // Fehlertext gehört genau hierhin (FA33) - vorher stand er
            // nur im nativen Tooltip des Graphknotens.
            <div className="flex flex-col gap-3">
              <div>
                <h3 className="truncate text-base font-semibold">{selectedNode}</h3>
                <p className="text-sm text-muted-foreground">
                  {selectedStep.op} · fehlgeschlagen
                </p>
              </div>
              <Alert variant="destructive">
                <TriangleAlert className="size-4" />
                <AlertTitle>Dieser Schritt ist fehlgeschlagen</AlertTitle>
                {/* Deckel + Scroll: ein langer Traceback darf die Karte
                    darunter nicht aus dem Blickfeld schieben. */}
                <AlertDescription className="max-h-48 overflow-y-auto whitespace-pre-wrap font-mono text-xs">
                  {selectedStep.error}
                </AlertDescription>
              </Alert>
              <p className="text-sm text-muted-foreground">
                Ein Ergebnis gibt es für diesen Schritt nicht. Die Eingangsdaten
                stehen in den Vorgängerknoten - dort anklicken, um zu sehen,
                womit der Schritt gerechnet hat.
              </p>
            </div>
          ) : selectedStep && selectedStep.status !== "done" ? (
            <div className="flex h-64 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
              <span className="font-mono">{selectedNode}</span>
              <span>
                {selectedStep.status === "running" || selectedStep.status === "loading"
                  ? "Läuft noch - das Ergebnis erscheint, sobald der Schritt fertig ist."
                  : "Dieser Schritt wurde nicht ausgeführt."}
              </span>
            </div>
          ) : (
            <NodeInspector
              runId={runId}
              nodeId={selectedNode}
              version={nodeVersion}
              warnings={selectedNode ? status?.steps[selectedNode]?.warnings : undefined}
              warningDetails={selectedNode ? status?.steps[selectedNode]?.warning_details : undefined}
              provenance={selectedNode ? status?.layer_provenance?.[selectedNode] : undefined}
              region={status?.region}
              origin={selectedNode ? status?.origins?.[selectedNode] : undefined}
              planAttribution={selectedNode ? status?.node_attribution?.[selectedNode] : undefined}
              configuredColorField={selectedNode ? outputColorFields[selectedNode] : undefined}
              onRunGone={onRunGone}
            />
          )}
        </section>
      )}
    </div>
  );
}

// --- Lauf-Attribution und Lauf-Warnungen (FA65/FA67, FA56) -----------------

// Kopfzeile der Ergebnisansicht: Region + Arbeits-CRS (FA55/FA56) und die
// Lizenzen aller Ausgabequellen des Laufs (FA65), Lizenz-Links in neuem Tab.
function RunAttribution({
  attribution,
  region,
}: {
  attribution?: AttributionOut | null;
  region?: RegionInfoOut | null;
}) {
  const licenses = attribution?.licenses ?? [];
  const undeclared = attribution?.undeclared ?? [];
  if (!region && licenses.length === 0 && undeclared.length === 0) return null;
  return (
    <div className="flex min-w-0 flex-col gap-0.5 text-xs text-muted-foreground">
      {region && (
        <span className="truncate" title={region.crs_label}>
          Region: {region.display_name ?? region.bbox.map((v) => formatDecimal(v, 3)).join(" / ")}
          {" · "}
          {formatCount(region.area_km2)} km² · Arbeits-CRS {region.crs} (
          {region.crs_mode === "auto" ? "auto" : "deklariert"})
        </span>
      )}
      {(licenses.length > 0 || undeclared.length > 0) && (
        <span className="truncate">
          Datenquellen:{" "}
          {licenses.map((lic, i) => (
            <span key={`${lic.name}-${i}`}>
              {i > 0 && " · "}
              {lic.url ? (
                <a href={lic.url} target="_blank" rel="noreferrer" className="underline underline-offset-2">
                  {lic.name}
                </a>
              ) : (
                lic.name
              )}
              {lic.attribution ? ` (${lic.attribution})` : ""}
            </span>
          ))}
          {undeclared.length > 0 && (
            <span className="text-amber-600 dark:text-amber-400">
              {licenses.length > 0 && " · "}
              ohne Lizenzangabe: {undeclared.join(", ")}
            </span>
          )}
        </span>
      )}
    </div>
  );
}

function normalizeWarning(w: RunWarningOut | string): RunWarningOut {
  return typeof w === "string" ? { message: w, severity: "warning" } : w;
}

// Warnungen auf Lauf-Ebene (Regionswarnungen, FA56) - getrennt nach Schwere:
// Hinweise (info) neutral, Warnungen gelb.
function RunWarnings({
  warnings,
  className,
}: {
  warnings?: (RunWarningOut | string)[];
  className?: string;
}) {
  const all = (warnings ?? []).map(normalizeWarning);
  if (all.length === 0) return null;
  const infos = all.filter((w) => w.severity === "info");
  const warns = all.filter((w) => w.severity !== "info");
  return (
    <>
      {warns.length > 0 && (
        <Alert className={`border-amber-400/40 bg-amber-400/10 text-amber-700 dark:text-amber-300 ${className ?? ""}`}>
          <TriangleAlert className="size-4" />
          <AlertTitle>{warns.length === 1 ? "Warnung zum Lauf" : `${warns.length} Warnungen zum Lauf`}</AlertTitle>
          <AlertDescription className="text-amber-700 dark:text-amber-300">
            <ul className="list-disc pl-4">
              {warns.map((w, i) => (
                <li key={i}>{w.message}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
      {infos.length > 0 && (
        <Alert className={className}>
          <Info className="size-4" />
          <AlertTitle>{infos.length === 1 ? "Hinweis zum Lauf" : `${infos.length} Hinweise zum Lauf`}</AlertTitle>
          <AlertDescription>
            <ul className="list-disc pl-4">
              {infos.map((w, i) => (
                <li key={i}>{w.message}</li>
              ))}
            </ul>
          </AlertDescription>
        </Alert>
      )}
    </>
  );
}

// FA59: `parameters:`-Block der YAML als ParameterInfo-Liste (Formular-Fallback
// und Namensfilter der Überschreibungen). Ungültige YAML -> keine Parameter;
// die Validierung meldet den Fehler mit Position.
function parseDeclaredParameters(configYaml: string): ParameterInfo[] {
  let doc: unknown;
  try {
    doc = loadYaml(configYaml);
  } catch {
    return [];
  }
  if (!doc || typeof doc !== "object") return [];
  const raw = (doc as Record<string, unknown>).parameters;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  return Object.entries(raw as Record<string, unknown>).map(([name, spec]) => {
    const s = (spec && typeof spec === "object" ? spec : {}) as Record<string, unknown>;
    return {
      name,
      type: typeof s.type === "string" ? s.type : "string",
      items: typeof s.items === "string" ? s.items : "string",
      default: s.default ?? null,
      description: typeof s.description === "string" ? s.description : null,
      choices: Array.isArray(s.choices) ? s.choices : null,
      required: s.default === undefined || s.default === null,
    };
  });
}

// Knoten, den man nach Ende eines Laufs sinnvoll aufschlägt: bei Erfolg
// der letzte, bei Fehler der fehlgeschlagene (dort steht die Ursache),
// sonst der letzte fertige. Fällt auf den letzten Knoten der Reihenfolge
// zurück. FA33.
function lastInspectableNode(status: RunStatus): string | null {
  const failed = status.order.filter((id) => status.steps[id]?.status === "error");
  if (failed.length > 0) return failed[failed.length - 1];
  const done = status.order.filter((id) => status.steps[id]?.status === "done");
  if (done.length > 0) return done[done.length - 1];
  return status.order[status.order.length - 1] ?? null;
}

function applyEvent(status: RunStatus, event: Record<string, unknown>): RunStatus {
  const type = event.type as string;
  const detail = (event.detail && typeof event.detail === "object" ? event.detail : {}) as Record<string, unknown>;

  // --- Ereignisse auf Lauf-Ebene (FA55/FA56/FA58/FA65) ---------------------
  if (type === "region_ready") {
    // detail = RegionInfo.to_dict(): Arbeits-CRS, Modus, BBox, Fläche, Name.
    return { ...status, region: detail as unknown as RegionInfoOut };
  }
  if (type === "region_warning") {
    const message = event.message as string | undefined;
    if (!message) return status;
    const warning: RunWarningOut = {
      message,
      category: (detail.category as string | undefined) ?? "RegionWarning",
      severity: (detail.severity as string | undefined) ?? "warning",
    };
    return { ...status, warnings: [...(status.warnings ?? []), warning] };
  }
  if (type === "plan") {
    // Lizenzen je Knoten (plan.attribution) - Grundlage für den Inspector,
    // solange das Knoten-Ergebnis keine eigene Attribution trägt.
    const perNode = detail.attribution as Record<string, AttributionOut> | undefined;
    return perNode ? { ...status, node_attribution: perNode } : status;
  }
  if (type === "run_done") {
    // Vereinigung der Lizenzen aller Ausgabequellen (Kopf der Ergebnisansicht).
    const attribution = detail.attribution as AttributionOut | undefined;
    return attribution ? { ...status, attribution } : status;
  }

  if (type === "layer_loaded") {
    const layerId = event.layer as string | undefined;
    if (!layerId) return status;
    const ms = event.duration_ms as number | undefined;
    // FA58: CRS-Herkunft des Layers (Quell-CRS -> Arbeits-CRS).
    const provenance = detail.provenance as LayerProvenanceOut | undefined;
    return {
      ...status,
      layer_load_ms: ms != null ? { ...status.layer_load_ms, [layerId]: ms } : status.layer_load_ms,
      layer_provenance: provenance
        ? { ...(status.layer_provenance ?? {}), [layerId]: provenance }
        : status.layer_provenance,
    };
  }

  const stepId = event.step as string | undefined;
  if (!stepId || !status.steps[stepId]) {
    if (type === "run_error") return { ...status, status: "error", error: event.error as string };
    if (type === "run_cancelled") return cancelRunStatus(status);
    // Wie run_manager._apply: eine Warnung ohne zugehörigen Schritt (Layer nur
    // für eine Ausgabe) landet auf Lauf-Ebene - nie verworfen.
    if ((type === "step_warning" || type === "layer_warning") && event.message) {
      const warning: RunWarningOut = {
        message: event.message as string,
        category: (detail.category as string | undefined) ?? null,
        severity: (detail.severity as string | undefined) ?? "warning",
        layer: (event.layer as string | undefined) ?? null,
        step: stepId ?? null,
      };
      return { ...status, warnings: [...(status.warnings ?? []), warning] };
    }
    return status;
  }
  const steps = { ...status.steps };
  const step: StepState = {
    ...steps[stepId],
    warnings: [...steps[stepId].warnings],
    warning_details: [...(steps[stepId].warning_details ?? [])],
  };
  if (type === "layer_loading") step.status = "loading";
  else if (type === "step_running") step.status = "running";
  // Warnungen und Fehler beim Laden eines Layers erscheinen am Schritt, der ihn
  // braucht (der Layer wird unmittelbar davor geladen) - wie im Backend
  // (run_manager._apply), damit Live-Ansicht und Status nach einem Neuladen
  // dasselbe zeigen. Die Schwere (info = Hinweis, warning) kommt aus detail.severity.
  else if (type === "step_warning" || type === "layer_warning") {
    if (event.message) {
      step.warnings.push(event.message as string);
      step.warning_details!.push({
        message: event.message as string,
        category: (detail.category as string | undefined) ?? null,
        severity: (detail.severity as string | undefined) ?? "warning",
        layer: (event.layer as string | undefined) ?? null,
        step: stepId,
      });
    }
  } else if (type === "step_done") {
    step.status = "done";
    step.duration_ms = (event.duration_ms as number) ?? null;
  } else if (type === "step_error" || type === "layer_error") {
    step.status = "error";
    step.error = event.error as string;
  }
  steps[stepId] = step;
  return { ...status, steps };
}
