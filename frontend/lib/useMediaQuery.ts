"use client";

import { useSyncExternalStore } from "react";

/** Reagiert live auf eine CSS-Media-Query (z. B. für Verhalten, das per
 * Tailwind-Klasse allein nicht steuerbar ist - CodeMirror-Extensions,
 * Kartenoptionen etc.). SSR-sicher: liefert `false` bis zur ersten
 * Client-Auswertung (kein window in Node), danach reaktiv bei Resize. */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    () => window.matchMedia(query).matches,
    () => false
  );
}
