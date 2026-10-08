"""Implements: FA14 (Tabellenformate des Tabellen-Konnektors), FA39 (feste Spaltenbreite), FA40.

Die mitgelieferten Tabellenformate csv, xlsx, sqlite, sql und fwf. Eine
Lesefunktion liefert einen DataFrame (alle Zellen als String); das Spalten-
und Geometrie-Mapping sowie der Dialekt-Cast gehören dem Konnektor
(builtin/sources/table.py) und gelten für jedes Format gleich.

Excel (.xlsx) wird über openpyxl gelesen; Zellen kommen wie bei CSV als String
(dtype=str), damit derselbe Cast-Pfad greift. Legacy-.xls wird nicht
unterstützt (openpyxl kann es nicht lesen, xlrd wäre eine zusätzliche
Abhängigkeit); der Konnektor meldet einen Fehler mit Konvertierungshinweis.
SQL-Dumps werden in eine temporäre SQLite-Datei eingespielt.

FA40: Ein neues Format ist eine neue Datei mit @register_table_format, deren
Lesefunktion einen DataFrame liefert.

Formatspezifische Felder (FA2): ``table``/``query`` (sql, sqlite), ``sheet``
(xlsx), ``colspecs``/``names``/``skiprows`` (fwf) sind die ``options_model``-Klassen
der Registrierungen unten; sie stehen in der YAML flach neben ``path`` und
``format`` und werden aus ``layer.options`` gelesen. csv hat keine eigenen Felder
(Kodierung und Trennzeichen gehören dem Konnektor)."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Self

import openpyxl
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geofact.plugin_api import register_table_format

if TYPE_CHECKING:
    from geofact.builtin.sources.table import TableLayer


class SqlOptions(BaseModel):
    """Felder der SQL-Formate (sql, sqlite): Tabellenname oder Abfrage."""

    model_config = ConfigDict(extra="forbid")

    table: Optional[str] = Field(None, description="Tabellenname (alternativ: query).")
    query: Optional[str] = Field(None, description="SQL-Abfrage (alternativ: table).")

    @model_validator(mode="after")
    def needs_table_or_query(self) -> Self:
        if not (self.table or self.query):
            raise ValueError("SQL/SQLite-Quelle braucht 'table' oder 'query'")
        return self


class XlsxOptions(BaseModel):
    """Felder des Excel-Formats."""

    model_config = ConfigDict(extra="forbid")

    sheet: Optional[str] = Field(
        None,
        description="Blattname; nötig, sobald die Arbeitsmappe mehr als ein Blatt enthält.",
    )


class FwfOptions(BaseModel):
    """Felder des Formats mit fester Spaltenbreite (FA39). Bewusst explizit
    statt geraten - die automatische Erkennung von pandas.read_fwf liefert bei
    realen Behördendateien (DWD-Stationsliste) kaputte Spalten, und ein
    falsch geratener Schnitt fällt erst später als unsinniger Wert auf."""

    model_config = ConfigDict(extra="forbid")

    colspecs: list[tuple[int, int]] = Field(
        ...,
        min_length=1,
        description="Spaltengrenzen [(start, ende), ...], halboffen; werden bewusst nicht geraten.",
    )
    names: list[str] = Field(..., min_length=1, description="Spaltennamen zu colspecs.")
    skiprows: int = Field(
        0, ge=0, description="Anzahl der zu überspringenden Kopfzeilen."
    )

    @model_validator(mode="after")
    def check_columns(self) -> Self:
        if len(self.colspecs) != len(self.names):
            raise ValueError(
                f"colspecs ({len(self.colspecs)}) und names ({len(self.names)}) "
                "müssen gleich lang sein"
            )
        for start, end in self.colspecs:
            if start < 0 or end <= start:
                raise ValueError(
                    f"ungültige Spaltengrenze ({start}, {end}) - erwartet wird 0 <= start < ende"
                )
        return self


@register_table_format(
    "sql",
    extensions=("sql",),
    options_model=SqlOptions,
    description="SQL-Dump (über temporäre SQLite)",
)
def _read_sql_dump(layer: TableLayer, path: Path) -> pd.DataFrame:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        conn = sqlite3.connect(tmp_path)
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.commit()
        conn.close()
        return _read_sql_source(layer, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


@register_table_format(
    "fwf",
    extensions=(),
    options_model=FwfOptions,
    description="Feste Spaltenbreite (nur explizit, format: fwf)",
)
def _read_fwf(layer: TableLayer, path: Path) -> pd.DataFrame:
    """FA39: Textdatei mit fester Spaltenbreite. colspecs/names sind von
    FwfOptions garantiert (gleiche Länge, aufsteigende Grenzen)."""
    options = layer.options
    try:
        return pd.read_fwf(
            path,
            colspecs=[tuple(c) for c in options.colspecs],
            names=list(options.names),
            skiprows=options.skiprows,
            dtype=str,
            encoding=layer.encoding,
        )
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Layer '{layer.id}': Datei '{layer.path}' lässt sich nicht mit der "
            f"konfigurierten Kodierung '{layer.encoding}' lesen ({exc}) - encoding "
            "in der Konfiguration anpassen (z. B. 'windows-1252' oder 'latin-1')."
        ) from exc


@register_table_format(
    "csv", extensions=("csv",), description="CSV mit Dialekt-Angaben"
)
def _read_csv(layer: TableLayer, path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(
            path,
            dtype=str,
            encoding=layer.encoding,
            sep=layer.delimiter or ",",
            quotechar=layer.quotechar,
        )
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Layer '{layer.id}': Datei '{layer.path}' lässt sich nicht mit der "
            f"konfigurierten Kodierung '{layer.encoding}' lesen ({exc}) - encoding "
            "in der Konfiguration anpassen (z. B. 'windows-1252' oder 'latin-1')."
        ) from exc


@register_table_format(
    "xlsx",
    extensions=("xlsx",),
    options_model=XlsxOptions,
    description="Excel-Arbeitsmappe (.xlsx)",
)
def _read_xlsx(layer: TableLayer, path: Path) -> pd.DataFrame:
    sheet_names = _xlsx_sheet_names(path)
    requested = layer.options.sheet
    if requested is not None:
        if requested not in sheet_names:
            raise ValueError(
                f"Layer '{layer.id}': Blatt '{requested}' nicht in '{layer.path}' "
                f"gefunden. Vorhandene Blätter: {sheet_names}"
            )
        sheet = requested
    elif len(sheet_names) == 1:
        sheet = sheet_names[0]
    else:
        raise ValueError(
            f"Layer '{layer.id}': Excel-Arbeitsmappe '{layer.path}' enthält mehrere "
            f"Blätter: {sheet_names}. 'sheet' in der Konfiguration angeben, um "
            "eindeutig auszuwählen."
        )
    return pd.read_excel(path, dtype=str, sheet_name=sheet, engine="openpyxl")


def _xlsx_sheet_names(path: Path) -> list[str]:
    workbook = openpyxl.load_workbook(path, read_only=True)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


@register_table_format(
    "sqlite",
    extensions=("sqlite", "db"),
    options_model=SqlOptions,
    description="SQLite-Datenbank",
)
def _read_sql_source(layer: TableLayer, sqlite_path: Path) -> pd.DataFrame:
    options = layer.options
    conn = sqlite3.connect(sqlite_path)
    try:
        query = options.query if options.query else f"SELECT * FROM {options.table}"
        return pd.read_sql_query(query, conn, dtype=str)
    finally:
        conn.close()
