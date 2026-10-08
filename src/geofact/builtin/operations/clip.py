"""Implements: FA8 (räumliche Operationen: clip).

Schneidet features auf die Ausdehnung von mask zu."""

from __future__ import annotations

from typing import ClassVar, Literal

from geopandas import GeoDataFrame

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class ClipStep(Step):
    """Schneidet 'features' auf die Ausdehnung von 'mask' zu (FA8: clip)."""

    op: Literal["clip"] = "clip"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "features": DataType.VECTOR,
        "mask": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        """Keine Parameter."""


@register_operation(ClipStep)
def run_clip(inputs: dict, params: dict) -> GeoDataFrame:
    features: GeoDataFrame = inputs["features"]
    mask: GeoDataFrame = inputs["mask"]
    return features.clip(mask)
