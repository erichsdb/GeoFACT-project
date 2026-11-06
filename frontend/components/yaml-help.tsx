"use client";

// Implements: FA97 (Hilfe zur YAML-Konfiguration am Editor).
//
// Der (?)-Knopf neben dem Editortitel öffnet eine Seitenleiste mit einer
// Kurzfassung der Anleitung (docs/konfiguration_schreiben.md); jeder Abschnitt
// hat einen Ausschnitt zum Kopieren. Der Inhalt liegt als Daten in
// lib/yamlHelp.ts, hier steht nur die Darstellung. Eine Seitenleiste statt
// eines Dialogs, damit der Editor neben der Hilfe sichtbar bleibt.

import { useState } from "react";
import { CircleHelp, Copy, ExternalLink } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { API_BASE } from "@/lib/api";
import { notify } from "@/lib/toast";
import { YAML_HELP_POINTERS, YAML_HELP_SECTIONS, apiDocsUrl } from "@/lib/yamlHelp";

/** Teilt einen Text an Backticks: ungerade Teile sind Code. */
function inlineCode(text: string) {
  return text.split("`").map((part, i) =>
    i % 2 === 1 ? (
      <code key={i} className="rounded bg-muted px-1 py-0.5 font-mono text-[0.85em]">
        {part}
      </code>
    ) : (
      part
    )
  );
}

async function copySnippet(yaml: string) {
  try {
    await navigator.clipboard.writeText(yaml);
    notify.success("Beispiel kopiert.");
  } catch (e) {
    notify.error(`Kopieren fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`);
  }
}

export default function YamlHelpButton() {
  const [open, setOpen] = useState(false);

  const jumpTo = (id: string) => {
    document.getElementById(`yaml-help-${id}`)?.scrollIntoView({ block: "start" });
  };

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label="Hilfe zur YAML-Konfiguration"
        title="Hilfe zur YAML-Konfiguration"
        data-action="yaml-help"
        onClick={() => setOpen(true)}
      >
        <CircleHelp className="size-4" />
      </Button>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent side="right" className="data-[side=right]:sm:max-w-2xl">
          <SheetHeader className="pr-12">
            <SheetTitle>Eine Szenario-Konfiguration schreiben</SheetTitle>
            <SheetDescription>Kurzfassung der Anleitung. Jeder Abschnitt hat ein Beispiel zum Kopieren.</SheetDescription>
          </SheetHeader>
          <ScrollArea className="min-h-0 flex-1">
            <div className="space-y-6 px-4 pb-6">
              <nav aria-label="Abschnitte der Hilfe" className="flex flex-wrap gap-1.5">
                {YAML_HELP_SECTIONS.map((section) => (
                  <button
                    key={section.id}
                    type="button"
                    onClick={() => jumpTo(section.id)}
                    className="rounded-md border px-2 py-1 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
                  >
                    {section.title}
                  </button>
                ))}
              </nav>

              {YAML_HELP_SECTIONS.map((section) => (
                <section key={section.id} id={`yaml-help-${section.id}`} className="space-y-2 scroll-mt-2">
                  <h3 className="text-sm font-semibold">{section.title}</h3>
                  <p className="text-sm leading-relaxed">{inlineCode(section.text)}</p>
                  {section.bullets && (
                    <ul className="list-disc space-y-1 pl-5 text-sm leading-relaxed">
                      {section.bullets.map((bullet) => (
                        <li key={bullet}>{inlineCode(bullet)}</li>
                      ))}
                    </ul>
                  )}
                  {section.yaml && (
                    <div className="relative rounded-lg border bg-muted/30">
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        data-action="copy-snippet"
                        className="absolute top-1 right-1"
                        onClick={() => copySnippet(section.yaml!)}
                      >
                        <Copy />
                        Kopieren
                      </Button>
                      <pre className="overflow-x-auto p-3 pt-9 font-mono text-xs leading-relaxed">{section.yaml}</pre>
                    </div>
                  )}
                </section>
              ))}

              <section className="space-y-2 border-t pt-4">
                <h3 className="text-sm font-semibold">Weitere Hilfe</h3>
                <ul className="list-disc space-y-1 pl-5 text-sm leading-relaxed">
                  <li>{YAML_HELP_POINTERS.catalogs}</li>
                  <li>{YAML_HELP_POINTERS.template}</li>
                  <li>{YAML_HELP_POINTERS.guide}</li>
                  <li>
                    <a
                      href={apiDocsUrl(API_BASE)}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 underline underline-offset-4"
                    >
                      {YAML_HELP_POINTERS.apiDocs}
                      <ExternalLink className="size-3.5" aria-hidden />
                    </a>
                  </li>
                </ul>
              </section>
            </div>
          </ScrollArea>
        </SheetContent>
      </Sheet>
    </>
  );
}
