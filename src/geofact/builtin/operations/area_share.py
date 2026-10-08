"""Implements: FA70 (Flächenanteil: area_share).

Schreibt je Zone den Anteil ihrer Fläche, der von ``features`` bedeckt ist -
z. B. den Grünflächenanteil je Stadtteil.

    Anteil = area(union(features) ∩ zone) / area(zone)

Bei ``dissolve: true`` (Default) werden überlappende Features je Zone vereinigt
und nur einmal gezählt, der Anteil ist nie > 1; das vermeidet die Doppelzählung
von ``aggregate`` mit ``sum`` über Flächen. ``dissolve: false`` summiert die
Schnittflächen einzeln (der Wert kann > 1 sein) und ist nur für bekannt
überlappungsfreie Features gedacht.

Vorbedingungen: projiziertes, gleiches CRS beider Eingänge; Zonen sind Flächen.
Nachbedingungen: Zone ohne Treffer -> 0.0; Zone mit Fläche 0 oder ohne Geometrie
-> NaN mit Warnung; Nicht-Flächen unter ``features`` werden mit einer Warnung
ausgelassen; ungültige Geometrien werden für die Berechnung per ``make_valid``
repariert und gemeldet; leere ``features`` -> alle Anteile 0.0; leere Zonen ->
leeres Ergebnis. Ergebnis = Kopie der Zonen plus ``output_field`` (und
``absolute_field`` mit der bedeckten Fläche in m2)."""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal, Optional

import numpy as np
import pandas as pd
import shapely
from geopandas import GeoDataFrame
from pydantic import Field, model_validator

from geofact.builtin.operations._geometry import (
    geometry_dimensions,
    require_new_column,
    require_projected_crs,
)
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class AreaShareStep(Step):
    """Flächenanteil je Zone (FA70); Vertrag siehe Modul-Docstring."""

    op: Literal["area_share"] = "area_share"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "zones": DataType.VECTOR,
        "features": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        output_field: str = Field(
            "area_share", min_length=1, description="Spalte mit dem Anteil (0..1)."
        )
        absolute_field: Optional[str] = Field(
            None,
            min_length=1,
            description="Wenn gesetzt: Spalte mit der bedeckten Fläche in m2.",
        )
        decimals: Optional[int] = Field(
            3, ge=0, le=12, strict=True, description="Nachkommastellen des Anteils."
        )
        dissolve: bool = Field(
            True,
            description="Überlappende Features je Zone einmal zählen (Anteil nie > 1).",
        )

        @model_validator(mode="after")
        def fields_differ(self) -> "AreaShareStep.Params":
            if (
                self.absolute_field is not None
                and self.absolute_field == self.output_field
            ):
                raise ValueError(
                    "absolute_field und output_field müssen verschieden sein"
                )
            return self

    def produced_fields(self) -> set[str]:
        fields = {self.params.get("output_field") or "area_share"}
        if self.params.get("absolute_field"):
            fields.add(self.params["absolute_field"])
        return fields


def _feature_polygons(features: GeoDataFrame) -> np.ndarray:
    """Flächen der Features als Shapely-Array; Nicht-Flächen werden mit einer
    Warnung ausgelassen, ungültige repariert (gemeldet)."""
    if features.empty:
        return np.empty(0, dtype=object)
    dims = geometry_dimensions(features).to_numpy()
    if (dims != 2).any():
        skipped = features.geometry[dims != 2]
        types = sorted(
            {
                "ohne Geometrie" if (geom is None or geom.is_empty) else geom.geom_type
                for geom in skipped
            }
        )
        warnings.warn(
            f"area_share: {int((dims != 2).sum())} Objekt(e) in 'features' sind keine Flächen "
            f"({', '.join(types)}) und werden ausgelassen",
            stacklevel=3,
        )
    polygons = features.geometry.to_numpy()[dims == 2]
    invalid = ~shapely.is_valid(polygons)
    if invalid.any():
        warnings.warn(
            f"area_share: {int(invalid.sum())} ungültige Fläche(n) in 'features' mit "
            "make_valid repariert",
            stacklevel=3,
        )
        polygons = polygons.copy()
        polygons[invalid] = shapely.make_valid(polygons[invalid])
    return polygons


