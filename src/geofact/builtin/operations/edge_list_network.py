"""Implements: FA76 (graph from an explicit edge list: edge_list_network).

Builds a graph from a node layer with a key column and an edge layer that names
its two end nodes by key (``from_column``/``to_column``), as power grid models
(PyPSA-Eur), GTFS-like tables or routing exports ship their topology. Unlike
``build_network`` (FA10), no geometry is matched: an edge connects exactly the
two nodes its keys name. The output is the same ``Graph``, so ``centrality``,
``graph_to_vector``, ``route`` and the graph outputs work unchanged.

Edge length (attribute ``length``, metres - the weight ``centrality`` and ``route``
read): from ``length_column`` (converted by ``length_unit``) if given; else, and
for rows with a missing value, the planar length of the edge geometry in the
working CRS, or the straight line between the two nodes for an edge without
geometry. The fallback for missing values is reported with one warning. An edge
whose length cannot be determined at all is an error, since a NaN weight would
silently falsify ``centrality`` and ``route``.

Edge geometry: lines only. A MultiLineString is merged into one line
(``shapely.line_merge``); one that does not merge keeps its length but is stored
without geometry (``route`` draws a straight line), with one ``EdgeGeometryWarning``.
Any other geometry type is an error.

No silent errors:

- a node key that is missing, empty or duplicated is an error (with examples);
- nodes and edges that both carry geometry must share the CRS;
- edge columns named ``length`` (unless it is ``length_column``), ``geometry`` or
  ``parallel_edges`` are overwritten; one ``EdgeAttributeOverwrittenWarning``
  names them;
- an edge whose key names no node is an error by default (``unknown_nodes: error``);
  with ``unknown_nodes: drop`` it is left out with one warning and the count;
- a self loop is left out with a warning;
- parallel edges between the same two nodes become one edge (``networkx.Graph``),
  the shortest one is kept and ``parallel_edges`` counts the merged rows;
- more than one connected component is reported (``DisconnectedNetworkWarning``).

Node attributes are all columns of the node layer (the key is the node id); edge
attributes are all columns of the edge layer plus ``length``, ``geometry`` and
``parallel_edges``. Non-point node geometries are reduced to their centroid."""

from __future__ import annotations

import math
import warnings
from typing import Any, ClassVar, Literal, Optional, Self

import networkx as nx
import pandas as pd
import shapely
from geopandas import GeoDataFrame
from pydantic import Field, model_validator

from geofact.builtin.operations.graph import DisconnectedNetworkWarning
from geofact.plugin_api import DataType, Graph, ParamsBase, Step, register_operation
from geofact.support.numeric import require_numeric


class DanglingEdgeWarning(UserWarning):
    """Edges naming an unknown node key were left out (``unknown_nodes: drop``)."""


class SelfLoopWarning(UserWarning):
    """Edges from a node to itself were left out."""


class ParallelEdgesMergedWarning(UserWarning):
    """Several edges between the same two nodes were merged into one."""


class EdgeLengthFallbackWarning(UserWarning):
    """Edges without a value in ``length_column`` got their geometric length."""


class EdgeGeometryWarning(UserWarning):
    """Multi-part edge geometries that do not merge into one line were dropped."""


class EdgeAttributeOverwrittenWarning(UserWarning):
    """Edge columns with a reserved name were overwritten at the edges."""


_UNIT_FACTOR = {"m": 1.0, "km": 1000.0}
_RESERVED = ("length", "geometry", "parallel_edges")
_EXAMPLES = 3


