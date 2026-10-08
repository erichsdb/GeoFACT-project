"use client";

import { Fragment, useEffect, useState } from "react";
import { ChevronRight, Plus } from "lucide-react";
import { api } from "@/lib/api";
import type { OperationEntry, ParamSpec } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";

function paramType(spec: ParamSpec): string {
  if (spec.choices) return spec.choices.join(" | ");
  if (spec.type === "array" && spec.item_type) return `${spec.item_type}[]`;
  return spec.type;
}

// Docstrings kommen mit Einrückung und reST-Backticks aus dem Kern.
function docText(doc: string | null | undefined): string {
  return (doc ?? "").replace(/``/g, "").replace(/\s+/g, " ").trim();
}

// Erster Satz als Kurzbeschreibung, der Rest bleibt hinter "Details". Ein Punkt
// nach einem einzelnen Buchstaben ("z. B.") beendet keinen Satz.
function splitDoc(doc: string): { summary: string; details: string } {
  const match = /^(.*?(?<![A-Za-z])[.!?])\s+(?=[A-ZÄÖÜ„"'])/.exec(doc);
  if (!match) return { summary: doc, details: "" };
  return { summary: match[1], details: doc.slice(match[0].length) };
}

const TYPE_COLOR: Record<string, string> = {
  vector: "text-sky-600 dark:text-sky-400",
  raster: "text-orange-600 dark:text-orange-400",
  graph: "text-fuchsia-600 dark:text-fuchsia-400",
};

// Vertrag der Operation auf einen Blick: (port: typ, …) → ausgabetyp.
function Signature({ op }: { op: OperationEntry }) {
  const ports = Object.entries(op.input_ports);
  return (
    <span className="font-mono text-xs text-muted-foreground">
      (
      {ports.length === 0 && <span className="font-sans italic">keine Eingänge</span>}
      {ports.map(([port, type], i) => (
        <Fragment key={port}>
          {i > 0 && ", "}
          {port}: <span className={TYPE_COLOR[type] ?? ""}>{type}</span>
        </Fragment>
      ))}
      ) → <span className={TYPE_COLOR[op.output_type] ?? ""}>{op.output_type}</span>
    </span>
  );
}

function ParamRow({ name, spec }: { name: string; spec: ParamSpec }) {
  return (
    <li className="py-1.5">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <code className="font-mono font-medium text-foreground">{name}</code>
        <span className="font-mono text-[11px] break-all">{paramType(spec)}</span>
        {spec.required ? (
          <span className="text-[11px] font-medium text-amber-700 dark:text-amber-500">Pflicht</span>
        ) : (
          spec.default !== undefined &&
          spec.default !== null && (
            <span className="font-mono text-[11px]">Standard: {JSON.stringify(spec.default)}</span>
          )
        )}
      </div>
      {spec.description && <p className="mt-0.5 leading-relaxed">{spec.description}</p>}
    </li>
  );
}

export default function OperationsPanel({ onInsert }: { onInsert: (op: OperationEntry) => void }) {
  const [ops, setOps] = useState<OperationEntry[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [detailsOpen, setDetailsOpen] = useState(false);

  useEffect(() => {
    api.operations().then(setOps).catch(() => setOps([]));
  }, []);

  return (
    <div className="flex flex-col">
      <h3 className="mb-1 text-base font-semibold">Operationen</h3>
      <p className="mb-3 text-sm text-muted-foreground">
        Referenz + „+ Schritt&quot; fügt ein Skelett in die Konfiguration ein.
      </p>
      <div>
        <div className="space-y-2">
          {ops.map((op) => {
            const isOpen = open === op.op;
            const { summary, details } = splitDoc(docText(op.doc));
            const params = Object.entries(op.params);
            return (
              <Collapsible
                key={op.op}
                open={isOpen}
                onOpenChange={(o) => {
                  setOpen(o ? op.op : null);
                  setDetailsOpen(false);
                }}
                className="rounded-lg border bg-card text-sm"
              >
                <div className="flex items-start gap-2 px-3 py-2">
                  <CollapsibleTrigger className="flex min-w-0 flex-1 items-start gap-1.5 text-left">
                    <ChevronRight
                      className={`mt-1 size-3.5 shrink-0 text-muted-foreground transition-transform ${isOpen ? "rotate-90" : ""}`}
                    />
                    <span className="min-w-0">
                      <span className="block font-mono font-medium text-card-foreground">{op.op}</span>
                      <Signature op={op} />
                      {summary && (
                        <span
                          className={`mt-1 block text-xs text-muted-foreground ${isOpen ? "" : "line-clamp-2"}`}
                        >
                          {summary}
                        </span>
                      )}
                    </span>
                  </CollapsibleTrigger>
                  <Button variant="secondary" size="sm" onClick={() => onInsert(op)}>
                    <Plus className="size-3.5" />
                    Schritt
                  </Button>
                </div>
                <CollapsibleContent>
                  <div className="border-t px-3 py-2 text-xs text-muted-foreground">
                    <div className="font-medium text-foreground/80">Parameter</div>
                    {params.length ? (
                      <ul className="divide-y">
                        {params.map(([name, spec]) => (
                          <ParamRow key={name} name={name} spec={spec} />
                        ))}
                      </ul>
                    ) : (
                      <p className="mt-0.5">keine</p>
                    )}
                    {details && (
                      <div className="mt-2 border-t pt-2">
                        <button
                          type="button"
                          onClick={() => setDetailsOpen((v) => !v)}
                          aria-expanded={detailsOpen}
                          className="text-primary underline underline-offset-2"
                        >
                          {detailsOpen ? "Details ausblenden" : "Details zum Verhalten"}
                        </button>
                        {detailsOpen && <p className="mt-1.5 leading-relaxed">{details}</p>}
                      </div>
                    )}
                  </div>
                </CollapsibleContent>
              </Collapsible>
            );
          })}
        </div>
      </div>
    </div>
  );
}
