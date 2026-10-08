"use client";

import { useMemo } from "react";
import { computeStats, extractRows, numericFields } from "@/lib/featureData";
import { formatCell } from "@/lib/format";
import type { NodeResult } from "@/lib/types";

function fmt(v: number): string {
  return formatCell(v, 4);
}

export default function StatsView({ result }: { result: NodeResult | null }) {
  const rows = useMemo(() => extractRows(result), [result]);
  const fields = useMemo(() => numericFields(rows), [rows]);

  if (result?.describe.kind === "raster") {
    const s = result.describe.stats;
    if (!s) return <Empty>Keine Rasterstatistik verfügbar.</Empty>;
    return (
      <div className="p-5 text-sm">
        <h4 className="mb-3 font-semibold text-foreground">Rasterwerte</h4>
        <dl className="grid grid-cols-2 gap-x-8 gap-y-2">
          <Row k="Minimum" v={fmt(s.min)} />
          <Row k="Maximum" v={fmt(s.max)} />
          <Row k="Mittel" v={fmt(s.mean)} />
          <Row k="2. Perzentil" v={fmt(s.p2)} />
          <Row k="98. Perzentil" v={fmt(s.p98)} />
          <Row k="Gültige Pixel" v={String(s.valid_count)} />
        </dl>
      </div>
    );
  }

  if (!fields.length) return <Empty>Keine numerischen Attribute für Statistik.</Empty>;

  return (
    <div className="h-full overflow-auto p-4">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="text-muted-foreground">
            {["Feld", "n", "min", "max", "Mittel", "Median", "σ"].map((h) => (
              <th key={h} className="border-b px-3 py-2 text-left">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {fields.map((f) => {
            const s = computeStats(rows, f);
            if (!s) return null;
            return (
              <tr key={f} className="odd:bg-muted/30">
                <td className="border-b px-3 py-1.5 font-medium text-foreground">{f}</td>
                <td className="border-b px-3 py-1.5 tabular-nums text-foreground/80">{s.count}</td>
                <td className="border-b px-3 py-1.5 tabular-nums text-foreground/80">{fmt(s.min)}</td>
                <td className="border-b px-3 py-1.5 tabular-nums text-foreground/80">{fmt(s.max)}</td>
                <td className="border-b px-3 py-1.5 tabular-nums text-foreground/80">{fmt(s.mean)}</td>
                <td className="border-b px-3 py-1.5 tabular-nums text-foreground/80">{fmt(s.median)}</td>
                <td className="border-b px-3 py-1.5 tabular-nums text-foreground/80">{fmt(s.stddev)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{k}</dt>
      <dd className="text-right tabular-nums text-foreground">{v}</dd>
    </>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full items-center justify-center text-sm text-muted-foreground">{children}</div>;
}
