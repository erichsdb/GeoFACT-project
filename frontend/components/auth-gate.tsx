"use client";

// Auth-Gate (FA24): zeigt LoginScreen, solange kein Token vorliegt UND der
// Server tatsächlich eine Anmeldung verlangt (GEOFACT_WEB_USERS gesetzt).
// Ohne konfigurierte Nutzer bleibt das Verhalten identisch zu vorher -
// kein Login-Screen, direkter Zugriff (siehe backend/geofact_web/auth.py).
//
// Ein bereits gespeichertes Token wird optimistisch angenommen (kein
// eigener Preflight-Call) - eine erste echte Anfrage aus AppShell prüft
// es tatsächlich; schlägt sie mit 401 fehl, meldet setUnauthorizedHandler
// das hierher zurück und der Login-Screen erscheint alsdann.

import { useCallback, useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { api, setUnauthorizedHandler } from "@/lib/api";
import { clearAuth, getToken } from "@/lib/auth";
import { forgetEntryMode } from "@/lib/entryMode";
import LoginScreen from "@/components/login-screen";

type GateState = "checking" | "authenticated" | "needs_login";

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<GateState>("checking");

  const handleUnauthorized = useCallback(() => {
    clearAuth();
    setState("needs_login");
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(handleUnauthorized);
    return () => setUnauthorizedHandler(null);
  }, [handleUnauthorized]);

  useEffect(() => {
    // /api/meta ist öffentlich, solange kein Token nötig ist, und ein
    // gutes leichtes "ist Auth überhaupt aktiv"-Signal über den
    // Statuscode - kein dedizierter Endpunkt nötig.
    const check: Promise<unknown> = getToken() ? Promise.resolve() : api.meta();
    check.then(() => setState("authenticated")).catch(() => setState("needs_login"));
  }, []);

  if (state === "checking") {
    return (
      <div className="flex h-full flex-1 items-center justify-center">
        <Loader2 className="size-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (state === "needs_login") {
    return (
      <LoginScreen
        onSuccess={() => {
          // FA80: nach jeder Anmeldung fragt die Oberfläche, was der Nutzer tun will.
          forgetEntryMode();
          setState("authenticated");
        }}
      />
    );
  }

  return <>{children}</>;
}
