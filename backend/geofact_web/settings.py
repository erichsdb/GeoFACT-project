"""Konfiguration des Web-Backends über Umgebungsvariablen.

Alle Pfade sind lokal (Single-User-Demo). Nichts hiervon gehört in die
Szenario-YAML, es ist Laufzeitkonfiguration des Servers.
"""

from __future__ import annotations

import math
import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Anker für die .env, die Beispiele und den Standard von GEOFACT_WEB_BASE_DIR.
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_DIR = REPO_ROOT / "examples"

# Vor dem Lesen von os.environ laden; echte Umgebungsvariablen haben Vorrang.
load_dotenv(REPO_ROOT / ".env", override=False)


def _path(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or default).expanduser().resolve()


def _non_negative_int(env: str, default: str) -> int:
    raw = os.environ.get(env) or default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{env} muss eine Ganzzahl sein, nicht '{raw}'") from None
    if value < 0:
        raise ValueError(f"{env} muss >= 0 sein, nicht {value}")
    return value


def _positive_float(env: str, default: str) -> float:
    raw = os.environ.get(env) or default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{env} muss eine Zahl sein, nicht '{raw}'") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{env} muss > 0 sein, nicht '{raw}'")
    return value


@dataclass(frozen=True)
class Settings:
    # --- Ablageorte ---
    data_dir: Path = field(
        default_factory=lambda: _path("GEOFACT_WEB_DATA_DIR", ".geofact_web")
    )
    uploads_dir: Path = field(
        default_factory=lambda: _path("GEOFACT_WEB_UPLOADS_DIR", ".geofact_web/uploads")
    )
    # Lokale GeoTIFFs für den Copernicus-/Raster-Katalog.
    copernicus_dir: Path = field(
        default_factory=lambda: _path("GEOFACT_COPERNICUS_DIR", "examples/data")
    )
    # FA4/FA44: Basis für relative Pfade in Szenario-Texten ohne Ursprung (Editor, LLM,
    # gespeicherte Szenarien). Szenarien mit Kopfzeile "# geofact-origin: ..." gelten
    # gegen ihre YAML-Datei. Standard ist der Repo-Root, nicht das Arbeitsverzeichnis.
    base_dir: Path = field(
        default_factory=lambda: _path("GEOFACT_WEB_BASE_DIR", str(REPO_ROOT))
    )
    # Mitgelieferte Referenzszenarien (schreibgeschützt).
    examples_dir: Path = field(default_factory=lambda: EXAMPLES_DIR)

    # --- OSM ---
    # Der Kern liest GEOFACT_OSM_BACKEND / GEOFACT_PG_DSN selbst; hier nur gespiegelt.
    osm_backend: str = field(
        default_factory=lambda: os.environ.get("GEOFACT_OSM_BACKEND", "overpass")
    )
    # FA82: ist ein PostGIS-DSN gesetzt? Nur dann steht "postgis" zur Wahl.
    pg_dsn_set: bool = field(
        default_factory=lambda: bool(os.environ.get("GEOFACT_PG_DSN", "").strip())
    )

    # --- LLM (OpenAI-kompatibel, Default: OpenRouter) ---
    llm_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "OPENAI_BASE_URL", "https://openrouter.ai/api/v1"
        )
    )
    llm_api_key: str = field(
        default_factory=lambda: (
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or ""
        )
    )
    llm_model: str = field(
        default_factory=lambda: os.environ.get(
            "GEOFACT_LLM_MODEL", "anthropic/claude-3.5-sonnet"
        )
    )
    # FA31: Reasoning-Ausgabe mitstreamen; wirkt nur bei Modellen mit Reasoning-Tokens.
    llm_reasoning: bool = field(
        default_factory=lambda: (
            os.environ.get("GEOFACT_LLM_REASONING", "true").strip().lower()
            in ("1", "true", "yes", "on")
        )
    )
    # Obergrenze der Antwortlänge; ohne sie könnte ein Modell endlos generieren (NFA2).
    llm_max_tokens: int = field(
        default_factory=lambda: int(os.environ.get("GEOFACT_LLM_MAX_TOKENS", "8000"))
    )
    # FA31: Gesamtfrist je LLM-Anfrage in Sekunden; der httpx-Timeout gilt nur je
    # Leseoperation, ein tropfenweise antwortender Anbieter hielte die Anfrage sonst offen.
    llm_deadline_s: float = field(
        default_factory=lambda: float(os.environ.get("GEOFACT_LLM_DEADLINE_S", "300"))
    )
    # FA31: Anbieterbindung (kommagetrennt, in dieser Reihenfolge, ohne Ausweichen).
    # Leer = der Router wählt.
    llm_providers: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            p.strip()
            for p in os.environ.get("GEOFACT_LLM_PROVIDER", "").split(",")
            if p.strip()
        )
    )
    # FA83: weitere wählbare Sprachmodelle ("modell" oder "modell@anbieter1+anbieter2",
    # kommagetrennt; geparst in options.llm_choices). Leer = nur GEOFACT_LLM_MODEL.
    llm_choices_raw: str = field(
        default_factory=lambda: os.environ.get("GEOFACT_LLM_CHOICES", "")
    )
    # FA78: Reparaturrunden je Erzeugung (Prüf- und Laufzeitmeldungen, 0 = keine).
    llm_repair_rounds: int = field(
        default_factory=lambda: _non_negative_int("GEOFACT_LLM_REPAIR_ROUNDS", "3")
    )
    # FA78: gültige Konfiguration zur Probe ausführen (ohne Ausgaben), Meldungen gehen in die Reparatur.
    llm_trial_run: bool = field(
        default_factory=lambda: (
            os.environ.get("GEOFACT_LLM_TRIAL_RUN", "true").strip().lower()
            in ("1", "true", "yes", "on")
        )
    )
    # FA78: Frist des Probelaufs in Sekunden; danach gilt sie als ungeprüft (timeout).
    llm_trial_run_s: float = field(
        default_factory=lambda: _positive_float("GEOFACT_LLM_TRIAL_RUN_S", "60")
    )

    # --- CORS (lokaler Next.js-Dev-Server) ---
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in os.environ.get(
                "GEOFACT_WEB_CORS", "http://localhost:3000,http://127.0.0.1:3000"
            ).split(",")
            if o.strip()
        )
    )

    # Maximale Upload-Größe (Bytes).
    max_upload_bytes: int = field(
        default_factory=lambda: int(
            os.environ.get("GEOFACT_WEB_MAX_UPLOAD", str(200 * 1024 * 1024))
        )
    )

    # --- Zugriffskontrolle (FA24) ---
    # "name:hash"-Paare, kommagetrennt; Format siehe auth.hash_password.
    users_raw: str = field(
        default_factory=lambda: os.environ.get("GEOFACT_WEB_USERS", "")
    )
    # Signaturschlüssel der Tokens. Ohne Wert entsteht pro Prozessstart ein zufälliger
    # (Neustart entwertet alle Tokens); mehrere Worker brauchen einen geteilten Schlüssel.
    secret_key: str = field(
        default_factory=lambda: (
            os.environ.get("GEOFACT_WEB_SECRET_KEY") or secrets.token_hex(32)
        )
    )
    token_ttl_s: int = field(
        default_factory=lambda: int(
            os.environ.get("GEOFACT_WEB_TOKEN_TTL_S", str(12 * 3600))
        )
    )
    max_concurrent_runs_per_user: int = field(
        default_factory=lambda: int(
            os.environ.get("GEOFACT_WEB_MAX_CONCURRENT_RUNS_PER_USER", "2")
        )
    )

    @property
    def users(self) -> dict[str, str]:
        """Nutzername -> PBKDF2-Hash, geparst aus users_raw."""
        result: dict[str, str] = {}
        for entry in self.users_raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, _, pw_hash = entry.partition(":")
            if not name or not pw_hash:
                raise ValueError(
                    f"GEOFACT_WEB_USERS: Eintrag '{entry}' hat nicht das Format 'name:hash'"
                )
            result[name] = pw_hash
        return result

    @property
    def auth_configured(self) -> bool:
        return bool(self.users)

    @property
    def runs_dir(self) -> Path:
        """Ablage der Laufverzeichnisse (FA44), im Datenverzeichnis statt im System-Temp."""
        return self.data_dir / "runs"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
