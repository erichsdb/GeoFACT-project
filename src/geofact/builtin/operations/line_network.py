"""Implements: FA50 (Netz aus Linien mit Knotenbildung).

line_network baut aus einem Linien-Layer (z. B. OSM-Straßen) ein routingfähiges
Netz ohne Knoten-Layer, anders als build_network (FA10).

Knotenregel (die Topologie von OSM, nicht die Geometrie): Knoten sind Start- und
Endpunkt jeder Linie sowie jede Koordinate, die in mehr als einer Linie oder
zweimal in derselben Linie vorkommt. Wo sich Linien nur kreuzen, ohne eine
Koordinate zu teilen, entsteht kein Knoten: Brücken und Tunnel bleiben
unverbunden. Jede Linie wird an ihren Knoten in Stücke geteilt; jedes Stück ist
eine Kante mit 'length' (Meter), 'geometry', optional 'travel_time_min' und den
gewählten Attributen. Knoten heißen 0..n-1 in der Reihenfolge ihres ersten
Auftretens (gleiche Eingabe, gleiche Knoten).

Das Netz ist ungerichtet (nx.Graph), Einbahnregeln zählen nicht. Zwei Stücke
zwischen demselben Knotenpaar (etwa die Wege um eine Insel) lassen nur eine Kante
zu: die kürzere bleibt, die Zahl der verworfenen steht in einer Warnung. Für
kürzeste Wege ist das ohne Wirkung, ein Kanten-Layer (FA53) zeigt aber nicht jeden
Weg. Eine Schleife bleibt als Kante an einem Knoten erhalten.

Geschlossene OSM-Wege (Kreisverkehre) liefert der OSM-Konnektor als Polygone;
'polygons: boundary' nutzt ihre Umrandung, 'skip' überspringt sie mit Warnung.
Mit tolerance_m > 0 werden Koordinaten innerhalb dieser Distanz vor der
Knotenbildung zusammengezogen (Einfachverkettung).
"""

from __future__ import annotations

import warnings
from collections import Counter
from typing import Any, ClassVar, Literal, Optional

import networkx as nx
import numpy as np
import pandas as pd
import shapely
from geopandas import GeoDataFrame
from pydantic import Field, field_validator
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from geofact.builtin.operations.graph import (
    DisconnectedNetworkWarning,
    NonLinearGeometryWarning,
)
from geofact.plugin_api import DataType, Graph, ParamsBase, register_operation, Step

Coord = tuple[float, float]

RESERVED_EDGE_ATTRIBUTES = ("length", "geometry", "travel_time_min")
"""Kantenattribute, die die Operation selbst setzt - nicht als 'attributes' wählbar."""


class ParallelEdgesWarning(UserWarning):
    """Zwischen demselben Knotenpaar liegen mehrere Stücke; ein nx.Graph hat nur
    eine Kante je Paar, die kürzeste bleibt (FA50)."""


class NoLinesWarning(UserWarning):
    """'lines' enthielt keine verwendbare Linie - das Netz ist leer (FA50)."""


class LineNetworkStep(Step):
    """Baut aus einem Linien-Layer ein Netz (FA50): Knoten an Linienenden und an
    gemeinsamen Koordinaten, Kanten als Linienstücke dazwischen mit Länge in
    Metern, Geometrie und optional Fahrzeit. Kreuzungen ohne gemeinsame Koordinate
    (Brücken, Tunnel) bleiben unverbunden. Ungerichtet: Einbahnregeln werden nicht
    berücksichtigt."""

    op: Literal["line_network"] = "line_network"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"lines": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.GRAPH

    class Params(ParamsBase):
        speed_kmh: Optional[float] = Field(
            None,
            gt=0,
            description="Konstante Geschwindigkeit in km/h; setzt an jeder Kante "
            "'travel_time_min' (Minuten). Ohne Angabe gibt es keine Fahrzeit.",
        )
        tolerance_m: float = Field(
            0,
            ge=0,
            description="Fangtoleranz in Metern: Koordinaten innerhalb dieser Distanz fallen "
            "vor der Knotenbildung zusammen. 0 = nur exakt gleiche Koordinaten.",
        )
        attributes: list[str] = Field(
            default_factory=list,
            description="Spalten des Linien-Layers, die an jede Kante übernommen werden "
            "(z. B. highway, name). Ohne Angabe tragen Kanten nur length, "
            "geometry und ggf. travel_time_min.",
        )
        polygons: Literal["skip", "boundary"] = Field(
            "skip",
            description="Umgang mit Flächen im Linien-Layer (OSM liefert geschlossene Wege wie "
            "Kreisverkehre als Polygone): 'skip' überspringt sie mit Warnung, "
            "'boundary' nutzt ihre Umrandung als Linie.",
        )

        @field_validator("attributes")
        @classmethod
        def check_attributes(cls, names: list[str]) -> list[str]:
            reserved = sorted(set(names) & set(RESERVED_EDGE_ATTRIBUTES))
            if reserved:
                raise ValueError(
                    f"attributes darf {reserved} nicht enthalten - diese Kantenattribute setzt "
                    "line_network selbst"
                )
            if len(set(names)) != len(names):
                raise ValueError("attributes enthält doppelte Spaltennamen")
            return names


