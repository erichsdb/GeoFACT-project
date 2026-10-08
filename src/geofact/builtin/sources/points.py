"""Implements: FA52 (Orte als Datenquelle, Quellart 'points'), Quellart für FA40.

Inline deklarierte Punkte (WGS84): ``features: [{name, lon, lat}, ...]``. Für
Szenarien, die ohne Netz und Geocoder reproduzierbar laufen sollen. Das Ergebnis
ist ein Punkt-Layer mit der Spalte ``name``; ins Arbeits-CRS bringt ihn die
Transformation 'reproject' (FA5).

Jeder Ort muss in der BBox der Szenario-Region (+ ``margin_km``) liegen
(builtin/_places.py)."""

from __future__ import annotations

from typing import Literal

import geopandas as gpd
from geopandas import GeoDataFrame
from pydantic import BaseModel, ConfigDict, Field, field_validator
from shapely.geometry import Point

from geofact.builtin._places import require_inside_region
from geofact.plugin_api import DataType, LayerBase, LoadContext, register_source


class PlaceSpec(BaseModel):
    """Ein Ort: Name und WGS84-Koordinaten (lon/lat in Grad)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    lon: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name darf nicht leer sein")
        return value


class PointsLayer(LayerBase):
    """Inline-Punkte in WGS84 (FA52). Mindestens ein Ort; jeder liegt in der
    Szenario-Region (BBox + margin_km)."""

    id: str
    source: Literal["points"] = "points"
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR
    features: list[PlaceSpec] = Field(..., min_length=1)
    margin_km: float = Field(
        1.0,
        ge=0,
        description="Rand in km um die BBox der Region, innerhalb dessen ein Ort noch als "
        "'in der Region' gilt.",
    )


@register_source(
    "points",
    PointsLayer,
    description="Inline deklarierte Punkte (name, lon, lat in WGS84) - offline und reproduzierbar",
)
def load(layer: PointsLayer, ctx: LoadContext) -> GeoDataFrame:
    places = gpd.GeoDataFrame(
        {"name": [spec.name for spec in layer.features]},
        geometry=[Point(spec.lon, spec.lat) for spec in layer.features],
        crs="EPSG:4326",
    )
    require_inside_region(places, ctx, layer_id=layer.id, margin_km=layer.margin_km)
    return places
