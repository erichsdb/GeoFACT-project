"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  useEdgesState,
  useNodesInitialized,
  useNodesState,
  useReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
  type NodeProps,
} from "reactflow";
import dagre from "@dagrejs/dagre";
import { ChevronRight, TriangleAlert } from "lucide-react";
import type { LayerProvenanceOut, NodeOriginOut, RegionInfoOut, RunStatus } from "@/lib/types";

const STATUS_LEGEND: { status: string; label: string; dotClass: string }[] = [
  { status: "pending", label: "wartend", dotClass: "bg-muted-foreground/50" },
  { status: "loading", label: "lädt/läuft", dotClass: "bg-amber-400" },
  { status: "done", label: "fertig", dotClass: "bg-emerald-400" },
  { status: "error", label: "fehler", dotClass: "bg-destructive" },
  { status: "cancelled", label: "abgebrochen", dotClass: "bg-slate-500" },
];

const STATUS_MINIMAP_COLOR: Record<string, string> = {
  pending: "#94a3b8",
  loading: "#fbbf24",
  running: "#fbbf24",
  done: "#34d399",
  error: "#f43f5e",
  cancelled: "#64748b",
};

// Legende und CRS-Hinweis oben rechts: oben links lagen sie über der
// Kopfzeile der ersten Gruppenbox (aufgeklappte Instanz, FA75).
function Legend() {
  return (
    <div className="pointer-events-none absolute right-3 top-3 z-10 flex gap-3 rounded-lg border bg-card/95 px-3 py-2 text-xs text-card-foreground shadow-sm backdrop-blur">
      {STATUS_LEGEND.map((l) => (
        <span key={l.status} className="flex items-center gap-1.5">
          <span className={`h-2 w-2 rounded-full ${l.dotClass}`} />
          {l.label}
        </span>
      ))}
    </div>
  );
}

// FA55: Kopf des Graphen - Arbeits-CRS des Laufs und wie es zustande kam
// (auto = UTM-Zone aus dem Regionszentroid, FA5; sonst deklariert).
function WorkingCrsBadge({ region }: { region: RegionInfoOut }) {
  return (
    <div
      className="pointer-events-none absolute right-3 top-12 z-10 rounded-lg border bg-card/95 px-3 py-1.5 text-xs text-card-foreground shadow-sm backdrop-blur"
      title={region.crs_label}
    >
      Arbeits-CRS {region.crs} ({region.crs_mode === "auto" ? "auto" : "deklariert"})
    </div>
  );
}

// FA75: eine Farbe je Modul (nicht je Durchlauf) - Index des Modulnamens in
// der sortierten Liste aller Module, die in den Herkunftsangaben vorkommen.
// Deterministisch, damit dieselbe Expansion immer gleich aussieht.
const MODULE_COLORS = ["#6366f1", "#0ea5e9", "#14b8a6", "#f97316", "#d946ef", "#84cc16", "#e11d48", "#a16207"];

function moduleColors(origins: Record<string, NodeOriginOut> | undefined): Record<string, string> {
  const names = [...new Set(Object.values(origins ?? {}).map((o) => o.module).filter((m): m is string => !!m))].sort();
  return Object.fromEntries(names.map((name, i) => [name, MODULE_COLORS[i % MODULE_COLORS.length]]));
}

// FA75: Knoten einer Instanz werden gruppiert. Eine Instanz mit mehr als einem
// Schlüssel (foreach) erscheint eingeklappt als EIN Knoten "@inst:<id>"
// ("stadt ×80"); aufgeklappt bekommt jeder Schlüssel eine Gruppenbox
// "@grp:<id>[<key>]" (dagre compound). Eine Instanz ohne foreach ist immer
// eine Gruppenbox "@grp:<id>".
const INSTANCE_PREFIX = "@inst:";
const GROUP_PREFIX = "@grp:";
const GROUP_HEADER = 22;

type InstanceGroup = {
  id: string;
  module: string;
  keys: string[];
  members: string[];
  layerCount: number;
  stepCount: number;
};

