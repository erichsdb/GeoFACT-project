"""In-Memory-Serialisierung von Zwischen-/Endergebnissen für die Web-Schicht.

Macht die Ergebnisse im LayerStore für die Web-Ansicht JSON-tauglich, damit das
Frontend sie direkt auf einer Karte (MapLibre) rendert:

- Vektor -> GeoJSON (FeatureCollection, EPSG:4326)
- Graph  -> Node-Link-JSON (Knoten mit lon/lat, Kanten source/target)
- Raster -> describe() und Bounds (das PNG-Rendering liegt in der Web-Schicht,
            damit der Kern keine Bild-Abhängigkeit bekommt)

describe() liefert eine kompakte, typunabhängige Zusammenfassung für Karten-Defaults
und Attributtabellen.
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable

import geopandas as gpd
import numpy as np
import pandas as pd
from geopandas import GeoDataFrame

from geofact.api import Graph, RasterLayer, data_type_of

VECTOR = "vector"
RASTER = "raster"
GRAPH = "graph"

WGS84 = "EPSG:4326"


# --- JSON-Sicherheit (numpy-Skalare, NaN/Inf -> None) ---


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (str, int)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


def _sanitize_tree(obj: Any) -> Any:
    """Ersetzt NaN/Inf rekursiv durch None, damit JSON.parse im Browser
    nicht an den (in Python zulässigen) NaN-Literalen scheitert."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize_tree(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_tree(v) for v in obj]
    return obj


# --- Typbestimmung + kompakte Beschreibung ---


def result_kind(result: Any) -> str:
    """'vector' | 'raster' | 'graph' - der Datentyp des Kerns (``data_type_of``);
    ein unbekanntes Objekt ist ein Fehler (TypeError)."""
    return data_type_of(result).value


def _bounds_wgs84_vector(gdf: GeoDataFrame) -> list[float] | None:
    if gdf.empty:
        return None
    g = gdf.to_crs(WGS84) if gdf.crs is not None else gdf
    minx, miny, maxx, maxy = (float(v) for v in g.total_bounds)
    if any(math.isnan(v) for v in (minx, miny, maxx, maxy)):
        return None
    return [minx, miny, maxx, maxy]


def _raster_stats(raster: RasterLayer) -> dict[str, Any] | None:
    """Kompakte Wertestatistik eines Rasters; deklariertes Nodata, Sentinels und NaN
    werden ausgeblendet."""
    band = np.asarray(raster.data)
    declared = raster.valid_mask(1 if band.ndim == 3 else None)
    if band.ndim == 3:
        band = band[0]
    band = band.astype(np.float64)
    valid = np.isfinite(band) & (band > -1e30) & declared
    if not valid.any():
        return None
    values = band[valid]
    return {
        "min": float(np.min(values)),
        "max": float(np.max(values)),
        "mean": float(np.mean(values)),
        "p2": float(np.percentile(values, 2)),
        "p98": float(np.percentile(values, 98)),
        "valid_count": int(values.size),
    }


def _numeric_columns(gdf: GeoDataFrame) -> list[str]:
    geom_col = gdf.geometry.name if gdf.geometry is not None else "geometry"
    return [
        str(col)
        for col, dtype in gdf.dtypes.items()
        if col != geom_col and pd.api.types.is_numeric_dtype(dtype)
    ]


def describe(result: Any) -> dict[str, Any]:
    kind = result_kind(result)
    if kind == VECTOR:
        gdf: GeoDataFrame = result
        geom_col = gdf.geometry.name if gdf.geometry is not None else "geometry"
        return _sanitize_tree(
            {
                "kind": VECTOR,
                "feature_count": int(len(gdf)),
                "geometry_types": sorted(gdf.geom_type.dropna().unique().tolist()),
                "crs": str(gdf.crs) if gdf.crs is not None else None,
                "columns": {
                    str(col): str(dtype)
                    for col, dtype in gdf.dtypes.items()
                    if col != geom_col
                },
                "numeric_fields": _numeric_columns(gdf),
                "bounds": _bounds_wgs84_vector(gdf),
            }
        )
    if kind == RASTER:
        raster: RasterLayer = result
        rows, cols = raster.shape
        return _sanitize_tree(
            {
                "kind": RASTER,
                "shape": [int(rows), int(cols)],
                "dtype": str(raster.data.dtype),
                "crs": str(raster.crs) if raster.crs is not None else None,
                "bounds": raster_bounds_wgs84(raster),
                "stats": _raster_stats(raster),
                "path": raster.path,
            }
        )
    graph: Graph = result
    nx_graph = graph.nx_graph
    node_attrs: set[str] = set()
    for _, attrs in nx_graph.nodes(data=True):
        node_attrs.update(k for k in attrs if k != "geometry")
    return _sanitize_tree(
        {
            "kind": GRAPH,
            "node_count": int(nx_graph.number_of_nodes()),
            "edge_count": int(nx_graph.number_of_edges()),
            "node_attributes": sorted(node_attrs),
            "crs": str(graph.crs) if graph.crs is not None else None,
        }
    )


