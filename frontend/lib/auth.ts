// Token-Verwaltung für die Web-Zugriffskontrolle (FA24, Backend:
// backend/geofact_web/auth.py). Der Server lässt ohne konfigurierte
// GEOFACT_WEB_USERS jede Anfrage unauthentifiziert durch (siehe
// require_auth_middleware) - das Frontend spiegelt das: ohne gespeichertes
// Token wird einfach kein Authorization-Header gesendet, erst ein 401 vom
// Server löst den Login-Screen aus (siehe app-shell.tsx).

const TOKEN_STORAGE_KEY = "geofact:authToken";
const USERNAME_STORAGE_KEY = "geofact:authUsername";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function getUsername(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(USERNAME_STORAGE_KEY);
}

export function setAuth(token: string, username: string): void {
  window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  window.localStorage.setItem(USERNAME_STORAGE_KEY, username);
}

export function clearAuth(): void {
  window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  window.localStorage.removeItem(USERNAME_STORAGE_KEY);
}

// Für URLs, die von Browser-APIs ohne Header-Unterstützung aufgerufen
// werden (EventSource für SSE, <img src>, <a href> für das ZIP und einzelne Dateien)
// - siehe require_auth_middleware im Backend für die passenden, bewusst
// auf vier Routen begrenzten ?token=-Fallback.
export function withTokenParam(url: string): string {
  const token = getToken();
  if (!token) return url;
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}token=${encodeURIComponent(token)}`;
}
