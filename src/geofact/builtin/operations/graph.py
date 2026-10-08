"""Implements: FA10 (Graphmodul).

Konstruiert aus Linien-/Knoten-Layern einen Graphen (networkx.Graph) und berechnet
Netzwerkmetriken: Betweenness-Zentralität, Brücken, Artikulationspunkte,
Zusammenhangskomponenten. graph_to_vector führt die Knoten mit ihren Metriken in
einen Punkt-Layer zurück, mit ``part: edges`` die Kanten als Linien-Layer (FA53,
Form wie die Graph-Ausgaben: ``node_u``, ``node_v`` + Kantenattribute).

Knotenschlüssel (build_network): die Spalte 'id' des Knoten-Layers, wenn sie
vorhanden, ohne leere Werte und eindeutig ist. Sonst heißen die Knoten wie die
Zeilen des Layers (Zeilenindex); ist 'id' vorhanden, aber unbrauchbar, oder der
Zeilenindex nicht eindeutig, gilt die Zeilenposition 0..n-1, mit einer Warnung, die
den Grund nennt. Der Parameter node_key wählt eine Spalte ausdrücklich, dann ist
ein unbrauchbarer Schlüssel ein Fehler. centrality arbeitet auf einer Kopie.

Kantengeometrie (FA51/FA53): jede Kante trägt neben 'length' (Meter) das Attribut
'geometry', das Linienstück im Arbeits-CRS des Graphen. Gelesen wird es nur von
route (FA51) und den Graph-Ausgaben (FA53); isochrone bleibt bei der Luftlinie
zwischen Endknoten.

Parallele Linien (build_network): je Knotenpaar gibt es genau eine Kante; verbinden
mehrere Linien dieselben zwei Knoten, bleibt die kürzeste (``ParallelEdgeWarning``).
Eine Spalte ``length`` in 'lines' wird an der Kante durch die Länge des
Linienstücks ersetzt (mit Warnung).

Verworfene Linienstücke: ein Stück, dessen Anfang oder Ende keinen Knoten
innerhalb tolerance_m findet, wird keine Kante; eine ``DroppedLinePieceWarning``
nennt die Anzahl je Grund. Schleifen (beide Enden am selben Knoten) bleiben Kanten.
"""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal, Optional

import networkx as nx
from geopandas import GeoDataFrame
from pydantic import Field
from shapely.geometry import Point

from geofact.builtin._graph_frames import edges_frame, nodes_frame
from geofact.plugin_api import DataType, Graph, ParamsBase, register_operation, Step


class DisconnectedNetworkWarning(UserWarning):
    """Signalisiert, dass das aus 'lines' konstruierte Netz aus mehr als
    einer Zusammenhangskomponente besteht (FA10-Vorbedingung: Linien-
    Layer ist topologisch verbunden oder über Toleranz verbindbar)."""


class ParallelEdgeWarning(UserWarning):
    """Mehrere Linien verbinden dieselben zwei Knoten; der Graph (networkx.Graph)
    hat je Knotenpaar eine Kante, behalten wird die kürzeste Linie."""


class DroppedLinePieceWarning(UserWarning):
    """Linienstücke aus 'lines', deren Anfang und/oder Ende keinen Knoten
    innerhalb tolerance_m findet und die deshalb keine Kante wurden; die
    Warnung nennt die Anzahl je Grund."""


class NodeKeyWarning(UserWarning):
    """Signalisiert, dass die Spalte 'id' des Knoten-Layers nicht als
    Knotenschlüssel taugt (leere oder doppelte Werte) oder der Zeilenindex
    nicht eindeutig ist, und die Knoten stattdessen nach Zeilenindex bzw.
    Zeilenposition nummeriert werden."""


class NonLinearGeometryWarning(UserWarning):
    """Signalisiert, dass 'lines' Geometrien enthält, die keine (Multi)
    LineString sind (z. B. weil eine OSM-Tag-Kombination sowohl als Weg
    als auch als Fläche vorkommt - PostgisBackend vereinigt alle
    plausiblen Tabellen). Solche Objekte werden übersprungen, nicht die
    ganze Operation abgebrochen."""


def _nearest_node_within_tolerance(
    point, node_ids: list, node_points: GeoDataFrame, tolerance_m: float
) -> object | None:
    distances = node_points.geometry.distance(point)
    nearest_pos = distances.values.argmin()
    if distances.iloc[nearest_pos] <= tolerance_m:
        return node_ids[nearest_pos]
    return None


def _key_problem(series) -> str | None:
    """Warum eine Spalte kein Knotenschlüssel ist (None: sie taugt)."""
    empty = int(series.isna().sum())
    if empty:
        return f"enthält {empty} leere Werte"
    duplicated = series[series.duplicated(keep=False)]
    if not duplicated.empty:
        examples = ", ".join(
            repr(value) for value in list(dict.fromkeys(duplicated))[:3]
        )
        return (
            f"ist nicht eindeutig ({int(series.duplicated().sum())} doppelte Werte, "
            f"z. B. {examples})"
        )
    return None


