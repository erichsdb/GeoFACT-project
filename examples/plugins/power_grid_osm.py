"""Implements: FA94 (Fach-Plugin power_grid_osm: Hochspannungsnetz aus OSM-Rohdaten).

Domaenen-Plugin fuer Szenario 1 der Arbeit (examples/sachsen_netz_resilienz.yaml):
Rekonstruktion des Hochspannungsnetzes aus OpenStreetMap-Rohdaten.

Wird ueber GEOFACT_PLUGIN_PATH=examples/plugins geladen; der GeoFACT-Kern bleibt
unveraendert (NFA4). Anders als das mitgelieferte Plugin power_grid_topology, das
Leitungsenden nur mit dem Schwerpunkt eines Umspannwerks verbindet, setzt
es drei fachliche Regeln um:

1. Spannung: Mehrfachangaben wie "110000;20000" werden als Liste gelesen,
   massgeblich ist die hoechste Spannungsebene.
2. Leitungen: OSM-Leitungen bestehen aus mehreren Wegstuecken, die sich an
   Masten treffen. Endpunkte innerhalb der Fangtoleranz werden zu einem
   gemeinsamen Netzknoten zusammengefasst, sodass Wegstuecke eine
   durchgehende Leitung bilden.
3. Umspannwerke: Eine Leitung endet in einem Umspannwerk, wenn ihr Ende an
   oder in seiner Flaeche liegt, nicht erst an seinem Schwerpunkt; das
   Umspannwerk ist damit Durchgangsknoten zwischen Leitungen. Punkt-Objekte,
   die innerhalb einer Umspannwerksflaeche liegen, sind Doppelerfassungen
   derselben Anlage und entfallen.

Ergebnis ist ein Graph aus Umspannwerken (kind = "substation") und
Leitungsknoten (kind = "junction"); Kanten tragen die Leitungslaenge.

Verworfene Wegstuecke (Goldene Regel 7): Ein Wegstueck wird nur dann keine
Kante, wenn (a) seine Geometrie nach dem Zerlegen keine LineString ist,
(b) beide Enden nach dem Fangen auf denselben Knoten fallen (Schleife, z. B.
ein kurzes Stueck zwischen zwei Masten innerhalb der Toleranz oder ganz in
einer Umspannwerksflaeche) oder (c) dasselbe Knotenpaar schon verbunden ist
(paralleler Stromkreis; die kuerzeste Leitung bleibt). Die Anzahlen je Grund
meldet EINE DroppedLinePieceWarning; Leitungen unter min_voltage oder ohne
lesbare Spannung nennt sie ebenfalls. Das Ergebnis selbst bleibt unveraendert.
"""

from __future__ import annotations

import warnings

from typing import ClassVar, Literal

import geopandas as gpd
import networkx as nx
import numpy as np
from pydantic import Field
from shapely import STRtree
from shapely.geometry import Point

from geofact.plugin_api import DataType, Graph, ParamsBase, Step, register_operation


class DroppedLinePieceWarning(UserWarning):
    """Wegstuecke aus 'power_lines', die keine Kante des Graphen wurden, mit
    der Anzahl je Grund (Schleife nach dem Fangen, paralleles Knotenpaar,
    keine LineString-Geometrie)."""


class PowerGridOsmStep(Step):
    """Stromnetz aus OSM-Rohdaten: hoechste Spannungsebene, Fangtoleranz fuer
    Leitungsenden, Umspannwerke als Durchgangsknoten."""

    op: Literal["power_grid_osm"] = "power_grid_osm"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "substations": DataType.VECTOR,
        "power_lines": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.GRAPH

    class Params(ParamsBase):
        min_voltage: float = Field(
            ...,
            ge=0,
            description="Mindestspannung (V) fuer Umspannwerke und Leitungen.",
        )
        tolerance_m: float = Field(
            50, ge=0, description="Fangtoleranz (m) fuer Leitungsenden."
        )


def max_voltage(value) -> float:
    """Hoechste Spannung aus einer OSM-Angabe wie "110000;20000" (sonst NaN)."""
    numbers = []
    for part in str(value).replace(",", ";").split(";"):
        try:
            numbers.append(float(part.strip()))
        except ValueError:
            continue
    return max(numbers) if numbers else np.nan