# --- Vektor -> GeoJSON ---


def _head_by_geometry_type(gdf: GeoDataFrame, max_features: int) -> GeoDataFrame:
    """Begrenzt auf max_features, proportional je Geometrietyp. Ein reines head(N) ließe
    bei zusammengefügten Quellen (osm_points vor osm_polygons) einen ganzen Geometrietyp
    verschwinden."""
    if len(gdf) <= max_features:
        return gdf
    groups = list(gdf.groupby(gdf.geom_type, sort=False, group_keys=False))
    if len(groups) <= 1:
        return gdf.head(max_features)
    base_share = max_features // len(groups)
    parts = [group.head(base_share) for _, group in groups]
    # Rest (Rundung, ungenutzter Anteil kleiner Gruppen) auffüllen, damit genau max_features bleiben.
    shortfall = max_features - sum(len(p) for p in parts)
    if shortfall > 0:
        remaining = [
            group.iloc[len(part) :]
            for (_, group), part in zip(groups, parts)
            if len(part) < len(group)
        ]
        if remaining:
            leftover = gpd.GeoDataFrame(pd.concat(remaining))
            parts.append(leftover.head(shortfall))
    return gpd.GeoDataFrame(pd.concat(parts), crs=gdf.crs)


def _drop_empty_columns(gdf: GeoDataFrame) -> GeoDataFrame:
    """Entfernt Spalten, die in keiner Zeile belegt sind.

    OSM-Layer sind breit und dünn besetzt, und GeoJSON schreibt pro Feature alle
    Spalten, auch leere als null. Nur vollständig leere Spalten fallen weg, echte
    Werte nie (Regel 7)."""
    geom_col = gdf.geometry.name if gdf.geometry is not None else "geometry"
    keep = [c for c in gdf.columns if c == geom_col or gdf[c].notna().any()]
    if len(keep) == len(gdf.columns):
        return gdf
    return gdf[keep]


def vector_to_geojson(
    gdf: GeoDataFrame,
    max_features: int | None = None,
    pinned_columns: Iterable[str] | None = None,
    column_budget: int | None = None,
) -> dict[str, Any]:
    """FeatureCollection in EPSG:4326, NaN/Inf -> null.

    max_features begrenzt vor der Reprojektion und Serialisierung (NFA2), proportional
    je Geometrietyp (_head_by_geometry_type). describe().feature_count bleibt die
    echte Gesamtzahl für "N von M angezeigt".

    column_budget begrenzt die Attributspalten (siehe view_columns): pinned_columns
    bleiben erhalten, der Rest wird nach Belegungsdichte aufgefüllt. Ohne Budget
    fallen nur vollständig leere Spalten weg."""
    fc, _ = _vector_to_geojson_with_stats(
        gdf,
        max_features=max_features,
        pinned_columns=pinned_columns,
        column_budget=column_budget,
    )
    return fc


