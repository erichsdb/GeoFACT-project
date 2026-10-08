"use client";

// Implements: FA59 (Szenario-Parameter, Web-Frontend-Teil).
//
// Formular aus den deklarierten ParameterSpec-Feldern (type, items, default,
// description, choices): string -> Textfeld, integer/float -> Zahlenfeld,
// boolean -> Schalter, choices -> Auswahl, list/mapping -> YAML-Textfeld.
// Der Aufrufer hält nur die UEBERSCHREIBUNGEN (Name -> Wert); was nicht
// überschrieben ist, läuft mit dem Default des Szenarios. Ungültige
// Eingaben (keine Zahl, kein YAML) bleiben sichtbar markiert und werden nicht
// still übernommen (Goldene Regel 7).

import { useState } from "react";
import { dump as dumpYaml, load as loadYaml } from "js-yaml";
import { RotateCcw, SlidersHorizontal } from "lucide-react";
import type { ParameterInfo } from "@/lib/types";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export default function ParameterPanel({
  parameters,
  values,
  onChange,
}: {
  parameters: ParameterInfo[];
  // Überschreibungen (nur gesetzte Namen).
  values: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
}) {
  // Zähler je Feld: "Zurücksetzen" baut das Feld neu auf (frischer Entwurf).
  const [resets, setResets] = useState<Record<string, number>>({});

  if (parameters.length === 0) return null;

  const set = (name: string, value: unknown) => onChange({ ...values, [name]: value });
  const reset = (name: string) => {
    const next = { ...values };
    delete next[name];
    onChange(next);
    setResets((r) => ({ ...r, [name]: (r[name] ?? 0) + 1 }));
  };

  return (
    <section className="rounded-xl border bg-card p-4 sm:p-6">
      <div className="mb-3 flex items-center gap-2">
        <SlidersHorizontal className="size-4 text-muted-foreground" />
        <h3 className="text-base font-semibold">Parameter</h3>
        <span className="text-xs text-muted-foreground">
          Werte überschreiben die Defaults des Szenarios, ohne die YAML zu ändern
        </span>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {parameters.map((p) => {
          const overridden = Object.prototype.hasOwnProperty.call(values, p.name);
          const current = overridden ? values[p.name] : p.default;
          return (
            <div key={p.name} className="flex min-w-0 flex-col gap-1.5">
              <div className="flex items-center justify-between gap-2">
                <label htmlFor={`param-${p.name}`} className="truncate font-mono text-sm font-medium">
                  {p.name}
                  <span className="ml-1.5 font-sans text-xs font-normal text-muted-foreground">
                    {typeLabel(p)}
                    {p.default === null || p.default === undefined ? " · Pflicht" : ""}
                  </span>
                </label>
                {overridden && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-6 px-1.5 text-xs"
                    title="Auf den Default des Szenarios zurücksetzen"
                    onClick={() => reset(p.name)}
                  >
                    <RotateCcw className="size-3" />
                    Default
                  </Button>
                )}
              </div>
              <ParameterField
                key={`${p.name}:${resets[p.name] ?? 0}`}
                param={p}
                value={current}
                onValue={(v) => set(p.name, v)}
              />
              {p.description && <p className="text-xs text-muted-foreground">{p.description}</p>}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function typeLabel(p: ParameterInfo): string {
  if (p.type === "list") return `Liste (${p.items ?? "string"})`;
  if (p.type === "float") return "Zahl";
  if (p.type === "integer") return "Ganzzahl";
  if (p.type === "boolean") return "ja/nein";
  if (p.type === "mapping") return "Zuordnung";
  return "Text";
}

function toYaml(value: unknown): string {
  if (value === null || value === undefined) return "";
  return dumpYaml(value, { flowLevel: 1 }).trimEnd();
}

function ParameterField({
  param,
  value,
  onValue,
}: {
  param: ParameterInfo;
  value: unknown;
  onValue: (v: unknown) => void;
}) {
  const id = `param-${param.name}`;
  const [draft, setDraft] = useState<string>(() =>
    param.type === "list" || param.type === "mapping" ? toYaml(value) : value == null ? "" : String(value)
  );
  const [error, setError] = useState<string | null>(null);

  // Auswahl aus choices (nur Skalare; bei Listen gelten choices je Element und
  // die Liste bleibt ein YAML-Feld).
  if (param.choices && param.choices.length > 0 && param.type !== "list" && param.type !== "mapping") {
    const index = param.choices.findIndex((c) => c === value);
    return (
      <Select
        value={index >= 0 ? String(index) : ""}
        onValueChange={(v) => {
          if (v === null || v === "") return;
          onValue(param.choices![Number(v)]);
        }}
      >
        <SelectTrigger id={id} className="w-full">
          <SelectValue placeholder="Wert wählen" />
        </SelectTrigger>
        <SelectContent>
          {param.choices.map((c, i) => (
            <SelectItem key={i} value={String(i)}>
              {String(c)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  if (param.type === "boolean") {
    return (
      <label className="flex items-center gap-2 text-sm">
        <input
          id={id}
          type="checkbox"
          className="size-4 accent-primary"
          checked={value === true}
          onChange={(e) => onValue(e.target.checked)}
        />
        {value === true ? "ja" : "nein"}
      </label>
    );
  }

  if (param.type === "integer" || param.type === "float") {
    return (
      <>
        <Input
          id={id}
          type="number"
          step={param.type === "integer" ? 1 : "any"}
          value={draft}
          aria-invalid={error ? true : undefined}
          onChange={(e) => {
            const text = e.target.value;
            setDraft(text);
            const parsed = text.trim() === "" ? NaN : Number(text);
            const ok =
              Number.isFinite(parsed) && (param.type === "float" || Number.isInteger(parsed));
            if (!ok) {
              setError(param.type === "integer" ? "keine gültige Ganzzahl" : "keine gültige Zahl");
              return;
            }
            setError(null);
            onValue(parsed);
          }}
        />
        {error && <p className="text-xs text-destructive">{error} – Wert nicht übernommen</p>}
      </>
    );
  }

  if (param.type === "list" || param.type === "mapping") {
    return (
      <>
        <Textarea
          id={id}
          rows={3}
          spellCheck={false}
          // wächst mit dem Inhalt (field-sizing), aber nur bis 10rem - eine
          // Liste mit 80 Städten schob das Formular sonst seitenweise (R12)
          className="max-h-40 overflow-y-auto font-mono text-xs"
          value={draft}
          aria-invalid={error ? true : undefined}
          placeholder={param.type === "list" ? "[a, b, c]" : "schluessel: wert"}
          onChange={(e) => {
            const text = e.target.value;
            setDraft(text);
            let parsed: unknown;
            try {
              parsed = loadYaml(text);
            } catch (err) {
              setError(`kein gültiges YAML: ${err instanceof Error ? err.message.split("\n")[0] : String(err)}`);
              return;
            }
            const isList = Array.isArray(parsed);
            const isMapping = parsed !== null && typeof parsed === "object" && !isList;
            if (param.type === "list" ? !isList : !isMapping) {
              setError(param.type === "list" ? "erwartet eine YAML-Liste, z. B. [a, b]" : "erwartet eine YAML-Zuordnung");
              return;
            }
            setError(null);
            onValue(parsed);
          }}
        />
        {error && <p className="text-xs text-destructive">{error} – Wert nicht übernommen</p>}
      </>
    );
  }

  // string (und unbekannte Typen): Text wird unverändert übernommen.
  return (
    <Input
      id={id}
      type="text"
      value={draft}
      onChange={(e) => {
        setDraft(e.target.value);
        onValue(e.target.value);
      }}
    />
  );
}
