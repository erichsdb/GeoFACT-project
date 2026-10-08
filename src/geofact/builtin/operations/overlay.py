"""Implements: FA8 (räumliche Operationen: overlay).

Boolesche Mengenoperation zweier Vektor-Layer."""

from __future__ import annotations

from typing import ClassVar, Literal

from geopandas import GeoDataFrame
from pydantic import Field

from geofact.builtin.operations._geometry import _homogenize
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class OverlayStep(Step):
    """Boolesche Mengenoperation zweier Vektor-Layer (FA8: overlay)."""

    op: Literal["overlay"] = "overlay"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "left": DataType.VECTOR,
        "right": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        method: Literal["union", "intersection", "difference"] = Field(
            ..., description="Boolesche Mengenoperation zweier Vektor-Layer."
        )


@register_operation(OverlayStep)
def run_overlay(inputs: dict, params: dict) -> GeoDataFrame:
    left = _homogenize(inputs["left"], label="left")
    right = _homogenize(inputs["right"], label="right")
    method = params["method"]
    return left.overlay(right, how=method)