class EdgeListNetworkStep(Step):
    op: Literal["edge_list_network"] = "edge_list_network"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "nodes": DataType.VECTOR,
        "edges": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.GRAPH

    class Params(ParamsBase):
        node_key: str = Field(
            ...,
            description="Spalte des Knoten-Layers, die die Knoten benennt "
            "(eindeutig, ohne leere Werte).",
        )
        from_column: str = Field(
            ...,
            description="Spalte des Kanten-Layers mit dem Schlüssel des Anfangsknotens.",
        )
        to_column: str = Field(
            ...,
            description="Spalte des Kanten-Layers mit dem Schlüssel des Endknotens.",
        )
        length_column: Optional[str] = Field(
            None,
            description="Spalte mit der Kantenlänge (Gewicht für centrality/route); "
            "ohne Angabe die Länge der Kantengeometrie im Arbeits-CRS.",
        )
        length_unit: Literal["m", "km"] = Field(
            "m",
            description="Einheit von length_column; gespeichert wird immer in Metern.",
        )
        unknown_nodes: Literal["error", "drop"] = Field(
            "error",
            description="Kanten mit unbekanntem Knotenschlüssel: error (Abbruch "
            "mit Beispielen) oder drop (weglassen, mit Warnung und Anzahl).",
        )

        @model_validator(mode="after")
        def distinct_columns(self) -> Self:
            if self.from_column == self.to_column:
                raise ValueError(
                    f"from_column und to_column müssen verschieden sein (beide '{self.from_column}')"
                )
            return self


def _require_column(frame: GeoDataFrame, column: str, layer: str) -> None:
    if column not in frame.columns:
        present = sorted(c for c in frame.columns if c != frame.geometry.name)
        raise ValueError(
            f"edge_list_network: Spalte '{column}' fehlt im Layer '{layer}' (vorhanden: {present})"
        )


def _examples(values) -> str:
    return ", ".join(repr(v) for v in list(dict.fromkeys(values))[:_EXAMPLES])


def _node_keys(nodes: GeoDataFrame, node_key: str) -> list:
    _require_column(nodes, node_key, "nodes")
    keys = nodes[node_key]
    blank = keys.map(lambda value: isinstance(value, str) and not value.strip()).astype(
        bool
    )
    empty = int((keys.isna() | blank).sum())
    if empty:
        raise ValueError(
            f"edge_list_network: node_key '{node_key}' enthält {empty} leere Werte "
            "(fehlend oder leere Zeichenkette)"
        )
    duplicated = keys[keys.duplicated(keep=False)]
    if not duplicated.empty:
        raise ValueError(
            f"edge_list_network: node_key '{node_key}' ist nicht eindeutig "
            f"({int(keys.duplicated().sum())} doppelte Werte, z. B. {_examples(duplicated)}) - "
            "doppelte Knoten vorher mit deduplicate entfernen"
        )
    return list(keys)


def _has_geometry(frame: GeoDataFrame) -> bool:
    geometry = frame.geometry
    return bool((geometry.notna() & ~geometry.is_empty).any())


def _clean(value: Any) -> Any:
    """Missing values (NaN/NA) as None, so node and edge attributes stay JSON-safe."""
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _edge_geometry(geometry: Any) -> tuple[Any, bool]:
    """(geometry stored at the edge, True if a multi-part line was dropped)."""
    if geometry is None or geometry.is_empty:
        return None, False
    if geometry.geom_type == "LineString":
        return geometry, False
    if geometry.geom_type == "MultiLineString":
        merged = shapely.line_merge(geometry)
        if merged.geom_type == "LineString":
            return merged, False
        return None, True
    raise ValueError(
        f"edge_list_network: Kantengeometrie vom Typ {geometry.geom_type} - Kanten brauchen "
        "Linien (LineString/MultiLineString) oder keine Geometrie"
    )


