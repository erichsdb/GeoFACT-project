"use client";

// Implements: FA86 (Herkunft) - die Sprünge zurück übernimmt der Stepper (FA96).

import { FolderOpen, PencilLine, Sparkles, CircleHelp } from "lucide-react";
import { describeOrigin, type OriginRecord } from "@/lib/configOrigin";
import { Badge } from "@/components/ui/badge";

const ICON = {
  generate: Sparkles,
  load: FolderOpen,
  write: PencilLine,
  unknown: CircleHelp,
} as const;

/** Sagt, woher die aktuelle Konfiguration stammt (Prompt, Szenario oder selbst
 * geschrieben, ggf. "bearbeitet"). Den Weg dorthin zurück zeigt der Stepper:
 * Schritt 1 ist von jedem Schritt aus erreichbar. */
export default function OriginBar({
  record,
  yaml,
  className,
}: {
  record: OriginRecord | null;
  yaml: string;
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
    </div>
  );
}
