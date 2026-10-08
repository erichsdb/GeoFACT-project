"use client";

import { useRef, useState } from "react";
import { Upload, Plus, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import type { Catalog, CatalogSource } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

function SourceRow({
  source,
  checked,
  onToggle,
  onInsert,
  onDelete,
  deleting,
}: {
  source: CatalogSource;
  checked: boolean;
  // FA80: ohne onToggle (Einstieg "selbst schreiben") keine Checkbox zum Vormerken.
  onToggle?: () => void;
  // FA80: ohne onInsert (Vorauswahl für den Prompt) gibt es keinen "+ Layer"-Knopf.
  onInsert?: () => void;
  onDelete?: () => void;
  deleting?: boolean;
}) {
  return (
    // Vormerken: die ganze Karte schaltet um, nicht nur das Kästchen.
    <div
      className={cn(
        "flex items-start gap-3 rounded-lg border bg-card p-3 text-sm transition-colors",
        onToggle && "cursor-pointer hover:bg-accent/50",
        onToggle && checked && "border-primary bg-primary/5 hover:bg-primary/10"
      )}
      onClick={onToggle}
    >
      {onToggle && (
        <Checkbox
          checked={checked}
          onCheckedChange={onToggle}
          onClick={(e) => e.stopPropagation()}
          aria-label={`${source.label} vormerken`}
          className="mt-1"
        />
      )}
      <div className="min-w-0 flex-1">
        <div className="font-medium [overflow-wrap:anywhere] text-card-foreground">{source.label}</div>
        {source.description && (
          <div className="mt-0.5 text-sm [overflow-wrap:anywhere] text-muted-foreground">{source.description}</div>
        )}
        <Badge variant="secondary" className="mt-2 text-xs uppercase">
          {source.data_type}
        </Badge>
      </div>
      {onInsert && (
        <Button
          variant="outline"
          size="sm"
          onClick={onInsert}
          title="Als Layer in die Konfiguration einfügen"
        >
          <Plus className="size-3.5" />
          Layer
        </Button>
      )}
      {/* Nur eigene Uploads sind löschbar (FA28) - OSM-Presets/Copernicus
          sind serverseitige, gemeinsame Kataloge ohne Eigentümer. */}
      {onDelete && (
        <Button
          variant="outline"
          size="sm"
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          disabled={deleting}
          title="Upload endgültig löschen"
        >
          <Trash2 className="size-3.5" />
        </Button>
      )}
    </div>
  );
}

export default function SourcePanel({
  catalog,
  selected,
  onToggle,
  onInsertLayer,
  onUploaded,
  onDeleted,
}: {
  catalog: Catalog | null;
  selected: Set<string>;
  onToggle?: (id: string) => void;
  onInsertLayer?: (source: CatalogSource) => void;
  onUploaded: (source: CatalogSource) => void;
  // FA28: nach erfolgreichem Löschen aufgerufen, damit der Aufrufer den
  // Katalog neu lädt (analog zu onUploaded) - optional, damit bestehende
  // Einbindungen ohne Löschfunktion (falls je nötig) unverändert bleiben.
  onDeleted?: () => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  async function handleUpload(files: FileList | null) {
    if (!files?.length) return;
    setUploading(true);
    setError(null);
    try {
      for (const file of Array.from(files)) {
        const { source } = await api.upload(file);
        onUploaded(source);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function handleDelete(sourceId: string) {
    if (!window.confirm("Diesen Upload endgültig löschen?")) return;
    setDeletingId(sourceId);
    setError(null);
    try {
      await api.deleteUpload(sourceId);
      onDeleted?.();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeletingId(null);
    }
  }

  const section = (title: string, items: CatalogSource[], deletable = false) =>
    items.length > 0 && (
      <div>
        <h4 className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {title}
        </h4>
        <div className="space-y-2">
          {items.map((s) => (
            <SourceRow
              key={s.id}
              source={s}
              checked={selected.has(s.id)}
              onToggle={onToggle ? () => onToggle(s.id) : undefined}
              onInsert={onInsertLayer ? () => onInsertLayer(s) : undefined}
              onDelete={deletable ? () => handleDelete(s.id) : undefined}
              deleting={deletingId === s.id}
            />
          ))}
        </div>
      </div>
    );

  return (
    <div className="flex flex-col">
      <input
        ref={fileRef}
        type="file"
        multiple
        className="hidden"
        onChange={(e) => handleUpload(e.target.files)}
      />
      <button
        type="button"
        onClick={() => fileRef.current?.click()}
        disabled={uploading}
        className="flex w-full items-center gap-2 rounded-lg border border-dashed px-3 py-2.5 text-sm text-muted-foreground transition-colors hover:border-primary/50 hover:bg-accent hover:text-foreground disabled:pointer-events-none disabled:opacity-50"
      >
        <Upload className="size-4 shrink-0" />
        <span className="truncate text-left">
          {uploading ? "Lädt hoch…" : "Datei hochladen"}
        </span>
        <span className="ml-auto hidden shrink-0 text-xs text-muted-foreground/70 sm:inline">
          GeoJSON · GeoTIFF · CSV
        </span>
      </button>
      {error && <p className="mt-1.5 text-sm text-destructive">{error}</p>}

      <div className="mt-1">
        {section("Uploads", catalog?.uploads ?? [], true)}
        {section("Beispieldaten", catalog?.samples ?? [])}
        {section("OSM-Presets", catalog?.osm_presets ?? [])}
        {section("Copernicus / Raster", catalog?.copernicus ?? [])}
        {!catalog && <p className="mt-4 text-sm text-muted-foreground">Lade Katalog…</p>}
      </div>
    </div>
  );
}