function instanceGroups(
  layers: string[],
  steps: string[],
  origins: Record<string, NodeOriginOut> | undefined,
): Map<string, InstanceGroup> {
  const groups = new Map<string, InstanceGroup>();
  const layerSet = new Set(layers);
  for (const id of [...layers, ...steps]) {
    const origin = origins?.[id];
    if (!origin?.module || !origin.instance) continue;
    let group = groups.get(origin.instance);
    if (!group) {
      group = { id: origin.instance, module: origin.module, keys: [], members: [], layerCount: 0, stepCount: 0 };
      groups.set(origin.instance, group);
    }
    group.members.push(id);
    if (origin.key != null && !group.keys.includes(origin.key)) group.keys.push(origin.key);
    if (layerSet.has(id)) group.layerCount += 1;
    else group.stepCount += 1;
  }
  return groups;
}

const collapsible = (group: InstanceGroup) => group.keys.length > 1;

function groupIdOf(origin: NodeOriginOut): string {
  return `${GROUP_PREFIX}${origin.key != null ? `${origin.instance}[${origin.key}]` : origin.instance}`;
}

// FA58: Untertitel eines Layer-Knotens - Quell-CRS -> Arbeits-CRS, "erzwungen"
// bei crs_override. EPSG-Codes statt langer Namen (Name im Tooltip).
function crsSubtitle(p: LayerProvenanceOut): string {
  const src = p.source_crs ?? p.source_crs_label ?? "?";
  const forced = p.crs_override ? " (erzwungen)" : "";
  return src === p.target_crs ? `${src}${forced}` : `${src} → ${p.target_crs}${forced}`;
}

type NodeData = {
  label: string;
  sub?: string;
  status: string;
  selected: boolean;
  kind: "layer" | "step" | "instance";
  warn?: boolean;
  error?: string;
  // FA75: Knoten im Modul (inner_id) bzw. Fortschritt einer eingeklappten Instanz.
  badge?: string;
  badgeColor?: string;
  // FA58: Tooltip-Zusatz (volle CRS-Namen); FA75: Position in der YAML.
  hint?: string;
};

type GroupData = {
  label: string;
  color: string;
  onCollapse?: () => void;
};

function StatusNode({ data }: NodeProps<NodeData>) {
  const cls = `rf-node rf-node--${data.status}`;
  // Bei Fehlerstatus die eigentliche Fehlermeldung im Tooltip zeigen statt
  // nur Label/Sub - sonst ist der Fehlgrund im Graphen unsichtbar.
  const base = data.error
    ? `${data.label} · Fehler: ${data.error}`
    : data.sub
      ? `${data.label} · ${data.sub}`
      : data.label;
  const title = [base, data.hint].filter(Boolean).join("\n");
  return (
    <div
      className={`${cls} ${data.selected ? "rf-node--selected" : ""} ${data.kind === "instance" ? "cursor-pointer" : ""}`}
      title={title}
      style={data.badgeColor ? { borderColor: data.badgeColor, borderWidth: 2 } : undefined}
    >
      <Handle type="target" position={Position.Left} className="bg-muted-foreground!" />
      <div className="flex items-center gap-1 font-semibold">
        {data.kind === "instance" && <ChevronRight className="size-3 shrink-0" />}
        <span className="truncate">{data.label}</span>
        {data.warn && <TriangleAlert className="size-3 shrink-0 text-amber-500" />}
        {data.badge && (
          <span
            className="ml-auto shrink-0 truncate rounded px-1 text-[9px] font-medium leading-4 text-white"
            style={{ backgroundColor: data.badgeColor, maxWidth: 90 }}
          >
            {data.badge}
          </span>
        )}
      </div>
      {data.sub && <div className="truncate opacity-70">{data.sub}</div>}
      <Handle type="source" position={Position.Right} className="bg-muted-foreground!" />
    </div>
  );
}

