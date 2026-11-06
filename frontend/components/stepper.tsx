"use client";

import { useEffect, useRef } from "react";
import { ChevronRight, Check } from "lucide-react";
import { cn } from "@/lib/utils";
import type { WizardStep, WizardStepInfo } from "@/lib/wizardSteps";

// Implements: FA96 - der Stepper zeigt die Schritte des gewählten Einstiegs (lib/wizardSteps.ts).
export type { WizardStep };

export default function Stepper({
  steps,
  current,
  reachable,
  completed,
  reasons,
  onSelect,
}: {
  steps: WizardStepInfo[];
  current: WizardStep;
  reachable: Record<WizardStep, boolean>;
  completed?: Partial<Record<WizardStep, boolean>>;
  // Grund, warum ein Schritt gesperrt ist (title des gesperrten Knopfes).
  reasons?: Partial<Record<WizardStep, string | null>>;
  onSelect: (step: WizardStep) => void;
}) {
  // Vier Schritte sind auf einem schmalen Bildschirm breiter als die Leiste: der
  // aktuelle Schritt wird in die Mitte geschoben. Nur waagerecht und nur in der
  // Leiste (scrollIntoView verschöbe unter Umständen auch die Seite).
  const navRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const nav = navRef.current;
    const active = nav?.querySelector<HTMLElement>('[aria-current="step"]');
    if (!nav || !active || nav.scrollWidth <= nav.clientWidth) return;
    const navBox = nav.getBoundingClientRect();
    const box = active.getBoundingClientRect();
    nav.scrollTo({ left: nav.scrollLeft + box.left - navBox.left - (nav.clientWidth - box.width) / 2 });
  }, [current, steps.length]);

  return (
    <nav
      ref={navRef}
      className="sticky top-13.25 z-10 flex items-stretch gap-0 overflow-x-auto border-b bg-muted/30 px-3 backdrop-blur sm:px-6"
    >
      {steps.map((step, i) => {
        const active = step.id === current;
        const enabled = reachable[step.id];
        const isDone = completed?.[step.id] && !active;
        return (
          <div key={step.id} className="flex shrink-0 items-stretch">
            <button
              onClick={() => onSelect(step.id)}
              // Der aktuelle Schritt wird nie entzogen, auch wenn er (noch) nicht erreichbar wäre.
              disabled={!enabled && !active}
              data-step={step.id}
              aria-current={active ? "step" : undefined}
              title={!enabled && !active ? (reasons?.[step.id] ?? undefined) : undefined}
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
                {isDone ? <Check className="size-4" /> : i + 1}
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
            {i < steps.length - 1 && (
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
