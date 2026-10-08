"use client";

// FA73: Ausgabelizenzen und Lizenzprüfung im Validierungsbereich des Editors.
// Die vom Nutzer festgelegten Ausgabelizenzen kommen ohne Netz aus der
// Validierung (validation.output_licenses); die Kompatibilitätsprüfung gegen
// DALICC läuft nur auf Knopfdruck (POST /api/config/licenses), weil sie einen
// externen Dienst fragt. Das Ergebnis ist eine Prüfhilfe, keine Rechtsauskunft.
// Der Aufrufer setzt key={licenseCheckKey(configYaml, parameters)}: ein
// geändertes Szenario ODER geänderte Parameterwerte (FA59; sie können
// Layer und Lizenzen umschalten) verwerfen ein früheres Prüfergebnis.

import { useState } from "react";
import { Scale } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import type {
  LicenseCheckOut,
  LicenseCheckResponse,
  LicenseStatus,
  OutputLicenseInfo,
} from "@/lib/types";

const STATUS_TEXT: Record<LicenseStatus, string> = {
  compatible: "vereinbar",
  conflicts: "Konflikt",
  not_checkable: "nicht prüfbar",
  unreachable: "DALICC nicht erreichbar",
  no_licenses: "keine Lizenzen",
};

const STATUS_CLASS: Record<LicenseStatus, string> = {
  compatible: "text-emerald-600 dark:text-emerald-400",
  conflicts: "text-destructive",
  not_checkable: "text-amber-600 dark:text-amber-400",
  unreachable: "text-amber-600 dark:text-amber-400",
  no_licenses: "text-muted-foreground",
};

/** Remount-Schlüssel des Panels: YAML plus Parameterwerte (Schlüssel sortiert). */
export function licenseCheckKey(configYaml: string, parameters?: Record<string, unknown>): string {
  const sorted = Object.keys(parameters ?? {})
    .sort()
    .map((name) => [name, parameters?.[name]]);
  return `${configYaml}\u0000${JSON.stringify(sorted)}`;
}

/** R26: Ausgaben mit derselben Quelle und demselben Urteil stehen in einer
 * Zeile, statt den gleichen Text je Ausgabe zu wiederholen. */
function groupChecks(checks: LicenseCheckOut[]): { first: LicenseCheckOut; checks: LicenseCheckOut[] }[] {
  const groups = new Map<string, { first: LicenseCheckOut; checks: LicenseCheckOut[] }>();
  for (const check of checks) {
    const key = JSON.stringify([
      check.source,
      check.status,
      check.message,
      check.output_license?.name ?? null,
      check.checked_at?.slice(0, 10) ?? null,
    ]);
    const group = groups.get(key);
    if (group) group.checks.push(check);
    else groups.set(key, { first: check, checks: [check] });
  }
  return [...groups.values()];
}

export default function LicenseCheckPanel({
  configYaml,
  parameters,
  outputLicenses,
}: {
  configYaml: string;
  parameters?: Record<string, unknown>;
  outputLicenses: OutputLicenseInfo[];
}) {
  const [result, setResult] = useState<LicenseCheckResponse | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function check() {
    setChecking(true);
    setError(null);
    try {
      setResult(await api.checkLicenses(configYaml, parameters));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setChecking(false);
    }
  }

  return (
    <div data-slot="license-check" className="mt-2 flex flex-col gap-1 text-xs text-muted-foreground">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground/80">Ausgabelizenzen:</span>
        {outputLicenses.length === 0 ? (
          <span>keine festgelegt</span>
        ) : (
          outputLicenses.map((item) => (
            <span key={item.index} title={item.line}>
              output[{item.index}] {item.type}: {item.license.name}
              {item.declared_at === "scenario" ? " (Szenario)" : ""}
            </span>
          ))
        )}
        <Button
          variant="ghost"
          size="sm"
          onClick={check}
          disabled={checking || !configYaml.trim()}
          title="Lizenzen der Quellen und Ausgabelizenz je Ausgabe mit DALICC prüfen (externer Dienst)"
        >
          <Scale className="size-4" />
          {checking ? "prüfe…" : "Lizenzen prüfen (DALICC)"}
        </Button>
      </div>
      {error && <p className="text-destructive">Lizenzprüfung fehlgeschlagen: {error}</p>}
      {result && !result.valid && (
        <p className="text-destructive">Konfiguration ungültig – keine Lizenzprüfung.</p>
      )}
      {result?.valid && result.checks.length === 0 && <p>Keine Ausgaben zu prüfen.</p>}
      {result?.valid &&
        groupChecks(result.checks).map(({ checks, first }) => (
          <div key={first.index} className="flex flex-col">
            <span>
              {checks.map((c) => `output[${c.index}] ${c.type}`).join(", ")} ← {first.source}:{" "}
              <span className={`font-medium ${STATUS_CLASS[first.status]}`}>
                {STATUS_TEXT[first.status]}
              </span>
              {first.checked_at ? ` · geprüft am ${first.checked_at.slice(0, 10)}` : ""}
            </span>
            {first.status !== "compatible" && first.status !== "no_licenses" && (
              <span className="pl-3">{first.message}</span>
            )}
          </div>
        ))}
      {result?.valid && result.checks.some((c) => c.status === "conflicts") && (
        // Q25: DALICC stellt Erlaubnisse und Verbote formal gegenüber; eine
        // Quelle, die mehr erlaubt (CC0 neben ODbL), erscheint dort als
        // Konflikt, obwohl sie rechtlich unproblematisch ist.
        <p>
          Das Urteil „Konflikt“ stammt von DALICC: es vergleicht Erlaubnisse und Verbote formal und
          meldet auch Fälle wie CC0 neben ODbL, die rechtlich unproblematisch sind.
        </p>
      )}
      {result?.valid && (
        <p className="italic">Prüfhilfe laut DALICC, keine Rechtsauskunft.</p>
      )}
    </div>
  );
}