def _edge_lengths(
    edges: GeoDataFrame, params: dict, node_geometry: dict
) -> list[float]:
    """Length in metres per edge row."""
    column = params.get("length_column")
    declared = pd.Series([math.nan] * len(edges), index=edges.index, dtype="float64")
    if column is not None:
        _require_column(edges, column, "edges")
        factor = _UNIT_FACTOR[params.get("length_unit", "m")]
        declared = (
            require_numeric(
                edges[column], where="edge_list_network", column=column
            ).astype("float64")
            * factor
        )
    lengths: list[float] = []
    fallback = 0
    for value, geometry, u, v in zip(
        declared,
        edges.geometry,
        edges[params["from_column"]],
        edges[params["to_column"]],
    ):
        if not math.isnan(value):
            lengths.append(float(value))
            continue
        if column is not None:
            fallback += 1
        if geometry is not None and not geometry.is_empty:
            lengths.append(float(geometry.length))
        else:
            a, b = node_geometry.get(u), node_geometry.get(v)
            defined = all(g is not None and not g.is_empty for g in (a, b))
            lengths.append(float(a.distance(b)) if defined else math.nan)
    if fallback:
        warnings.warn(
            f"edge_list_network: {fallback} von {len(edges)} Kante(n) ohne Wert in "
            f"'{column}' - ihre Länge ist die Länge der Kantengeometrie im Arbeits-CRS "
            "(ohne Geometrie: Luftlinie zwischen den Knoten)",
            EdgeLengthFallbackWarning,
            stacklevel=3,
        )
    return lengths


def _check_dangling(edges: GeoDataFrame, params: dict, known: set) -> pd.Series:
    """Mask of edges whose two keys name existing nodes; error or warning otherwise."""
    from_col, to_col = params["from_column"], params["to_column"]
    unknown_from = ~edges[from_col].isin(known)
    unknown_to = ~edges[to_col].isin(known)
    dangling = unknown_from | unknown_to
    count = int(dangling.sum())
    if count:
        examples = _examples(
            list(edges.loc[unknown_from, from_col])
            + list(edges.loc[unknown_to, to_col])
        )
        detail = (
            f"{count} von {len(edges)} Kante(n) nennen einen unbekannten Knoten "
            f"('{from_col}': {int(unknown_from.sum())}, '{to_col}': {int(unknown_to.sum())}; "
            f"z. B. {examples})"
        )
        if params.get("unknown_nodes", "error") == "error":
            raise ValueError(
                f"edge_list_network: {detail} - Knoten-Layer ergänzen oder "
                "unknown_nodes: drop setzen"
            )
        warnings.warn(
            f"edge_list_network: {detail}; sie wurden weggelassen (unknown_nodes: drop)",
            DanglingEdgeWarning,
            stacklevel=3,
        )
    return ~dangling


