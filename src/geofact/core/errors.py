"""Implements: FA40 (Plugin-Fehler), FA56 (gemeinsame Kategorie RegionWarning), Grundlage für den Executor-Vertrag.

Gemeinsame Fehlertypen des Kerns. Jeder Fehler nennt Position und Kontext
(Goldene Regel 7): welcher Schritt, welcher Layer, welche Plugin-Datei."""

from __future__ import annotations

from typing import Any, Sequence

from pydantic import ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError


class ConfigError(ValueError):
    """Ungültige Szenario-Konfiguration. issues ist eine Liste von
    (Position, Meldung), z. B. ("steps -> 3 -> params -> radius_km", "...")."""

    def __init__(
        self, issues: Sequence[tuple[str, str]], message: str | None = None
    ) -> None:
        self.issues: list[tuple[str, str]] = list(issues)
        lines = [f"  [{location}] {text}" for location, text in self.issues]
        super().__init__(message or "Konfiguration ungültig:\n" + "\n".join(lines))


class PluginError(RuntimeError):
    """Ein Baustein konnte nicht geladen oder registriert werden."""


class PluginLoadWarning(UserWarning):
    """Ein Plugin wurde wegen eines Fehlers übersprungen (siehe
    Registry.failed)."""


class RegionWarning(UserWarning):
    """Regionswarnung (FA56), EINE Kategorie für alle Ebenen: Spannweite oder
    OSM-Klasse der Szenario-Region (engine/region.py), ein Layer ohne
    Überdeckung der Region (engine/loading.py), eine Region teilweise
    außerhalb des PostGIS-Imports (builtin/sources/osm.py). Steht in core,
    damit Engine und Bausteine dieselbe Klasse nutzen (Schichtenregel FA45)."""


class OutputSkipped(Exception):
    """Ein Ausgabeformat kann ein konkretes Ergebnis nicht schreiben
    (z. B. RDF ohne CRS). write_outputs() meldet die Ausgabe dann als
    übersprungen, statt den gesamten Export abzubrechen."""


class StepExecutionError(RuntimeError):
    """Eine Operation ist im Lauf gescheitert. Nennt Schritt, Operation und
    Herkunft des Bausteins; die Ursache hängt per ``from exc`` daran."""

    def __init__(
        self, step_id: str, op: str, cause: BaseException, origin: str = ""
    ) -> None:
        self.step_id = step_id
        self.op = op
        self.origin = origin
        self.cause = cause
        where = f"{op}, {origin}" if origin else op
        super().__init__(
            f"Schritt '{step_id}' ({where}): {type(cause).__name__}: {cause}"
        )


class LayerLoadError(RuntimeError):
    """Ein Layer konnte nicht geladen werden. Nennt Layer-id und Quellart;
    die Ursache hängt per ``from exc`` daran."""

    def __init__(self, layer_id: str, source: str, cause: BaseException) -> None:
        self.layer_id = layer_id
        self.source = source
        self.cause = cause
        super().__init__(
            f"Layer '{layer_id}' (source: {source}): {type(cause).__name__}: {cause}"
        )


class RunCancelled(Exception):
    """Kooperativer Abbruch eines Laufs an einer Schritt-/Layer-Grenze."""


# --- Fehler mit Position (FA2): Pydantic-Fehler umsetzen und neu aufbauen ---


def _line_error(error: dict[str, Any], loc: tuple[Any, ...]) -> InitErrorDetails:
    detail: dict[str, Any] = {
        "type": error["type"],
        "loc": loc,
        "input": error.get("input"),
    }
    if error.get("ctx"):
        detail["ctx"] = error["ctx"]
    try:
        ValidationError.from_exception_data("probe", [detail])  # type: ignore[list-item]
    except (KeyError, ValueError, TypeError):
        # Fehlerart, die Pydantic nicht kennt (eigene Art eines Bausteins):
        # der fertige Text bleibt erhalten.
        detail = {
            "type": PydanticCustomError(str(error["type"]), str(error["msg"])),
            "loc": loc,
            "input": error.get("input"),
        }
    return detail  # type: ignore[return-value]


