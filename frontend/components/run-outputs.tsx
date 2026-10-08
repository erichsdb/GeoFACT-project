"use client";

// Implements: FA88 (Dateien eines Laufs einzeln abrufen), FA89 (Liste der Läufe).
//
// RunFiles zeigt, was ein Lauf geschrieben hat: jede Ausgabe mit Format, Quelle
// und Größe als eigener Link, die übersprungenen Ausgaben mit Grund, darunter
// die Begleitdateien und „Alle als ZIP“. RunFilesButton ist der Knopf „Ausgaben“
// der Ergebnisansicht: er öffnet die Liste als Dialog, damit sie der Karte
// keinen Platz nimmt. RunHistoryDialog listet die Läufe, die das Backend noch
// hält, und nutzt dieselbe Dateiliste je Lauf.
//
// Jeder Abruf läuft über `onDownload` (app-shell): dort wird vor dem Verlassen
// der Seite gefragt, ob das Backend den Lauf noch kennt (FA54).

import { useCallback, useEffect, useState } from "react";
import { ChevronDown, Download, FileArchive, FileDown, History, Loader2, TriangleAlert } from "lucide-react";
import { api, isNotFoundError, RUN_GONE_MESSAGE } from "@/lib/api";
import {
  canDownloadZip,
  fileCaption,
  formatBytes,
  formatRunTime,
  historyNote,
  runCounts,
  runStatusLabel,
  splitFiles,
} from "@/lib/runFiles";
import { cn } from "@/lib/utils";
import type { RunFile, RunHistory, RunOutputs, RunSummary } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

/** Abruf einer Datei des Laufs `runId` unter `url`; fragt vorher das Backend (FA54). */
export type RunDownloadHandler = (e: React.MouseEvent<HTMLElement>, runId: string, url: string) => void;

type FilesState = { key: string; data: RunOutputs | null; error: string | null; gone: boolean };

type LoadedFiles = { loading: boolean; data: RunOutputs | null; error: string | null };

