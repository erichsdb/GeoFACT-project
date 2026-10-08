"use client";

import { useMemo, useState } from "react";
import { extractRows, histogram, labelField, numericFields, topN } from "@/lib/featureData";
import { formatCell, formatDecimal } from "@/lib/format";
import type { NodeResult } from "@/lib/types";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export default function ChartView({ result }: { result: NodeResult | null }) {
  const rows = useMemo(() => extractRows(result), [result]);
  const fields = useMemo(() => numericFields(rows), [rows]);
  const label = useMemo(() => labelField(rows), [rows]);
  const [requestedField, setRequestedField] = useState<string>("");
  const field = requestedField && fields.includes(requestedField) ? requestedField : fields[0] ?? "";

  if (!fields.length) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
        Keine numerischen Attribute zum Plotten.
      </div>
    );
  }

  const bins = histogram(rows, field, 14);
  const ranking = topN(rows, field, label, 12);
  const maxBin = Math.max(...bins.map((b) => b.count), 1);
  const maxRank = Math.max(...ranking.map((d) => d.value), 1);
  const minRank = Math.min(...ranking.map((d) => d.value), 0);
  const rankSpan = maxRank - minRank || 1;

  return (
    <div className="flex h-full flex-col gap-5 overflow-auto p-4">
      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">Feld</span>
        <Select value={field} onValueChange={(v) => setRequestedField(v ?? "")}>
          <SelectTrigger className="w-auto">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {fields.map((f) => (
              <SelectItem key={f} value={f}>
                {f}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Histogramm */}
      <div>
        <h4 className="mb-2 text-sm font-semibold text-foreground">Verteilung</h4>
        <svg viewBox={`0 0 320 120`} className="w-full" preserveAspectRatio="none">
          {bins.map((b, i) => {
            const w = 320 / bins.length;
            const h = (b.count / maxBin) * 100;
            return (
              <g key={i}>
                <rect
                  x={i * w + 1}
                  y={110 - h}
                  width={w - 2}
                  height={h}
                  className="fill-primary"
                  opacity={0.85}
                >
                  <title>{`[${formatDecimal(b.x0, 2)}; ${formatDecimal(b.x1, 2)}): ${b.count}`}</title>
                </rect>
              </g>
            );
          })}
          <line x1={0} y1={110} x2={320} y2={110} className="stroke-border" />
        </svg>
        <div className="flex justify-between text-xs text-muted-foreground">
          <span>{bins[0] ? formatDecimal(bins[0].x0, 2) : ""}</span>
          <span>{bins.length > 0 ? formatDecimal(bins[bins.length - 1].x1, 2) : ""}</span>
        </div>
      </div>

      {/* Top-N Rangliste */}
      <div>
        <h4 className="mb-2 text-sm font-semibold text-foreground">Top {ranking.length}</h4>
        <div className="space-y-1.5">
          {ranking.map((d, i) => (
            <div key={i} className="flex items-center gap-3 text-xs">
              <span className="w-28 truncate text-muted-foreground" title={d.label}>
                {d.label}
              </span>
              <div className="h-3 flex-1 rounded bg-muted">
                <div
                  className="h-3 rounded bg-emerald-500"
                  style={{ width: `${((d.value - minRank) / rankSpan) * 100}%` }}
                />
              </div>
              <span className="w-16 text-right tabular-nums text-foreground/80">
                {formatCell(d.value, 3)}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