def custom_line_error(
    kind: str, message: str, loc: tuple[Any, ...], value: Any = None
) -> InitErrorDetails:
    """Ein eigener Fehler (z. B. unbekannter Baustein) an einer Position."""
    return {  # type: ignore[return-value]
        "type": PydanticCustomError(kind, message),
        "loc": loc,
        "input": value,
    }


def line_errors(
    exc: ValidationError, prefix: tuple[Any, ...] = ()
) -> list[InitErrorDetails]:
    """Die Einzelfehler von ``exc`` mit vorangestellter Position (``prefix``);
    Art und Kontext bleiben für den Formatierer (engine/run.py) erhalten."""
    return [_line_error(error, (*prefix, *error["loc"])) for error in exc.errors()]


def located_error(title: str, errors: Sequence[InitErrorDetails]) -> ValidationError:
    """Ein ValidationError aus Einzelfehlern mit eigener Position; in einem
    Pydantic-Validator geworfen, übernimmt Pydantic alle Positionen."""
    return ValidationError.from_exception_data(title, list(errors))


# --- Deutsche Meldungstexte der häufigen Pydantic-Fehlerarten ---

_SHOWN_INPUT_MAX = 60


def _shown(value: Any) -> str:
    text = repr(value)
    return (
        text if len(text) <= _SHOWN_INPUT_MAX else text[: _SHOWN_INPUT_MAX - 3] + "..."
    )


def _number(value: Any) -> Any:
    """Grenzwert ohne Nachkommastelle, wenn er ganzzahlig ist (0 statt 0.0)."""
    return int(value) if isinstance(value, float) and value.is_integer() else value


def german_message(error: dict[str, Any]) -> str:
    """Eine Pydantic-Fehlermeldung (Eintrag aus ``ValidationError.errors()``) auf
    Deutsch, sonst der Pydantic-Text. Im Kern, damit ``Scenario.expand_macros``
    (FA75) denselben Text bildet wie engine/run.py."""
    kind = error.get("type", "")
    ctx = error.get("ctx") or {}
    given = f" (erhalten: {_shown(error['input'])})" if "input" in error else ""
    if kind in ("value_error", "assertion_error"):
        inner = ctx.get("error")
        return str(inner) if inner is not None else str(error.get("msg", ""))
    if kind == "missing":
        return "Pflichtfeld fehlt"
    if kind == "extra_forbidden":
        return "unbekanntes Feld (hier nicht erlaubt; Tippfehler?)"
    if kind == "literal_error":
        return f"ungültiger Wert, erlaubt sind: {ctx.get('expected', '?')}{given}"
    if kind == "greater_than":
        return f"muss größer als {_number(ctx.get('gt'))} sein{given}"
    if kind == "greater_than_equal":
        return f"muss mindestens {_number(ctx.get('ge'))} sein{given}"
    if kind == "less_than":
        return f"muss kleiner als {_number(ctx.get('lt'))} sein{given}"
    if kind == "less_than_equal":
        return f"darf höchstens {_number(ctx.get('le'))} sein{given}"
    if kind in ("float_parsing", "float_type", "decimal_parsing", "decimal_type"):
        return f"muss eine Zahl sein{given}"
    if kind in ("int_parsing", "int_type", "int_from_float", "int_parsing_size"):
        return f"muss eine ganze Zahl sein{given}"
    if kind in ("bool_parsing", "bool_type"):
        return f"muss true oder false sein{given}"
    if kind == "string_type":
        return f"muss ein Text sein{given}"
    if kind == "list_type":
        return f"muss eine Liste sein{given}"
    if kind in ("dict_type", "model_type", "model_attributes_type"):
        return f"muss ein Mapping (Schlüssel: Wert) sein{given}"
    if kind == "too_short":
        minimum = ctx.get("min_length", 1)
        noun = "Eintrag" if minimum == 1 else "Einträge"
        return f"muss mindestens {minimum} {noun} enthalten (hat {ctx.get('actual_length', 0)})"
    return str(error.get("msg", kind))
