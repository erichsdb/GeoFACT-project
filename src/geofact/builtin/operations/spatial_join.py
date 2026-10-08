"""Implements: FA8 (räumliche Operationen: spatial_join).

Anreicherung von left um Attribute von right über ein räumliches Prädikat."""

from __future__ import annotations

from typing import ClassVar, Literal

import geopandas as gpd
from geopandas import GeoDataFrame
from pydantic import Field

from geofact.builtin.operations._geometry import _homogenize, free_join_index_columns
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class SpatialJoinStep(Step):
    """Reichert 'left' um Attribute von 'right' an, verknüpft über ein
    räumliches Prädikat (FA8: spatial_join)."""

    op: Literal["spatial_join"] = "spatial_join"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "left": DataType.VECTOR,
        "right": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        predicate: Literal["within", "intersects", "contains"] = Field(
            ..., description="Räumliches Prädikat der Verknüpfung."
        )


@register_operation(SpatialJoinStep)
def run_spatial_join(inputs: dict, params: dict) -> GeoDataFrame:
    left = _homogenize(inputs["left"], label="left")
    right = _homogenize(inputs["right"], label="right")
    # ein Eingang aus einem früheren spatial_join trägt schon index_right
    left = free_join_index_columns(left, op="spatial_join", port="left")
    right = free_join_index_columns(right, op="spatial_join", port="right")
    predicate = params["predicate"]
    return gpd.sjoin(left, right, how="left", predicate=predicate, rsuffix="right")
