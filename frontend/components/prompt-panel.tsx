"use client";

import { useEffect, useRef, useState } from "react";
import { Sparkles, Loader2, RotateCcw, Brain, ChevronRight, TriangleAlert } from "lucide-react";
import { api } from "@/lib/api";
import type { GenerationOptions } from "@/lib/generationSettings";
import type { MissingDataset } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

// thinking/validating/repairing kommen als eigene SSE-Ereignisse vom
// Backend (FA31) - vorher war jede dieser Wartezeiten von außen
// ununterscheidbar "Generiere YAML" und wirkte bei langen Läufen wie ein
// Hänger.
type Phase =
  | "idle"
  | "thinking"
  | "streaming"
  | "validating"
  | "repairing"
  | "trial_run"
  | "reviewing"
  | "done"
  | "error";

const PHASE_LABEL: Record<Phase, string> = {
  idle: "",
  thinking: "Modell denkt nach …",
  streaming: "Generiere YAML …",
  validating: "Validiere Konfiguration …",
  repairing: "Korrigiere Konfiguration …",
  // FA78: Probelauf auf den echten Daten und Durchsicht seiner Warnungen.
  trial_run: "Probelauf auf echten Daten …",
  reviewing: "Prüfe Warnungen des Probelaufs …",
  done: "Fertig",
  error: "Fehler",
};

// Autoscroll, das sich abschalten lässt: neue Token laufen mit, aber sobald
// der Nutzer selbst hochscrollt, bleibt die Ansicht stehen - bis er wieder
// ans untere Ende scrollt.
//
// Kern des Problems: das scroll-Event taugt hier nicht als Grundlage. Es
// unterscheidet nicht, WER gescrollt hat, und es feuert asynchron. Zwei
// Anläufe sind daran gescheitert:
//
//  1. "Im scroll-Event die Position prüfen": das eigene Scrollen landet per
//     Definition unten und setzt die Marke bei jedem Token wieder auf
//     "mitlaufen".
//  2. "Absicht an wheel/touchmove ablesen, Position per
//     requestAnimationFrame messen": beim Streamen kommt das nächste Token
//     VOR dem nächsten Frame - die Messung kam zu spät, die Ansicht war
//     schon zurückgesprungen. (wheel deckt außerdem kein Ziehen am
//     Scrollbalken ab.)
//
// Beiden gemeinsam war eine GEMERKTE Marke, die im entscheidenden Moment
// veraltet war. Deshalb gibt es hier gar keinen Listener mehr: beim
// Nachführen wird die LIVE-Position gelesen (el.scrollTop stimmt sofort,
// auch wenn ein scroll-Event noch aussteht) und mit der Stelle verglichen,
// an der das eigene Scrollen zuletzt abgelegt hat. Weicht sie ab, war es
// der Nutzer - unabhängig davon, womit er gescrollt hat.
const STICK_TOLERANCE_PX = 24;

