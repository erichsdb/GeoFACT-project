"""Implements: FA18 (Homogene Geometriedimension erzwingen).

Macht die Dominante-Dimension-Reduktion, die overlay und spatial_join intern
nutzen (_homogenize in _geometry.py), zu einem eigenen Pipeline-Schritt. Er
gehört vor to_points, wenn ein Layer lose Fragmente anderer Dimension enthält
(etwa Grenzsegmente neben den Flächen bei boundary=administrative), die sonst
zu sinnlosen Punkten würden.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from geopandas import GeoDataFrame

from geofact.builtin.operations._geometry import _homogenize
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class HomogenizeGeometryStep(Step):
    """Reduziert 'features' auf seine dominante (höchste) Geometrie-
    dimension - Fläche > Linie > Punkt (FA18). Generalisiert die
    Dominante-Dimension-Reduktion, die overlay/spatial_join (FA8) bereits
    intern anwenden, zu einem eigenständigen Pipeline-Schritt: für
    gemischt-geometrische OSM-Layer (z. B. boundary=administrative mit
    vereinzelten Linien-Grenzfragmenten neben den eigentlichen Flächen-
    Objekten), die schon vor overlay/spatial_join bereinigt werden sollen
    (z. B. vor to_points, damit keine Grenzfragmente zu sinnlosen
    Punkten werden)."""

    op: Literal["homogenize_geometry"] = "homogenize_geometry"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        """Keine Parameter."""


@register_operation(HomogenizeGeometryStep)
def run_homogenize_geometry(inputs: dict, params: dict) -> GeoDataFrame:
    """Ein leerer Layer wird unverändert durchgereicht."""
    features: GeoDataFrame = inputs["features"]
    if features.empty:
        return features
    return _homogenize(features, label="features")