// FA75: Gruppenbox eines Schlüssels einer aufgeklappten Instanz (Rahmen in der
// Modulfarbe); "Einklappen" klappt die ganze Instanz wieder ein.
function InstanceGroupNode({ data }: NodeProps<GroupData>) {
  return (
    <div
      className="h-full w-full rounded-lg"
      style={{ border: `1px solid ${data.color}`, backgroundColor: `${data.color}0d` }}
    >
      <div className="flex items-center gap-1 px-2 text-[10px] font-medium" style={{ color: data.color, height: GROUP_HEADER }}>
        <span className="truncate font-mono">{data.label}</span>
        {data.onCollapse && (
          <button
            type="button"
            className="nodrag ml-auto rounded px-1 hover:bg-muted"
            onClick={(event) => {
              event.stopPropagation();
              data.onCollapse?.();
            }}
          >
            Einklappen
          </button>
        )}
      </div>
    </div>
  );
}

const nodeTypes = { status: StatusNode, instanceGroup: InstanceGroupNode };

/** Knotenmaße fürs dagre-Layout, passend zur CSS-Fixgröße (.rf-node,
 * globals.css: max-width 210px, height 52px). dagre braucht feste
 * Maße pro Knoten, um Reihen-/Spaltenabstände ohne Überlappung zu
 * berechnen - Inhalt wird per CSS ohnehin abgeschnitten statt zu wachsen. */
const NODE_WIDTH = 210;
const NODE_HEIGHT = 52;
const FIT_PADDING = 0.15; // Rand beim Einpassen, Platz für Legende/CRS-Hinweis
const RANKSEP = 110; // Abstand zwischen Spalten (zusätzlich zur Knotenbreite)
const NODESEP = 30; // Abstand zwischen Zeilen (zusätzlich zur Knotenhöhe)

type Rect = { x: number; y: number; width: number; height: number };
type Layout = { positions: Record<string, { x: number; y: number }>; groups: Record<string, Rect> };

/**
 * Kreuzungsminimiertes Schichten-Layout (Sugiyama-Verfahren) via dagre.
 * dagre ordnet Knoten je Rang so um, dass Kantenkreuzungen minimiert werden
 * (Barycenter-/Median-Heuristik). multigraph: true, weil dieselben zwei
 * Knoten über mehrere benannte Ports verbunden sein können. FA75: compound
 * true, wenn aufgeklappte Instanzen Gruppenboxen bekommen (``parents``: Knoten
 * -> Gruppe); dagre liefert dann auch die Rechtecke der Gruppen. Scheitert das
 * compound-Layout, fällt es auf das flache Layout ohne Boxen zurück (die
 * Knoten behalten Modulfarbe und Abzeichen).
 */
