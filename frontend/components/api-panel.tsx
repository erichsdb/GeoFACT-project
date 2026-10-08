"use client";

/**
 * API-Katalog (FA36): behoerdliche/offene Schnittstellen als Layer-Quelle.
 *
 * Eine API ist von außen undurchdringlich - darum zeigt dieses Panel je
 * Schnittstelle, welche Endpunkte/Feature-Types definiert sind, welche
 * Attribute sie führen und wo die maschinenlesbare Spezifikation liegt.
 * Die Spezifikation öffnet sich beim Anbieter in einem neuen Tab: sie ist
 * je nach Schnittstelle XML, JSON, HTML oder ein PDF und mehrere MB groß -
 * das zeigt der Browser besser als ein Textfeld in der Seitenleiste.
 *
 * Ausgewählte APIs sind für das LLM automatisch sichtbar: der Katalog
 * steckt im Grounding-Prompt (backend/geofact_web/llm.py), eine Auswahl
 * reicht ihr Layer-Fragment zusätzlich als vorausgewählte Quelle durch.
 */

import { useEffect, useState } from "react";
import { ChevronRight, Plus, FileCode2, ExternalLink } from "lucide-react";
import { api } from "@/lib/api";
import type { CatalogApi, CatalogApiEndpoint } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";

// Beschriftung des Spezifikations-Links: sagt vor dem Klick, was sich öffnet.
function specLabel(entry: CatalogApi): string {
  if (entry.spec_type === "wfs_capabilities") return "GetCapabilities (XML)";
  if (entry.spec_type === "ckan_api") return "Paketliste (JSON)";
  if (entry.spec_url?.toLowerCase().endsWith(".pdf")) return "Handbuch (PDF)";
  if (entry.spec_type === "documentation") return "Schnittstellenbeschreibung";
  return "Spezifikation";
}

function EndpointRow({ endpoint }: { endpoint: CatalogApiEndpoint }) {
  return (
    <li className="min-w-0 rounded-md border bg-background/60 px-2.5 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <code className="min-w-0 font-mono text-xs font-medium break-all text-foreground">{endpoint.name}</code>
        {endpoint.method && (
          <Badge variant="outline" className="text-[10px] uppercase">
            {endpoint.method}
          </Badge>
        )}
        {endpoint.geometry && (
          <Badge variant="secondary" className="text-[10px] uppercase">
            {endpoint.geometry}
          </Badge>
        )}
      </div>
      {endpoint.title && (
        <p className="mt-0.5 text-xs text-muted-foreground">{endpoint.title}</p>
      )}
      {endpoint.attributes.length > 0 && (
        <p className="mt-1 font-mono text-[11px] leading-relaxed [overflow-wrap:anywhere] text-muted-foreground">
          {endpoint.attributes.join(" · ")}
        </p>
      )}
      {Object.entries(endpoint.attribute_notes).length > 0 && (
        <dl className="mt-1 space-y-0.5">
          {Object.entries(endpoint.attribute_notes).map(([field, note]) => (
            <div key={field} className="flex gap-1.5 text-[11px]">
              <dt className="font-mono text-foreground/70">{field}</dt>
              <dd className="text-muted-foreground">{note}</dd>
            </div>
          ))}
        </dl>
      )}
      {endpoint.notes && (
        <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-500">{endpoint.notes}</p>
      )}
    </li>
  );
}