@register_operation(AreaShareStep)
def run_area_share(inputs: dict, params: dict) -> GeoDataFrame:
    zones: GeoDataFrame = inputs["zones"]
    features: GeoDataFrame = inputs["features"]
    output_field = params.get("output_field") or "area_share"
    absolute_field = params.get("absolute_field")
    decimals = params.get("decimals")
    dissolve = params.get("dissolve", True)

    require_projected_crs(zones, op="area_share", port="zones")
    require_projected_crs(features, op="area_share", port="features")
    if zones.crs != features.crs:
        raise ValueError(
            f"area_share: 'zones' ({zones.crs.to_string()}) und 'features' "
            f"({features.crs.to_string()}) haben verschiedene CRS"
        )
    require_new_column(zones, output_field, op="area_share")
    if absolute_field:
        require_new_column(zones, absolute_field, op="area_share")

    result = zones.copy()
    if zones.empty:
        result[output_field] = pd.Series(dtype="float64", index=result.index)
        if absolute_field:
            result[absolute_field] = pd.Series(dtype="float64", index=result.index)
        return result

    zone_dims = geometry_dimensions(zones).to_numpy()
    wrong = (zone_dims != 2) & (zone_dims != -1)
    if wrong.any():
        types = sorted(set(zones.geometry[wrong].geom_type))
        raise ValueError(
            f"area_share: {int(wrong.sum())} Zone(n) sind keine Flächen ({', '.join(types)}) - "
            "ein Flächenanteil braucht Polygon-Zonen"
        )

    polygons = _feature_polygons(features)
    tree = shapely.STRtree(polygons) if len(polygons) else None
    zone_geoms = zones.geometry.to_numpy()
    present = ~(shapely.is_missing(zone_geoms) | shapely.is_empty(zone_geoms))
    invalid = present & ~shapely.is_valid(zone_geoms)
    if invalid.any():
        # selbstschneidende Zonen (häufig in OSM) brechen sonst mit einer
        # TopologyException ab
        warnings.warn(
            f"area_share: {int(invalid.sum())} ungültige Zone(n) für die Berechnung mit "
            "make_valid repariert (die Zonengeometrie im Ergebnis bleibt unverändert)",
            stacklevel=2,
        )
        zone_geoms = zone_geoms.copy()
        zone_geoms[invalid] = shapely.make_valid(zone_geoms[invalid])
    zone_area = np.zeros(len(zones), dtype="float64")
    covered = np.zeros(len(zones), dtype="float64")
    for index, zone in enumerate(zone_geoms):
        if zone is None or zone.is_empty:
            continue
        zone_area[index] = zone.area
        if tree is None:
            continue
        hits = tree.query(zone, predicate="intersects")
        if len(hits) == 0:
            continue
        pieces = shapely.intersection(polygons[hits], zone)
        if dissolve:
            covered[index] = shapely.union_all(pieces).area
        else:
            covered[index] = float(shapely.area(pieces).sum())

    degenerate = zone_area <= 0
    with np.errstate(divide="ignore", invalid="ignore"):
        share = np.where(
            degenerate, np.nan, covered / np.where(degenerate, 1.0, zone_area)
        )
    if dissolve:
        share = np.minimum(share, 1.0)  # nur Rundungsrauschen des Verschneidens
    if degenerate.any():
        warnings.warn(
            f"area_share: {int(degenerate.sum())} Zone(n) haben die Fläche 0 oder keine "
            f"Geometrie - '{output_field}' ist dort NaN",
            stacklevel=2,
        )
    if decimals is not None:
        share = np.round(share, decimals)
    result[output_field] = share
    if absolute_field:
        result[absolute_field] = covered
    return result