function layoutWithDagre(
  nodeIds: string[],
  edges: { source: string; target: string; port: string }[],
  parents: Record<string, string>,
): Layout {
  const compound = Object.keys(parents).length > 0;
  const g = new dagre.graphlib.Graph({ multigraph: true, compound });
  g.setGraph({ rankdir: "LR", ranksep: RANKSEP, nodesep: NODESEP });
  g.setDefaultEdgeLabel(() => ({}));

  for (const id of nodeIds) g.setNode(id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  if (compound) {
    for (const group of new Set(Object.values(parents))) g.setNode(group, {});
    for (const [child, group] of Object.entries(parents)) g.setParent(child, group);
  }
  for (const e of edges) {
    // Nur Kanten zwischen bekannten Knoten setzen - dagre legt sonst
    // stillschweigend einen neuen (unsichtbaren) Knoten an.
    if (g.hasNode(e.source) && g.hasNode(e.target)) {
      g.setEdge(e.source, e.target, {}, e.port);
    }
  }

  try {
    dagre.layout(g);
  } catch (error) {
    if (!compound) throw error;
    console.warn("dagre compound layout failed, falling back to the flat layout", error);
    return layoutWithDagre(nodeIds, edges, {});
  }

  // dagre liefert Mittelpunktskoordinaten, ReactFlow erwartet die
  // linke obere Ecke - daher um die halbe Knotengröße verschieben.
  const positions: Record<string, { x: number; y: number }> = {};
  for (const id of nodeIds) {
    const n = g.node(id);
    if (!n) continue;
    positions[id] = { x: n.x - NODE_WIDTH / 2, y: n.y - NODE_HEIGHT / 2 };
  }
  const groups: Record<string, Rect> = {};
  for (const group of new Set(Object.values(parents))) {
    const n = g.node(group);
    if (!n || n.width == null || n.height == null) continue;
    // dagre lässt oben ~25px Rand im Cluster - dort steht die Kopfzeile (GROUP_HEADER)
    groups[group] = { x: n.x - n.width / 2, y: n.y - n.height / 2, width: n.width, height: n.height };
  }
  return { positions, groups };
}

/** Struktursignatur: sortierte Knoten- und Kanten-IDs plus die aufgeklappten
 * Instanzen. Ändert sie sich nicht, bleibt das Layout stabil (Status-Updates
 * wie Farbe/Dauer/Auswahl dürfen Knoten nicht verschieben) - ändert sie
 * sich (neuer/entfernter Knoten, neue/entfernte Kante, Instanz auf- oder
 * zugeklappt), wird komplett neu layoutet, da ein kreuzungsminimiertes Layout
 * nur als Ganzes funktioniert. Manuelle Drag-Positionen gehen dabei bewusst
 * verloren - ein Kompromiss, den ein Struktur-Rebuild rechtfertigt. */
function structureSignature(nodeIds: string[], edgeIds: string[], expanded: Set<string>): string {
  return `${[...nodeIds].sort().join(",")}|${[...edgeIds].sort().join(",")}|${[...expanded].sort().join(",")}`;
}

/** Status einer eingeklappten Instanz aus den Status ihrer Mitglieder. */
function aggregateStatus(statuses: string[]): string {
  if (statuses.includes("error")) return "error";
  if (statuses.some((s) => s === "running" || s === "loading")) return "running";
  if (statuses.length > 0 && statuses.every((s) => s === "done")) return "done";
  return "pending";
}

export default function RunGraph(props: {
  status: RunStatus;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <ReactFlowProvider>
      <RunGraphInner {...props} />
    </ReactFlowProvider>
  );
}

function RunGraphInner({
  status,
  selected,
  onSelect,
}: {
  status: RunStatus;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<NodeData | GroupData>([]);
  const [edges, setEdges] = useEdgesState([]);
  // FA75: aufgeklappte Instanzen (Standard: alle mit mehreren Schlüsseln zu).
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  // Positionen (absolut, linke obere Ecke) bleiben über Re-Layouts hinweg
  // erhalten (auch nach manuellem Verschieben per Drag), solange sich die
  // Graph-Struktur nicht ändert - siehe structureSignature/layoutWithDagre.
  const positionsRef = useRef<Record<string, { x: number; y: number }>>({});
  const groupsRef = useRef<Record<string, Rect>>({});
  const structureRef = useRef<string | null>(null);
  const { setCenter, fitView } = useReactFlow();
  const didInitialFit = useRef(false);
  // Auf-/Zuklappen per Klick legt den Graphen neu an; danach den Ausschnitt
  // anpassen, sobald die neuen Knoten vermessen sind - sonst lagen die
  // Gruppenboxen außerhalb der Ansicht (Browsercheck 03.10., B7).
  const fitAfterLayout = useRef(false);
  const nodesInitialized = useNodesInitialized();
  const toggleInstance = (instance: string, open: boolean) => {
    fitAfterLayout.current = true;
    setExpanded((current) => {
      const next = new Set(current);
      if (open) next.add(instance);
      else next.delete(instance);
      return next;
    });
  };

  const groups = useMemo(
    () => instanceGroups(status.layers, status.order, status.origins),
    [status.layers, status.order, status.origins],
  );

  // FA75: Auswahl eines Mitglieds (Inspektor, "Endergebnis anzeigen",
  // Fehlersprung) klappt seine Instanz auf - einmal je Auswahlwechsel, damit
  // "Einklappen" danach wirkt (Zustand beim Rendern angepasst, kein Effekt).
  const [seenSelection, setSeenSelection] = useState<string | null>(null);
  if (selected !== seenSelection) {
    setSeenSelection(selected);
    const instance = selected ? status.origins?.[selected]?.instance : undefined;
    const group = instance ? groups.get(instance) : undefined;
    if (group && collapsible(group) && !expanded.has(group.id)) {
      setExpanded(new Set(expanded).add(group.id));
    }
  }

  useEffect(() => {
    const colors = moduleColors(status.origins);
    const collapsedOf = (id: string): InstanceGroup | undefined => {
      const instance = status.origins?.[id]?.instance;
      const group = instance ? groups.get(instance) : undefined;
      return group && collapsible(group) && !expanded.has(group.id) ? group : undefined;
    };
    // sichtbarer Knoten eines Knotens: er selbst oder seine eingeklappte Instanz
    const visible = (id: string) => {
      const group = collapsedOf(id);
      return group ? `${INSTANCE_PREFIX}${group.id}` : id;
    };

    const leafIds: string[] = [];
    for (const id of [...status.layers, ...status.order]) {
      const shown = visible(id);
      if (!leafIds.includes(shown)) leafIds.push(shown);
    }
    const parents: Record<string, string> = {};
    for (const id of leafIds) {
      const origin = status.origins?.[id];
      if (origin?.module && origin.instance) parents[id] = groupIdOf(origin);
    }

    // Kanten: innerhalb einer eingeklappten Instanz entfallen sie; Kanten zu
    // oder von ihr werden je (Quelle, Ziel) zusammengefasst (collect hat dann
    // EINE eingehende Kante statt einer je Element).
    const layoutEdges: { source: string; target: string; port: string }[] = [];
    const merged = new Map<string, { source: string; target: string; ports: string[] }>();
    for (const sid of status.order) {
      const step = status.steps[sid];
      for (const [port, src] of Object.entries(step?.inputs || {})) {
        const source = visible(src);
        const target = visible(sid);
        if (source === target) continue;
        if (source.startsWith(INSTANCE_PREFIX) || target.startsWith(INSTANCE_PREFIX)) {
          const key = `${source}->${target}`;
          const entry = merged.get(key) ?? { source, target, ports: [] };
          entry.ports.push(port);
          merged.set(key, entry);
        } else {
          layoutEdges.push({ source, target, port });
        }
      }
    }
    for (const { source, target } of merged.values()) layoutEdges.push({ source, target, port: "*" });
    const edgeIds = layoutEdges.map((e) => `${e.source}->${e.target}:${e.port}`);

    const signature = structureSignature(leafIds, edgeIds, expanded);
    const structureChanged = structureRef.current !== signature;
    if (structureChanged) {
      structureRef.current = signature;
      const layout = layoutWithDagre(leafIds, layoutEdges, parents);
      positionsRef.current = layout.positions;
      groupsRef.current = layout.groups;
    }
    const positions = positionsRef.current;
    const groupRects = groupsRef.current;

    // Ein Layer gilt als "wird gerade geladen", wenn ein Schritt, der ihn
    // direkt braucht, gerade lädt und der Layer noch keine Ladezeit hat.
    const loadingLayers = new Set<string>();
    for (const sid of status.order) {
      const step = status.steps[sid];
      if (step?.status !== "loading") continue;
      for (const src of Object.values(step.inputs || {})) {
        if (status.layers.includes(src) && !(src in status.layer_load_ms)) loadingLayers.add(src);
      }
    }
    const statusOf = (id: string) =>
      status.steps[id] ? (status.steps[id]?.status ?? "pending") : loadingLayers.has(id) ? "loading" : "done";

    // FA75: Abzeichen = Knoten im Modul, Rahmen in der Modulfarbe, Position im Tooltip.
    const originOf = (id: string) => {
      const origin = status.origins?.[id];
      if (!origin?.module) return {};
      return { badge: origin.inner_id ?? undefined, badgeColor: colors[origin.module], hint: origin.location };
    };
    // Kinder einer Gruppenbox stehen relativ zu ihr (ReactFlow parentId).
    const placed = (id: string) => {
      const absolute = positions[id] || { x: 0, y: 0 };
      const parent = parents[id];
      const rect = parent ? groupRects[parent] : undefined;
      if (!rect) return { position: absolute };
      return {
        position: { x: absolute.x - rect.x, y: absolute.y - rect.y },
        parentId: parent,
        extent: "parent" as const,
      };
    };

    const groupNodes: Node<GroupData>[] = [];
    for (const [groupId, rect] of Object.entries(groupRects)) {
      const child = Object.keys(parents).find((id) => parents[id] === groupId);
      const origin = child ? status.origins?.[child] : undefined;
      const group = origin?.instance ? groups.get(origin.instance) : undefined;
      if (!origin?.module || !group) continue;
      groupNodes.push({
        id: groupId,
        type: "instanceGroup",
        position: { x: rect.x, y: rect.y },
        style: { width: rect.width, height: rect.height },
        selectable: false,
        draggable: false,
        data: {
          label: groupId.slice(GROUP_PREFIX.length),
          color: colors[origin.module],
          onCollapse: collapsible(group) ? () => toggleInstance(group.id, false) : undefined,
        },
      });
    }

    const nextNodes: Node<NodeData>[] = [];
    for (const id of leafIds) {
      if (id.startsWith(INSTANCE_PREFIX)) {
        const group = groups.get(id.slice(INSTANCE_PREFIX.length))!;
        const keysDone = group.keys.filter((key) =>
          group.members
            .filter((m) => status.origins?.[m]?.key === key && status.steps[m])
            .every((m) => statusOf(m) === "done"),
        ).length;
        nextNodes.push({
          id,
          type: "status",
          position: positions[id] || { x: 0, y: 0 },
          data: {
            label: `${group.id} ×${group.keys.length}`,
            // Zähler vorn: abgeschnitten wird höchstens der Modulname.
            sub: `${group.stepCount} Schritte · ${group.layerCount} Layer · ${group.module}`,
            status: aggregateStatus(group.members.map(statusOf)),
            selected: false,
            kind: "instance",
            badge: `${keysDone}/${group.keys.length}`,
            badgeColor: colors[group.module],
            hint: "Klicken zum Aufklappen",
          },
        });
      } else if (status.steps[id]) {
        const step = status.steps[id];
        const dur = step?.duration_ms != null ? ` · ${Math.round(step.duration_ms)}ms` : "";
        nextNodes.push({
          id,
          type: "status",
          ...placed(id),
          data: {
            label: id,
            sub: `${step?.op ?? ""}${dur}`,
            status: step?.status ?? "pending",
            selected: selected === id,
            kind: "step",
            warn: (step?.warnings?.length ?? 0) > 0,
            error: step?.error ?? undefined,
            ...originOf(id),
          },
        });
      } else {
        const loadMs = status.layer_load_ms[id];
        const prov = status.layer_provenance?.[id];
        const ms = loadMs != null ? ` · ${Math.round(loadMs)}ms` : "";
        const sub = prov ? `${crsSubtitle(prov)}${ms}` : loadMs != null ? `Quelle · Laden ${Math.round(loadMs)}ms` : "Quelle";
        const origin = originOf(id);
        nextNodes.push({
          id,
          type: "status",
          ...placed(id),
          data: {
            label: id,
            sub,
            status: statusOf(id),
            selected: selected === id,
            kind: "layer",
            ...origin,
            hint: [prov ? `${prov.source_crs_label} → ${prov.target_crs_label}` : undefined, origin.hint]
              .filter(Boolean)
              .join("\n") || undefined,
          },
        });
      }
    }

    // smoothstep statt Standard-Bezier: mit dagres großzügigem
    // ranksep/nodesep-Abstand verläuft die Kante dann in klaren
    // rechtwinkligen Segmenten um Knoten herum statt sie zu diagonal zu
    // schneiden - reduziert sichtbare Kanten-Knoten-Überlappungen weiter.
    const nodeStatus = new Map(nextNodes.map((n) => [n.id, n.data.status]));
    const nextEdges: Edge[] = layoutEdges.map(({ source, target, port }) => {
      const ports = merged.get(`${source}->${target}`)?.ports;
      const targetStatus = nodeStatus.get(target);
      return {
        id: `${source}->${target}:${port}`,
        source,
        target,
        label: ports ? (ports.length > 1 ? `${ports.length} Eingänge` : ports[0]) : port,
        type: "smoothstep",
        animated: targetStatus === "running" || targetStatus === "loading",
        style: { stroke: "var(--color-border)" },
        labelStyle: { fill: "var(--color-muted-foreground)", fontSize: 9 },
        zIndex: 1,
      };
    });

    setNodes((current) => {
      // Bei unveränderter Struktur: Position eines bereits gerenderten
      // (ggf. per Drag verschobenen) Knotens beibehalten, statt sie aus dem
      // frischen Layout zu überschreiben - nur data (Status/Laufzeit/
      // Auswahl) aktualisieren. Gruppen stehen vor ihren Kindern (ReactFlow).
      const currentById = new Map(current.map((n) => [n.id, n]));
      return [...groupNodes, ...nextNodes].map((n) => {
        const existing = currentById.get(n.id);
        if (existing && !structureChanged) {
          return { ...n, position: existing.position };
        }
        return n;
      });
    });
    setEdges(nextEdges);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status, selected, expanded, groups]);

  // Ausgewählten Knoten ins Zentrum rücken, sobald seine Position bekannt
  // ist - nicht beim allerersten Layout (da übernimmt fitView), nur bei
  // späteren Auswahlwechseln (Klick im Graph oder von außen gesetzt).
  useEffect(() => {
    if (!selected) return;
    if (!didInitialFit.current) return;
    const pos = positionsRef.current[selected];
    if (!pos) return;
    setCenter(pos.x + NODE_WIDTH / 2, pos.y + NODE_HEIGHT / 2, { zoom: 1, duration: 400 });
  }, [selected, setCenter, expanded]);

  useEffect(() => {
    if (!fitAfterLayout.current || !nodesInitialized) return;
    const frame = requestAnimationFrame(() => {
      fitAfterLayout.current = false;
      fitView({ padding: FIT_PADDING, duration: 300 });
    });
    return () => cancelAnimationFrame(frame);
  }, [nodes, nodesInitialized, fitView]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      nodeTypes={nodeTypes}
      onNodeClick={(_, node) => {
        if (node.id.startsWith(INSTANCE_PREFIX)) {
          toggleInstance(node.id.slice(INSTANCE_PREFIX.length), true);
          return;
        }
        if (node.id.startsWith(GROUP_PREFIX)) return;
        onSelect(node.id);
      }}
      onNodeDragStop={(_, node) => {
        positionsRef.current[node.id] = node.positionAbsolute ?? node.position;
      }}
      onInit={() => {
        didInitialFit.current = true;
      }}
      fitView
      fitViewOptions={{ padding: FIT_PADDING }}
      proOptions={{ hideAttribution: true }}
      minZoom={0.05}
    >
      <Background className="bg-background!" color="var(--color-muted-foreground)" gap={20} />
      <Controls showInteractive={false} className="[&_button]:border-border! [&_button]:bg-card! [&_button]:fill-foreground! [&_button]:hover:bg-muted!" />
      <MiniMap
        pannable
        zoomable
        nodeColor={(n) =>
          n.type === "instanceGroup" ? "transparent" : (STATUS_MINIMAP_COLOR[(n.data as NodeData)?.status] ?? "#94a3b8")
        }
        maskColor="var(--color-background)"
        className="bg-card!"
      />
      <Legend />
      {status.region && <WorkingCrsBadge region={status.region} />}
    </ReactFlow>
  );
}
