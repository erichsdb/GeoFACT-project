"use client";

import { useState } from "react";
import { useTheme } from "next-themes";
import { Settings2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  describeOverrides,
  effectiveOptions,
  hasOverrides,
  llmChoiceLabel,
  overridesFrom,
  parseSeconds,
  type GenerationOptions,
  type ServerSettings,
} from "@/lib/generationSettings";

/** FA81-FA83: Knopf oben rechts und Dialog für die Einstellungen - OSM-Quelle
 * (FA82), Sprachmodell aus der Vorauswahl des Servers (FA83), Frist je
 * Modellanfrage, Probelauf an/aus und dessen Frist (FA81). Vorbelegt mit den
 * Werten des Servers (Umgebungsvariablen); gespeichert wird nur die Abweichung.
 * FA84: dazu der Schalter für den dunklen Modus (Standard: hell).
 * Ohne Server-Werte (Meta noch nicht geladen) ist der Knopf gesperrt. */
export default function GenerationSettings({
  server,
  value,
  onChange,
}: {
  server: ServerSettings | null;
  value: GenerationOptions;
  onChange: (next: GenerationOptions) => void;
}) {
  const [open, setOpen] = useState(false);
  const changed = hasOverrides(value);
  // Was gerade gilt, steht am Knopf: die Kopfzeile selbst nennt OSM-Quelle und
  // Sprachmodell nicht mehr.
  const active = server ? effectiveOptions(server.defaults, value) : null;
  const activeLlm = server?.choices.llm_choices.find((c) => c.id === active?.llm_choice);
  const summary = active ? `OSM: ${active.osm_backend}, Sprachmodell: ${activeLlm?.model ?? active.llm_choice}` : "";

  return (
    <>
      <Button
        variant="ghost"
        size="icon-sm"
        onClick={() => setOpen(true)}
        disabled={!server}
        aria-label="Einstellungen"
        title={
          changed
            ? `Einstellungen – ${summary} – geändert: ${describeOverrides(value)}`
            : `Einstellungen – ${summary}`
        }
        className="relative"
      >
        <Settings2 className="size-4" />
        {changed && <span className="absolute top-1 right-1 size-2 rounded-full bg-primary" aria-hidden />}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-md">
          {/* Erst beim Öffnen einhängen: das Formular startet jedes Mal mit den geltenden Werten. */}
          {open && server && (
            <SettingsForm
              server={server}
              value={value}
              onSave={(next) => {
                onChange(next);
                setOpen(false);
              }}
              onCancel={() => setOpen(false)}
            />
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}

function SettingsForm({
  server,
  value,
  onSave,
  onCancel,
}: {
  server: ServerSettings;
  value: GenerationOptions;
  onSave: (next: GenerationOptions) => void;
  onCancel: () => void;
}) {
  const { defaults, limits, choices } = server;
  const current = effectiveOptions(defaults, value);
  const [osmBackend, setOsmBackend] = useState(current.osm_backend);
  const [llmChoice, setLlmChoice] = useState(current.llm_choice);
  const [deadline, setDeadline] = useState(String(current.llm_deadline_s));
  const [trialRun, setTrialRun] = useState(current.trial_run);
  const [trialSeconds, setTrialSeconds] = useState(String(current.trial_run_s));
  // FA84: die Darstellung merkt sich next-themes selbst (nicht Teil der GenerationOptions).
  const { resolvedTheme, setTheme } = useTheme();
  const [darkMode, setDarkMode] = useState(resolvedTheme === "dark");

  const deadlineResult = parseSeconds(deadline, limits.llm_deadline_s);
  // Bei ausgeschaltetem Probelauf zählt seine Frist nicht - ein dort stehender
  // ungültiger Wert blockiert das Speichern nicht und wird nicht übernommen.
  const trialResult = trialRun
    ? parseSeconds(trialSeconds, limits.trial_run_s)
    : ({ ok: true, value: current.trial_run_s } as const);
  const valid = deadlineResult.ok && trialResult.ok;

  const save = () => {
    if (!deadlineResult.ok || !trialResult.ok) return;
    setTheme(darkMode ? "dark" : "light");
    onSave(
      overridesFrom(
        {
          llm_deadline_s: deadlineResult.value,
          trial_run: trialRun,
          trial_run_s: trialResult.value,
          osm_backend: osmBackend,
          llm_choice: llmChoice,
        },
        defaults
      )
    );
  };

  const reset = () => {
    setOsmBackend(defaults.osm_backend);
    setLlmChoice(defaults.llm_choice);
    setDeadline(String(defaults.llm_deadline_s));
    setTrialRun(defaults.trial_run);
    setTrialSeconds(String(defaults.trial_run_s));
    setDarkMode(false);
  };

  const defaultLlm = choices.llm_choices.find((c) => c.id === defaults.llm_choice);

  return (
    <>
      <DialogHeader>
        <DialogTitle>Einstellungen</DialogTitle>
        <DialogDescription>
          Gilt in diesem Browser. Die Standardwerte und die Auswahl kommen vom Server.
        </DialogDescription>
      </DialogHeader>

      <div className="flex flex-col gap-5">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="settings-osm-backend">OSM-Quelle</Label>
          {choices.osm_backends.length > 1 ? (
            <Select value={osmBackend} onValueChange={(v) => v && setOsmBackend(v)}>
              <SelectTrigger id="settings-osm-backend" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {choices.osm_backends.map((name) => (
                  <SelectItem key={name} value={name}>
                    {name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <p id="settings-osm-backend" className="text-sm">
              {defaults.osm_backend}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            {choices.osm_backends.length > 1
              ? `Woher OSM-Layer kommen – für Läufe und den Probelauf. Standard: ${defaults.osm_backend}.`
              : "Der Server bietet nur diese Quelle an."}
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="settings-llm-choice">Sprachmodell</Label>
          {choices.llm_choices.length > 1 ? (
            <Select value={llmChoice} onValueChange={(v) => v && setLlmChoice(v)}>
              <SelectTrigger id="settings-llm-choice" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {choices.llm_choices.map((choice) => (
                  <SelectItem key={choice.id} value={choice.id}>
                    {llmChoiceLabel(choice)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          ) : (
            <p id="settings-llm-choice" className="text-sm">
              {defaultLlm ? llmChoiceLabel(defaultLlm) : defaults.llm_choice}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            {choices.llm_choices.length > 1
              ? `Modell und Anbieter für „Szenario generieren“. Standard: ${defaultLlm ? llmChoiceLabel(defaultLlm) : defaults.llm_choice}.`
              : "Der Server gibt keine Auswahl vor (GEOFACT_LLM_CHOICES)."}
          </p>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="generation-deadline">Frist je Modellanfrage (Sekunden)</Label>
          <Input
            id="generation-deadline"
            inputMode="decimal"
            value={deadline}
            onChange={(e) => setDeadline(e.target.value)}
            aria-invalid={!deadlineResult.ok}
          />
          <p className={`text-xs ${deadlineResult.ok ? "text-muted-foreground" : "text-destructive"}`}>
            {deadlineResult.ok
              ? `Gilt für jede einzelne Anfrage an das Sprachmodell (Erzeugung und jede Korrekturrunde), nicht für die ganze Erzeugung. Standard: ${defaults.llm_deadline_s} s.`
              : deadlineResult.error}
          </p>
        </div>

        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1">
            <Label htmlFor="generation-trial-run">Probelauf</Label>
            <p className="text-xs text-muted-foreground">
              Führt die erzeugte Konfiguration einmal ohne Ausgaben auf den echten Daten aus und gibt Abbrüche und
              Warnungen in die Korrektur. Standard: {defaults.trial_run ? "an" : "aus"}.
            </p>
          </div>
          <Switch id="generation-trial-run" checked={trialRun} onCheckedChange={setTrialRun} />
        </div>

        <div className="flex flex-col gap-1.5">
          <Label htmlFor="generation-trial-seconds" className={trialRun ? undefined : "text-muted-foreground"}>
            Frist des Probelaufs (Sekunden)
          </Label>
          <Input
            id="generation-trial-seconds"
            inputMode="decimal"
            value={trialSeconds}
            onChange={(e) => setTrialSeconds(e.target.value)}
            disabled={!trialRun}
            aria-invalid={!trialResult.ok}
          />
          <p className={`text-xs ${trialResult.ok ? "text-muted-foreground" : "text-destructive"}`}>
            {trialResult.ok
              ? `Danach gilt die Konfiguration als gültig, aber ungeprüft. Standard: ${defaults.trial_run_s} s.`
              : trialResult.error}
          </p>
        </div>

        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1">
            <Label htmlFor="settings-dark-mode">Dunkler Modus</Label>
            <p className="text-xs text-muted-foreground">Darstellung der Oberfläche. Standard: aus (hell).</p>
          </div>
          <Switch id="settings-dark-mode" checked={darkMode} onCheckedChange={setDarkMode} />
        </div>
      </div>

      <DialogFooter className="sm:justify-between">
        <Button variant="ghost" onClick={reset}>
          Standardwerte
        </Button>
        <div className="flex gap-2">
          <Button variant="outline" onClick={onCancel}>
            Abbrechen
          </Button>
          <Button onClick={save} disabled={!valid}>
            Übernehmen
          </Button>
        </div>
      </DialogFooter>
    </>
  );
}
