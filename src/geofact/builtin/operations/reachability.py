"""Implements: FA15 (Erreichbarkeitsanalyse).

shortest_path und isochrone auf dem FA10-Netz (graph.Graph, dessen Kanten das
Attribut 'length' in Metern tragen; der Executor harmonisiert alle Vektor-Layer
vorher auf das UTM-CRS der Region). Zwei Einschränkungen, die aus FA10 geerbt
und nicht verdeckt werden:

1. Das Netz ist ungerichtet (nx.Graph), Einbahnstraßen zählen nicht.
2. Isochronen sind eine Approximation: erreichte Knoten werden gepuffert, erreichte
   Kanten als Luftlinie zwischen ihren Endknoten. Die Kantengeometrie ('geometry')
   liest isochrone bewusst nicht, damit die Ergebnisse bestehender Szenarien
   unverändert bleiben (test_fa15_isochrone_ignores_edge_geometry). Den echten
   Verlauf nutzen route (FA51) und die Graph-Ausgaben (FA53).
"""

from __future__ import annotations

import math
import warnings
from typing import ClassVar, Literal

import geopandas as gpd
import networkx as nx
import numpy as np
from geopandas import GeoDataFrame
from pydantic import Field, field_validator
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from geofact.plugin_api import DataType, Graph, ParamsBase, register_operation, Step


class UnsnappableFeatureWarning(UserWarning):
    """Signalisiert, dass mindestens ein Objekt keinen Netzknoten
    innerhalb von max_snap_m hat und deshalb NaN bzw. keinen Beitrag
    zur Isochrone erhält (FA15-Nachbedingung: kein stiller Fehler)."""


class UnreachableFeatureWarning(UserWarning):
    """Signalisiert, dass mindestens ein Objekt zwar gesnappt werden
    konnte, aber im (ggf. getrennten, FA10) Netz keine destination
    erreicht bzw. kein Knoten innerhalb der größten breaks_m-Stufe
    liegt."""


def _require_graph(value: object, *, op: str, port: str) -> Graph:
    if not isinstance(value, Graph):
        raise TypeError(
            f"{op}: Eingang '{port}' muss ein Graph (FA10 build_network) sein, "
            f"nicht {type(value).__name__}"
        )
    return value


def _require_vector(value: object, *, op: str, port: str) -> GeoDataFrame:
    if not isinstance(value, GeoDataFrame):
        raise TypeError(
            f"{op}: Eingang '{port}' muss ein Vektor-Layer (GeoDataFrame) sein, "
            f"nicht {type(value).__name__}"
        )
    return value


def _node_frame(graph: Graph) -> GeoDataFrame:
    """Knoten des Graphen als Punkt-GeoDataFrame (Spalte 'node_id'). Snapping per
    sjoin_nearest (STRtree) statt brute force, da es viele origins/destinations
    geben kann."""
    node_ids = list(graph.nx_graph.nodes)
    geometries = [graph.nx_graph.nodes[n]["geometry"] for n in node_ids]
    return gpd.GeoDataFrame({"node_id": node_ids}, geometry=geometries, crs=graph.crs)


def _snap_to_nodes(
    points: GeoDataFrame, node_frame: GeoDataFrame, max_snap_m: float
) -> "gpd.array.GeometryArray | list":
    """Ordnet jeder Zeile in points den nächsten Knoten in node_frame zu
    (Toleranz max_snap_m); Nicht-Punkte gehen über ihren Centroid ein. Liste
    gleicher Länge wie points (node_id oder None)."""
    if points.empty or node_frame.empty:
        return [None] * len(points)

    probe = gpd.GeoDataFrame(
        {"_probe_idx": range(len(points))},
        geometry=points.geometry.centroid.values,
        crs=points.crs,
    )
    joined = gpd.sjoin_nearest(
        probe,
        node_frame[["node_id", node_frame.geometry.name]],
        how="left",
        max_distance=max_snap_m,
        distance_col="_snap_dist",
    )
    # Bei Gleichstand liefert sjoin_nearest mehrere Treffer; ein beliebiger gleich
    # naher Knoten genügt.
    joined = joined.sort_values("_snap_dist").drop_duplicates("_probe_idx")
    joined = joined.set_index("_probe_idx").reindex(range(len(points)))
    return [None if _is_missing(v) else v for v in joined["node_id"]]


def _is_missing(value: object) -> bool:
    # kein Knoten innerhalb max_snap_m: sjoin_nearest liefert NaN im 'left'-Join
    return value is None or (isinstance(value, float) and math.isnan(value))


