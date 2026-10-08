"""Login-Endpunkt (FA24)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import auth
from ..openapi import errors
from ..settings import Settings, get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    expires_in_s: int


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Anmelden",
    description="Tauscht Nutzername und Passwort gegen ein Token mit Ablaufzeit. Nur nötig, "
    "wenn GEOFACT_WEB_USERS gesetzt ist; ohne konfigurierte Nutzer gibt es keine Anmeldung.",
    responses=errors({401: "Nutzername oder Passwort falsch."}),
)
def login(
    request: LoginRequest, settings: Settings = Depends(get_settings)
) -> LoginResponse:
    stored_hash = settings.users.get(request.username)
    if stored_hash is None or not auth.verify_password(request.password, stored_hash):
        # Gleiche Meldung für unbekannten Nutzer und falsches Passwort, damit ein
        # Nutzername nicht erraten werden kann.
        raise HTTPException(status_code=401, detail="Nutzername oder Passwort falsch.")
    token = auth.create_token(request.username, settings)
    return LoginResponse(
        token=token, username=request.username, expires_in_s=settings.token_ttl_s
    )
