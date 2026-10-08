"""Implements: FA21 (Quellart 'region'), FA3 (Region als Layer), FA40, FA65 (Default-Lizenz).

Macht die vom Kern aufgelöste Szenario-Region (scenario.region, siehe
engine/region.py::resolve_region()) als regulären Vektor-Layer referenzierbar.
Die Auflösung selbst (BBox-Literal, Nominatim, Snapshot) ist ein fester
Kerndienst der Engine und gehört nicht zu diesem Baustein.

Default-Lizenz (FA65): ODbL-1.0 - eine namensaufgelöste Region stammt aus
OpenStreetMap (Nominatim); ein BBox-Literal (``region`` des Layers, sonst
``scenario.region``) enthält keine OSM-Daten und hat keinen Default. Eine
abweichende Lizenz deklariert ``license``."""

from __future__ import annotations

from typing import Literal, Optional

import geopandas as gpd
from pydantic import PrivateAttr, ValidationInfo, model_validator

from geofact.builtin._licenses import OSM_LICENSE, License
from geofact.core.contracts import SCENARIO_REGION_CONTEXT
from geofact.core.crs import parse_bbox_literal
from geofact.plugin_api import (
    DataType,
    LayerBase,
    LayerValidation,
    LoadContext,
    register_source,
)


# --- Layer-Modell (Vertrag der Quellart, FA40) ---


class RegionLayer(LayerBase):
    """NEU (FA21): macht die bereits intern aufgelöste Szenario-Region
    (scenario.region, engine/region.py::resolve_region()) als
    regulären Vektor-Layer referenzierbar - keine eigene Eingabe nötig,
    da die Region bereits über scenario.region deklariert ist. Motiviert
    durch FA22 (hex_grid), das eine flächendeckende Polygon-Eingabe
    braucht statt eines zweckentfremdeten thematischen OSM-Layers."""

    id: str
    source: Literal["region"] = "region"
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR
    validation: Optional[LayerValidation] = None
    _bbox_literal: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _note_region_kind(self, info: ValidationInfo) -> "RegionLayer":
        """Merkt sich, ob die Region ein BBox-Literal ist (Layer-Region vor
        scenario.region aus dem Validierungskontext)."""
        region = self.region or (info.context or {}).get(SCENARIO_REGION_CONTEXT)
        self._bbox_literal = (
            isinstance(region, str) and parse_bbox_literal(region) is not None
        )
        return self

    def default_license(self) -> Optional[License]:
        """FA65: eine Region aus OpenStreetMap (Nominatim) steht unter der ODbL-1.0;
        ein BBox-Literal ist reine Geometrie ohne Default-Lizenz."""
        return None if self._bbox_literal else OSM_LICENSE


# --- Registrierung als Quellart (FA40) ---


@register_source(
    "region",
    RegionLayer,
    description="Szenario-Region (scenario.region) als Vektor-Layer (ein Polygon)",
)
def load(layer: RegionLayer, ctx: LoadContext) -> gpd.GeoDataFrame:
    """ctx.region ist bereits die aufgelöste Szenario-Region (FA3) - kein
    zweiter Auflösungspfad (FA21-Nachbedingung: bytegleich mit der intern
    verwendeten Region). copy(), damit nachfolgende Operationen die vom
    Executor gehaltene Referenz nicht versehentlich mutieren."""
    return ctx.region.copy()