def _node_ids(nodes: GeoDataFrame, node_key: str | None) -> list:
    """Knotenschlüssel je Zeile des Knoten-Layers (siehe Modul-Docstring)."""
    if node_key is not None:
        if node_key not in nodes.columns:
            raise ValueError(
                f"build_network: node_key '{node_key}' ist keine Spalte des Knoten-Layers "
                f"(vorhanden: {sorted(c for c in nodes.columns if c != 'geometry')})"
            )
        problem = _key_problem(nodes[node_key])
        if problem is not None:
            raise ValueError(f"build_network: node_key '{node_key}' {problem}")
        return list(nodes[node_key])
    if nodes.empty:
        return []
    problem = None
    if "id" in nodes.columns:
        problem = _key_problem(nodes["id"])
        if problem is None:
            return list(nodes["id"])
        problem = f"Spalte 'id' des Knoten-Layers {problem}"
    # Ohne brauchbare 'id' benennen die Zeilen die Knoten (Zeilenindex; ohne
    # 'id'-Spalte ist das der Normalfall und keine Warnung wert).
    if nodes.index.is_unique:
        if problem is not None:
            _warn_node_key(problem, "Zeilenindex")
        return list(nodes.index)
    _warn_node_key(
        problem or "Zeilenindex des Knoten-Layers ist nicht eindeutig",
        "Zeilenposition (0..n-1)",
    )
    return list(range(len(nodes)))


def _warn_node_key(problem: str, numbering: str) -> None:
    warnings.warn(
        f"build_network: {problem} - die Knoten werden nach {numbering} nummeriert; mit "
        "node_key lässt sich eine eindeutige Spalte wählen",
        NodeKeyWarning,
        stacklevel=4,
    )


class BuildNetworkStep(Step):
    op: Literal["build_network"] = "build_network"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "lines": DataType.VECTOR,
        "nodes": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.GRAPH

    class Params(ParamsBase):
        tolerance_m: float = Field(
            0,
            ge=0,
            description="Fangtoleranz in Metern, um Linienenden Knoten zuzuordnen.",
        )
        node_key: Optional[str] = Field(
            None,
            description="Spalte des Knoten-Layers, die die Knoten benennt (eindeutig, ohne leere "
            "Werte). Ohne Angabe: 'id', falls tauglich, sonst der Zeilenindex "
            "(bei unbrauchbarer 'id' mit Warnung).",
        )


@register_operation(BuildNetworkStep)
def run_build_network(inputs: dict, params: dict) -> Graph:
    lines: GeoDataFrame = inputs["lines"]
    nodes: GeoDataFrame = inputs["nodes"]
    tolerance_m = params.get("tolerance_m", 0)

    # Knoten sind Punkte, auch wenn die Quellgeometrie eine Fläche ist (z. B. ein
    # Umspannwerk als Polygon): Centroid, damit Abstandsmatching konsistent bleibt.
    nodes = nodes.copy()
    nodes["geometry"] = nodes.geometry.centroid

    nx_graph = nx.Graph()

    node_ids = _node_ids(nodes, params.get("node_key"))
    for node_id, (_, row) in zip(node_ids, nodes.iterrows()):
        nx_graph.add_node(
            node_id,
            geometry=row.geometry,
            **{k: v for k, v in row.drop("geometry").items()},
        )

    def _match_endpoint(x: float, y: float):
        if nodes.empty:
            return None
        return _nearest_node_within_tolerance(Point(x, y), node_ids, nodes, tolerance_m)

    reserved = [column for column in ("length",) if column in lines.columns]
    if reserved:
        warnings.warn(
            f"build_network: Spalte(n) {reserved} in 'lines' werden an den Kanten durch die "
            "Länge des Linienstücks (Meter) ersetzt",
            stacklevel=2,
        )
    skipped_non_linear = 0
    parallel = 0
    n_pieces = 0
    unmatched = {"start": 0, "end": 0, "both": 0}
    for idx, row in lines.iterrows():
        line = row.geometry
        if line is None or line.geom_type not in ("LineString", "MultiLineString"):
            skipped_non_linear += 1
            continue
        # MultiLineString hat keine .coords: jedes Teilstück wird eine eigene Kante
        # mit den Attributen der Ausgangszeile.
        parts = list(line.geoms) if line.geom_type == "MultiLineString" else [line]
        for part in parts:
            n_pieces += 1
            coords = list(part.coords)
            start_id = _match_endpoint(*coords[0])
            end_id = _match_endpoint(*coords[-1])
            if start_id is None or end_id is None:
                reason = (
                    "both"
                    if start_id is None and end_id is None
                    else ("start" if start_id is None else "end")
                )
                unmatched[reason] += 1
                continue
            if nx_graph.has_edge(start_id, end_id):
                # parallele Linie (zweiter Stromkreis, Richtungsfahrbahn): eine
                # Kante je Knotenpaar - die kürzere bleibt, gemeldet statt still
                parallel += 1
                if part.length >= nx_graph.edges[start_id, end_id]["length"]:
                    continue
                nx_graph.remove_edge(start_id, end_id)
            attributes = {k: v for k, v in row.drop("geometry").items()}
            attributes.update(length=part.length, geometry=part)
            nx_graph.add_edge(start_id, end_id, **attributes)

    dropped = sum(unmatched.values())
    if dropped:
        warnings.warn(
            f"build_network: {dropped} von {n_pieces} Linienstück(en) wurden keine Kante, "
            f"weil kein Knoten innerhalb tolerance_m={tolerance_m:g} m liegt: "
            f"{unmatched['start']} am Anfang, {unmatched['end']} am Ende, "
            f"{unmatched['both']} an beiden Enden (Knoten-Layer passt nicht zu den "
            "Linienenden - network_nodes oder ein größeres tolerance_m helfen)",
            DroppedLinePieceWarning,
            stacklevel=2,
        )
    if parallel:
        warnings.warn(
            f"build_network: {parallel} Linie(n) verlaufen parallel zu einer anderen zwischen "
            "denselben zwei Knoten; je Knotenpaar gibt es eine Kante, behalten wurde jeweils "
            "die kürzeste Linie",
            ParallelEdgeWarning,
            stacklevel=2,
        )
    if skipped_non_linear:
        warnings.warn(
            f"build_network: {skipped_non_linear} Objekt(e) in 'lines' sind "
            "keine (Multi)LineString-Geometrie und wurden übersprungen",
            NonLinearGeometryWarning,
            stacklevel=2,
        )

    n_components = nx.number_connected_components(nx_graph)
    if n_components > 1:
        warnings.warn(
            f"build_network: Netz besteht aus {n_components} getrennten "
            "Zusammenhangskomponenten (FA10-Vorbedingung: Linien-Layer "
            "sollte verbunden oder über tolerance_m verbindbar sein)",
            DisconnectedNetworkWarning,
            stacklevel=2,
        )

    return Graph(nx_graph, crs=nodes.crs)


