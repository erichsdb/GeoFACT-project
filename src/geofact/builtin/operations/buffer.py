"""Implements: FA8 (räumliche Operationen: buffer).

Puffert jede Geometrie um radius_km (metrisch im Arbeits-CRS)."""

from __future__ import annotations

from typing import ClassVar, Literal

from geopandas import GeoDataFrame
from pydantic import Field

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class BufferStep(Step):
    op: Literal["buffer"] = "buffer"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"geometry": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        radius_km: float = Field(..., gt=0, description="Pufferradius in Kilometern.")


@register_operation(BufferStep)
def run_buffer(inputs: dict, params: dict) -> GeoDataFrame:
    gdf: GeoDataFrame = inputs["geometry"]
    radius_m = params["radius_km"] * 1000
    result = gdf.copy()
    result["geometry"] = gdf.geometry.buffer(radius_m)
    return result
