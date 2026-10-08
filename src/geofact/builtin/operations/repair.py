"""Implements: FA23 (Geometrie-Validität reparieren).

Amtliche Open-Data-Quellen enthalten oft topologisch ungültige Polygone, die
fehlerfrei laden, aber erst mehrere Schritte später in overlay/spatial_join an
einer GEOS-TopologyException scheitern. repair_geometry macht die Reparatur zu
einem expliziten Schritt: ungültige Geometrien werden per shapely.make_valid
repariert, leere oder fehlende verworfen. Beides wird mit Anzahl per Warnung
gemeldet.
"""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal

import geopandas as gpd
from geopandas import GeoDataFrame
from shapely import make_valid

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class GeometryRepairedWarning(UserWarning):
    """Signalisiert, dass 'features' ungültige Geometrien enthielt, die
    per shapely.make_valid repariert wurden (FA23)."""


class EmptyGeometryDroppedWarning(UserWarning):
    """Signalisiert, dass 'features' leere oder fehlende (None)
    Geometrien enthielt, die verworfen wurden (FA23) - geprüft sowohl
    vor als auch nach der Reparatur, da make_valid degenerierte
    Eingaben (z. B. entartete Polygone) zu leeren Geometrien reduzieren
    kann."""


class RepairGeometryStep(Step):
    """Prüft 'features' auf topologische Validität und repariert
    ungültige Geometrien per shapely.make_valid (FA23); leere/fehlende
    Geometrien werden verworfen. Motiviert durch amtliche Open-Data-
    Quellen (Kataster-/ALKIS-Derivate, Flächennutzung), die regelmäßig
    topologisch ungültige Polygone (Self-Intersections, Bowties)
    enthalten - diese laden fehlerfrei, lassen aber overlay/spatial_join
    (FA8) erst mehrere Pipeline-Schritte später mit einer GEOS-
    TopologyException abstürzen. repair_geometry macht die Reparatur zu
    einem expliziten, deklarierten Pipeline-Schritt, analog dazu, wie
    FA18 (homogenize_geometry) die Dimensionsreduktion explizit machte.
    make_valid kann den Geometrietyp ändern (z. B. Bowtie-Polygon ->
    MultiPolygon) - dokumentiertes, erwartetes Verhalten; für
    nachfolgende Dimensions-Anforderungen ggf. homogenize_geometry
    (FA18) nachschalten."""

    op: Literal["repair_geometry"] = "repair_geometry"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        """Keine Parameter."""


@register_operation(RepairGeometryStep)
def run_repair_geometry(inputs: dict, params: dict) -> GeoDataFrame:
    """Ein leerer Eingabe-Layer wird unverändert durchgereicht."""
    features: GeoDataFrame = inputs["features"]
    if features.empty:
        return features

    geom = features.geometry

    # Vor der Reparatur verwerfen: make_valid wirft auf None.
    missing_or_empty_before = geom.isna() | geom.is_empty
    dropped_before = int(missing_or_empty_before.sum())
    result = features[~missing_or_empty_before]

    # Nur ungültige Geometrien anfassen, gültige bleiben unverändert.
    invalid_mask = ~result.geometry.is_valid
    repaired_count = int(invalid_mask.sum())
    if repaired_count:
        repaired_geoms = result.geometry.copy()
        repaired_geoms.loc[invalid_mask] = result.geometry.loc[invalid_mask].apply(
            make_valid
        )
        result = result.set_geometry(repaired_geoms)

    # make_valid kann degenerierte Eingaben zu leeren Geometrien reduzieren.
    empty_after = result.geometry.is_empty
    dropped_after = int(empty_after.sum())
    if dropped_after:
        result = result[~empty_after]

    total_dropped = dropped_before + dropped_after
    if total_dropped:
        warnings.warn(
            f"repair_geometry: {total_dropped} Objekt(e) in 'features' hatten "
            "eine leere oder fehlende Geometrie und wurden verworfen",
            EmptyGeometryDroppedWarning,
            stacklevel=2,
        )
    if repaired_count:
        warnings.warn(
            f"repair_geometry: {repaired_count} Objekt(e) in 'features' waren "
            "topologisch ungültig und wurden per shapely.make_valid repariert",
            GeometryRepairedWarning,
            stacklevel=2,
        )

    return gpd.GeoDataFrame(result, geometry=result.geometry.name, crs=features.crs)