def _vector_to_geojson_with_stats(
    gdf: GeoDataFrame,
    max_features: int | None = None,
    pinned_columns: Iterable[str] | None = None,
    column_budget: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Wie vector_to_geojson, dazu die Zahl verworfener Attributspalten."""
    if gdf.geometry is None:
        return {"type": "FeatureCollection", "features": []}, 0
    limited = (
        _head_by_geometry_type(gdf, max_features) if max_features is not None else gdf
    )
    dropped = 0
    if column_budget is not None:
        # Spaltenauswahl nach der Zeilenbegrenzung: ein notna() über den vollen Layer
        # kostet bei breiten Layern Sekunden, ohne das Ergebnis zu verbessern.
        from .view_columns import select_columns

        columns, dropped = select_columns(
            limited, pinned=pinned_columns or (), budget=column_budget
        )
        limited = limited[columns]
    else:
        # Erst kappen, dann leere Spalten verwerfen.
        limited = _drop_empty_columns(limited)
    out = limited.to_crs(WGS84) if limited.crs is not None else limited
    # to_json emittiert Python-NaN, das JSON.parse nicht liest; daher bereinigen.
    fc = json.loads(out.to_json(drop_id=False))
    return _sanitize_tree(fc), dropped


# --- Graph -> Node-Link-JSON (mit Geo-Koordinaten in EPSG:4326) ---


def _with_reserved(reserved: dict[str, Any], attrs: dict[str, Any]) -> dict[str, Any]:
    """Topologie und Lage zuerst: ein Attribut mit reserviertem Namen (``id``/``lon``/``lat``,
    ``source``/``target``, z. B. der OSM-Tag ``source``) steht als ``attr_<name>`` daneben,
    sonst fand die Karte die Endknoten nicht und ließe die Kante still weg."""
    record = dict(reserved)
    for key, value in attrs.items():
        name = key
        while name in record:
            name = f"attr_{name}"
        record[name] = value
    return record


def graph_to_nodelink(graph: Graph, max_features: int | None = None) -> dict[str, Any]:
    """Node-Link-JSON. max_features begrenzt vor der Reprojektion (wie vector_to_geojson);
    Kanten entstehen nur für die verbleibenden Knoten."""
    nx_graph = graph.nx_graph
    node_ids: list[Any] = []
    geometries = []
    attr_list: list[dict[str, Any]] = []
    for node_id, attrs in nx_graph.nodes(data=True):
        if max_features is not None and len(node_ids) >= max_features:
            break
        attrs = dict(attrs)
        geom = attrs.pop("geometry", None)
        node_ids.append(node_id)
        geometries.append(geom)
        attr_list.append({str(k): _json_safe(v) for k, v in attrs.items()})

    lonlats: list[tuple[float | None, float | None]] = []
    if geometries and any(g is not None for g in geometries):
        series = gpd.GeoSeries(geometries, crs=graph.crs)
        if graph.crs is not None:
            series = series.to_crs(WGS84)
        for geom in series:
            if geom is None or geom.is_empty:
                lonlats.append((None, None))
            else:
                lonlats.append((float(geom.x), float(geom.y)))
    else:
        lonlats = [(None, None)] * len(node_ids)

    nodes = []
    for node_id, (lon, lat), attrs in zip(node_ids, lonlats, attr_list):
        nodes.append(
            _with_reserved({"id": _json_safe(node_id), "lon": lon, "lat": lat}, attrs)
        )

    kept_ids = set(node_ids) if max_features is not None else None
    edges = []
    for u, v, attrs in nx_graph.edges(data=True):
        if kept_ids is not None and (u not in kept_ids or v not in kept_ids):
            continue
        safe = {str(k): _json_safe(val) for k, val in attrs.items() if k != "geometry"}
        edges.append(
            _with_reserved({"source": _json_safe(u), "target": _json_safe(v)}, safe)
        )

    return _sanitize_tree({"nodes": nodes, "edges": edges})


# --- Raster-Bounds (EPSG:4326) ---


def raster_bounds_wgs84(raster: RasterLayer) -> list[float] | None:
    from rasterio.transform import array_bounds
    from rasterio.warp import transform_bounds

    rows, cols = raster.shape
    left, bottom, right, top = array_bounds(rows, cols, raster.transform)
    if raster.crs is None:
        return [float(left), float(bottom), float(right), float(top)]
    minx, miny, maxx, maxy = transform_bounds(
        raster.crs, WGS84, left, bottom, right, top, densify_pts=21
    )
    if any(math.isnan(v) or math.isinf(v) for v in (minx, miny, maxx, maxy)):
        return None
    return [float(minx), float(miny), float(maxx), float(maxy)]


# --- Dispatcher für die JSON-Antwort eines Knotens ---


def serialize_result(
    result: Any,
    max_features: int | None = None,
    pinned_columns: Iterable[str] | None = None,
    column_budget: int | None = None,
) -> dict[str, Any]:
    """Vollständige JSON-Antwort für einen Knoten. Bei Raster kommt das Bild über einen
    eigenen Binärendpunkt (hier nur Bounds).

    max_features begrenzt Vektor-/Graph-Objekte; feature_count bzw. node_count bleiben
    die echte Gesamtzahl. pinned_columns/column_budget steuern die Attributauswahl
    (view_columns); die Zahl verworfener Spalten steht als 'dropped_columns' im Payload
    (Regel 7)."""
    kind = result_kind(result)
    payload: dict[str, Any] = {"describe": describe(result)}
    if kind == VECTOR:
        fc, dropped = _vector_to_geojson_with_stats(
            result,
            max_features=max_features,
            pinned_columns=pinned_columns,
            column_budget=column_budget,
        )
        payload["geojson"] = fc
        if column_budget is not None:
            payload["dropped_columns"] = dropped
    elif kind == GRAPH:
        payload["graph"] = graph_to_nodelink(result, max_features=max_features)
    # Raster: nur describe (inkl. Bounds).
    return payload
