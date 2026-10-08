"use client";

import CodeMirror from "@uiw/react-codemirror";
import { yaml } from "@codemirror/lang-yaml";
import { EditorView } from "@codemirror/view";
import { useTheme } from "next-themes";
import type { ReactNode } from "react";
import { CircleCheck, CircleX, Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useMediaQuery } from "@/lib/useMediaQuery";
import type { ValidateResponse } from "@/lib/types";

const editorTheme = EditorView.theme({
  "&": { fontSize: "13px", backgroundColor: "transparent" },
  ".cm-gutters": { backgroundColor: "transparent", border: "none" },
  ".cm-content": { fontFamily: "var(--font-app-mono), monospace" },
});

export default function ConfigEditor({
  value,
  onChange,
  onRun,
  running,
  validation,
  validating,
  runLabel,
  minHeight = "20rem",
  maxHeight = "70vh",
  toolbar,
  footer,
}: {
  value: string;
  onChange: (v: string) => void;
  onRun: () => void;
  running: boolean;
  validation: ValidateResponse | null;
  validating: boolean;
  runLabel?: string;
  minHeight?: string;
  // Obergrenze, darüber scrollt der Editor selbst: ein 241-Zeilen-Beispiel
  // war sonst 4400 px hoch und schob alles darunter aus dem Blick (R12).
  maxHeight?: string;
  toolbar?: ReactNode;
  // Zusatz unter der Reihenfolge, nur bei gültiger Konfiguration (FA73: Lizenzen).
  footer?: ReactNode;
}) {
  const valid = validation?.valid ?? false;
  const { resolvedTheme } = useTheme();
  // Unter Tailwinds sm-Breakpoint (640px) umbrechen statt horizontal
  // scrollen zu lassen - lange YAML-Kommentare liefen sonst auf schmalen
  // Screens sichtbar aus dem Editor heraus, ohne erkennbaren Scroll-
  // Hinweis. Desktop-Verhalten (horizontal scrollbar) bleibt unverändert.
  const wrapLines = useMediaQuery("(max-width: 639px)");

  return (
    <div className="flex flex-col">
      <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
        <h3 className="text-base font-semibold">Szenario-Konfiguration (YAML)</h3>
        {toolbar && <div className="flex items-center gap-1.5">{toolbar}</div>}
        <div className="ml-auto flex items-center gap-3">
          {validating && <span className="text-sm text-muted-foreground">prüfe…</span>}
          {validation && (
            <span
              className={`flex items-center gap-1 text-sm font-medium ${
                valid ? "text-emerald-600 dark:text-emerald-400" : "text-destructive"
              }`}
            >
              {valid ? <CircleCheck className="size-4" /> : <CircleX className="size-4" />}
              {valid ? "gültig" : `${validation.issues.length} Fehler`}
            </span>
          )}
          <Button onClick={onRun} disabled={!valid || running}>
            <Play className="size-4" />
            {running ? "läuft…" : runLabel ?? "Ausführen"}
          </Button>
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border bg-muted/30">
        <CodeMirror
          value={value}
          height="auto"
          minHeight={minHeight}
          maxHeight={maxHeight}
          theme={resolvedTheme === "dark" ? "dark" : "light"}
          extensions={wrapLines ? [yaml(), editorTheme, EditorView.lineWrapping] : [yaml(), editorTheme]}
          onChange={onChange}
        />
      </div>

      {validation && !valid && (
        <div className="mt-3 max-h-32 overflow-y-auto rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm">
          {validation.issues.map((issue, i) => (
            <div key={i} className="text-destructive">
              <span className="font-mono">{issue.location}</span>: {issue.message}
            </div>
          ))}
        </div>
      )}
      {validation?.valid && validation.execution_order && (
        <p className="mt-2 truncate text-xs text-muted-foreground">
          Reihenfolge: {validation.execution_order.join(" → ")}
          {validation.unused_layers &&
            validation.unused_layers.length > 0 &&
            ` · ungenutzt: ${validation.unused_layers.join(", ")}`}
        </p>
      )}
      {validation?.valid && footer}
    </div>
  );
}