class ShortestPathStep(Step):
    """Netzdistanz je Objekt in 'origins' zum nächsten Objekt in
    'destinations' über das FA10-Netz (FA15). Beide werden dem
    nächsten Netzknoten zugeordnet (Snapping, Toleranz max_snap_m);
    Objekte ohne Knoten in Reichweite oder ohne Pfad zu einer
    erreichbaren destination erhalten NaN + Warnung statt eines
    stillen Fehlers (Goldene Regel 7). Das Netz ist UNGERICHTET
    (FA10) - Einbahnstraßen werden nicht berücksichtigt; die
    Distanz ist reine Netzdistanz zwischen den gesnappten Knoten,
    ohne Zu-/Abgangsweg zum tatsächlichen Objektstandort."""

    op: Literal["shortest_path"] = "shortest_path"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "graph": DataType.GRAPH,
        "origins": DataType.VECTOR,
        "destinations": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        max_snap_m: float = Field(
            ...,
            gt=0,
            description="Fangtoleranz in Metern: origins/destinations werden dem nächsten "
            "Netzknoten zugeordnet; weiter entfernte Objekte erhalten NaN statt "
            "einer falschen Distanz.",
        )

    def produced_fields(self) -> set[str]:
        return {"network_distance"}


@register_operation(ShortestPathStep)
def run_shortest_path(inputs: dict, params: dict) -> GeoDataFrame:
    graph = _require_graph(inputs["graph"], op="shortest_path", port="graph")
    origins = _require_vector(inputs["origins"], op="shortest_path", port="origins")
    destinations = _require_vector(
        inputs["destinations"], op="shortest_path", port="destinations"
    )
    max_snap_m = params["max_snap_m"]

    result = origins.copy()
    result["network_distance"] = np.nan
    if origins.empty:
        return result

    node_frame = _node_frame(graph)
    origin_nodes = _snap_to_nodes(origins, node_frame, max_snap_m)

    n_unsnapped_origins = sum(1 for n in origin_nodes if n is None)
    if n_unsnapped_origins:
        warnings.warn(
            f"shortest_path: {n_unsnapped_origins} von {len(origins)} origins "
            f"liegen weiter als max_snap_m={max_snap_m} vom nächsten Netzknoten "
            "entfernt und erhalten NaN als network_distance",
            UnsnappableFeatureWarning,
            stacklevel=2,
        )

    if destinations.empty:
        warnings.warn(
            "shortest_path: 'destinations' ist leer - alle origins erhalten NaN",
            UnreachableFeatureWarning,
            stacklevel=2,
        )
        return result

    dest_nodes = [
        n for n in _snap_to_nodes(destinations, node_frame, max_snap_m) if n is not None
    ]
    n_unsnapped_dest = len(destinations) - len(dest_nodes)
    if n_unsnapped_dest:
        warnings.warn(
            f"shortest_path: {n_unsnapped_dest} von {len(destinations)} destinations "
            f"liegen weiter als max_snap_m={max_snap_m} vom nächsten Netzknoten entfernt "
            "und werden ignoriert",
            UnsnappableFeatureWarning,
            stacklevel=2,
        )
    if not dest_nodes:
        warnings.warn(
            "shortest_path: keine destination ließ sich einem Netzknoten zuordnen - "
            "alle origins erhalten NaN",
            UnreachableFeatureWarning,
            stacklevel=2,
        )
        return result

    # Ein multi-source Dijkstra statt je destination einzeln: Distanz zur
    # nächstgelegenen destination.
    lengths = nx.multi_source_dijkstra_path_length(
        graph.nx_graph, sources=set(dest_nodes), weight="length"
    )

    distances = []
    n_unreachable = 0
    for node in origin_nodes:
        if node is None:
            distances.append(np.nan)
            continue
        d = lengths.get(node)
        if d is None:
            n_unreachable += 1
            distances.append(np.nan)
        else:
            distances.append(float(d))

    if n_unreachable:
        warnings.warn(
            f"shortest_path: {n_unreachable} origin(s) haben im Netz keinen Pfad zu "
            "einer erreichbaren destination (getrennte Zusammenhangskomponente, FA10) "
            "und erhalten NaN",
            UnreachableFeatureWarning,
            stacklevel=2,
        )

    result["network_distance"] = distances
    return result


def _reached_within(lengths: dict, break_m: float) -> set:
    return {node for node, d in lengths.items() if d <= break_m}