function useStickToBottom(
  ref: React.RefObject<HTMLElement | null>,
  deps: React.DependencyList
) {
  // Die Position, an der das eigene Scrollen den Bereich zuletzt abgelegt
  // hat. Steht der Bereich immer noch genau dort, hat der Nutzer seither
  // nicht selbst gescrollt - dann (und nur dann) wird nachgeführt.
  //
  // Das ersetzt eine gemerkte "kleben ja/nein"-Marke bewusst: die war im
  // entscheidenden Moment veraltet. Der Nutzer scrollt, das scroll-Event
  // steht aber noch in der Warteschlange, während schon das nächste Token
  // eintrifft - die Marke sagt dann noch "kleben" und die Ansicht springt
  // zurück. el.scrollTop ist dagegen SOFORT korrekt, auch wenn das Event
  // noch aussteht. Deshalb wird hier die Live-Position gelesen statt einer
  // Marke vertraut.
  const ownTop = useRef<number | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const target = Math.max(0, el.scrollHeight - el.clientHeight);

    if (ownTop.current !== null) {
      // Gemessen wird gegen die Position, an der das eigene Scrollen zuletzt
      // abgelegt hat - NICHT gegen das neue Ende. Der Inhalt ist in diesem
      // Durchlauf ja gerade gewachsen; gegen das neue Ende gemessen wäre
      // der Nutzer schon durch dieses Wachstum "zu weit oben".
      if (ownTop.current - el.scrollTop > STICK_TOLERANCE_PX) {
        ownTop.current = null; // Nutzer ist hochgescrollt -> abkoppeln
        return;
      }
    } else if (target - el.scrollTop > STICK_TOLERANCE_PX) {
      // Abgekoppelt: nur wieder andocken, wenn der Nutzer unten steht.
      return;
    }

    // scrollTop wird vom Browser auf scrollHeight - clientHeight geklemmt;
    // mit dem ungeklemmten scrollHeight zu vergleichen würde nie treffen.
    if (el.scrollTop !== target) el.scrollTop = target;
    ownTop.current = target;
    // deps steuert, wann nachgeführt wird (neuer Text, Pane aufgeklappt).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}

export default function PromptPanel({
  prompt,
  onPromptChange,
  region,
  onRegionChange,
  selectedIds,
  llmConfigured,
  options,
  onGenerated,
  onBusyChange,
}: {
  // FA86: Prompt und Region gehören der Seite (AppShell), nicht dem Panel: sie
  // überstehen Schrittwechsel und Neuladen.
  prompt: string;
  onPromptChange: (v: string) => void;
  region: string;
  onRegionChange: (v: string) => void;
  selectedIds: string[];
  llmConfigured: boolean;
  // FA81: Einstellungen des Nutzers (Fristen, Probelauf) für die nächste Erzeugung.
  options?: GenerationOptions;
  // `used` ist der Prompt samt Region, mit dem die Erzeugung gestartet wurde.
  onGenerated: (yaml: string, notes: string | null | undefined, used: { prompt: string; region: string }) => void;
  // FA86: meldet, ob gerade erzeugt wird (die Seite füllt dann den Prompt nicht neu).
  onBusyChange?: (busy: boolean) => void;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [streamedYaml, setStreamedYaml] = useState("");
  const [thinking, setThinking] = useState("");
  const [showThinking, setShowThinking] = useState(true);
  const thinkingRef = useRef<HTMLPreElement | null>(null);
  const yamlRef = useRef<HTMLPreElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  // FA87: das Modell hat statt einer Konfiguration fehlende Daten gemeldet.
  const [missing, setMissing] = useState<MissingDataset[]>([]);
  const [elapsedMs, setElapsedMs] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startRef = useRef(0);

  // FA78: auch Reparatur und Durchsicht streamen - dann läuft Text ein,
  // während die Phase weiter "Korrigiere ..." bzw. "Prüfe Warnungen ..." heißt.
  const receiving =
    phase === "streaming" || ((phase === "repairing" || phase === "reviewing") && streamedYaml !== "");

  const busy =
    phase === "thinking" ||
    phase === "streaming" ||
    phase === "validating" ||
    phase === "repairing" ||
    phase === "trial_run" ||
    phase === "reviewing";

  useEffect(() => {
    onBusyChange?.(busy);
  }, [busy, onBusyChange]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []);

  // Mitlaufendes Ende des Reasoning-Texts zeigen: der Nutzer will sehen,
  // WO das Modell gerade steht, nicht den Anfang seiner Überlegung.
  // Aber nur, solange er nicht selbst hochgescrollt hat - sonst reißt
  // ihm jedes neue Token die Stelle weg, die er gerade liest.
  useStickToBottom(thinkingRef, [thinking, showThinking]);
  useStickToBottom(yamlRef, [streamedYaml]);

  async function generate() {
    if (!prompt.trim()) return;
    const used = { prompt, region };
    setPhase("thinking");
    setStreamedYaml("");
    setThinking("");
    setError(null);
    setMissing([]);
    setElapsedMs(0);
    startRef.current = Date.now();
    timerRef.current = setInterval(() => setElapsedMs(Date.now() - startRef.current), 100);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const res = await api.generateStream(
        prompt,
        selectedIds,
        region || undefined,
        (delta) => {
          setStreamedYaml((prev) => prev + delta);
          // Erstes YAML-Token beendet die Denkphase der Erzeugung. In einer
          // Korrekturrunde bleibt die Phase stehen (FA78).
          setPhase((p) => (p === "thinking" ? "streaming" : p));
        },
        controller.signal,
        (delta) => setThinking((prev) => prev + delta),
        (serverPhase) => {
          if (serverPhase === "repairing" || serverPhase === "reviewing") {
            // FA78: die Tokens ab hier gehören zur korrigierten Fassung - die
            // Vorschau beginnt neu, der Gedankengang bekommt einen Absatz.
            setStreamedYaml("");
            setThinking((prev) => (prev ? `${prev.trimEnd()}\n\n` : prev));
          }
          if (
            serverPhase === "validating" ||
            serverPhase === "repairing" ||
            serverPhase === "trial_run" ||
            serverPhase === "reviewing"
          )
            setPhase(serverPhase);
        },
        options
      );
      if (res.missing_data && res.missing_data.length > 0) {
        // FA87: keine Konfiguration - der Editortext bleibt, wie er ist.
        setMissing(res.missing_data);
        setPhase("done");
        return;
      }
      onGenerated(res.config_yaml, res.notes, used);
      setPhase("done");
    } catch (e) {
      if ((e as Error)?.name === "AbortError") {
        setPhase("idle");
      } else {
        setError(e instanceof Error ? e.message : String(e));
        setPhase("error");
      }
    } finally {
      if (timerRef.current) clearInterval(timerRef.current);
    }
  }

  function cancelGenerate() {
    abortRef.current?.abort();
  }

  return (
    <div className="flex flex-col gap-4">
      <h3 className="text-base font-semibold">Ihre Frage</h3>

      <fieldset
        disabled={!llmConfigured}
        className="flex flex-col gap-4 disabled:opacity-50"
      >
        <label htmlFor="prompt-panel-prompt" className="sr-only">
          Prompt für die Konfiguration
        </label>
        <Textarea
          id="prompt-panel-prompt"
          value={prompt}
          onChange={(e) => onPromptChange(e.target.value)}
          disabled={busy}
          placeholder="z. B. Finde die kritischsten Umspannwerke im Stromnetz und die davon betroffene Bevölkerung."
          className="h-28 resize-none"
        />
        <label htmlFor="prompt-panel-region" className="sr-only">
          Region (optional)
        </label>
        <Input
          id="prompt-panel-region"
          value={region}
          onChange={(e) => onRegionChange(e.target.value)}
          disabled={busy}
          placeholder='Region (optional), z. B. "Dresden, Deutschland" oder BBox'
        />

        {!busy ? (
          <Button onClick={generate} disabled={!prompt.trim()}>
            <Sparkles className="size-4" />
            Konfiguration generieren
          </Button>
        ) : (
          <Button variant="secondary" onClick={cancelGenerate}>
            <Loader2 className="size-4 animate-spin" />
            {PHASE_LABEL[phase]} · {(elapsedMs / 1000).toFixed(1)}s · Abbrechen
          </Button>
        )}
      </fieldset>

      {selectedIds.length > 0 && !busy && (
        <p className="text-sm text-muted-foreground">{selectedIds.length} Quelle(n) vorausgewählt</p>
      )}
      {error && (
        <Alert variant="destructive" className="flex items-center justify-between gap-2">
          {/* min-w-0 + break: sonst drückt eine lange Fehlermeldung (URL,
              Stacktrace) den "Erneut"-Knopf aus der Box heraus. */}
          <AlertDescription className="min-w-0 flex-1 wrap-anywhere">
            {error}
          </AlertDescription>
          <Button variant="destructive" size="sm" className="shrink-0" onClick={generate}>
            <RotateCcw className="size-3.5" />
            Erneut
          </Button>
        </Alert>
      )}

      {missing.length > 0 && !busy && (
        <Alert
          data-slot="missing-data"
          className="border-amber-400/40 bg-amber-400/10 text-amber-700 dark:text-amber-300"
        >
          <TriangleAlert className="size-4" />
          <AlertTitle>
            {missing.length === 1 ? "Dem Sprachmodell fehlt ein Datensatz" : "Dem Sprachmodell fehlen Datensätze"}
          </AlertTitle>
          <AlertDescription className="text-amber-700 dark:text-amber-300 [&_p:not(:last-child)]:mb-2">
            <ul className="mb-2 list-disc pl-5">
              {missing.map((m) => (
                <li key={m.name} className="wrap-anywhere">
                  <span className="font-medium">{m.name}</span>
                  {m.reason && <> – {m.reason}</>}
                  {m.hint && <span className="block text-foreground/70">Geeignet: {m.hint}</span>}
                  {m.alternative && (
                    <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-foreground/70">
                      <span>Ohne diesen Datensatz: „{m.alternative}“</span>
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-6 px-2 text-xs"
                        onClick={() => onPromptChange(m.alternative ?? "")}
                      >
                        Als Frage übernehmen
                      </Button>
                    </span>
                  )}
                </li>
              ))}
            </ul>
            <p>
              Es wurde keine Konfiguration erzeugt. Laden Sie den Datensatz unter „Daten vormerken“ hoch –
              er wird für den Prompt vorgemerkt – und starten Sie die Erzeugung erneut.
            </p>
            <p>
              Oder formulieren Sie die Frage um und nennen Sie, woran sie gemessen werden soll: statt „die
              reichsten Wohnviertel“ etwa „die reichsten Wohnviertel anhand der Anzahl der Einfamilienhäuser“.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" onClick={generate} disabled={!prompt.trim()}>
                <RotateCcw className="size-3.5" />
                Erneut generieren
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}

      {thinking && (
        <div className="rounded-lg border bg-muted/30">
          <button
            type="button"
            onClick={() => setShowThinking((v) => !v)}
            aria-expanded={showThinking}
            className="flex w-full items-center justify-between gap-2 border-b px-3 py-2 text-left"
          >
            <span className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              <ChevronRight
                className={`size-3.5 transition-transform ${showThinking ? "rotate-90" : ""}`}
              />
              <Brain className="size-3.5" />
              Gedankengang
            </span>
            {phase === "thinking" || ((phase === "repairing" || phase === "reviewing") && !receiving) ? (
              <span className="flex items-center gap-1 text-xs text-primary">
                <Loader2 className="size-3.5 animate-spin" /> denkt …
              </span>
            ) : (
              <span className="text-xs text-muted-foreground">
                {thinking.length} Zeichen
              </span>
            )}
          </button>
          {showThinking && (
            <pre
              ref={thinkingRef}
              // Scrollbarer Bereich: per Tastatur fokussier- und scrollbar,
              // sonst kommt man ohne Maus nicht an den Text.
              tabIndex={0}
              className="max-h-40 overflow-auto whitespace-pre-wrap wrap-break-word px-3 py-2.5 font-mono text-xs leading-relaxed text-muted-foreground"
            >
              {thinking}
              {phase === "thinking" && <span className="animate-pulse text-primary">▍</span>}
            </pre>
          )}
        </div>
      )}

      {/* Nur während der Erzeugung: danach steht dasselbe YAML im Editor
          darunter, die Vorschau schob ihn nur aus dem Blick (R21). */}
      {busy && (
        <div className="rounded-lg border bg-muted/30">
          <div className="flex items-center justify-between border-b px-3 py-2">
            <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {phase === "repairing" || phase === "reviewing" ? "Live-Vorschau der Korrektur" : "Live-Vorschau"}
            </span>
            {receiving && (
              <span className="flex items-center gap-1 text-xs text-primary">
                <Loader2 className="size-3.5 animate-spin" /> streamt …
              </span>
            )}
          </div>
          <pre
            ref={yamlRef}
            tabIndex={0}
            className="max-h-56 overflow-auto whitespace-pre-wrap wrap-break-word px-3 py-2.5 font-mono text-xs leading-relaxed text-foreground/80"
          >
            {streamedYaml || " "}
            {receiving && <span className="animate-pulse text-primary">▍</span>}
          </pre>
        </div>
      )}
    </div>
  );
}
