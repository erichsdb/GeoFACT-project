"""Implements: FA34 (nicht-räumlicher Attribut-Join).

attribute_join verbindet zwei Vektor-Layer über einen gemeinsamen Attributwert
statt über Geometrie, als Gegenpart zu spatial_join (FA8). Viele amtliche
Statistiken liegen als Tabelle ohne Geometrie vor und tragen ihren Raumbezug nur
als Name oder Schlüssel (Bundesland, AGS); die Geometrie des Tabellen-Konnektors
(FA14) ist ein künstlicher Platzhalter. Behördliche Schreibweisen weichen oft ab
("Baden-Wuerttemberg" vs. "Baden-Württemberg", führende Nullen im AGS);
normalize=true macht den Vergleich robust, ohne die Ausgabewerte zu verändern.
"""

from __future__ import annotations

import unicodedata
import warnings
from typing import ClassVar, Literal, Optional

import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step

# Explizit statt nur per NFKD: 'ö' -> 'oe' ist die in deutschen Behördendaten
# übliche Ersatzschreibung, NFKD ergäbe 'o' und "Baden-Wuerttemberg" passte
# nicht auf "Baden-Württemberg".
_UMLAUT_FOLD = {
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "Ä": "ae",
    "Ö": "oe",
    "Ü": "ue",
    "ß": "ss",
}


def normalize_key(value: object) -> str:
    """Normalisierte Form eines Join-Schlüssels (FA34): Umlaute in ASCII-
    Ersatzschreibung, Diakritika entfernt, kleingeschrieben, ohne Leer- und
    Satzzeichen, ohne führende Nullen bei rein numerischen Schlüsseln.
    Nur für den Vergleich; die ausgegebenen Werte bleiben unverändert."""
    text = str(value).strip()
    for char, replacement in _UMLAUT_FOLD.items():
        text = text.replace(char, replacement)
    text = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )
    text = text.casefold()
    text = "".join(ch for ch in text if ch.isalnum())
    if text.isdigit():
        stripped = text.lstrip("0")
        return stripped or "0"
    return text


class AttributeJoinStep(Step):
    """Verbindet 'left' und 'right' über einen gemeinsamen Attributwert
    (nicht-räumlich, FA34). Die Geometrie von 'left' bleibt erhalten;
    aus 'right' kommen die Attribute hinzu (dessen Geometrie wird
    verworfen - bei tabellarischen Quellen ist sie ein künstlicher
    Platzhalter). Zeilenzahl und -reihenfolge von 'left' bleiben
    erhalten: nicht getroffene Objekte behalten leere Zusatzattribute
    statt herauszufallen (Goldene Regel 7 - eine Lücke bleibt sichtbar
    statt still zu verschwinden).

    left_on/right_on benennen die Schlüsselspalten; ist right_on nicht
    angegeben, gilt left_on für beide Seiten. normalize (Default true)
    vergleicht die Schlüssel unabhängig von Umlautschreibweise,
    Groß-/Kleinschreibung, Leer-/Satzzeichen und führenden Nullen -
    ohne die ausgegebenen Werte zu verändern. Mehrfachtreffer auf der
    rechten Seite sind ein Fehler (eine Statistiktabelle soll pro
    Gebiet genau eine Zeile haben), es sei denn on_duplicate=first."""

    op: Literal["attribute_join"] = "attribute_join"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "left": DataType.VECTOR,
        "right": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        left_on: str = Field(
            ..., description="Schluesselspalte im linken Layer (behält die Geometrie)."
        )
        right_on: Optional[str] = Field(
            None,
            description="Schluesselspalte im rechten Layer; ohne Angabe wird left_on verwendet.",
        )
        columns: Optional[list[str]] = Field(
            None,
            description="Zu übernehmende Attribute aus dem rechten Layer; "
            "ohne Angabe alle außer Geometrie und Schlüssel.",
        )
        normalize: bool = Field(
            True,
            description="Schlüssel vor dem Vergleich normalisieren "
            "(Umlaute, Groß-/Kleinschreibung, führende Nullen).",
        )
        on_duplicate: Literal["error", "first"] = Field(
            "error",
            description="Verhalten bei mehreren Treffern je Schlüssel im rechten Layer.",
        )


