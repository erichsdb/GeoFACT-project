import path from "node:path";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Schlankes Server-Bundle für Docker-Deployment (kopiert nur benötigte
  // node_modules statt des kompletten Ordners). Ändert `next dev` nicht.
  output: "standalone",
  // Ohne dies erkennt Turbopack fälschlich den Repo-Root (eine leere
  // package-lock.json dort, außerhalb von frontend/) als Projekt-Root und
  // schreibt .next/standalone/frontend/server.js statt .next/standalone/
  // server.js - bricht Dockerfile.frontend, das den Docker-Doku-Standardpfad
  // erwartet (docs/output.md#automatically-copying-traced-files).
  turbopack: {
    root: path.join(__dirname),
  },
};

export default nextConfig;
