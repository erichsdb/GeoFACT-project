"use client";

import dynamic from "next/dynamic";
import { Loader2 } from "lucide-react";
import AuthGate from "@/components/auth-gate";

const AppShell = dynamic(() => import("@/components/app-shell"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full flex-1 items-center justify-center">
      <Loader2 className="size-5 animate-spin text-muted-foreground" />
    </div>
  ),
});

export default function Page() {
  return (
    <AuthGate>
      <AppShell />
    </AuthGate>
  );
}