@register_operation(AttributeJoinStep)
def run_attribute_join(inputs: dict, params: dict) -> GeoDataFrame:
    left: GeoDataFrame = inputs["left"]
    right = inputs["right"]

    left_on = params["left_on"]
    right_on = params.get("right_on") or left_on
    normalize = params.get("normalize", True)
    on_duplicate = params.get("on_duplicate", "error")
    columns = params.get("columns")

    if left_on not in left.columns:
        raise ValueError(
            f"attribute_join: Schluesselspalte '{left_on}' fehlt im linken Layer "
            f"(vorhanden: {sorted(c for c in left.columns if c != 'geometry')})"
        )
    if right_on not in right.columns:
        raise ValueError(
            f"attribute_join: Schluesselspalte '{right_on}' fehlt im rechten Layer "
            f"(vorhanden: {sorted(c for c in right.columns if c != 'geometry')})"
        )

    # Übernommen werden nie die Geometrie und nicht der Schlüssel (steht schon links).
    available = [
        col
        for col in right.columns
        if col != "geometry" and col != right_on and col != right.geometry.name
    ]
    if columns is None:
        take = available
    else:
        missing = [col for col in columns if col not in right.columns]
        if missing:
            raise ValueError(
                f"attribute_join: Spalte(n) {missing} fehlen im rechten Layer "
                f"(vorhanden: {sorted(available)})"
            )
        take = list(columns)

    result = left.copy()
    if result.empty:
        # Zielspalten trotzdem anlegen, damit filter/top_n auf einer Join-Spalte
        # nicht an einer fehlenden Spalte scheitern.
        for col in take:
            if col not in result.columns:
                result[col] = pd.Series(dtype="object")
        return result

    right_frame = pd.DataFrame(right.drop(columns=right.geometry.name, errors="ignore"))
    left_keys = left[left_on].map(normalize_key) if normalize else left[left_on]
    right_keys = (
        right_frame[right_on].map(normalize_key) if normalize else right_frame[right_on]
    )

    lookup = right_frame[take].copy()
    lookup.index = pd.Index(right_keys, name="_join_key")

    duplicated = lookup.index.duplicated()
    if duplicated.any():
        examples = sorted({str(k) for k in lookup.index[duplicated]})[:3]
        if on_duplicate == "error":
            raise ValueError(
                f"attribute_join: der rechte Layer hat mehrere Zeilen je "
                f"Schlüssel '{right_on}' (z. B. {', '.join(examples)}). "
                f"Erwartet wird eine Zeile je Gebiet - entweder die Quelle "
                f"vorher verdichten (dissolve/aggregate) oder "
                f"on_duplicate=first setzen."
            )
        warnings.warn(
            f"attribute_join: {int(duplicated.sum())} mehrfach belegte(r) "
            f"Schlüssel im rechten Layer (z. B. {', '.join(examples)}); "
            f"je Schlüssel wird die erste Zeile verwendet (on_duplicate=first).",
            stacklevel=2,
        )
        lookup = lookup[~duplicated]

    joined = lookup.reindex(pd.Index(left_keys))
    for col in take:
        # Eine gleichnamige Spalte links wird nicht überschrieben.
        target = col if col not in result.columns else f"{col}_right"
        result[target] = joined[col].to_numpy()

    unmatched = int(joined[take[0]].isna().sum()) if take else 0
    if unmatched:
        missing_keys = sorted(
            {str(v) for v, ok in zip(left[left_on], joined[take[0]].notna()) if not ok}
        )[:3]
        warnings.warn(
            f"attribute_join: {unmatched} von {len(left)} Objekt(en) des linken "
            f"Layers haben keinen Treffer im rechten Layer (z. B. "
            f"{', '.join(missing_keys)}); ihre übernommenen Attribute bleiben "
            f"leer. Bei abweichenden Schreibweisen hilft normalize=true.",
            stacklevel=2,
        )
    return result
