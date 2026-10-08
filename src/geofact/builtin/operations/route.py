"""Implements: FA51 (Route zwischen Orten).

route berechnet den kürzesten Weg zwischen Orten auf einem Netz (FA10 build_network
oder FA50 line_network) und liefert ihn als Linie entlang der Kantengeometrie, nicht
als Luftlinie. shortest_path (FA15) liefert dagegen nur eine Zahl je Ursprung.

Paarbildung (``pairing``): ``pairwise`` verbindet Zeile i von origins mit Zeile i von
destinations (gleich viele Zeilen), ``cross`` jeden Ursprung mit jedem Ziel. Die
Ausgabe hat eine Zeile je Paar: Linie, Beschriftungen (Spalte ``name``, sonst der
Zeilenindex), ``length_m``, ``travel_time_min`` (nur wenn jede Kante eine Fahrzeit
trägt), ``straight_line_m``, ``detour_factor`` (length_m / straight_line_m) und
``origin_snap_m`` / ``destination_snap_m`` (Abstand des Ortes zum Netzknoten; der
Weg vom Ort zum Knoten ist nicht Teil der Route).

Fangen: jeder Ort kommt an den nächsten Netzknoten innerhalb ``max_snap_m``. Mit
``snap_to: largest_component`` (Standard) zählen nur Knoten der größten
Zusammenhangskomponente; liegt der nächste Knoten in einer kleineren (z. B. eine
isolierte Sackgasse), wird gewarnt und der nächste Knoten der größten genommen.

Gewicht (``weight``): ``length`` (Meter, Standard) oder ``travel_time_min`` (braucht
``speed_kmh`` in line_network). Jede Kante muss das Attribut tragen, sonst ginge
networkx stillschweigend von Gewicht 1 aus.

Ein nicht fangbares oder nicht erreichbares Paar bekommt eine Zeile ohne Geometrie
mit NaN und eine Warnung; lässt sich kein Paar routen, ist das ein Fehler. Eine
Kante ohne Geometrie wird als Gerade zwischen ihren Knoten gezeichnet, mit Warnung.
"""

from __future__ import annotations

import math
import warnings
from itertools import product
from typing import ClassVar, Literal

import networkx as nx
import numpy as np
import pandas as pd
from geopandas import GeoDataFrame
from pydantic import Field
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from geofact.builtin.operations.reachability import (
    UnreachableFeatureWarning,
    UnsnappableFeatureWarning,
)
from geofact.plugin_api import DataType, Graph, ParamsBase, register_operation, Step

NAN = float("nan")


class OtherComponentWarning(UserWarning):
    """Der nächste Netzknoten eines Ortes liegt in einer kleineren Zusammenhangs-
    komponente; gefangen wurde stattdessen in der größten (FA51)."""


class EdgeGeometryMissingWarning(UserWarning):
    """Kanten auf der Route tragen keine Geometrie; sie sind als Gerade zwischen ihren
    Knoten gezeichnet (FA51)."""


class SameNodeWarning(UserWarning):
    """Ursprung und Ziel wurden demselben Netzknoten zugeordnet: Länge 0, keine
    Linie (FA51)."""


class RouteStep(Step):
    """Kürzeste Route zwischen Orten auf einem Netz (FA51): je Paar origins -> destinations
    eine Linie entlang der Kanten mit Länge, Fahrzeit (falls im Netz vorhanden), Luftlinie
    und Umwegfaktor. Orte werden dem nächsten Netzknoten (max_snap_m) zugeordnet, standard-
    mäßig in der größten Zusammenhangskomponente. Nicht fangbare oder nicht erreichbare
    Paare erhalten eine Zeile ohne Geometrie und eine Warnung; ist kein Paar routbar, ist das
    ein Fehler. Ungerichtet: Einbahnregeln werden nicht berücksichtigt."""

    op: Literal["route"] = "route"
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
            description="Fangtoleranz in Metern: Orte werden dem nächsten Netzknoten innerhalb "
            "dieser Distanz zugeordnet; weiter entfernte Orte sind nicht routbar.",
        )
        weight: Literal["length", "travel_time_min"] = Field(
            "length",
            description="Gewicht der kürzesten Route: 'length' (Meter) oder 'travel_time_min' "
            "(Minuten; das Netz braucht dafür Fahrzeiten, z. B. speed_kmh in "
            "line_network).",
        )
        pairing: Literal["pairwise", "cross"] = Field(
            "pairwise",
            description="'pairwise': Zeile i von origins -> Zeile i von destinations (gleich viele "
            "Zeilen); 'cross': jeder Ursprung zu jedem Ziel.",
        )
        snap_to: Literal["largest_component", "any_node"] = Field(
            "largest_component",
            description="Welche Knoten Orte fangen: nur die der größten Zusammenhangskomponente "
            "(Standard, mit Warnung bei Umlenkung) oder jeder Knoten.",
        )

    def produced_fields(self) -> set[str]:
        # travel_time_min nur, wenn das Netz Fahrzeiten trägt; nicht vorhandene
        # Namen verwirft die Ansicht.
        return {
            "origin",
            "destination",
            "length_m",
            "travel_time_min",
            "straight_line_m",
            "detour_factor",
            "origin_snap_m",
            "destination_snap_m",
        }