// Lädt die Dateiliste des Laufs; ändert sich `refreshKey` (z. B. der Status des
// Laufs), wird neu geladen. `onGone`: das Backend kennt den Lauf nicht mehr (404).
function useRunOutputs(runId: string, refreshKey: string, onGone?: (runId: string) => void): LoadedFiles {
  const key = `${runId}|${refreshKey}`;
  const [state, setState] = useState<FilesState>({ key: "", data: null, error: null, gone: false });

  useEffect(() => {
    let cancelled = false;
    api
      .runOutputs(runId)
      .then((data) => {
        if (!cancelled) setState({ key, data, error: null, gone: false });
      })
      .catch((e) => {
        if (cancelled) return;
        const gone = isNotFoundError(e);
        setState({ key, data: null, error: gone ? RUN_GONE_MESSAGE : e instanceof Error ? e.message : String(e), gone });
        if (gone) onGone?.(runId);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const loading = state.key !== key;
  return { loading, data: loading ? null : state.data, error: loading ? null : state.error };
}

export function RunFiles({
  runId,
  onDownload,
  onGone,
  refreshKey = "",
  bare = false,
}: {
  runId: string;
  onDownload: RunDownloadHandler;
  onGone?: (runId: string) => void;
  refreshKey?: string;
  // Ohne Rahmen und Titel (im Dialog und in der Liste der Läufe).
  bare?: boolean;
}) {
  const files = useRunOutputs(runId, refreshKey, onGone);
  return <RunFilesView runId={runId} files={files} onDownload={onDownload} bare={bare} />;
}

function RunFilesView({
  runId,
  files,
  onDownload,
  bare,
}: {
  runId: string;
  files: LoadedFiles;
  onDownload: RunDownloadHandler;
  bare: boolean;
}) {
  const { loading, data } = files;
  const { outputs, companions } = splitFiles(data);
  const skipped = data?.skipped ?? [];
  const total = outputs.reduce((sum, f) => sum + f.size_bytes, 0);
  const finished = data ? canDownloadZip(data.status) : false;

  return (
    <section
      data-slot="run-files"
      aria-label="Ausgaben des Laufs"
      className={cn("flex flex-col gap-2", !bare && "rounded-xl border bg-card px-4 py-3")}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {!bare && <h3 className="text-sm font-semibold">Ausgaben</h3>}
        <span className="text-xs text-muted-foreground">
          {loading
            ? "Dateien werden geladen …"
            : data
              ? outputs.length > 0
                ? `${outputs.length === 1 ? "1 Datei" : `${outputs.length} Dateien`} · ${formatBytes(total)}`
                : finished
                  ? "Dieser Lauf hat keine Ausgabe geschrieben."
                  : "Der Lauf ist nicht abgeschlossen – es gibt keine Ausgaben."
              : ""}
        </span>
        {finished && (
          <Button
            size="sm"
            variant="outline"
            className="ml-auto"
            nativeButton={false}
            render={<a href={api.downloadUrl(runId)} />}
            onClick={(e: React.MouseEvent<HTMLElement>) => onDownload(e, runId, api.downloadUrl(runId))}
            title="Alle Dateien dieses Laufs in einem ZIP"
          >
            <FileArchive className="size-4" />
            Alle als ZIP
          </Button>
        )}
      </div>

      {files.error && (
        <p className="flex items-start gap-2 text-sm text-destructive">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" />
          {files.error}
        </p>
      )}

      {outputs.length > 0 && (
        <ul className={cn("grid gap-1.5 sm:grid-cols-2", !bare && "xl:grid-cols-3")}>
          {outputs.map((file) => (
            <li key={file.name}>
              <FileLink runId={runId} file={file} onDownload={onDownload} />
            </li>
          ))}
        </ul>
      )}

      {/* Übersprungene Ausgaben stehen hier und nicht nur in SKIPPED_OUTPUTS.txt (Goldene Regel 7). */}
      {skipped.length > 0 && (
        <ul data-slot="skipped-outputs" className="flex flex-col gap-1">
          {skipped.map((s) => (
            <li
              key={s.index}
              className="flex items-start gap-2 rounded-lg border border-amber-400/40 bg-amber-400/10 px-2.5 py-1.5 text-xs text-amber-700 dark:text-amber-300"
            >
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
              <span>
                <span className="font-medium">
                  Nicht geschrieben: {s.type}
                  {s.source ? ` aus ${s.source}` : ""} (output[{s.index}])
                </span>
                {" – "}
                {s.reason}
              </span>
            </li>
          ))}
        </ul>
      )}

      {companions.length > 0 && (
        <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <span>Begleitdateien:</span>
          {companions.map((file) => (
            <a
              key={file.name}
              href={api.runFileUrl(runId, file.name)}
              onClick={(e) => onDownload(e, runId, api.runFileUrl(runId, file.name))}
              className="inline-flex items-center gap-1 font-mono underline-offset-2 hover:text-foreground hover:underline"
              title={`${file.name} herunterladen (${formatBytes(file.size_bytes)})`}
            >
              <Download className="size-3" />
              {file.name}
            </a>
          ))}
        </p>
      )}
    </section>
  );
}

/** Knopf „Ausgaben“ der Ergebnisansicht: nennt die Zahl der Dateien und öffnet
 * die Liste als Dialog. Eine übersprungene Ausgabe ist schon am Knopf zu sehen. */
export function RunFilesButton({
  runId,
  onDownload,
  onGone,
  refreshKey = "",
}: {
  runId: string;
  onDownload: RunDownloadHandler;
  onGone?: (runId: string) => void;
  refreshKey?: string;
}) {
  const [open, setOpen] = useState(false);
  const files = useRunOutputs(runId, refreshKey, onGone);
  const count = splitFiles(files.data).outputs.length;
  const skipped = files.data?.skipped.length ?? 0;
  const problem = skipped > 0 || !!files.error;

  return (
    <>
      <Button
        data-slot="run-files-trigger"
        onClick={() => setOpen(true)}
        title={
          skipped > 0
            ? `Dateien dieses Laufs herunterladen – ${skipped === 1 ? "eine Ausgabe wurde" : `${skipped} Ausgaben wurden`} nicht geschrieben`
            : "Dateien dieses Laufs herunterladen"
        }
      >
        <Download className="size-4" />
        Ausgaben
        {files.data && (
          <span className="rounded-full bg-primary-foreground/20 px-1.5 text-xs tabular-nums">{count}</span>
        )}
        {problem && <TriangleAlert className="size-4 text-amber-300" aria-label="mit Hinweis" />}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Ausgaben</DialogTitle>
            <DialogDescription>
              Was dieser Lauf geschrieben hat – jede Datei einzeln oder alle zusammen als ZIP.
            </DialogDescription>
          </DialogHeader>
          <RunFilesView runId={runId} files={files} onDownload={onDownload} bare />
        </DialogContent>
      </Dialog>
    </>
  );
}

function FileLink({
  runId,
  file,
  onDownload,
}: {
  runId: string;
  file: RunFile;
  onDownload: RunDownloadHandler;
}) {
  const url = api.runFileUrl(runId, file.name);
  const caption = fileCaption(file);
  return (
    <a
      href={url}
      onClick={(e) => onDownload(e, runId, url)}
      title={`${file.name} herunterladen`}
      className="group flex items-center gap-2.5 rounded-lg border bg-background px-2.5 py-1.5 transition-colors hover:border-primary/40 hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
    >
      <FileDown className="size-4 shrink-0 text-muted-foreground group-hover:text-primary" />
      <span className="min-w-0 flex-1">
        <span className="block truncate font-mono text-xs font-medium">{file.name}</span>
        <span className="block truncate text-xs text-muted-foreground">
          {caption ? `${caption} · ` : ""}
          {formatBytes(file.size_bytes)}
        </span>
      </span>
    </a>
  );
}

// --- Liste der Läufe (FA89) ----------------------------------------------------

type HistoryState = { data: RunHistory | null; error: string | null; loading: boolean };

export function RunHistoryDialog({
  currentRunId,
  onOpenRun,
  onDownload,
}: {
  currentRunId: string | null;
  // Öffnet den Lauf in Schritt 3; false = nicht geöffnet (z. B. dem Backend nicht mehr bekannt).
  onOpenRun: (run: RunSummary) => Promise<boolean>;
  onDownload: RunDownloadHandler;
}) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<HistoryState>({ data: null, error: null, loading: false });
  const [expanded, setExpanded] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);

  const reload = useCallback(() => {
    setState((prev) => ({ ...prev, loading: true }));
    api
      .listRuns()
      .then((data) => setState({ data, error: null, loading: false }))
      .catch((e) => setState({ data: null, error: e instanceof Error ? e.message : String(e), loading: false }));
  }, []);

  const show = () => {
    setOpen(true);
    reload();
  };

  const view = async (run: RunSummary) => {
    setOpening(run.run_id);
    try {
      if (await onOpenRun(run)) setOpen(false);
      else reload();
    } finally {
      setOpening(null);
    }
  };

  const runs = state.data?.runs ?? [];

  return (
    <>
      <Button
        variant="ghost"
        size="sm"
        onClick={show}
        title="Bisherige Läufe und ihre Dateien"
        aria-label="Läufe"
        data-slot="run-history-trigger"
      >
        <History className="size-4" />
        <span className="hidden sm:inline">Läufe</span>
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>Läufe</DialogTitle>
            <DialogDescription>{historyNote(state.data ? state.data.max_retained : null)}</DialogDescription>
          </DialogHeader>

          {state.error && (
            <p className="flex items-start gap-2 text-sm text-destructive">
              <TriangleAlert className="mt-0.5 size-4 shrink-0" />
              {state.error}
            </p>
          )}
          {state.loading && runs.length === 0 && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              Läufe werden geladen …
            </p>
          )}
          {!state.loading && !state.error && runs.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Noch kein Lauf. Ein Szenario in Schritt 2 ausführen – der Lauf erscheint dann hier.
            </p>
          )}

          <ul className="flex flex-col gap-2">
            {runs.map((run) => {
              const isOpen = expanded === run.run_id;
              const current = run.run_id === currentRunId;
              return (
                <li key={run.run_id} data-slot="run-history-entry" className="rounded-lg border">
                  <div className="flex flex-wrap items-center gap-2 px-3 py-2">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate font-medium">{run.scenario_name}</span>
                        {current && <Badge variant="secondary">geöffnet</Badge>}
                      </div>
                      <div className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
                        <span>{formatRunTime(run.created_at)}</span>
                        <Badge
                          variant={run.status === "error" ? "destructive" : "outline"}
                          className="h-4 px-1.5 text-[0.7rem]"
                        >
                          {runStatusLabel(run.status)}
                        </Badge>
                        <span>{runCounts(run)}</span>
                      </div>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-expanded={isOpen}
                      onClick={() => setExpanded(isOpen ? null : run.run_id)}
                    >
                      Dateien
                      <ChevronDown className={cn("size-3.5 transition-transform", isOpen && "rotate-180")} />
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={opening !== null}
                      onClick={() => void view(run)}
                      title="Diesen Lauf in Schritt 3 ansehen – der Editor bleibt, wie er ist"
                    >
                      {opening === run.run_id && <Loader2 className="size-3.5 animate-spin" />}
                      Ansehen
                    </Button>
                  </div>
                  {isOpen && (
                    <div className="border-t px-3 py-2">
                      <RunFiles
                        runId={run.run_id}
                        onDownload={onDownload}
                        onGone={reload}
                        refreshKey={run.status}
                        bare
                      />
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </DialogContent>
      </Dialog>
    </>
  );
}
