"""Implements: FA8 (räumliche Operationen: nearest_distance).

Abstand jedes from-Objekts zum nächsten to-Objekt (Selbstausschluss, FA8 Nachbedingung)."""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from geopandas import GeoDataFrame

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class NearestDistanceStep(Step):
    """Fügt 'from' ein Attribut 'distance' hinzu: der Abstand zur nächsten
    Geometrie in 'to' (planare Distanz im harmonisierten CRS der Region -
    FA5 projiziert auf die UTM-Zone der Region, also METER, nicht Grad
    oder Kilometer). Bei nachfolgendem filter auf 'distance' die Einheit
    beachten (z. B. 10 km -> distance > 10000, nicht > 10).
    Geometrien, die mit der jeweiligen from-Geometrie identisch sind,
    werden aus 'to' ausgeschlossen - so liefert from: X, to: X (gleicher
    Layer) den nächsten ANDEREN Punkt statt distance=0 für jedes Objekt
    (z. B. 'nächstes Nachbardorf' im selben Orte-Layer)."""

    op: Literal["nearest_distance"] = "nearest_distance"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "from": DataType.VECTOR,
        "to": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        """Keine Parameter."""

    def produced_fields(self) -> set[str]:
        # feste Spalte 'distance', kein output_field-Parameter
        return {"distance"}


@register_operation(NearestDistanceStep)
def run_nearest_distance(inputs: dict, params: dict) -> GeoDataFrame:
    """Nutzt sjoin_nearest(exclusive=True): STRtree-indiziert (O(n log n)) und
    mit Selbstausschluss identischer Geometrien. Bei Gleichstand liefert
    sjoin_nearest mehrere Zeilen; die jeweils erste bleibt, damit pro
    from-Objekt genau ein Ergebnis entsteht."""
    from_gdf: GeoDataFrame = inputs["from"]
    to_gdf: GeoDataFrame = inputs["to"]
    result = from_gdf.copy()

    if to_gdf.empty or from_gdf.empty:
        result["distance"] = np.nan
        return result

    # Nur die Geometrien joinen: Attribute wie 'index_right' aus einem früheren
    # spatial_join ließen sjoin_nearest sonst abbrechen.
    probe = from_gdf[[from_gdf.geometry.name]]
    targets = to_gdf[[to_gdf.geometry.name]]
    joined = probe.sjoin_nearest(
        targets, how="left", distance_col="distance", exclusive=True
    )
    distances = joined["distance"].groupby(joined.index).first()
    result["distance"] = distances.reindex(from_gdf.index)
    return result