# --- Eingaben prüfen ---


def _require_graph(value: object) -> Graph:
    if not isinstance(value, Graph):
        raise TypeError(
            f"route: Eingang 'graph' muss ein Graph (build_network oder line_network) sein, "
            f"nicht {type(value).__name__}"
        )
    return value


def _require_places(value: object, port: str) -> GeoDataFrame:
    if not isinstance(value, GeoDataFrame):
        raise TypeError(
            f"route: Eingang '{port}' muss ein Vektor-Layer (GeoDataFrame) sein, "
            f"nicht {type(value).__name__}"
        )
    if value.empty:
        raise ValueError(
            f"route: Eingang '{port}' ist leer - es gibt keine Orte, zwischen denen eine "
            "Route berechnet werden könnte"
        )
    if value.crs is None:
        raise ValueError(
            f"route: Eingang '{port}' hat kein CRS (der Executor harmonisiert, FA5)"
        )
    return value


def _require_same_crs(
    graph: Graph, origins: GeoDataFrame, destinations: GeoDataFrame
) -> None:
    crs_list = {
        "graph": graph.crs,
        "origins": origins.crs,
        "destinations": destinations.crs,
    }
    known = {name: crs for name, crs in crs_list.items() if crs is not None}
    if len({str(crs) for crs in known.values()}) > 1:
        raise ValueError(
            f"route: graph, origins und destinations haben unterschiedliche CRS ({known}) - "
            "erst per FA5-Harmonisierung angleichen, route reprojiziert nicht stillschweigend"
        )


def _require_edge_attribute(nx_graph: nx.Graph, attribute: str, why: str) -> None:
    missing = sum(
        1 for _, _, data in nx_graph.edges(data=True) if attribute not in data
    )
    if missing:
        raise ValueError(
            f"route: {missing} von {nx_graph.number_of_edges()} Kanten tragen kein Attribut "
            f"'{attribute}' ({why})"
        )


def _labels(places: GeoDataFrame) -> list[str]:
    if "name" in places.columns:
        return [
            str(index) if pd.isna(name) else str(name)
            for index, name in zip(places.index, places["name"])
        ]
    return [str(index) for index in places.index]


def _representative_points(places: GeoDataFrame) -> list:
    """Ein Punkt je Zeile (Schwerpunkt; ein Punkt bleibt er selbst), None bei fehlender
    oder leerer Geometrie - solche Orte sind nicht fangbar."""
    return [
        None if geometry is None or geometry.is_empty else geometry.centroid
        for geometry in places.geometry
    ]


def _pairs(n_origins: int, n_destinations: int, pairing: str) -> list[tuple[int, int]]:
    if pairing == "cross":
        return list(product(range(n_origins), range(n_destinations)))
    if n_origins != n_destinations:
        raise ValueError(
            f"route: pairing 'pairwise' verbindet Zeile i von origins mit Zeile i von destinations "
            f"und braucht gleich viele Zeilen (origins: {n_origins}, destinations: "
            f"{n_destinations}); für jeden Ursprung zu jedem Ziel pairing: cross wählen"
        )
    return [(i, i) for i in range(n_origins)]


# --- Fangen ---


