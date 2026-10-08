"""Implements: FA8 (räumliche Operationen: dissolve).

Vereinigt Geometrien (alle oder je Gruppenwert). Ohne ``by`` entsteht eine Zeile
je zusammenhängender Teilgeometrie (Spalte ``id``), nicht eine einzige
Gesamtgeometrie; so bleiben Teilflächen und gemischte Typen einzeln
weiterverarbeitbar."""

from __future__ import annotations

from typing import ClassVar, Literal, Optional

import geopandas as gpd
from geopandas import GeoDataFrame
from pydantic import Field

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


def _flatten_geometries(geom) -> list:
    """Zerlegt (Multi-)Geometrien und GeometryCollections in Einzelgeometrien.
    union_all() über gemischte Typen liefert eine GeometryCollection, mit der
    buffer/overlay nichts anfangen können."""
    if geom.geom_type.startswith("Multi") or geom.geom_type == "GeometryCollection":
        result = []
        for part in geom.geoms:
            result.extend(_flatten_geometries(part))
        return result
    return [geom]


class DissolveStep(Step):
    """Vereinigt die Geometrien von 'features' überlappungsfrei (FA8-
    Erweiterung, Grundlage für überlappungsfreie Distanzringe, Szenario 3).
    Ohne 'by': EINE ZEILE JE ZUSAMMENHAENGENDER TEILFLAECHE der Vereinigung
    (getrennte Flächen bleiben getrennte Zeilen, Spalte 'id'; Attribute der
    Eingabe entfallen) - KEINE einzelne Gesamtzeile. Mit 'by': eine Zeile je
    Gruppenwert (Mehrfachgeometrie). Für EINEN Gesamtwert über alle
    Teilflächen danach aggregate (zones = Region-Layer) oder zonal_stats
    auf der Region statt auf den Teilflächen verwenden."""

    op: Literal["dissolve"] = "dissolve"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        by: Optional[str] = Field(
            None,
            description=(
                "Attribut zum Gruppieren (eine Zeile je Wert); ohne Angabe werden alle "
                "Geometrien vereinigt und je zusammenhängender Teilfläche eine Zeile "
                "ausgegeben."
            ),
        )


@register_operation(DissolveStep)
def run_dissolve(inputs: dict, params: dict) -> GeoDataFrame:
    features: GeoDataFrame = inputs["features"]
    by = params.get("by")

    if features.empty:
        return features.copy()

    if by is not None:
        return features.dissolve(by=by).reset_index()

    unioned = features.union_all()
    geometries = _flatten_geometries(unioned)
    return gpd.GeoDataFrame(
        {"id": range(len(geometries))}, geometry=geometries, crs=features.crs
    )
