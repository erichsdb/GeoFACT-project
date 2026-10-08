"""Zugriffskontrolle für das Web-Backend.

Implements: FA24

Bewusst minimal (nur stdlib): feste Nutzerliste (GEOFACT_WEB_USERS), PBKDF2-Hashes,
signierte Tokens mit Ablaufzeit statt Sessions/DB. Kein Self-Signup, keine Rollen.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .settings import Settings, get_settings

# Ohne Token erreichbar: Login und Health-Check. Alles andere unter /api/ verlangt
# ein Bearer-Token, sobald GEOFACT_WEB_USERS gesetzt ist.
_PUBLIC_PATHS = {"/api/auth/login", "/api/health"}

# Diese vier Endpunkte rufen Browser-APIs ohne Authorization-Header auf (EventSource,
# <img src>, <a href> für das ZIP und einzelne Dateien, FA88); nur hier gilt das Token
# auch als ?token=. Nicht global, damit Tokens nicht überall in Server- und Proxy-Logs landen.
_TOKEN_QUERY_PATH_PATTERNS = (
    re.compile(r"^/api/runs/[^/]+/events$"),
    re.compile(r"^/api/runs/[^/]+/nodes/[^/]+/raster\.png$"),
    re.compile(r"^/api/runs/[^/]+/download$"),
    re.compile(r"^/api/runs/[^/]+/outputs/[^/]+$"),
)


def requires_token(path: str) -> bool:
    """Ob die Middleware für diesen Pfad ein Token verlangt; auch OpenAPI leitet daraus ab (FA85)."""
    return path.startswith("/api/") and path not in _PUBLIC_PATHS


def accepts_query_token(path: str) -> bool:
    """Ob der Pfad das Token auch als ?token= annimmt."""
    return any(pattern.match(path) for pattern in _TOKEN_QUERY_PATH_PATTERNS)


_PBKDF2_ITERATIONS = 600_000
_PBKDF2_ALGO = "sha256"


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Erzeugt einen 'salt.hash'-Hex-String für GEOFACT_WEB_USERS.

    Trennzeichen ist '.' statt '$': Docker Compose und POSIX-Shells expandieren '$'
    in Umgebungsvariablen und würden den Hash stillschweigend verstümmeln.
    Kommandozeile: scripts/add_web_user.py.
    """
    salt = salt or hashlib.sha256(str(time.time_ns()).encode()).digest()[:16]
    derived = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return f"{salt.hex()}.{derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, expected_hex = stored_hash.split(".", 1)
    except ValueError:
        raise ValueError(
            f"Ungültiges Hash-Format in GEOFACT_WEB_USERS (erwartet 'salt.hash'): '{stored_hash}'"
        ) from None
    salt = bytes.fromhex(salt_hex)
    derived = hashlib.pbkdf2_hmac(
        _PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(derived.hex(), expected_hex)


def _sign(payload: bytes, secret_key: str) -> str:
    return hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def create_token(username: str, settings: Settings) -> str:
    body = json.dumps(
        {"sub": username, "exp": time.time() + settings.token_ttl_s}
    ).encode("utf-8")
    body_b64 = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    signature = _sign(body_b64.encode("ascii"), settings.secret_key)
    return f"{body_b64}.{signature}"


@dataclass(frozen=True)
class AuthenticatedUser:
    username: str


def _decode_token(token: str, settings: Settings) -> AuthenticatedUser:
    try:
        body_b64, signature = token.split(".", 1)
    except ValueError:
        raise HTTPException(
            status_code=401, detail="Ungültiges Token-Format."
        ) from None

    expected_signature = _sign(body_b64.encode("ascii"), settings.secret_key)
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=401, detail="Token-Signatur ungültig.")

    padding = "=" * (-len(body_b64) % 4)
    try:
        body = json.loads(base64.urlsafe_b64decode(body_b64 + padding))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(
            status_code=401, detail="Token-Inhalt nicht lesbar."
        ) from None

    if body.get("exp", 0) < time.time():
        raise HTTPException(
            status_code=401, detail="Token abgelaufen, bitte erneut anmelden."
        )

    username = body.get("sub")
    if not username or username not in settings.users:
        raise HTTPException(
            status_code=401, detail="Token gehört zu keinem bekannten Nutzer."
        )

    return AuthenticatedUser(username=username)


_bearer_scheme = HTTPBearer(auto_error=False)


def require_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """FastAPI-Dependency für jede zu schützende Route.

    Ohne konfigurierte Nutzer läuft alles als Pseudo-Nutzer 'anonymous' (lokale
    Demo ohne Login), sonst ist ein Token Pflicht. Das ?token=-Fallback muss mit
    require_auth_middleware übereinstimmen, da beide das Token unabhängig prüfen.
    """
    if not settings.auth_configured:
        return AuthenticatedUser(username="anonymous")
    token = (
        credentials.credentials
        if credentials is not None
        else request.query_params.get("token")
    )
    if not token:
        raise HTTPException(status_code=401, detail="Anmeldung erforderlich.")
    return _decode_token(token, settings)


async def require_auth_middleware(request: Request, call_next):
    """App-weites Gate (FA24) zusätzlich zu require_user, damit kein Endpunkt, auch
    kein später hinzugefügter, versehentlich ungeschützt bleibt.

    Nur für /api/*; /docs und /openapi.json bleiben erreichbar.
    """
    settings = get_settings()
    path = request.url.path
    if (
        not settings.auth_configured
        or not requires_token(path)
        # CORS-Preflights tragen nie einen Authorization-Header; mit 401 würde der
        # Browser die eigentliche Anfrage gar nicht erst senden.
        or request.method == "OPTIONS"
    ):
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[len("bearer ") :]
    elif accepts_query_token(path):
        token = request.query_params.get("token", "")
    else:
        token = ""

    if not token:
        return JSONResponse(
            status_code=401, content={"detail": "Anmeldung erforderlich."}
        )
    try:
        _decode_token(token, settings)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)
