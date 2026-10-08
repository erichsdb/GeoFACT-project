"use client";

import { ChevronRight, Check } from "lucide-react";
import { cn } from "@/lib/utils";

export type WizardStep = "prompt" | "execution" | "result";

const STEPS: { id: WizardStep; index: number; label: string; hint: string }[] = [
  { id: "prompt", index: 1, label: "Szenario", hint: "Generieren, laden oder selbst schreiben" },
  { id: "execution", index: 2, label: "Ausführung", hint: "Pipeline starten und verfolgen" },
  { id: "result", index: 3, label: "Ergebnis", hint: "Karte, Tabelle, Statistik" },
];

export default function Stepper({
  current,
  reachable,
  completed,
  onSelect,
}: {
  current: WizardStep;
  reachable: Record<WizardStep, boolean>;
  completed?: Partial<Record<WizardStep, boolean>>;
  onSelect: (step: WizardStep) => void;
}) {
  return (
    <nav className="sticky top-13.25 z-10 flex items-stretch gap-0 overflow-x-auto border-b bg-muted/30 px-3 backdrop-blur sm:px-6">
      {STEPS.map((step, i) => {
        const active = step.id === current;
        const enabled = reachable[step.id];
        const isDone = completed?.[step.id] && !active;
        return (
          <div key={step.id} className="flex shrink-0 items-stretch">
            <button
              onClick={() => enabled && onSelect(step.id)}
              disabled={!enabled}
              className={cn(
                "group relative flex items-center gap-2 px-3 py-3 text-left transition-colors sm:gap-3 sm:px-5 sm:py-4",
                active
                  ? "text-foreground"
                  : enabled
                  ? "text-muted-foreground hover:text-foreground"
                  : "cursor-not-allowed text-muted-foreground/40"
              )}
            >
              <span
                className={cn(
                  "flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-bold transition-colors sm:size-7",
                  active
                    ? "bg-primary text-primary-foreground"
                    : isDone
                    ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"
                    : enabled
                    ? "bg-secondary text-secondary-foreground"
                    : "bg-muted text-muted-foreground/50"
                )}
              >
                {isDone ? <Check className="size-4" /> : step.index}
              </span>
              <div className="leading-tight">
                <div className="text-sm font-semibold whitespace-nowrap">{step.label}</div>
                {/* Hint-Text braucht auf schmalen Screens zu viel Breite
                    für zu wenig Nutzen - Nummer/Haekchen + Label reichen
                    dort, um den aktuellen Schritt zu erkennen. */}
                <div className="hidden text-xs text-muted-foreground sm:block">{step.hint}</div>
              </div>
              {active && <span className="absolute inset-x-0 bottom-0 h-0.5 bg-primary" />}
            </button>
            {i < STEPS.length - 1 && (
              <span className="flex shrink-0 items-center text-muted-foreground/30" aria-hidden>
                <ChevronRight className="size-4" />
              </span>
            )}
          </div>
        );
      })}
    </nav>
  );
}
