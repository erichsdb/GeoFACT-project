"""Implements: FA2 (strikte Konfigurationsverträge), FA40 (Formate mit eigenen Feldern).

Gemeinsame Grundlage der Konnektoren ``source: file`` und ``source: table``, deren
Layer je nach Dateiformat weitere Felder tragen (GeoPackage: ``layer``, SQL:
``table``/``query``, Excel: ``sheet`` ...). Die Felder stehen flach in der YAML,
gehören aber in die Registrierung des Formats (``options_model``), damit der
Konnektor keinen Formatnamen kennt. ``FormatOptionLayer`` nimmt sie als zusätzliche
Schlüssel an (``extra='allow'``) und prüft sie gegen das Optionsmodell des
Formats; ein unbekannter Schlüssel ist ein Fehler mit Position
(``layers -> 1 -> delimeter``), kein stillschweigend ignorierter Tippfehler.

Ist das Format erst beim Laden bekannt (Zip-Container, URL ohne Endung), prüft
die Validierung nur, dass der Schlüssel irgendeinem in Frage kommenden Format
gehört; die Werte prüft der Konnektor beim Laden (``bind_options``)."""

from __future__ import annotations

from typing import Any, ClassVar, Iterable, Mapping, Optional

from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError

from geofact.plugin_api import LayerBase
from geofact.core.errors import custom_line_error, line_errors, located_error


class FormatOptionLayer(LayerBase):
    """Layer, dessen formatspezifische Felder flach neben den gemeinsamen
    stehen. Nach der Prüfung sind sie als ``layer.options`` lesbar (Instanz
    des ``options_model`` des Formats, None bei einem Format ohne Optionen)."""

    model_config = ConfigDict(extra="allow")

    # Art der Registry-Tabelle, aus der die Formate dieser Quellart stammen
    # ("file_format", "table_format"). Der Katalog (engine/catalog.py) liest sie,
    # um die formatspezifischen Felder im JSON-Schema des Layers aufzuführen,
    # und braucht dafür keinen Quellart- oder Formatnamen zu kennen.
    FORMAT_KIND: ClassVar[Optional[str]] = None

    _options: Optional[BaseModel] = PrivateAttr(default=None)

    @property
    def options(self) -> Optional[BaseModel]:
        return self._options

    @property
    def format_extras(self) -> dict[str, Any]:
        """Die Schlüssel des Layers, die keines der gemeinsamen Felder sind."""
        return dict(self.model_extra or {})

    def bind_options(self, options: Optional[BaseModel]) -> None:
        """Hängt die geprüften Optionen an den Layer. Nur für die Phase, in
        der das Format erst beim Laden feststeht (Zip, URL ohne Endung)."""
        self._options = options


def _unknown(key: str, value: Any, owner: str, known: Iterable[str]) -> Any:
    known = sorted(known)
    tail = f"Formatfelder: {known}" if known else "das Format hat keine eigenen Felder"
    return custom_line_error(
        "unknown_option",
        f"unbekanntes Feld (kein Feld des Layers und kein Feld {owner}; {tail})",
        (key,),
        value,
    )


def validate_format_options(
    extras: Mapping[str, Any],
    model: Optional[type[BaseModel]],
    *,
    owner: str,
    context: Any = None,
) -> Optional[BaseModel]:
    """Prüft die zusätzlichen Schlüssel eines Layers gegen das Optionsmodell
    seines Formats. ``owner`` nennt das Format im Genitiv für die Meldung
    (``des Dateiformats 'gpkg'``). Wirft ein ``ValidationError`` mit Position
    (der Schlüssel), das Pydantic in der Validierung des Layers mit dessen
    Position übernimmt."""
    known = model.model_fields if model is not None else {}
    errors = [
        _unknown(key, value, owner, known)
        for key, value in extras.items()
        if key not in known
    ]
    if errors:
        raise located_error("Layer", errors)
    if model is None:
        return None
    try:
        return model.model_validate(dict(extras), context=context)
    except ValidationError as exc:
        raise located_error(exc.title, line_errors(exc)) from None


def check_option_names(
    extras: Mapping[str, Any], models: Iterable[type[BaseModel]], *, owner: str
) -> None:
    """Wie ``validate_format_options``, wenn das Format noch nicht feststeht:
    jeder Schlüssel muss wenigstens einem der in Frage kommenden Formate
    gehören; die Werte prüft der Konnektor beim Laden."""
    known = {name for model in models for name in model.model_fields}
    errors = [
        _unknown(key, value, owner, known)
        for key, value in extras.items()
        if key not in known
    ]
    if errors:
        raise located_error("Layer", errors)


def describe_errors(exc: ValidationError) -> str:
    """Einzeilige Beschreibung eines ValidationError für eine Meldung beim Laden."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'Layer'}: {error['msg']}"
        for error in exc.errors()
    )