def _cluster(points: list[Point], tolerance_m: float) -> list[int]:
    """Ordnet jedem Punkt eine Knotennummer zu; Punkte innerhalb der Toleranz
    (transitiv) erhalten dieselbe Nummer (Union-Find ueber einen R-Baum)."""
    parent = list(range(len(points)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    tree = STRtree(points)
    for i, point in enumerate(points):
        for j in tree.query(point, predicate="dwithin", distance=tolerance_m):
            a, b = find(i), find(int(j))
            if a != b:
                parent[b] = a
    roots = [find(i) for i in range(len(points))]
    numbering = {root: n for n, root in enumerate(dict.fromkeys(roots))}
    return [numbering[r] for r in roots]


@register_operation(PowerGridOsmStep)
def run_power_grid_osm(inputs: dict, params: dict) -> Graph:
    min_voltage = params["min_voltage"]
    tolerance_m = params.get("tolerance_m", 50)
    substations: gpd.GeoDataFrame = inputs["substations"]
    lines: gpd.GeoDataFrame = inputs["power_lines"]
    if "voltage" not in substations.columns or "voltage" not in lines.columns:
        raise ValueError(
            "power_grid_osm: 'substations' und 'power_lines' brauchen ein 'voltage'-Attribut"
        )

    # Regel 1: hoechste Spannungsebene
    substations = substations.assign(
        voltage_max=substations["voltage"].map(max_voltage)
    )
    substations = substations[substations["voltage_max"] >= min_voltage]
    lines = lines.assign(voltage_max=lines["voltage"].map(max_voltage))
    no_voltage = int(lines["voltage_max"].isna().sum())
    below_voltage = int((lines["voltage_max"] < min_voltage).sum())
    lines = lines[lines["voltage_max"] >= min_voltage]

    # Regel 3 (Teil): Punkte innerhalb einer Umspannwerksflaeche sind Doppelerfassungen
    areas = substations[substations.geom_type.isin(["Polygon", "MultiPolygon"])]
    points = substations[~substations.index.isin(areas.index)]
    if not areas.empty and not points.empty:
        inside = gpd.sjoin(
            points[["geometry"]], areas[["geometry"]], predicate="within"
        ).index
        substations = substations.drop(index=inside.unique())

    # Regel 3: Ein Leitungsende an oder in einer Umspannwerksflaeche (plus
    # Toleranz) endet in diesem Umspannwerk; das Umspannwerk ist damit ein
    # Durchgangsknoten des Netzes und kein Blatt neben ihm.
    parts = lines.explode(index_parts=False)
    non_linear = int((parts.geom_type != "LineString").sum())
    parts = parts[parts.geom_type == "LineString"]
    ends = [Point(g.coords[0]) for g in parts.geometry] + [
        Point(g.coords[-1]) for g in parts.geometry
    ]
    sub_ids = [f"uw{idx}" for idx in substations.index]
    sub_geoms = list(substations.geometry)
    node_of: list[str | None] = [None] * len(ends)
    if sub_geoms:
        tree = STRtree(sub_geoms)
        for i, point in enumerate(ends):
            hits = tree.query(point, predicate="dwithin", distance=tolerance_m)
            if len(hits):
                nearest = min(hits, key=lambda j: sub_geoms[int(j)].distance(point))
                node_of[i] = sub_ids[int(nearest)]

    # Regel 2: uebrige Enden innerhalb der Toleranz sind ein gemeinsamer Mast-
    # oder Verbindungsknoten, an dem Wegstuecke zu einer Leitung zusammenlaufen
    free = [i for i, n in enumerate(node_of) if n is None]
    for i, cluster in zip(free, _cluster([ends[i] for i in free], tolerance_m)):
        node_of[i] = f"k{cluster}"

    graph = nx.Graph()
    for node_id, (idx, row) in zip(sub_ids, substations.iterrows()):
        attrs = {k: v for k, v in row.drop("geometry").items()}
        graph.add_node(
            node_id, kind="substation", geometry=row.geometry.centroid, **attrs
        )
    members: dict[str, list[Point]] = {}
    for point, node in zip(ends, node_of):
        if node.startswith("k"):
            members.setdefault(node, []).append(point)
    for node, pts in members.items():
        graph.add_node(
            node,
            kind="junction",
            geometry=Point(np.mean([p.x for p in pts]), np.mean([p.y for p in pts])),
        )
    n_parts = len(parts)
    self_loops = parallel = 0
    for k, geom in enumerate(parts.geometry):
        a, b = node_of[k], node_of[k + n_parts]
        if a == b:
            self_loops += 1
            continue
        if graph.has_edge(a, b):
            parallel += 1
        if not graph.has_edge(a, b) or graph.edges[a, b]["length"] > geom.length:
            graph.add_edge(a, b, length=geom.length)
    _warn_dropped(
        n_parts + non_linear,
        self_loops,
        parallel,
        non_linear,
        below_voltage,
        no_voltage,
        min_voltage,
        tolerance_m,
    )
    return Graph(
        graph, crs=substations.crs if substations.crs is not None else lines.crs
    )


def _warn_dropped(
    total: int,
    self_loops: int,
    parallel: int,
    non_linear: int,
    below_voltage: int,
    no_voltage: int,
    min_voltage: float,
    tolerance_m: float,
) -> None:
    """EINE Warnung mit der Anzahl verworfener Wegstuecke je Grund (Goldene
    Regel 7); ohne verworfene Stuecke keine Warnung."""
    dropped = self_loops + parallel + non_linear
    if not dropped:
        return
    reasons = [
        f"{self_loops} Schleife(n) - beide Enden fallen innerhalb {tolerance_m:g} m "
        "auf denselben Knoten",
        f"{parallel} parallel zu einer Kante zwischen denselben zwei Knoten "
        "(die kuerzeste Leitung bleibt)",
        f"{non_linear} ohne LineString-Geometrie",
    ]
    filtered = (
        f"; vorher entfielen {below_voltage} Leitung(en) unter {min_voltage:g} V und "
        f"{no_voltage} ohne lesbare Spannung"
    )
    warnings.warn(
        f"power_grid_osm: {dropped} von {total} Wegstueck(en) wurden keine Kante: "
        + ", ".join(reasons)
        + filtered,
        DroppedLinePieceWarning,
        stacklevel=3,
    )