function ApiCard({
  entry,
  checked,
  onToggle,
  onInsertLayer,
}: {
  entry: CatalogApi;
  checked: boolean;
  // FA80: ohne onToggle (Einstieg "selbst schreiben") keine Checkbox zum Vormerken.
  onToggle?: () => void;
  // FA80: ohne onInsertLayer (Vorauswahl für den Prompt) kein "+ Layer"-Knopf.
  onInsertLayer?: () => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div
      className={cn(
        "rounded-lg border bg-card text-sm transition-colors",
        onToggle && checked && "border-primary bg-primary/5"
      )}
    >
      {/* Vormerken: die ganze Kopfzeile schaltet um, nicht nur das Kästchen. */}
      <div
        className={cn("flex items-start gap-3 p-3", onToggle && "cursor-pointer rounded-t-lg hover:bg-accent/50")}
        onClick={onToggle}
      >
        {onToggle && (
          <Checkbox
            checked={checked}
            onCheckedChange={onToggle}
            onClick={(e) => e.stopPropagation()}
            aria-label={`${entry.label} vormerken`}
            className="mt-1"
          />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="font-medium text-card-foreground">{entry.label}</span>
            <Badge variant="outline" className="text-[10px] uppercase">
              {entry.protocol}
            </Badge>
            {/* 'rest' ist kein Layer-Quelltyp - solche Daten kommen als
                Tabelle herein und werden per attribute_join verbunden. */}
            {entry.protocol === "rest" && (
              <Badge variant="secondary" className="text-[10px]">
                via Tabelle + attribute_join
              </Badge>
            )}
            {entry.auth && (
              <Badge variant="secondary" className="text-[10px]">
                Zugang: {entry.auth}
              </Badge>
            )}
          </div>
          {entry.provider && (
            <p className="mt-0.5 text-xs text-muted-foreground">{entry.provider}</p>
          )}
          {entry.description && (
            <p className={cn("mt-1 text-sm text-muted-foreground", !open && "line-clamp-3")}>{entry.description}</p>
          )}
        </div>
        {entry.layer_template && onInsertLayer && (
          <Button
            variant="outline"
            size="sm"
            onClick={onInsertLayer}
            title="Beispiel-Layer in die Konfiguration einfügen"
          >
            <Plus className="size-3.5" />
            Layer
          </Button>
        )}
      </div>

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-start gap-1.5 border-t px-3 py-2 text-left text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
      >
        <ChevronRight className={`mt-0.5 size-3.5 shrink-0 transition-transform ${open ? "rotate-90" : ""}`} />
        <span className="shrink-0 whitespace-nowrap">
          {entry.endpoints.length} {entry.endpoints.length === 1 ? "Endpunkt" : "Endpunkte"}
        </span>
        {entry.license && <span className="ml-auto pl-2 text-right">{entry.license}</span>}
      </button>

      {open && (
        <div className="space-y-2 border-t px-3 py-2.5">
          <p className="font-mono text-[11px] break-all text-muted-foreground">{entry.base_url}</p>
          <ul className="space-y-1.5">
            {entry.endpoints.map((endpoint) => (
              <EndpointRow key={endpoint.name} endpoint={endpoint} />
            ))}
          </ul>
          {entry.notes && (
            <p className="text-[11px] text-amber-700 dark:text-amber-500">{entry.notes}</p>
          )}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            {entry.spec_url && (
              <a
                href={entry.spec_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 text-xs text-primary underline underline-offset-2"
              >
                <FileCode2 className="size-3" />
                {specLabel(entry)}
              </a>
            )}
            {entry.documentation_url && (
              <a
                href={entry.documentation_url}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 text-xs text-primary underline underline-offset-2"
              >
                <ExternalLink className="size-3" />
                Dokumentation
              </a>
            )}
            {entry.verified && (
              <span className="ml-auto text-[11px] text-muted-foreground">
                geprüft {entry.verified}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default function ApiPanel({
  selected,
  onToggle,
  onInsertLayer,
}: {
  selected: Set<string>;
  onToggle?: (id: string) => void;
  onInsertLayer?: (template: Record<string, unknown>) => void;
}) {
  const [apis, setApis] = useState<CatalogApi[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listApis()
      .then((res) => setApis(res.apis))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  if (error) {
    return <p className="mt-4 text-sm text-destructive">{error}</p>;
  }
  if (!apis) {
    return <p className="mt-4 text-sm text-muted-foreground">Lade API-Katalog…</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-muted-foreground">
        Offene Schnittstellen von Behörden und Portalen.
        {onToggle &&
          " Ausgewählte APIs sieht die Konfigurationsgenerierung automatisch mit allen Endpunkten und Attributen."}
      </p>
      <div className="space-y-2">
        {apis.map((entry) => (
          <ApiCard
            key={entry.id}
            entry={entry}
            checked={selected.has(entry.id)}
            onToggle={onToggle ? () => onToggle(entry.id) : undefined}
            onInsertLayer={
              onInsertLayer
                ? () => entry.layer_template && onInsertLayer(entry.layer_template)
                : undefined
            }
          />
        ))}
      </div>
    </div>
  );
}