class _Snapper:
    """Ordnet Punkte dem nächsten Netzknoten zu (STRtree), optional nur in der
    größten Zusammenhangskomponente."""

    def __init__(self, nx_graph: nx.Graph, max_snap_m: float, snap_to: str) -> None:
        self.max_snap_m = max_snap_m
        self.snap_to = snap_to
        self.node_ids = list(nx_graph.nodes)
        missing = [
            n for n in self.node_ids if nx_graph.nodes[n].get("geometry") is None
        ]
        if missing:
            raise ValueError(
                f"route: {len(missing)} Knoten des Graphen tragen keine Geometrie "
                f"(z. B. {missing[:3]}) - ohne Punkte lässt sich nicht fangen"
            )
        geometries = [nx_graph.nodes[n]["geometry"] for n in self.node_ids]
        self.tree_all = STRtree(geometries)
        largest = max(nx.connected_components(nx_graph), key=len)
        self.main = np.array([i for i, n in enumerate(self.node_ids) if n in largest])
        self.tree_main = STRtree([geometries[i] for i in self.main])
        self.in_largest = {n for n in largest}

    def _nearest(
        self, tree: STRtree, points: list[Point]
    ) -> dict[int, tuple[int, float]]:
        found, distance = tree.query_nearest(
            np.array(points, dtype=object),
            max_distance=self.max_snap_m,
            return_distance=True,
            all_matches=False,
        )
        return {
            int(i): (int(j), float(d))
            for i, j, d in zip(found[0].tolist(), found[1].tolist(), distance.tolist())
        }

    def snap(self, points: list[Point]) -> tuple[list, list[float], list[int]]:
        """(Knoten oder None, Fangabstand oder NaN, Positionen der umgelenkten Punkte)."""
        overall = self._nearest(self.tree_all, points)
        if self.snap_to == "any_node":
            chosen = {i: (self.node_ids[j], d) for i, (j, d) in overall.items()}
            diverted: list[int] = []
        else:
            in_main = self._nearest(self.tree_main, points)
            chosen = {
                i: (self.node_ids[int(self.main[j])], d)
                for i, (j, d) in in_main.items()
            }
            diverted = [
                i
                for i, (j, _) in overall.items()
                if self.node_ids[j] not in self.in_largest and i in chosen
            ]
        nodes = [chosen[i][0] if i in chosen else None for i in range(len(points))]
        distances = [chosen[i][1] if i in chosen else NAN for i in range(len(points))]
        return nodes, distances, diverted


# --- Weg als Linie ---


def _oriented(
    coords: list[tuple[float, float]], start: Point
) -> list[tuple[float, float]]:
    """Die Koordinaten so gedreht, dass sie am Punkt start beginnen."""
    to_first = math.hypot(coords[0][0] - start.x, coords[0][1] - start.y)
    to_last = math.hypot(coords[-1][0] - start.x, coords[-1][1] - start.y)
    return coords if to_first <= to_last else coords[::-1]


def _path_line(nx_graph: nx.Graph, path: list) -> tuple[LineString, int]:
    """Die Route als Linie entlang der Kantengeometrien; zweiter Wert: Zahl der Kanten
    ohne Geometrie (als Gerade gezeichnet)."""
    coords: list[tuple[float, float]] = []
    straight = 0
    for u, v in zip(path[:-1], path[1:]):
        node_u, node_v = nx_graph.nodes[u]["geometry"], nx_graph.nodes[v]["geometry"]
        geometry = nx_graph.edges[u, v].get("geometry")
        if geometry is None or geometry.is_empty:
            straight += 1
            piece = [(node_u.x, node_u.y), (node_v.x, node_v.y)]
        else:
            piece = _oriented([(c[0], c[1]) for c in geometry.coords], node_u)
        coords.extend(piece[1:] if coords and coords[-1] == piece[0] else piece)
    return LineString(coords), straight


# --- Operation ---


def _count_text(n: int, total: int) -> str:
    return f"{n} von {total}"


