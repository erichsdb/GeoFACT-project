"use client";

// Implements: FA86 - Herkunftsleiste mit Direktsprüngen.

import { ArrowLeft, FolderOpen, PencilLine, Sparkles, CircleHelp } from "lucide-react";
import { describeOrigin, type OriginRecord } from "@/lib/configOrigin";
import type { EntryMode } from "@/lib/entryMode";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

const ICON = {
  generate: Sparkles,
  load: FolderOpen,
  write: PencilLine,
  unknown: CircleHelp,
} as const;

/** Sagt, woher die aktuelle Konfiguration stammt, und bietet von jedem Schritt
 * aus den Sprung dorthin zurück: zum Prompt, zur Szenarioauswahl oder in den
 * Editor. `hideEdit` blendet "Konfiguration bearbeiten" aus, wo der Editor
 * ohnehin zu sehen ist (Schritt 1). `mode` (nur Schritt 1) nennt den gewählten
 * Einstieg: dessen Eingabe bleibt erreichbar, auch wenn der Text anders entstand -
 * sonst gäbe es beim Laden mit einem erzeugten Text keinen Weg zur Szenarioauswahl. */
export default function OriginBar({
  record,
  yaml,
  onAdjustPrompt,
  onPickScenario,
  onEditConfig,
  hideEdit = false,
  mode = null,
  className,
}: {
  record: OriginRecord | null;
  yaml: string;
  onAdjustPrompt: () => void;
  onPickScenario: () => void;
  onEditConfig: () => void;
  hideEdit?: boolean;
  mode?: EntryMode | null;
  className?: string;
}) {
  if (!yaml.trim()) return null;
  const view = describeOrigin(record, yaml);
  const Icon = ICON[view.kind];
  return (
    <div
      data-slot="origin-bar"
      data-origin={view.kind}
      className={`flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border bg-card px-4 py-2.5 text-sm ${className ?? ""}`}
    >
      <Icon className="size-4 shrink-0 text-primary" aria-hidden />
      <p className="min-w-0 flex-1 basis-56 wrap-anywhere" title={view.full ?? undefined}>
        <span className="text-muted-foreground">{view.prefix}</span>
        {view.subject && <span className="font-medium"> {view.subject}</span>}
        {view.edited && (
          <Badge variant="outline" className="ml-2 align-middle">
            bearbeitet
          </Badge>
        )}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        {(view.kind === "generate" || mode === "generate") && (
          <Button variant="outline" size="sm" onClick={onAdjustPrompt}>
            <ArrowLeft className="size-4" />
            {view.kind === "generate" ? "Prompt anpassen" : "Zum Prompt"}
          </Button>
        )}
        {(view.kind === "load" || mode === "load") && (
          <Button variant="outline" size="sm" onClick={onPickScenario}>
            <ArrowLeft className="size-4" />
            {view.kind === "load" ? "Anderes Szenario wählen" : "Szenario wählen"}
          </Button>
        )}
        {!hideEdit && (
          <Button variant="ghost" size="sm" onClick={onEditConfig}>
            <PencilLine className="size-4" />
            Konfiguration bearbeiten
          </Button>
        )}
      </div>
    </div>
  );
}
