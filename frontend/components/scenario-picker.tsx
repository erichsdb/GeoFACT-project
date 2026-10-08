"use client";

import { useEffect, useMemo, useState } from "react";
import { Search, FolderOpen, Loader2 } from "lucide-react";
import CodeMirror from "@uiw/react-codemirror";
import { yaml } from "@codemirror/lang-yaml";
import { useTheme } from "next-themes";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type { SavedScenario } from "@/lib/types";

/** Szenarioauswahl des Einstiegs "Szenario laden" (FA80): eigene gespeicherte
 * Konfigurationen und die eingecheckten Beispiele (Sachsen/Dresden + Leipzig-Suite)
 * als durchsuchbare, gruppierte Liste links, mit einer YAML-Vorschau des gerade
 * ausgewählten Eintrags rechts - bei > 10 Leipzig-Beispielen ist der Name
 * allein oft nicht aussagekräftig genug, um das passende Szenario zu
 * erkennen, und ein Blick in die tatsächliche Konfiguration hilft mehr als
 * nur die Kurzbeschreibung. Ein Klick links wählt nur aus (Vorschau lädt) -
 * erst der explizite "Laden"-Button rechts übernimmt die Konfiguration in den
 * Editor, damit exploratives Durchklicken nicht versehentlich die aktuelle
 * Arbeit überschreibt. */
