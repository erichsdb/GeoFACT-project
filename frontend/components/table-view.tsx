"use client";

import { useMemo, useState } from "react";
import { ArrowUp, ArrowDown } from "lucide-react";
import { columnsOf, extractRows, type Row } from "@/lib/featureData";
import { formatCell, formatCount } from "@/lib/format";
import type { NodeResult } from "@/lib/types";

const MAX_ROWS = 500;

export default function TableView({ result }: { result: NodeResult | null }) {
  const rows = useMemo(() => extractRows(result), [result]);
  const cols = useMemo(() => columnsOf(rows), [rows]);
  const [sortCol, setSortCol] = useState<string | null>(null);
  const [asc, setAsc] = useState(false);

  const sorted = useMemo(() => {
    if (!sortCol) return rows;
    const copy = [...rows];
    copy.sort((a, b) => {
      const va = a[sortCol];
      const vb = b[sortCol];
      if (va == null) return 1;
      if (vb == null) return -1;
      if (typeof va === "number" && typeof vb === "number") return asc ? va - vb : vb - va;
      return asc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
    });
    return copy;
  }, [rows, sortCol, asc]);

  if (!rows.length) {
    return <Empty>Keine tabellarischen Attribute für diesen Knoten.</Empty>;
  }

  // Das Backend begrenzt Vektor-/Graph-Objekte serverseitig (siehe
  // MapView.tsx); die echte Gesamtzahl kommt aus describe() (unbeschränkt
  // berechnet), damit "X von Y" auch bei serverseitiger Kappung stimmt.
  const trueTotal =
    result?.describe.kind === "vector"
      ? result.describe.feature_count
      : result?.describe.kind === "graph"
        ? result.describe.node_count
        : rows.length;
  const serverTruncated = trueTotal > rows.length;
  const droppedColumns = result?.dropped_columns ?? 0;

  const clickSort = (col: string) => {
    if (sortCol === col) setAsc(!asc);
    else {
      setSortCol(col);
      setAsc(false);
    }
  };

  const cell = (v: unknown) => (typeof v === "number" ? formatCell(v, 3) : String(v ?? ""));

  return (
    <div className="h-full overflow-auto">
      <table className="w-full border-collapse text-sm">
        <thead className="sticky top-0 bg-background">
          <tr>
            {cols.map((c) => {
              const sortDirection: "ascending" | "descending" | "none" =
                sortCol === c ? (asc ? "ascending" : "descending") : "none";
              return (
                <th key={c} aria-sort={sortDirection} className="border-b px-0 py-0 text-left font-semibold">
                  <button
                    type="button"
                    onClick={() => clickSort(c)}
                    className="w-full cursor-pointer select-none px-3 py-2 text-left text-muted-foreground hover:text-foreground"
                  >
                    <span className="inline-flex items-center gap-1">
                      {c}
                      {sortCol === c && (asc ? <ArrowUp className="size-3.5" /> : <ArrowDown className="size-3.5" />)}
                    </span>
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.slice(0, MAX_ROWS).map((row: Row, i) => (
            <tr key={i} className="odd:bg-muted/30 hover:bg-muted/60">
              {cols.map((c) => (
                <td key={c} className="border-b px-3 py-1.5 text-foreground/80">
                  {cell(row[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {(sorted.length > MAX_ROWS || serverTruncated || droppedColumns > 0) && (
        <p className="p-3 text-xs text-muted-foreground">
          {serverTruncated
            ? `${formatCount(trueTotal)} Objekte insgesamt · erste ${formatCount(rows.length)} vom Server geladen${
                sorted.length > MAX_ROWS ? `, erste ${MAX_ROWS} angezeigt` : ""
              } · vollständig im ZIP-Download.`
            : sorted.length > MAX_ROWS
              ? `${sorted.length} Zeilen · erste ${MAX_ROWS} angezeigt.`
              : `${sorted.length} Zeilen.`}
          {droppedColumns > 0 &&
            ` ${cols.length} von ${formatCount(cols.length + droppedColumns)} Attributen angezeigt (dünn besetzte ausgeblendet) · alle im ZIP-Download.`}
        </p>
      )}
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full items-center justify-center text-sm text-muted-foreground">{children}</div>;
}
