"""FastAPI-App-Fabrik + Entry-Point für das GeoFACT-Web-Backend.

Start (lokal):
    uv run --extra web geofact-api
oder:
    uv run --extra web uvicorn geofact_web.main:app --reload
"""

from __future__ import annotations

import os
import socket
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from geofact import api

from . import __version__, openapi
from . import options as option_module
from .auth import require_auth_middleware
from .routers import auth as auth_router
from .routers import apis, catalog, config, meta, runs, scenarios, uploads
from .run_manager import manager
from .settings import EXAMPLES_DIR, get_settings


def ship_example_plugins() -> None:
    """FA94: Ohne GEOFACT_PLUGIN_PATH lädt die Web-Demo die mitgelieferten Beispiel-Plugins
    (examples/plugins), damit ihre Beispiele in der Oberfläche ausführbar sind. Eine gesetzte
    Variable gilt unverändert - wer eigene Plugin-Ordner angibt, nennt diesen bei Bedarf mit."""
    plugins = EXAMPLES_DIR / "plugins"
    if not os.environ.get(api.PLUGIN_PATH_ENV, "").strip() and plugins.is_dir():
        os.environ[api.PLUGIN_PATH_ENV] = str(plugins)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Erkennt beim Start einmal alle Erweiterungen (FA40); die Registry ist danach
    eingefroren und ohne Sperre lesbar. Räumt alte Laufverzeichnisse auf (FA44)."""
    ship_example_plugins()
    api.load_extensions()
    manager.purge_orphaned_run_dirs()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    # FA83: eine fehlerhafte GEOFACT_LLM_CHOICES scheitert schon beim Start.
    option_module.llm_choices(settings)
    app = FastAPI(
        title="GeoFACT Web API",
        version=__version__,
        description="Backend-API zur interaktiven Demonstration des GeoFACT-Frameworks.",
        openapi_tags=openapi.TAGS,
        lifespan=lifespan,
    )
    # FA24: Auth-Gate vor CORS registrieren; die zuletzt registrierte Middleware liegt
    # außen. CORS muss außen liegen, sonst trägt die 401 keine CORS-Header und
    # die Oberfläche erfährt bei einem abgelaufenen Token nichts davon.
    app.middleware("http")(require_auth_middleware)
    # Knotenergebnisse sind große, redundante GeoJSON-Antworten; kleine Antworten
    # (Status, SSE-Events) bleiben unkomprimiert.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router.router)
    app.include_router(meta.router)
    app.include_router(catalog.router)
    app.include_router(apis.router)
    app.include_router(uploads.router)
    app.include_router(config.router)
    app.include_router(runs.router)
    app.include_router(scenarios.router)
    # FA85: Security und 401 je geschütztem Pfad in der Spezifikation.
    openapi.install(app)
    return app


app = create_app()


def reload_dirs() -> list[str]:
    """Verzeichnisse, die uvicorn bei ``GEOFACT_WEB_RELOAD`` beobachtet (FA54): Kern und
    Backend. Sonst löst jede Änderung an tests/, examples/ oder docs/ einen Neustart
    aus und die Läufe im Prozessspeicher gingen verloren."""
    return [
        str(Path(api.__file__).resolve().parent),
        str(Path(__file__).resolve().parent),
    ]


def warn_if_port_in_use(host: str, port: int) -> None:
    """Warnt, wenn auf dem Port schon ein Prozess lauscht (FA54).

    Unter Windows kann ein zweites Backend an 127.0.0.1:PORT binden, während das
    erste an 0.0.0.0:PORT lauscht; Anfragen verteilen sich, und ein Prozess kennt die
    Läufe des anderen nicht. Best effort per Verbindungsversuch."""
    candidates = ["127.0.0.1", "::1"]
    if host not in ("", "0.0.0.0", "::") and host not in candidates:
        candidates.append(host)
    for address in candidates:
        try:
            with socket.create_connection((address, port), timeout=0.25):
                pass
        except OSError:
            continue  # niemand lauscht (oder nicht erreichbar): keine Warnung
        warnings.warn(
            f"Auf Port {port} lauscht bereits ein anderer Prozess ({address}). Ein zweites "
            "Backend teilt seine Läufe nicht mit dem ersten: Anfragen landen mal bei dem "
            "einen, mal bei dem anderen Prozess ('Lauf nicht gefunden'). Das alte Backend "
            "beenden oder GEOFACT_WEB_PORT ändern.",
            RuntimeWarning,
            stacklevel=2,
        )
        return


def run() -> None:
    """Console-Script-Entry-Point (geofact-api)."""
    import uvicorn

    settings = get_settings()
    host = os.environ.get("GEOFACT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("GEOFACT_WEB_PORT", "8000"))
    reload = bool(os.environ.get("GEOFACT_WEB_RELOAD"))
    warn_if_port_in_use(host, port)
    uvicorn.run(
        "geofact_web.main:app",
        host=host,
        port=port,
        reload=reload,
        reload_dirs=reload_dirs() if reload else None,
    )
    _ = settings  # nur um ensure_dirs() garantiert einmal auszuführen


if __name__ == "__main__":
    run()
