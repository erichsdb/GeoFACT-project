"""Implements: FA8 (räumliche Operationen, Erweiterung to_points).

Führt einen Layer mit gemischten Geometrietypen (bei OSM häufig, z. B. Node
und Gebäudefläche zum selben Tag) auf Punkte zurück, weil GeoPandas'
overlay() und sjoin() gemischte Layer ablehnen. Linien und Flächen werden durch
ihren representative_point() ersetzt; ein Punkt je Eingabeobjekt, Attribute
und Reihenfolge bleiben.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from geopandas import GeoDataFrame

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class ToPointsStep(Step):
    """Führt einen Vektor-Layer auf einen homogenen Punkt-Layer zurück:
    Punkte bleiben, Linien/Flaechen werden durch ihren representative_point()
    ersetzt (ein Punkt je Objekt, Attribute erhalten) - FA8-Erweiterung.
    Löst gemischte OSM-Geometrien (z. B. shop=supermarket mal Node, mal
    Gebäudefläche) auf, bevor overlay/spatial_join einen homogenen Typ
    verlangen."""

    op: Literal["to_points"] = "to_points"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        """Keine Parameter."""


@register_operation(ToPointsStep)
def run_to_points(inputs: dict, params: dict) -> GeoDataFrame:
    """Idempotent auf reinen Punkt-Layern; ein leerer Layer wird unverändert
    zurückgegeben."""
    features: GeoDataFrame = inputs["features"]
    if features.empty:
        return features.copy()

    result = features.copy()
    geom = result.geometry
    non_point = geom.geom_type != "Point"
    if non_point.any():
        # Anders als der Zentroid liegt der representative_point() immer innerhalb
        # der Geometrie (wichtig bei konkaven Flächen und within-/intersects).
        new_geom = geom.copy()
        new_geom[non_point] = geom[non_point].representative_point()
        result = result.set_geometry(new_geom)
    return result