class _Part:
    """Ein lineares Teilstück einer Eingabezeile: Koordinaten + Attribute der Zeile."""

    __slots__ = ("coords", "attrs")

    def __init__(self, coords: list[Coord], attrs: dict[str, Any]) -> None:
        self.coords = coords
        self.attrs = attrs


def _dedupe(coords: list[Coord]) -> list[Coord]:
    """Entfernt unmittelbar aufeinanderfolgende gleiche Koordinaten (Segmente der Länge 0)."""
    result: list[Coord] = []
    for coord in coords:
        if not result or result[-1] != coord:
            result.append(coord)
    return result


def _clean(value: Any) -> Any:
    """Fehlende Attributwerte (NaN, NA, NaT) werden None - sauberer Kantenattributwert."""
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return value
    return None if isinstance(missing, (bool, np.bool_)) and missing else value


def _require_lines(value: object) -> GeoDataFrame:
    if not isinstance(value, GeoDataFrame):
        raise TypeError(
            f"line_network: Eingang 'lines' muss ein Vektor-Layer (GeoDataFrame) sein, "
            f"nicht {type(value).__name__}"
        )
    if value.crs is None:
        raise ValueError(
            "line_network: 'lines' hat kein CRS - Kantenlängen in Metern brauchen ein "
            "metrisches Arbeits-CRS (der Executor harmonisiert Layer, FA5)"
        )
    if value.crs.is_geographic:
        raise ValueError(
            f"line_network: 'lines' liegt in einem geographischen CRS ({value.crs}) - Längen "
            "wären Grad statt Meter. Layer vorher ins metrische Arbeits-CRS bringen (FA5)"
        )
    return value


def _linear_parts(
    lines: GeoDataFrame, attributes: list[str], polygons: str
) -> tuple[list[_Part], dict[str, int]]:
    """Zerlegt die Zeilen in lineare Teilstücke und zählt, was nicht verwendbar war."""
    missing = [name for name in attributes if name not in lines.columns]
    if missing:
        available = sorted(c for c in lines.columns if c != lines.geometry.name)
        raise ValueError(
            f"line_network: attributes {missing} sind keine Spalten des Linien-Layers "
            f"(vorhanden: {available})"
        )
    columns = {name: lines[name].tolist() for name in attributes}
    skipped = {"empty": 0, "polygon": 0, "other": 0}
    parts: list[_Part] = []

    def add(line: LineString, attrs: dict[str, Any]) -> None:
        coords = _dedupe([(c[0], c[1]) for c in line.coords])
        if len(coords) >= 2:
            parts.append(_Part(coords, attrs))

    for position, geometry in enumerate(lines.geometry):
        if geometry is None or geometry.is_empty:
            skipped["empty"] += 1
            continue
        attrs = {name: _clean(values[position]) for name, values in columns.items()}
        kind = geometry.geom_type
        if kind == "LineString":
            add(geometry, attrs)
        elif kind == "MultiLineString":
            for part in geometry.geoms:
                add(part, attrs)
        elif kind in ("Polygon", "MultiPolygon"):
            if polygons == "skip":
                skipped["polygon"] += 1
                continue
            shapes = [geometry] if kind == "Polygon" else list(geometry.geoms)
            for shape in shapes:
                for ring in (shape.exterior, *shape.interiors):
                    add(LineString(ring.coords), attrs)
        else:
            skipped["other"] += 1
    return parts, skipped


def _warn_skipped(skipped: dict[str, int]) -> None:
    if skipped["empty"]:
        warnings.warn(
            f"line_network: {skipped['empty']} Objekt(e) in 'lines' ohne Geometrie wurden "
            "übersprungen",
            NonLinearGeometryWarning,
            stacklevel=3,
        )
    other = skipped["polygon"] + skipped["other"]
    if other:
        detail = []
        if skipped["polygon"]:
            detail.append(
                f"davon {skipped['polygon']} Fläche(n) - mit polygons: boundary dient ihre "
                "Umrandung als Linie, z. B. für geschlossene OSM-Wege wie Kreisverkehre"
            )
        warnings.warn(
            f"line_network: {other} Objekt(e) in 'lines' sind keine (Multi)LineString-"
            f"Geometrie und wurden übersprungen ({'; '.join(detail) or 'andere Geometrietypen'})",
            NonLinearGeometryWarning,
            stacklevel=3,
        )