export default function ScenarioPicker({
  scenarios,
  onSelect,
  preselect,
}: {
  scenarios: SavedScenario[];
  onSelect: (scenario: SavedScenario) => void;
  /** FA72: der per URL/Laden aktuelle Eintrag, anfangs vorausgewählt. */
  preselect?: { id: string; source: "user" | "example" } | null;
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState<SavedScenario | null>(null);
  const [previewYaml, setPreviewYaml] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const { resolvedTheme } = useTheme();

  const filtered = useMemo(() => filterScenarios(scenarios, query), [scenarios, query]);

  const own = filtered.filter((s) => s.source === "user");
  const examples = filtered.filter((s) => s.source === "example");

  // Sobald die Liste da ist (sie kann nach dem Einblenden eintreffen), einmal
  // den aktuellen bzw. ersten Eintrag vorauswählen, damit die Vorschau nicht
  // leer bleibt. Zustandsanpassung während des Renderns (statt im Effekt),
  // damit kein kaskadierendes Neurendern entsteht.
  const [preselected, setPreselected] = useState(false);
  if (!preselected && scenarios.length > 0) {
    setPreselected(true);
    const linked = preselect
      ? scenarios.find((s) => s.id === preselect.id && s.source === preselect.source)
      : undefined;
    setActive(linked ?? scenarios[0]);
  }

  // Beim Wechsel der Auswahl die Vorschau auf "lädt" setzen (während des
  // Renderns, nicht im Effekt).
  const [prevActive, setPrevActive] = useState<SavedScenario | null>(null);
  if (active !== prevActive) {
    setPrevActive(active);
    if (active) {
      setPreviewLoading(true);
      setPreviewError(null);
    }
  }

  useEffect(() => {
    if (!active) {
      return;
    }
    let cancelled = false;
    api
      .loadScenario(active)
      .then(({ config_yaml }) => {
        if (!cancelled) setPreviewYaml(config_yaml);
      })
      .catch((e) => {
        if (!cancelled) setPreviewError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setPreviewLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

  // R17: Vorschau und "Laden" folgen dem Suchfilter - fällt der gewählte
  // Eintrag aus der Liste, rückt der erste Treffer nach (oder keiner), sonst
  // lüde "Laden" ein unsichtbares Szenario.
  const onQueryChange = (next: string) => {
    setQuery(next);
    const visible = filterScenarios(scenarios, next);
    if (!active || !visible.includes(active)) {
      setActive(visible[0] ?? null);
      if (!visible[0]) setPreviewYaml(null);
    }
  };

  const confirmLoad = () => {
    if (!active) return;
    onSelect(active);
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 gap-4 md:h-[65vh] md:grid-cols-[minmax(0,18rem)_1fr]">
        <div className="flex max-h-72 min-h-0 flex-col gap-3 md:max-h-none">
          <div className="relative shrink-0">
            <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => onQueryChange(e.target.value)}
              placeholder="Suchen…"
              className="pl-8"
            />
          </div>

          <ScrollArea className="-mx-1 min-h-0 flex-1 px-1">
            <div className="flex flex-col gap-4 pb-1">
              <ScenarioGroup
                title="Eigene Szenarien"
                items={own}
                activeId={active ? `${active.source}:${active.id}` : null}
                onSelect={setActive}
              />
              <ScenarioGroup
                title="Beispiele"
                items={examples}
                activeId={active ? `${active.source}:${active.id}` : null}
                onSelect={setActive}
              />
              {filtered.length === 0 && (
                <p className="px-1 py-6 text-center text-sm text-muted-foreground">
                  Keine Szenarien gefunden.
                </p>
              )}
            </div>
          </ScrollArea>
        </div>

        <div className="flex h-96 min-h-0 flex-col overflow-hidden rounded-lg border bg-muted/30 md:h-auto">
          {active ? (
            <>
              <div className="flex shrink-0 items-start justify-between gap-3 border-b bg-card px-3 py-2.5">
                <div className="min-w-0">
                  <p className="line-clamp-2 text-sm font-medium">{active.name}</p>
                  {active.description && (
                    <p className="line-clamp-2 text-xs text-muted-foreground">{active.description}</p>
                  )}
                </div>
                <Button size="sm" onClick={confirmLoad} className="shrink-0">
                  <FolderOpen className="size-4" />
                  Laden
                </Button>
              </div>
              <div className="min-h-0 flex-1 overflow-auto">
                {previewLoading && (
                  <div className="flex h-full items-center justify-center text-muted-foreground">
                    <Loader2 className="size-5 animate-spin" />
                  </div>
                )}
                {previewError && (
                  <p className="p-3 text-sm text-destructive">{previewError}</p>
                )}
                {!previewLoading && !previewError && previewYaml != null && (
                  <CodeMirror
                    value={previewYaml}
                    editable={false}
                    basicSetup={{ foldGutter: false, highlightActiveLine: false }}
                    height="100%"
                    theme={resolvedTheme === "dark" ? "dark" : "light"}
                    extensions={[yaml()]}
                    className="h-full text-xs [&_.cm-editor]:h-full"
                  />
                )}
              </div>
            </>
          ) : (
            <p className="flex h-full items-center justify-center px-4 text-center text-sm text-muted-foreground">
              Kein Szenario ausgewählt.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function filterScenarios(scenarios: SavedScenario[], query: string): SavedScenario[] {
  const q = query.trim().toLowerCase();
  if (!q) return scenarios;
  return scenarios.filter(
    (s) => s.name.toLowerCase().includes(q) || (s.description ?? "").toLowerCase().includes(q)
  );
}

function ScenarioGroup({
  title,
  items,
  activeId,
  onSelect,
}: {
  title: string;
  items: SavedScenario[];
  activeId: string | null;
  onSelect: (scenario: SavedScenario) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div>
      <p className="mb-1.5 px-1 text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {title}
      </p>
      <div className="flex flex-col gap-0.5">
        {items.map((s) => {
          const id = `${s.source}:${s.id}`;
          const isActive = id === activeId;
          return (
            <button
              key={id}
              type="button"
              onClick={() => onSelect(s)}
              className={`line-clamp-2 rounded-lg px-2.5 py-1.5 text-left text-sm transition-colors focus-visible:outline-none ${
                isActive
                  ? "bg-primary/10 font-medium text-primary"
                  : "hover:bg-muted focus-visible:bg-muted"
              }`}
              title={s.name}
            >
              {s.name}
            </button>
          );
        })}
      </div>
    </div>
  );
}
