"""Implements: FA60 (Sammelknoten ``collect``, Operationsteil).

Führt beliebig viele Vektor-Layer zeilenweise in einen Layer zusammen, etwa die
Ergebnisse einer Instanz mit ``foreach`` (FA75). Der Selektor
``instanz[*].export`` liefert die Eingänge (Portname = Schlüssel des
Elements); ein ausgeschriebenes ``inputs``-Mapping geht ebenso:

    steps:
      - id: alle_staedte
        op: collect
        inputs: "stadt[*].anteil"     # = {leipzig: stadt[leipzig].anteil, ...}
        params: {source_field: stadt_id}

Der Vertrag steht wie bei ``op: rest`` (FA41) in der Konfiguration: jeder
Eintrag in ``inputs`` ist ein Vektor-Eingang (``input_ports()``), mindestens
einer ist Pflicht. Semantik wie ``vector_union`` (FA19) für n Eingänge, in
Reihenfolge der Konfiguration. ``source_field`` (Standard ``source``, ``null`` =
keine Spalte) trägt den Portnamen. Verschiedene CRS und eine schon vorhandene
``source_field``-Spalte sind Fehler. Die Geometriefamilie wird anders als bei
vector_union nicht verglichen: die Kopien derselben Pipeline sind gleichartig.
"""

from __future__ import annotations

import re
from typing import ClassVar, Literal, Optional, Self

import geopandas as gpd
import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field, field_validator, model_validator

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CollectStep(Step):
    """Führt die Vektor-Layer aller Eingänge verlustfrei zu einem Layer
    zusammen (FA60). Jeder Eintrag in ``inputs`` ist ein Eingang vom Typ
    Vektor (beliebige Portnamen, mindestens einer). Zeilenzahl = Summe der
    Eingänge, Spaltenunion wie vector_union, Spalte ``source_field`` mit dem
    Portnamen; gleiches CRS ist Pflicht, leere Eingänge sind erlaubt."""

    op: Literal["collect"] = "collect"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        source_field: Optional[str] = Field(
            "source",
            description="Spalte mit dem Portnamen der Herkunft je Zeile; null = keine Spalte.",
        )

        @field_validator("source_field")
        @classmethod
        def check_identifier(cls, value: Optional[str]) -> Optional[str]:
            if value is not None and not _IDENTIFIER.match(value):
                raise ValueError(
                    f"source_field '{value}' ist kein gültiger Spaltenname "
                    "(Buchstaben, Ziffern, Unterstrich; nicht mit Ziffer beginnend)"
                )
            return value

    def input_ports(self) -> dict[str, DataType]:
        return {port: DataType.VECTOR for port in self.inputs}

    @model_validator(mode="after")
    def check_inputs(self) -> Self:
        if not self.inputs:
            raise ValueError(
                f"Schritt '{self.id}' (collect): braucht mindestens einen Eingang "
                "(inputs: {name: quelle, ...})"
            )
        return self


def _with_geometry_column(gdf: GeoDataFrame) -> GeoDataFrame:
    """Gemeinsamer Geometriespaltenname ``geometry`` für die Union."""
    if gdf.geometry.name != "geometry":
        return gdf.rename_geometry("geometry")
    return gdf


@register_operation(CollectStep)
def run_collect(inputs: dict[str, GeoDataFrame], params: dict) -> GeoDataFrame:
    """Postbedingung: len(result) == Summe der Eingänge (verlustfrei)."""
    source_field: Optional[str] = params.get("source_field", "source")

    crs_of = {port: gdf.crs for port, gdf in inputs.items() if gdf.crs is not None}
    distinct = {crs for crs in crs_of.values()}
    if len(distinct) > 1:
        listing = ", ".join(
            f"{port}: {crs.to_string()}" for port, crs in crs_of.items()
        )
        raise ValueError(
            f"collect: Eingänge haben verschiedene CRS ({listing}) - collect reprojiziert "
            "nicht stillschweigend; innerhalb eines Laufs liegt jeder Layer im Arbeits-CRS (FA5)"
        )
    crs = next(iter(crs_of.values()), None)

    if source_field is not None:
        for port, gdf in inputs.items():
            if source_field in gdf.columns:
                raise ValueError(
                    f"collect: Eingang '{port}' hat bereits eine Spalte '{source_field}' - "
                    "anderen source_field wählen oder source_field: null setzen"
                )

    frames = {port: _with_geometry_column(gdf) for port, gdf in inputs.items()}
    columns = list(dict.fromkeys(c for gdf in frames.values() for c in gdf.columns))
    if source_field is not None:
        columns = [c for c in columns if c != "geometry"] + [source_field, "geometry"]

    parts = []
    for port, gdf in frames.items():
        if gdf.empty:
            continue
        part = pd.DataFrame(gdf)
        if source_field is not None:
            part[source_field] = port
        parts.append(part)

    if not parts:
        return gpd.GeoDataFrame(columns=columns, geometry="geometry", crs=crs)

    combined = pd.concat(parts, ignore_index=True, sort=False).reindex(columns=columns)
    result = gpd.GeoDataFrame(combined, geometry="geometry", crs=crs)

    expected = sum(len(gdf) for gdf in inputs.values())
    assert len(result) == expected, (
        "collect: Zeilenzahl der Ausgabe weicht von der Summe der Eingänge ab - "
        "verlustfreie Kernpostbedingung verletzt"
    )
    return result