def _snap(parts: list[_Part], tolerance_m: float) -> tuple[list[_Part], int]:
    """Zieht Koordinaten innerhalb tolerance_m auf eine gemeinsame Koordinate
    (Einfachverkettung; Vertreter ist die zuerst aufgetretene Koordinate). Zweiter
    Rückgabewert: Zahl der Linien, die dabei auf einen Punkt zusammenfielen (zu kurz)."""
    unique = list(dict.fromkeys(coord for part in parts for coord in part.coords))
    points = shapely.points(np.array(unique, dtype="float64"))
    left, right = STRtree(points).query(
        points, predicate="dwithin", distance=tolerance_m
    )
    parent = list(range(len(unique)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for a, b in zip(left.tolist(), right.tolist()):
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[max(root_a, root_b)] = min(root_a, root_b)
    target = {coord: unique[find(i)] for i, coord in enumerate(unique)}

    snapped: list[_Part] = []
    for part in parts:
        coords = _dedupe([target[coord] for coord in part.coords])
        if len(coords) >= 2:
            snapped.append(_Part(coords, part.attrs))
    return snapped, len(parts) - len(snapped)


def _node_coordinates(parts: list[_Part]) -> dict[Coord, int]:
    """Knoten nach der Regel des Moduls; Nummern nach erstem Auftreten."""
    occurrences = Counter(coord for part in parts for coord in part.coords)
    endpoints = {coord for part in parts for coord in (part.coords[0], part.coords[-1])}
    node_ids: dict[Coord, int] = {}
    for part in parts:
        for coord in part.coords:
            if coord not in node_ids and (coord in endpoints or occurrences[coord] > 1):
                node_ids[coord] = len(node_ids)
    return node_ids


def _pieces(coords: list[Coord], node_ids: dict[Coord, int]) -> list[list[Coord]]:
    """Teilt eine Linie an ihren inneren Knoten."""
    pieces: list[list[Coord]] = []
    start = 0
    for index in range(1, len(coords) - 1):
        if coords[index] in node_ids:
            pieces.append(coords[start : index + 1])
            start = index
    pieces.append(coords[start:])
    return pieces


@register_operation(LineNetworkStep)
def run_line_network(inputs: dict, params: dict) -> Graph:
    lines = _require_lines(inputs["lines"])
    p = LineNetworkStep.Params.model_validate(params)

    parts, skipped = _linear_parts(lines, p.attributes, p.polygons)
    _warn_skipped(skipped)
    if p.tolerance_m > 0 and parts:
        parts, collapsed = _snap(parts, p.tolerance_m)
        if collapsed:
            warnings.warn(
                f"line_network: {collapsed} Linie(n) sind nicht länger als tolerance_m="
                f"{p.tolerance_m:g} m und fielen beim Zusammenziehen auf einen Punkt - sie entfallen",
                NonLinearGeometryWarning,
                stacklevel=2,
            )

    nx_graph = nx.Graph()
    if not parts:
        warnings.warn(
            "line_network: 'lines' enthält keine verwendbare Linie - das Netz ist leer",
            NoLinesWarning,
            stacklevel=2,
        )
        return Graph(nx_graph, crs=lines.crs)

    node_ids = _node_coordinates(parts)
    for coord, node_id in node_ids.items():
        nx_graph.add_node(node_id, geometry=Point(coord))

    minutes_per_metre = (
        60.0 / (p.speed_kmh * 1000.0) if p.speed_kmh is not None else None
    )
    parallel = 0
    for part in parts:
        for piece in _pieces(part.coords, node_ids):
            u, v = node_ids[piece[0]], node_ids[piece[-1]]
            geometry = LineString(piece)
            data: dict[str, Any] = {"length": geometry.length, "geometry": geometry}
            if minutes_per_metre is not None:
                data["travel_time_min"] = geometry.length * minutes_per_metre
            data.update(part.attrs)
            if nx_graph.has_edge(u, v):
                parallel += 1
                if data["length"] >= nx_graph.edges[u, v]["length"]:
                    continue
                nx_graph.remove_edge(u, v)
            nx_graph.add_edge(u, v, **data)

    if parallel:
        warnings.warn(
            f"line_network: {parallel} Stück(e) liegen parallel zu einem anderen Stück "
            "zwischen demselben Knotenpaar; ein ungerichtetes Netz hat nur eine Kante je "
            "Paar, die kürzeste blieb (kürzeste Wege ändern sich dadurch nicht)",
            ParallelEdgesWarning,
            stacklevel=2,
        )

    components = list(nx.connected_components(nx_graph))
    if len(components) > 1:
        largest = max(len(component) for component in components)
        total = nx_graph.number_of_nodes()
        warnings.warn(
            f"line_network: Netz besteht aus {len(components)} getrennten "
            f"Zusammenhangskomponenten (größte: {largest} von {total} Knoten, "
            f"{100 * largest / total:.1f} %) - Linien teilen dort keine Koordinate; ggf. "
            "tolerance_m erhöhen. route (FA51) fängt standardmäßig an der größten "
            "Komponente",
            DisconnectedNetworkWarning,
            stacklevel=2,
        )

    return Graph(nx_graph, crs=lines.crs)