@register_operation(EdgeListNetworkStep)
def run_edge_list_network(inputs: dict, params: dict) -> Graph:
    nodes: GeoDataFrame = inputs["nodes"]
    edges: GeoDataFrame = inputs["edges"]
    from_col, to_col = params["from_column"], params["to_column"]
    keys = _node_keys(nodes, params["node_key"])
    _require_column(edges, from_col, "edges")
    _require_column(edges, to_col, "edges")
    if _has_geometry(nodes) and _has_geometry(edges) and nodes.crs != edges.crs:
        raise ValueError(
            f"edge_list_network: Knoten ({nodes.crs}) und Kanten ({edges.crs}) haben "
            "verschiedene CRS - beide Layer ins Arbeits-CRS bringen bzw. crs angeben"
        )

    nx_graph = nx.Graph()
    node_geometry: dict = {}
    geometry_name = nodes.geometry.name
    attribute_columns = [c for c in nodes.columns if c != geometry_name]
    for key, geometry, values in zip(
        keys,
        nodes.geometry,
        nodes[attribute_columns].itertuples(index=False, name=None),
    ):
        if (
            geometry is not None
            and not geometry.is_empty
            and geometry.geom_type != "Point"
        ):
            geometry = geometry.centroid
        node_geometry[key] = geometry
        nx_graph.add_node(
            key,
            geometry=geometry,
            **{
                column: _clean(value)
                for column, value in zip(attribute_columns, values)
            },
        )

    keep = _check_dangling(edges, params, set(keys))
    edges = edges[keep]
    lengths = _edge_lengths(edges, params, node_geometry)

    edge_geometry = edges.geometry.name
    edge_columns = [c for c in edges.columns if c != edge_geometry]
    overwritten = [
        c
        for c in _RESERVED
        if c in edge_columns
        and not (c == "length" and params.get("length_column") == "length")
    ]
    if overwritten:
        warnings.warn(
            f"edge_list_network: Spalte(n) {overwritten} der Kanten werden an den Kanten "
            "überschrieben (length = Länge in Metern, geometry = Kantengeometrie, "
            "parallel_edges = Zahl zusammengefasster Zeilen) - Spalte vorher umbenennen, "
            "um den Wert zu behalten",
            EdgeAttributeOverwrittenWarning,
            stacklevel=2,
        )
    self_loops = 0
    parallel = 0
    dropped_geometry = 0
    undefined: list[tuple] = []
    for length, geometry, values in zip(
        lengths, edges.geometry, edges[edge_columns].itertuples(index=False, name=None)
    ):
        row = dict(zip(edge_columns, values))
        u, v = row[from_col], row[to_col]
        if u == v:
            self_loops += 1
            continue
        if math.isnan(length):
            undefined.append((u, v))
            continue
        attributes = {column: _clean(value) for column, value in row.items()}
        stored, dropped = _edge_geometry(geometry)
        dropped_geometry += dropped
        attributes.update(length=length, geometry=stored)
        if nx_graph.has_edge(u, v):
            parallel += 1
            existing = nx_graph.edges[u, v]
            merged = existing["parallel_edges"] + 1
            if not length < existing["length"]:
                existing["parallel_edges"] = merged
                continue
            attributes["parallel_edges"] = merged
            nx_graph.remove_edge(u, v)
        else:
            attributes["parallel_edges"] = 1
        nx_graph.add_edge(u, v, **attributes)

    if undefined:
        raise ValueError(
            f"edge_list_network: {len(undefined)} Kante(n) ohne bestimmbare Länge (kein Wert "
            f"in length_column, keine Kantengeometrie, Knoten ohne Geometrie; z. B. "
            f"{_examples(undefined)}) - length_column angeben oder Geometrie für Kanten "
            "bzw. Knoten liefern"
        )
    if dropped_geometry:
        warnings.warn(
            f"edge_list_network: {dropped_geometry} Kante(n) mit mehrteiliger Liniengeometrie, "
            "die sich nicht zu einer Linie verbinden lässt; ihre Länge bleibt, die Geometrie "
            "wird nicht gespeichert (route zeichnet sie als Gerade)",
            EdgeGeometryWarning,
            stacklevel=2,
        )
    if self_loops:
        warnings.warn(
            f"edge_list_network: {self_loops} Kante(n) verbinden einen Knoten mit sich selbst "
            "und wurden weggelassen",
            SelfLoopWarning,
            stacklevel=2,
        )
    if parallel:
        warnings.warn(
            f"edge_list_network: {parallel} Kante(n) verlaufen parallel zu einer anderen "
            "zwischen denselben zwei Knoten; je Knotenpaar gibt es eine Kante (die kürzeste), "
            "parallel_edges zählt die zusammengefassten Zeilen",
            ParallelEdgesMergedWarning,
            stacklevel=2,
        )
    if nx_graph.number_of_nodes():
        components = [len(c) for c in nx.connected_components(nx_graph)]
        if len(components) > 1:
            warnings.warn(
                f"edge_list_network: Netz besteht aus {len(components)} getrennten "
                f"Zusammenhangskomponenten (größte: {max(components)} von "
                f"{nx_graph.number_of_nodes()} Knoten, {sum(1 for c in components if c == 1)} "
                "isolierte Knoten) - so in den Quelldaten, nicht durch Fangen entstanden",
                DisconnectedNetworkWarning,
                stacklevel=2,
            )
    return Graph(nx_graph, crs=nodes.crs if nodes.crs is not None else edges.crs)