@register_operation(RouteStep)
def run_route(inputs: dict, params: dict) -> GeoDataFrame:
    graph = _require_graph(inputs["graph"])
    origins = _require_places(inputs["origins"], "origins")
    destinations = _require_places(inputs["destinations"], "destinations")
    p = RouteStep.Params.model_validate(params)
    _require_same_crs(graph, origins, destinations)

    nx_graph = graph.nx_graph
    if nx_graph.number_of_nodes() == 0 or nx_graph.number_of_edges() == 0:
        raise ValueError(
            "route: der Graph hat keine Kanten - es gibt kein Netz, auf dem geroutet werden könnte"
        )
    _require_edge_attribute(
        nx_graph,
        "length",
        "Kantenlänge in Metern, von build_network/line_network gesetzt",
    )
    if p.weight == "travel_time_min":
        _require_edge_attribute(
            nx_graph,
            "travel_time_min",
            "Fahrzeit; in line_network speed_kmh angeben oder weight: length wählen",
        )
    has_time = all(
        "travel_time_min" in data for _, _, data in nx_graph.edges(data=True)
    )

    pairs = _pairs(len(origins), len(destinations), p.pairing)
    origin_points = _representative_points(origins)
    destination_points = _representative_points(destinations)
    origin_labels, destination_labels = _labels(origins), _labels(destinations)

    snapper = _Snapper(nx_graph, p.max_snap_m, p.snap_to)
    origin_nodes, origin_snap, origin_diverted = snapper.snap(origin_points)
    destination_nodes, destination_snap, destination_diverted = snapper.snap(
        destination_points
    )

    diverted = len(origin_diverted) + len(destination_diverted)
    if diverted:
        names = [origin_labels[i] for i in origin_diverted] + [
            destination_labels[i] for i in destination_diverted
        ]
        warnings.warn(
            f"route: bei {diverted} Ort(en) liegt der nächste Netzknoten in einer kleineren "
            f"Zusammenhangskomponente ({', '.join(repr(n) for n in names[:5])}"
            f"{', ...' if len(names) > 5 else ''}); gefangen wurde stattdessen in der größten "
            "Komponente (snap_to: any_node fängt am absolut nächsten Knoten)",
            OtherComponentWarning,
            stacklevel=2,
        )

    rows: list[dict] = []
    geometries: list = []
    unsnapped = unreachable = same_node = without_geometry = 0
    for i, j in pairs:
        a, b = origin_nodes[i], destination_nodes[j]
        straight_line = (
            origin_points[i].distance(destination_points[j])
            if origin_points[i] is not None and destination_points[j] is not None
            else NAN
        )
        row = {
            "origin": origin_labels[i],
            "destination": destination_labels[j],
            "length_m": NAN,
            "travel_time_min": NAN,
            "straight_line_m": straight_line,
            "detour_factor": NAN,
            "origin_snap_m": origin_snap[i],
            "destination_snap_m": destination_snap[j],
        }
        line = None
        if a is None or b is None:
            unsnapped += 1
        elif a == b:
            same_node += 1
            row["length_m"], row["travel_time_min"] = 0.0, 0.0
        else:
            try:
                _, path = nx.single_source_dijkstra(
                    nx_graph, a, target=b, weight=p.weight
                )
            except nx.NetworkXNoPath:
                unreachable += 1
            else:
                edges = [nx_graph.edges[u, v] for u, v in zip(path[:-1], path[1:])]
                row["length_m"] = float(sum(edge["length"] for edge in edges))
                if has_time:
                    row["travel_time_min"] = float(
                        sum(edge["travel_time_min"] for edge in edges)
                    )
                if straight_line > 0:  # NaN > 0 ist falsch
                    row["detour_factor"] = row["length_m"] / straight_line
                line, straight_edges = _path_line(nx_graph, path)
                without_geometry += straight_edges
        rows.append(row)
        geometries.append(line)

    total = len(pairs)
    routed = sum(1 for row in rows if not math.isnan(row["length_m"]))
    if routed == 0:
        raise ValueError(
            f"route: kein einziges der {total} Paare ließ sich routen "
            f"({unsnapped} nicht gefangen - weiter als max_snap_m={p.max_snap_m:g} vom nächsten "
            f"Netzknoten; {unreachable} ohne Weg im Netz). max_snap_m erhöhen oder das Netz "
            "(Region, Straßenklassen, tolerance_m) prüfen"
        )
    if unsnapped:
        warnings.warn(
            f"route: {_count_text(unsnapped, total)} Paaren fehlt ein Netzknoten innerhalb "
            f"max_snap_m={p.max_snap_m:g}; sie erhalten NaN und keine Geometrie",
            UnsnappableFeatureWarning,
            stacklevel=2,
        )
    if unreachable:
        warnings.warn(
            f"route: {_count_text(unreachable, total)} Paaren liegen im Netz in getrennten "
            "Zusammenhangskomponenten (kein Weg); sie erhalten NaN und keine Geometrie",
            UnreachableFeatureWarning,
            stacklevel=2,
        )
    if same_node:
        warnings.warn(
            f"route: bei {_count_text(same_node, total)} Paaren fangen Ursprung und Ziel am "
            "selben Netzknoten - Länge 0, keine Linie",
            SameNodeWarning,
            stacklevel=2,
        )
    if without_geometry:
        warnings.warn(
            f"route: {without_geometry} Kante(n) auf den Routen tragen keine Geometrie und sind "
            "als Gerade zwischen ihren Knoten gezeichnet",
            EdgeGeometryMissingWarning,
            stacklevel=2,
        )

    result = GeoDataFrame(rows, geometry=geometries, crs=graph.crs)
    if not has_time:
        result = result.drop(columns="travel_time_min")
    return result