class IsochroneStep(Step):
    """Erreichbarkeitsflächen (Service Areas) je Distanzstufe ab
    'origins' über das FA10-Netz (FA15). Approximation: erreichte
    Knoten und die Kanten dazwischen (als Luftlinie zwischen den
    Endknoten, die Kantengeometrie wird nicht gelesen) werden um buffer_m gepuffert und je
    breaks_m-Stufe zu einem Polygon vereinigt - KEINE anteilige
    Kantenabdeckung (kein Abschneiden einer Kante an der Budgetgrenze).
    Netz ist ungerichtet (FA10-Einschränkung, s. ShortestPathStep)."""

    op: Literal["isochrone"] = "isochrone"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "graph": DataType.GRAPH,
        "origins": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        breaks_m: list[float] = Field(
            ...,
            min_length=1,
            description="Aufsteigende Netzdistanzen in Metern; je Stufe ein kumulatives "
            "Service-Area-Polygon (Spalte break_m).",
        )
        max_snap_m: float = Field(
            ...,
            gt=0,
            description="Fangtoleranz in Metern: origins werden dem nächsten Netzknoten "
            "zugeordnet; weiter entfernte Objekte tragen nicht zur Fläche bei "
            "(Warnung).",
        )
        buffer_m: float = Field(
            100,
            gt=0,
            description="Puffer um erreichte Knoten/Kanten zur Polygonbildung "
            "(Glättung der Approximation).",
        )

        @field_validator("breaks_m")
        @classmethod
        def check_breaks(cls, breaks: list[float]) -> list[float]:
            if any(b <= 0 for b in breaks):
                raise ValueError("breaks_m muss ausschließlich > 0 sein")
            if breaks != sorted(breaks):
                raise ValueError("breaks_m muss aufsteigend sortiert sein")
            return breaks

    def produced_fields(self) -> set[str]:
        return {"break_m"}


@register_operation(IsochroneStep)
def run_isochrone(inputs: dict, params: dict) -> GeoDataFrame:
    graph = _require_graph(inputs["graph"], op="isochrone", port="graph")
    origins = _require_vector(inputs["origins"], op="isochrone", port="origins")
    # ganzzahlige Stufen bleiben ganzzahlig (break_m trägt 500 statt 500.0)
    breaks_m = [int(b) if float(b).is_integer() else b for b in params["breaks_m"]]
    max_snap_m = params["max_snap_m"]
    buffer_m = params.get("buffer_m", 100)

    empty_result = gpd.GeoDataFrame({"break_m": []}, geometry=[], crs=graph.crs)
    if origins.empty:
        warnings.warn(
            "isochrone: 'origins' ist leer - keine Service-Area-Polygone erzeugt",
            UnsnappableFeatureWarning,
            stacklevel=2,
        )
        return empty_result

    node_frame = _node_frame(graph)
    origin_nodes = {
        n for n in _snap_to_nodes(origins, node_frame, max_snap_m) if n is not None
    }

    if len(origin_nodes) < len(origins):
        warnings.warn(
            f"isochrone: mindestens {len(origins) - len(origin_nodes)} origin(s) liegen weiter "
            f"als max_snap_m={max_snap_m} vom nächsten Netzknoten entfernt und tragen nicht "
            "zur Fläche bei",
            UnsnappableFeatureWarning,
            stacklevel=2,
        )
    if not origin_nodes:
        warnings.warn(
            "isochrone: keine origin ließ sich einem Netzknoten zuordnen - keine "
            "Service-Area-Polygone erzeugt",
            UnreachableFeatureWarning,
            stacklevel=2,
        )
        return empty_result

    lengths = nx.multi_source_dijkstra_path_length(
        graph.nx_graph, sources=origin_nodes, weight="length"
    )
    node_geometry = {
        n: graph.nx_graph.nodes[n]["geometry"] for n in graph.nx_graph.nodes
    }

    rows = []
    geometries = []
    for break_m in breaks_m:
        reached = _reached_within(lengths, break_m)
        if not reached:
            warnings.warn(
                f"isochrone: break_m={break_m} erreicht keinen Netzknoten - keine "
                "Fläche für diese Stufe erzeugt",
                UnreachableFeatureWarning,
                stacklevel=2,
            )
            continue

        shapes = [
            Point(node_geometry[n].x, node_geometry[n].y).buffer(buffer_m)
            for n in reached
        ]
        for u, v in graph.nx_graph.edges(reached):
            if u in reached and v in reached:
                chord = LineString([node_geometry[u], node_geometry[v]])
                shapes.append(chord.buffer(buffer_m))

        rows.append({"break_m": break_m})
        geometries.append(unary_union(shapes))

    if not rows:
        return empty_result

    return gpd.GeoDataFrame(rows, geometry=geometries, crs=graph.crs)
