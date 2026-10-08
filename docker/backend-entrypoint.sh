#!/bin/sh
# Startet tailscaled im Hintergrund (echtes TUN-Device, siehe
# docker-compose.yml: cap_add NET_ADMIN + /dev/net/tun-Mount - der
# Standard-, best getestete Tailscale-Docker-Weg), verbindet mit dem
# Tailnet über TS_AUTHKEY, dann den eigentlichen GeoFACT-Backend-Prozess
# im Vordergrund.
#
# Tailscale läuft bewusst IM backend-Container selbst statt als separater
# Sidecar-Service (network_mode: service:tailscale) - letzteres nimmt dem
# backend-Container seinen eigenen Netzwerk-Namespace/Servicenamen, was
# Coolifys automatisches Traefik-Routing erschwert. Nur DIESER Container tritt dem Tailnet bei,
# nicht der Coolify-Host - "Tailscale nur für den Container" bleibt damit
# erhalten.
#
# GEOFACT_PG_DSN referenziert die Tailscale-IP/den MagicDNS-Namen des NAS
# (z. B. postgresql://user:pw@geofact-nas.tailXXXX.ts.net:15432/db) - sobald
# tailscaled verbunden ist, loest/routet das Backend dorthin wie in jedes
# andere Netzwerk.

set -e

if [ -z "$TS_AUTHKEY" ]; then
    echo "TS_AUTHKEY nicht gesetzt - Backend startet OHNE Tailscale-Anbindung." >&2
    echo "GEOFACT_PG_DSN muss dann direkt erreichbar sein (kein NAS-Tailnet-Zugriff)." >&2
else
    tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock &
    TAILSCALED_PID=$!

    # Kurze Wartezeit, bis der Tailscale-Daemon sein Socket bereitstellt -
    # "tailscale up" schlägt sonst mit einer Race-Condition fehl.
    for i in $(seq 1 20); do
        [ -S /var/run/tailscale/tailscaled.sock ] && break
        sleep 0.5
    done

    tailscale up --authkey="$TS_AUTHKEY" --hostname="geofact-backend" --accept-routes

    # tailscaled am Leben halten, aber den Skriptfluss nicht blockieren -
    # der eigentliche Container-Health-Zustand hängt am Hauptprozess unten.
    trap "kill $TAILSCALED_PID 2>/dev/null || true" EXIT
fi

exec geofact-api