class CentralityStep(Step):
    op: Literal["centrality"] = "centrality"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"graph": DataType.GRAPH}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.GRAPH

    class Params(ParamsBase):
        method: Literal["betweenness"] = Field(
            "betweenness",
            description="Zentralitätsmaß (weitere Metriken werden stets mitberechnet).",
        )


@register_operation(CentralityStep)
def run_centrality(inputs: dict, params: dict) -> Graph:
    graph: Graph = inputs["graph"]
    # Kopie: der Eingabegraph gehört einem anderen Knoten des DAG.
    nx_graph = graph.nx_graph.copy()

    betweenness = nx.betweenness_centrality(nx_graph, weight="length")
    nx.set_node_attributes(nx_graph, betweenness, "betweenness")

    bridge_edges = set(nx.bridges(nx_graph))
    nx.set_edge_attributes(nx_graph, False, "is_bridge")
    for u, v in bridge_edges:
        nx_graph.edges[u, v]["is_bridge"] = True

    articulation_points = set(nx.articulation_points(nx_graph))
    is_articulation = {node: node in articulation_points for node in nx_graph.nodes}
    nx.set_node_attributes(nx_graph, is_articulation, "is_articulation_point")

    components = list(nx.connected_components(nx_graph))
    component_of = {node: i for i, comp in enumerate(components) for node in comp}
    nx.set_node_attributes(nx_graph, component_of, "component")

    return Graph(nx_graph, crs=graph.crs)


class GraphToVectorStep(Step):
    op: Literal["graph_to_vector"] = "graph_to_vector"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"graph": DataType.GRAPH}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        part: Literal["nodes", "edges"] = Field(
            "nodes",
            description="nodes = ein Punkt je Knoten mit id + Knotenattributen (Standard); "
            "edges = eine Linie je Kante mit node_u, node_v + Kantenattributen.",
        )

    def produced_fields(self) -> set[str]:
        if self.params.get("part") == "edges":
            # _graph_frames.edges_frame: node_u/node_v + Kantenattribute; 'length'
            # setzen build_network/line_network, 'is_bridge' setzt centrality.
            return {"node_u", "node_v", "length", "is_bridge"}
        # Knoten tragen 'id' plus die Metriken eines vorgelagerten centrality-
        # Schritts; 'is_bridge' ist ein Kantenattribut und erreicht diesen Pfad nicht.
        # Zu viel Deklariertes schadet nicht: unbekannte Spalten fallen beim Schnitt
        # mit den echten Spalten weg (view_columns.py).
        return {"id", "betweenness", "is_articulation_point", "component"}


@register_operation(GraphToVectorStep)
def run_graph_to_vector(inputs: dict, params: dict) -> GeoDataFrame:
    graph: Graph = inputs["graph"]
    if params.get("part", "nodes") == "edges":
        return edges_frame(graph)
    return nodes_frame(graph)
