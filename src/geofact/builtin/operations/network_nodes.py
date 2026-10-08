"""Implements: FA17 (Netzknoten aus Linientopologie ableiten).

build_network (FA10) verlangt einen 'nodes'-Layer, der mit jedem Endpunkt von
'lines' innerhalb tolerance_m übereinstimmt, sonst wird das Segment verworfen.
Ein Attribut-Layer (Haltestellen, Kreuzungen) deckt bei rohen OSM-Liniendaten nur
einen Teil der Endpunkte ab und fragmentiert das Netz. network_nodes leitet
deshalb den Punkt-Layer aus der Liniengeometrie ab: jeder Start-/Endpunkt jedes
(Multi-)LineString-Teilstücks wird ein Knoten, Endpunkte innerhalb tolerance_m
fallen zusammen. Es werden keine Attribute übernommen; Haltestellen bleiben
eigene origins/destinations-Eingaben (FA15), die auf den nächsten Knoten
snappen.
"""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal

import geopandas as gpd
from geopandas import GeoDataFrame
from pydantic import Field
from shapely.geometry import Point

from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class NonLinearGeometryWarning(UserWarning):
    """Signalisiert, dass 'lines' Geometrien enthält, die keine (Multi)
    LineString sind - analog builtin/operations/graph.py. Solche Objekte werden
    übersprungen, nicht die ganze Operation abgebrochen."""


class NetworkNodesStep(Step):
    """Leitet aus 'lines' einen Punkt-Layer der eindeutigen Segment-
    Endpunkte ab (FA17) - Grundlage für einen zu build_network passenden
    'nodes'-Layer, wenn kein eigener Knoten-Layer vorliegt, der bereits
    jeden Linienendpunkt abdeckt (z. B. rohe OSM-Liniendaten wie Tramgleise:
    Attribut-Layer wie Haltestellen decken nur eine Teilmenge der
    tatsächlichen Endpunkte ab - Kreuzungen/Kurvenpunkte fehlen)."""

    op: Literal["network_nodes"] = "network_nodes"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"lines": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        tolerance_m: float = Field(
            0,
            ge=0,
            description="Snap-Toleranz in Metern: Endpunkte innerhalb dieses Radius fallen "
            "zu einem gemeinsamen Knoten zusammen.",
        )


@register_operation(NetworkNodesStep)
def run_network_nodes(inputs: dict, params: dict) -> GeoDataFrame:
    """Leerer Eingabe-Layer ergibt einen leeren Punkt-Layer (kein Fehler)."""
    lines: GeoDataFrame = inputs["lines"]
    tolerance_m = params.get("tolerance_m", 0)

    if lines.empty:
        return gpd.GeoDataFrame({"id": []}, geometry=[], crs=lines.crs)

    endpoints: list[Point] = []
    skipped_non_linear = 0
    for geom in lines.geometry:
        if geom is None or geom.geom_type not in ("LineString", "MultiLineString"):
            skipped_non_linear += 1
            continue
        parts = geom.geoms if geom.geom_type == "MultiLineString" else [geom]
        for part in parts:
            coords = list(part.coords)
            if not coords:
                continue
            endpoints.append(Point(coords[0]))
            endpoints.append(Point(coords[-1]))

    if skipped_non_linear:
        warnings.warn(
            f"network_nodes: {skipped_non_linear} Objekt(e) in 'lines' sind "
            "keine (Multi)LineString-Geometrie und wurden übersprungen",
            NonLinearGeometryWarning,
            stacklevel=2,
        )

    if not endpoints:
        return gpd.GeoDataFrame({"id": []}, geometry=[], crs=lines.crs)

    nodes = _dedupe_within_tolerance(endpoints, tolerance_m)
    return gpd.GeoDataFrame({"id": range(len(nodes))}, geometry=nodes, crs=lines.crs)


def _dedupe_within_tolerance(points: list[Point], tolerance_m: float) -> list[Point]:
    """Fasst Punkte innerhalb tolerance_m zu einem Knoten zusammen (Mittelpunkt
    der Gruppe). Bei tolerance_m=0 zählen nur exakt gleiche Koordinaten. Der
    STRtree-Query vermeidet eine Alle-gegen-alle-Suche bei großen Netzen."""
    if tolerance_m <= 0:
        seen: dict[tuple[float, float], Point] = {}
        for p in points:
            key = (round(p.x, 6), round(p.y, 6))
            seen.setdefault(key, p)
        return list(seen.values())

    from shapely.strtree import STRtree

    tree = STRtree(points)
    n = len(points)
    assigned = [-1] * n
    clusters: list[list[int]] = []

    for i, p in enumerate(points):
        if assigned[i] != -1:
            continue
        candidate_idx = tree.query(p.buffer(tolerance_m))
        cluster = [i]
        assigned[i] = len(clusters)
        for j in candidate_idx:
            j = int(j)
            if j == i or assigned[j] != -1:
                continue
            if points[j].distance(p) <= tolerance_m:
                assigned[j] = len(clusters)
                cluster.append(j)
        clusters.append(cluster)

    result = []
    for cluster in clusters:
        xs = [points[i].x for i in cluster]
        ys = [points[i].y for i in cluster]
        result.append(Point(sum(xs) / len(xs), sum(ys) / len(ys)))
    return result
