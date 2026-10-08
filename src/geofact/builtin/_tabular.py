"""Implements: FA14 (Spalten- und Geometrie-Mapping), FA37 (Geometrie optional).

Gemeinsame Bausteine tabellarischer Quellen: ``GeometryMapping`` (wie aus
Tabellenspalten eine Geometrie wird) und ``ColumnSpec`` (Spaltenauswahl mit
Umbenennung und Zieltyp). Sie gehören weder zum Kern noch zu genau einem
Konnektor: der Tabellen-Konnektor (``sources/table.py``) und der REST-Client
(``_rest_client.py``) teilen sie. Ein Modul mit führendem Unterstrich wird
nicht als Baustein gescannt und registriert nichts."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GeometryMapping(BaseModel):
    """Deklariert, wie aus Tabellenspalten eine Geometrie wird.
    Entweder x_column+y_column (Punkte) ODER wkt_column."""

    model_config = ConfigDict(extra="forbid")

    x_column: Optional[str] = None
    y_column: Optional[str] = None
    wkt_column: Optional[str] = None
    crs: str = "EPSG:4326"

    @model_validator(mode="after")
    def xy_or_wkt(self) -> "GeometryMapping":
        has_xy = self.x_column is not None and self.y_column is not None
        has_wkt = self.wkt_column is not None
        if has_xy == has_wkt:  # beides oder keins
            raise ValueError(
                "GeometryMapping: entweder x_column+y_column ODER wkt_column angeben"
            )
        return self


class ColumnSpec(BaseModel):
    """Spaltenauswahl: Umbenennung + Zieltyp. Nicht gelistete Spalten
    werden verworfen ('Welche Spalten bedeuten was?')."""

    as_: Optional[str] = Field(None, alias="as")
    type: Literal["integer", "float", "string"] = "string"

    model_config = ConfigDict(populate_by_name=True, extra="forbid")
