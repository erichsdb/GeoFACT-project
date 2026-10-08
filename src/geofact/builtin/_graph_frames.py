"""Implements: FA53 (Graphen als Ausgabe: Kanten und Knoten als Vektor-Layer).

Macht aus einem Graph (FA10/FA50) einen Vektor-Layer, den die Ausgabeformate
(GeoJSON, CSV, Karte) schreiben können:

- Kanten-Layer: eine Linie je Kante mit allen Kantenattributen (z. B. ``length``,
  ``highway``, ``is_bridge``) und den Knoten der Kante in ``node_u`` / ``node_v``.
  Die Geometrie ist die Kantengeometrie; eine Kante ohne eigene Geometrie wird als
  Gerade zwischen ihren Knoten gezeichnet (Warnung mit der Zahl).
- Knoten-Layer: ein Punkt je Knoten mit ``id`` und allen Knotenattributen (z. B.
  ``betweenness`` nach centrality) - dieselbe Form wie graph_to_vector.

Ein Modul mit führendem Unterstrich wird nicht als Baustein gescannt und
registriert nichts."""

from __future__ import annotations

import warnings
from typing import Any, Literal

import geopandas as gpd
from geopandas import GeoDataFrame
from shapely.geometry import LineString

from geofact.plugin_api import Graph

GraphPart = Literal["edges", "nodes"]


class EdgeWithoutGeometryWarning(UserWarning):
    """Kanten ohne eigene Geometrie sind als Gerade zwischen ihren Knoten ausgegeben (FA53)."""


def nodes_frame(graph: Graph) -> GeoDataFrame:
    """Ein Punkt je Knoten: Spalte ``id`` + alle Knotenattribute."""
    rows: list[dict[str, Any]] = []
    geometries = []
    for node_id, attrs in graph.nx_graph.nodes(data=True):
        attrs = dict(attrs)
        geometries.append(attrs.pop("geometry", None))
        rows.append({"id": node_id, **attrs})
    if not rows:
        return gpd.GeoDataFrame({"id": []}, geometry=[], crs=graph.crs)
    return gpd.GeoDataFrame(rows, geometry=geometries, crs=graph.crs)


def edges_frame(graph: Graph) -> GeoDataFrame:
    """Eine Linie je Kante: ``node_u``, ``node_v`` + alle Kantenattribute."""
    nx_graph = graph.nx_graph
    rows: list[dict[str, Any]] = []
    geometries = []
    straight = 0
    shadowed: set[str] = set()
    for u, v, attrs in nx_graph.edges(data=True):
        attrs = dict(attrs)
        for key in ("node_u", "node_v"):
            # die Knoten-ids sind die Topologie; ein gleichnamiges Kantenattribut
            # (z. B. eine Linienspalte) darf sie nicht ersetzen
            if key in attrs:
                attrs.pop(key)
                shadowed.add(key)
        geometry = attrs.pop("geometry", None)
        if geometry is None or geometry.is_empty:
            node_u, node_v = (
                nx_graph.nodes[u].get("geometry"),
                nx_graph.nodes[v].get("geometry"),
            )
            if node_u is not None and node_v is not None:
                geometry = LineString([(node_u.x, node_u.y), (node_v.x, node_v.y)])
                straight += 1
            else:
                geometry = None
        geometries.append(geometry)
        rows.append({"node_u": u, "node_v": v, **attrs})
    if shadowed:
        warnings.warn(
            f"Graph-Ausgabe: Kantenattribut(e) {sorted(shadowed)} heißen wie die Spalten der "
            "Knoten-ids und sind nicht ausgegeben - die Spalten nennen die Endknoten der Kante",
            stacklevel=2,
        )
    if straight:
        warnings.warn(
            f"Graph-Ausgabe: {straight} von {nx_graph.number_of_edges()} Kanten tragen keine "
            "Geometrie und sind als Gerade zwischen ihren Knoten ausgegeben",
            EdgeWithoutGeometryWarning,
            stacklevel=2,
        )
    if not rows:
        return gpd.GeoDataFrame(
            {"node_u": [], "node_v": []}, geometry=[], crs=graph.crs
        )
    return gpd.GeoDataFrame(rows, geometry=geometries, crs=graph.crs)


def frame_for_output(data: Any, spec: Any) -> GeoDataFrame:
    """Der Vektor-Layer, den ein Ausgabeformat schreibt: ein Vektor-Layer bleibt
    unverändert, ein Graph wird je nach ``graph_part`` (Standard ``edges``) zum Kanten-
    oder Knoten-Layer. ``graph_part`` an einer Vektor-Quelle ist ein Fehler - es würde
    sonst stillschweigend ignoriert."""
    explicit = (getattr(spec, "model_extra", None) or {}).get("graph_part")
    if isinstance(data, Graph):
        part = getattr(getattr(spec, "options", None), "graph_part", "edges")
        return nodes_frame(data) if part == "nodes" else edges_frame(data)
    if explicit is not None:
        raise ValueError(
            f"graph_part: {explicit} gilt nur für eine Graph-Quelle, die Quelle "
            f"'{getattr(spec, 'source', '?')}' ist ein Vektor-Layer"
        )
    return data
