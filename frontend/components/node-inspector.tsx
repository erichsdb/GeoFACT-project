"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dynamic from "next/dynamic";
import { Info, TriangleAlert } from "lucide-react";
import { api, isNotFoundError, RUN_GONE_MESSAGE } from "@/lib/api";
import { formatCount, formatDecimal } from "@/lib/format";
import type {
  AttributionOut,
  LayerProvenanceOut,
  NodeOriginOut,
  NodeResult,
  RegionInfoOut,
  RunWarningOut,
} from "@/lib/types";
import TableView from "./table-view";
import ChartView from "./chart-view";
import StatsView from "./stats-view";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";

const MapView = dynamic(() => import("./map-view"), { ssr: false });

const ID_LIKE = /(^id$|_id$)/i;

// Fallback-Heuristik, wenn das Szenario für diesen Knoten kein
// output[].color_field deklariert (configuredColorField, s. u.) - ohne
// dieses rät defaultColorField anhand der Spaltenreihenfolge, was bei
// spatial_join-Ergebnissen mit Spaltenkollisionen (z. B. q_left/r_left
// statt des eigentlich gewünschten Attributs) das falsche Feld wählen
// kann. Nur ein Rateversuch, kein Ersatz für eine explizite Deklaration.
function defaultColorField(fields: string[]): string {
  const meaningful = fields.filter((f) => !ID_LIKE.test(f));
  return (meaningful[0] ?? fields[0]) ?? "";
}

type Tab = "map" | "table" | "chart" | "stats";
const TABS: { id: Tab; label: string }[] = [
  { id: "map", label: "Karte" },
  { id: "table", label: "Tabelle" },
  { id: "chart", label: "Diagramm" },
  { id: "stats", label: "Statistik" },
];

