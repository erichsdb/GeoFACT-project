"""Auswahl von OSM-Quelle und Sprachmodell je Anfrage.

Implements: FA82 (OSM-Quelle je Lauf) und FA83 (Sprachmodell aus einer Vorauswahl,
``GEOFACT_LLM_CHOICES``). Beide lassen nur zu, was der Server anbietet, nie einen
frei eingegebenen Namen.
"""

from __future__ import annotations

from dataclasses import dataclass

from geofact import api

from .settings import Settings


def osm_backends(settings: Settings) -> list[str]:
    """Die wählbaren OSM-Backends: jedes des Kerns, ``postgis`` nur mit gesetztem
    ``GEOFACT_PG_DSN``. Das Backend des Servers steht immer dabei und zuerst."""
    names = [
        name
        for name in api.osm_backend_names()
        if name != "postgis" or settings.pg_dsn_set
    ]
    if settings.osm_backend in names:
        names.remove(settings.osm_backend)
    return [settings.osm_backend, *names]


def osm_connector_options(
    settings: Settings, name: str | None
) -> dict[str, str] | None:
    """Konnektor-Optionen für ``api.run``: None ohne Wahl (dann gilt die Umgebungsvariable),
    sonst die Option für ``name``. Ein Name außerhalb von ``osm_backends`` ist ein ValueError."""
    if name is None:
        return None
    available = osm_backends(settings)
    if name not in available:
        raise ValueError(
            f"OSM-Quelle '{name}' steht nicht zur Wahl, verfügbar: {available}"
        )
    return api.osm_backend_option(name)


@dataclass(frozen=True)
class LLMChoice:
    """Ein wählbares Sprachmodell mit seiner Anbieterbindung (leer = der
    Router wählt). ``id`` ist die Schreibweise der Umgebungsvariable."""

    id: str
    model: str
    providers: tuple[str, ...] = ()


def _choice(model: str, providers: tuple[str, ...]) -> LLMChoice:
    return LLMChoice(
        id=model + ("@" + "+".join(providers) if providers else ""),
        model=model,
        providers=providers,
    )


def llm_choices(settings: Settings) -> list[LLMChoice]:
    """Die wählbaren Sprachmodelle: zuerst das des Servers, danach die Einträge aus
    ``GEOFACT_LLM_CHOICES`` (``modell`` oder ``modell@anbieter1+anbieter2``). Doppelte
    fallen weg; ein Eintrag ohne Modellnamen ist ein ValueError (Regel 7)."""
    choices = [_choice(settings.llm_model, settings.llm_providers)]
    for raw in settings.llm_choices_raw.split(","):
        entry = raw.strip()
        if not entry:
            continue
        model, _, provider_text = entry.partition("@")
        model = model.strip()
        providers = tuple(p.strip() for p in provider_text.split("+") if p.strip())
        if not model or ("@" in entry and not providers):
            raise ValueError(
                f"GEOFACT_LLM_CHOICES: Eintrag '{entry}' hat nicht die Form 'modell' oder "
                "'modell@anbieter1+anbieter2'"
            )
        choice = _choice(model, providers)
        if all(choice.id != known.id for known in choices):
            choices.append(choice)
    return choices


def llm_choice(settings: Settings, choice_id: str) -> LLMChoice:
    """Die Wahl ``choice_id`` aus der Vorauswahl, sonst ValueError: der Schlüssel des
    Servers darf nie für ein nicht freigegebenes Modell dienen."""
    choices = llm_choices(settings)
    for choice in choices:
        if choice.id == choice_id:
            return choice
    raise ValueError(
        f"Sprachmodell '{choice_id}' steht nicht zur Wahl, verfügbar: {[c.id for c in choices]}"
    )