export default function NodeInspector({
  runId,
  nodeId,
  version,
  warnings,
  warningDetails,
  provenance,
  region,
  origin,
  planAttribution,
  configuredColorField,
  onRunGone,
}: {
  runId: string;
  nodeId: string | null;
  version: number;
  warnings?: string[];
  // Schwere je Warnung (info = Hinweis, warning), soweit bekannt.
  warningDetails?: RunWarningOut[];
  // FA58: CRS-Herkunft (nur Layer-Knoten) und Region/Arbeits-CRS des Laufs.
  provenance?: LayerProvenanceOut;
  region?: RegionInfoOut | null;
  // FA75: Herkunft eines aus einem Modul erzeugten Knotens.
  origin?: NodeOriginOut;
  // FA65: Lizenzen dieses Knotens laut Plan - Fallback, solange das
  // Knoten-Ergebnis keine eigene Attribution mitbringt.
  planAttribution?: AttributionOut | null;
  // Vom Szenario deklariertes output[].color_field für diesen Knoten
  // (type: map) - hat Vorrang vor der defaultColorField-Heuristik, siehe
  // deren Docstring.
  configuredColorField?: string;
  // FA54: das Backend kennt den Lauf nicht mehr (404 der Statusabfrage, z. B.
  // nach einem Neustart). Der Aufrufer verwirft dann den Lauf und bietet
  // erneutes Ausführen an.
  onRunGone?: (runId: string) => void;
}) {
  const [result, setResult] = useState<NodeResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [colorField, setColorField] = useState<string>("");
  // Vom Select explizit gewähltes Farbfeld, getrennt von colorField (das
  // auch heuristisch/konfiguriert gesetzt wird) - nur ein echter Klick im
  // Select darf hier landen, siehe onValueChange unten und den Kommentar
  // im Fetch-Effekt zur Priorität. Ref statt State: wird nur im
  // Fetch-Effekt gelesen (nicht gerendert), ein Ref vermeidet dort eine
  // zusätzliche Effekt-Abhaengigkeit/erneuten Fetch bei jeder Auswahl.
  const userPickRef = useRef<string | null>(null);
  const [tab, setTab] = useState<Tab>("map");
  // Angefordertes Objektlimit. null = Server-Default (schneller
  // Erstaufruf); "mehr laden" setzt einen höheren Wert und lädt neu.
  const [limit, setLimit] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  // Ref, damit ein inline übergebener Callback den Fetch-Effekt nicht bei
  // jedem Render neu auslöst.
  const onRunGoneRef = useRef(onRunGone);
  useEffect(() => {
    onRunGoneRef.current = onRunGone;
  }, [onRunGone]);
  // Die Rasterbild-Probe läuft je Abruf höchstens einmal (siehe handleRasterError).
  const rasterProbedRef = useRef(false);

  // Rasterbild (raster.png) nicht ladbar: eigene Anfrage, die auch nach einem
  // erfolgreichen Knoten-Ergebnis scheitern kann (z. B. antwortet eine andere
  // Backend-Instanz). Einmal den Lauf prüfen (FA54) - bei jedem anderen
  // Ausgang bleibt es bei dem Eintrag im Konsolenlog, wie bisher.
  const handleRasterError = useCallback(() => {
    if (rasterProbedRef.current) return;
    rasterProbedRef.current = true;
    void api.runIsGone(runId).then((gone) => {
      if (!gone) return;
      setError(RUN_GONE_MESSAGE);
      onRunGoneRef.current?.(runId);
    });
  }, [runId]);

  // Knotenwechsel/-neulauf: zurück auf den schnellen Default, sonst
  // würde ein einmal erhöhtes Limit ungefragt für jeden weiteren
  // Knoten gelten. Gleichzeitig die Nutzerwahl des Farbfelds verwerfen -
  // sie gilt nur für den Knoten, an dem sie getroffen wurde, und darf das
  // deklarierte color_field eines ANDEREN Knotens nicht überstimmen (s.
  // Priorität im Fetch-Effekt unten). "mehr laden" (limit-Änderung
  // allein) löst diesen Effekt bewusst NICHT aus - dort bleibt die
  // Nutzerwahl für denselben Knoten erhalten.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLimit(null);
    userPickRef.current = null;
  }, [runId, nodeId, version]);

  useEffect(() => {
    if (!nodeId) return;
    let cancelled = false;
    // Fetch-in-progress flags for an async request kicked off by this
    // effect - not derivable from render, so the sync set here is intended.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (limit === null) setLoading(true);
    else setLoadingMore(true);
    setError(null);
    rasterProbedRef.current = false;
    api
      .nodeResult(runId, nodeId, limit ?? undefined)
      .then((r) => {
        if (cancelled) return;
        setResult(r);
        const fields = r.describe.numeric_fields ?? [];
        // Bei Knotenwechsel kann das bisher gewählte Farbfeld im neuen
        // Ergebnis fehlen (andere Attribute) - dann bliebe eine veraltete
        // Legende/Auswahl stehen, obwohl das Feld gar nicht mehr existiert.
        // Priorität: (1) echte Nutzerwahl (userPick, per Select-Klick
        // gesetzt), falls im neuen Ergebnis noch gültig, (2) das vom
        // Szenario für diesen Knoten deklarierte output[].color_field,
        // falls vorhanden und gültig, (3) die Spaltenreihenfolge-Heuristik
        // als letzter Rateversuch. Wichtig: colorField selbst darf hier
        // NICHT als "prev war Nutzerwahl" gelesen werden - colorField wird
        // auch heuristisch/konfiguriert gesetzt, und geteilte numerische
        // Spalten (z. B. objectid) zwischen zwei WFS-Layern ließen eine
        // Heuristik-Wahl des vorherigen Knotens sonst fälschlich als
        // "Nutzerwahl" überleben und die deklarierte color_field-Angabe
        // des neuen Knotens übertönen.
        setColorField(() => {
          // null = noch keine Nutzerwahl für diesen Knoten getroffen;
          // "" ist eine gültige Wahl ("Einfarbig") und zählt hier
          // mit - fields.includes("") wäre sonst nie wahr und "" würde
          // bei jedem Nachladen ("mehr laden") auf die Heuristik
          // zurückfallen, obwohl der Knoten unverändert ist.
          const userPick = userPickRef.current;
          if (userPick !== null && (userPick === "" || fields.includes(userPick))) {
            return userPick;
          }
          if (configuredColorField && fields.includes(configuredColorField)) {
            return configuredColorField;
          }
          return defaultColorField(fields);
        });
      })
      .catch(async (e) => {
        if (cancelled) return;
        // Ein 404 am Knoten-Endpunkt heißt zweierlei: der Knoten hat (noch)
        // kein Ergebnis, oder das Backend kennt den Lauf nicht mehr (FA54).
        // Der Status löst die Rückfrage aus, die Statusabfrage des Laufs
        // entscheidet - nie der Text der Meldung.
        const gone = isNotFoundError(e) && (await api.runIsGone(runId));
        if (cancelled) return;
        if (gone) {
          setError(RUN_GONE_MESSAGE);
          onRunGoneRef.current?.(runId);
        } else {
          setError(e instanceof Error ? e.message : String(e));
        }
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
        setLoadingMore(false);
      });
    return () => {
      cancelled = true;
      // Beim Nachladen das bisherige Ergebnis stehen lassen - sonst
      // flackert die Karte auf leer, obwohl schon Daten da sind.
      if (limit === null) setResult(null);
    };
  }, [runId, nodeId, version, limit, configuredColorField]);

  // Wie viele Objekte hat der Knoten wirklich, und wie viele sind da?
  const shownFeatures = result
    ? result.describe.kind === "vector"
      ? (result.geojson?.features.length ?? 0)
      : result.describe.kind === "graph"
        ? (result.graph?.nodes.length ?? 0)
        : 0
    : 0;
  const totalFeatures = result
    ? result.describe.kind === "vector"
      ? result.describe.feature_count
      : result.describe.kind === "graph"
        ? result.describe.node_count
        : 0
    : 0;
  const serverMax = result?.max_feature_limit ?? 0;
  const appliedLimit = result?.feature_limit ?? 0;
  // Nachladen lohnt nur, wenn wirklich noch etwas fehlt UND der Server
  // mehr erlaubt als bisher angefordert.
  const canLoadMore = totalFeatures > shownFeatures && appliedLimit < serverMax;
  const nextLimit = Math.min(Math.max(appliedLimit * 5, 50_000), serverMax, totalFeatures);

  const numericFields = result?.describe.numeric_fields ?? [];
  const describe = result?.describe;
  const attribution = result?.attribution ?? planAttribution ?? null;
  const { infos, warns } = splitWarnings(warnings ?? [], warningDetails ?? []);

  const summary = useMemo(() => {
    if (!describe) return null;
    if (describe.kind === "vector")
      return `${describe.feature_count} Objekte · ${(describe.geometry_types ?? []).join(", ")}`;
    if (describe.kind === "graph") return `${describe.node_count} Knoten · ${describe.edge_count} Kanten`;
    if (describe.kind === "raster") return `Raster ${describe.shape?.join("×")} · ${describe.dtype}`;
    return null;
  }, [describe]);

  if (!nodeId) {
    return (
      <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
        Knoten im Graphen anklicken, um sein Ergebnis zu sehen.
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-base font-semibold">{nodeId}</h3>
          {summary && <p className="text-sm text-muted-foreground">{summary}</p>}
          {canLoadMore && (
            <p className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>
                {formatCount(shownFeatures)} von {formatCount(totalFeatures)} geladen
              </span>
              <Button
                variant="outline"
                size="sm"
                className="h-6 px-2 text-xs"
                disabled={loadingMore}
                onClick={() => setLimit(nextLimit)}
              >
                {loadingMore
                  ? "lädt …"
                  : nextLimit >= totalFeatures
                    ? "alle laden"
                    : `${formatCount(nextLimit)} laden`}
              </Button>
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {tab === "map" && numericFields.length > 0 && describe?.kind !== "raster" && (
            <Select
              value={colorField || "__none"}
              onValueChange={(v) => {
                const next = v === "__none" || !v ? "" : v;
                // Sowohl ein konkretes Feld als auch "Einfarbig" (next ===
                // "") sind eine bewusste Nutzerwahl. "" über userPickRef
                // zu verfolgen macht sie innerhalb DESSELBEN Knotens
                // stabil (z. B. bei "mehr laden": fields.includes("") wäre
                // sonst nie wahr, die Wahl fällt beim Nachladen auf die
                // Heuristik zurück - siehe Fetch-Effekt, dort behandelt
                // ein leerer String als gültige Wahl einen Sonderfall).
                // Beim Knotenwechsel wird userPickRef ohnehin zurückgesetzt
                // (s. o.), "" blockiert dort also nie configuredColorField.
                userPickRef.current = next;
                setColorField(next);
              }}
            >
              <SelectTrigger className="w-auto">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none">Einfarbig</SelectItem>
                {numericFields.map((f) => (
                  <SelectItem key={f} value={f}>
                    Farbe: {f}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
            <TabsList>
              {TABS.map((t) => (
                <TabsTrigger key={t.id} value={t.id}>
                  {t.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>
      </div>

      {warns.length > 0 && (
        <Alert className="mt-3 border-amber-400/40 bg-amber-400/10 text-amber-700 dark:text-amber-300">
          <TriangleAlert className="size-4" />
          <AlertDescription className="text-sm text-amber-700 dark:text-amber-300">
            {warns.join(" · ")}
          </AlertDescription>
        </Alert>
      )}
      {infos.length > 0 && (
        <Alert className="mt-3">
          <Info className="size-4" />
          <AlertDescription className="text-sm">
            <span className="font-medium">{infos.length === 1 ? "Hinweis" : "Hinweise"}:</span>{" "}
            {infos.join(" · ")}
          </AlertDescription>
        </Alert>
      )}

      <div className="relative mt-3 h-[65vh] overflow-hidden rounded-lg border bg-muted/20 sm:h-136">
        {loading && (
          <div className="space-y-2 p-4">
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-32 w-full" />
          </div>
        )}
        {error && <div className="p-4 text-sm text-destructive">{error}</div>}
        {!loading && !error && (
          <>
            {/* Karte bleibt stets montiert (korrekte MapLibre-Größe); andere
                Ansichten überlagern sie, wenn ihr Tab aktiv ist - mit z-20
                über den MapLibre-Controls (z-index 2) und der Attribution,
                die sonst durch Tabelle/Statistik scheinen. */}
            <div className="h-full">
              <MapView
                result={result}
                colorField={colorField || undefined}
                showOverlays={tab === "map"}
                onRasterError={handleRasterError}
                attribution={attribution}
              />
            </div>
            {tab !== "map" && (
              <div data-slot="tab-overlay" className="absolute inset-0 z-20 bg-background">
                {tab === "table" && <TableView result={result} />}
                {tab === "chart" && <ChartView result={result} />}
                {tab === "stats" && (
                  <div className="h-full overflow-y-auto">
                    <ProvenanceBlock provenance={provenance} region={region} origin={origin} />
                    <StatsView result={result} />
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      <AttributionFooter attribution={attribution} />
    </div>
  );
}

// Warnungen nach Schwere trennen: eine Meldung ist ein Hinweis, wenn ihre
// Detailangabe severity === "info" trägt (Kategorie endet auf "Notice").
function splitWarnings(
  warnings: string[],
  details: RunWarningOut[]
): { infos: string[]; warns: string[] } {
  const infos: string[] = [];
  const warns: string[] = [];
  const pool = [...details];
  for (const message of warnings) {
    const at = pool.findIndex((d) => d.message === message);
    const severity = at >= 0 ? pool.splice(at, 1)[0].severity : "warning";
    if (severity === "info") infos.push(message);
    else warns.push(message);
  }
  return { infos, warns };
}

function fmtBounds(b: [number, number, number, number] | null | undefined): string {
  if (!b) return "–";
  const digits = Math.max(...b.map((v) => Math.abs(v))) > 1000 ? 0 : 4;
  // deutsche Dezimalkommas - die vier Werte daher mit " / " getrennt
  return b.map((v) => formatDecimal(v, digits)).join(" / ");
}

// FA58/FA56/FA75: Block "Herkunft" im Statistik-Tab - woher der Layer kam
// (Quell-CRS, Rohausdehnung) und wohin er harmonisiert wurde, dazu Region und
// Arbeits-CRS des Laufs und Modul/Instanz eines erzeugten Knotens.
function ProvenanceBlock({
  provenance,
  region,
  origin,
}: {
  provenance?: LayerProvenanceOut;
  region?: RegionInfoOut | null;
  origin?: NodeOriginOut;
}) {
  const generated = origin?.module ? origin : undefined;
  const instanceName = generated
    ? generated.key != null
      ? `${generated.instance}[${generated.key}]`
      : (generated.instance ?? "")
    : "";
  if (!provenance && !region && !generated) return null;
  return (
    <div className="border-b px-4 py-3 text-sm">
      <h4 className="mb-2 font-semibold">Herkunft</h4>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
        {provenance && (
          <>
            <dt className="text-muted-foreground">Quelle</dt>
            <dd>{provenance.source}</dd>
            <dt className="text-muted-foreground">Quell-CRS</dt>
            <dd>
              {provenance.source_crs ?? "–"} · {provenance.source_crs_label}
              {provenance.crs_override && (
                <span className="ml-1 text-amber-600 dark:text-amber-400">(erzwungen per crs_override)</span>
              )}
            </dd>
            <dt className="text-muted-foreground">Arbeits-CRS</dt>
            <dd>
              {provenance.target_crs} · {provenance.target_crs_label}
            </dd>
            <dt className="text-muted-foreground">Ausdehnung roh</dt>
            <dd className="font-mono">{fmtBounds(provenance.raw_bounds)}</dd>
            <dt className="text-muted-foreground">Ausdehnung harmonisiert</dt>
            <dd className="font-mono">{fmtBounds(provenance.bounds)}</dd>
            {provenance.feature_count != null && (
              <>
                <dt className="text-muted-foreground">Objekte</dt>
                <dd>{formatCount(provenance.feature_count)}</dd>
              </>
            )}
          </>
        )}
        {region && (
          <>
            <dt className="text-muted-foreground">Region</dt>
            <dd>
              {region.display_name ?? "BBox"} · {formatCount(region.area_km2)} km²
              {region.osm_class ? ` · ${region.osm_class}/${region.osm_type ?? "?"}` : ""}
            </dd>
            <dt className="text-muted-foreground">Region BBox</dt>
            <dd className="font-mono">{fmtBounds(region.bbox)}</dd>
            {!provenance && (
              <>
                <dt className="text-muted-foreground">Arbeits-CRS</dt>
                <dd>
                  {region.crs} ({region.crs_mode === "auto" ? "auto" : "deklariert"}) · {region.crs_label}
                </dd>
              </>
            )}
          </>
        )}
        {generated && (
          <>
            <dt className="text-muted-foreground">Modul</dt>
            <dd>{generated.module}</dd>
            <dt className="text-muted-foreground">Instanz</dt>
            <dd className="font-mono">{instanceName}</dd>
            <dt className="text-muted-foreground">Knoten im Modul</dt>
            <dd className="font-mono">{generated.inner_id}</dd>
            <dt className="text-muted-foreground">Position</dt>
            <dd className="font-mono">{generated.location}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

// FA65: Fußzeile "Datenquellen" - Lizenzen der Layer, aus denen dieser Knoten
// entstand, mit Links; Layer ohne Lizenzangabe werden genannt, nicht verschwiegen.
function AttributionFooter({ attribution }: { attribution: AttributionOut | null }) {
  if (!attribution) return null;
  const { licenses, undeclared } = attribution;
  if (licenses.length === 0 && undeclared.length === 0) return null;
  return (
    <p className="mt-2 text-xs text-muted-foreground">
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
    </p>
  );
}
