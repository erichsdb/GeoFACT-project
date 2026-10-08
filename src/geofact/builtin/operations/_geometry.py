"""Implements: FA8, FA68, FA69, FA70 (gemeinsame Geometrie-Helfer der Operationen).

Hilfsmodul (führender Unterstrich: wird nicht gescannt, registriert nichts).
_homogenize() reduziert gemischte Geometrietypen auf die dominante Dimension;
_GEOM_DIMENSION nutzen zusätzlich vector_union, deduplicate und area_share.
require_projected_crs() ist die Vorbedingung der metrisch rechnenden Operationen
(FA68/FA70), measure_geometries() der gemeinsame Kern von area und length.
free_join_index_columns() macht einen Eingang für sjoin nutzbar, der schon
index_left/index_right trägt."""

from __future__ import annotations

import warnings
from collections import Counter
from typing import Callable, Optional

import numpy as np
import pandas as pd
from geopandas import GeoDataFrame

# Topologische Dimension je Geometrietyp (Punkt=0, Linie=1, Flaeche=2)
_GEOM_DIMENSION = {
    "Point": 0,
    "MultiPoint": 0,
    "LineString": 1,
    "MultiLineString": 1,
    "LinearRing": 1,
    "Polygon": 2,
    "MultiPolygon": 2,
}


def _homogenize(gdf: GeoDataFrame, *, label: str) -> GeoDataFrame:
    """Reduziert einen gemischten Layer auf die dominante (höchste) Dimension.
    overlay() und sjoin() brechen bei gemischten Typen ab; bei OSM-Flächen sind
    Punkte/Linien meist Kartier-Artefakte. Das Verwerfen wird als Warnung gemeldet."""
    if gdf.empty:
        return gdf
    dims = gdf.geometry.geom_type.map(_GEOM_DIMENSION)
    if dims.nunique(dropna=True) <= 1:
        return gdf
    keep_dim = dims.max()
    dropped = gdf[dims != keep_dim]
    dropped_types = sorted(set(dropped.geometry.geom_type))
    kept = gdf[dims == keep_dim]
    warnings.warn(
        f"Layer '{label}' enthielt gemischte Geometrietypen; "
        f"{len(dropped)} Objekt(e) der Typen {dropped_types} verworfen, "
        f"{len(kept)} der dominanten Dimension behalten.",
        stacklevel=2,
    )
    return kept


_JOIN_INDEX_COLUMNS = ("index_left", "index_right")
"""Spalten, die GeoPandas' sjoin selbst anlegt und deshalb in keinem
Eingang duldet ('index_right' cannot be a column name ...)."""


def free_join_index_columns(gdf: GeoDataFrame, *, op: str, port: str) -> GeoDataFrame:
    """Benennt vorhandene Spalten ``index_left``/``index_right`` eines
    sjoin-Eingangs in den ersten freien Namen ``<spalte>_<n>`` um und meldet es.
    Ein Eingang aus einem früheren ``spatial_join`` trägt ``index_right``,
    das ein zweiter Join sonst ablehnt."""
    renames: dict[str, str] = {}
    for column in _JOIN_INDEX_COLUMNS:
        if column not in gdf.columns:
            continue
        number = 1
        while (
            f"{column}_{number}" in gdf.columns
            or f"{column}_{number}" in renames.values()
        ):
            number += 1
        renames[column] = f"{column}_{number}"
    if not renames:
        return gdf
    listed = ", ".join(f"'{old}' -> '{new}'" for old, new in renames.items())
    warnings.warn(
        f"{op}: Eingang '{port}' hat Spalten, die der räumliche Join selbst anlegt "
        f"(meist aus einem früheren spatial_join); umbenannt: {listed}.",
        stacklevel=2,
    )
    return gdf.rename(columns=renames)


def geometry_dimensions(gdf: GeoDataFrame) -> pd.Series:
    """Topologische Dimension je Objekt (0/1/2); -1 für fehlende, leere oder
    unbekannte Geometrien (z. B. GeometryCollection)."""
    geometry = gdf.geometry
    dims = geometry.geom_type.map(_GEOM_DIMENSION)
    missing = geometry.isna() | geometry.is_empty
    return dims.where(~missing, np.nan).fillna(-1).astype(int)


def require_projected_crs(
    gdf: GeoDataFrame, *, op: str, port: str = "features"
) -> None:
    """Vorbedingung metrischer Operationen (FA68/FA70): der Eingang hat ein
    projiziertes CRS. Ein geographisches oder fehlendes CRS ist ein Fehler."""
    crs = gdf.crs
    if crs is None:
        raise ValueError(
            f"{op}: Eingang '{port}' hat kein CRS - die Operation rechnet in Metern; "
            "innerhalb eines Laufs ist jeder Layer im Arbeits-CRS (FA5)"
        )
    if not crs.is_projected:
        raise ValueError(
            f"{op}: Eingang '{port}' hat kein projiziertes CRS ({crs.to_string()}, Grad) - "
            "die Operation rechnet in Metern; innerhalb eines Laufs ist jeder Layer im "
            "Arbeits-CRS (FA5)"
        )


def require_new_column(
    gdf: GeoDataFrame, column: str, *, op: str, overwrite: bool = False
) -> None:
    """Vorbedingung: die Ergebnisspalte existiert noch nicht (außer bei
    ``overwrite``); die Geometriespalte wird nie überschrieben."""
    if column == gdf.geometry.name:
        raise ValueError(f"{op}: Ergebnisspalte '{column}' ist die Geometriespalte")
    if column in gdf.columns and not overwrite:
        raise ValueError(
            f"{op}: Ergebnisspalte '{column}' existiert bereits im Layer - anderen "
            "output_field wählen"
            + (" oder overwrite: true setzen" if op == "calculate_field" else "")
        )


def measure_geometries(
    gdf: GeoDataFrame,
    *,
    op: str,
    dimension: int,
    measure: Callable[[GeoDataFrame], pd.Series],
    factor: float,
    output_field: str,
    decimals: Optional[int],
    expected: str,
) -> GeoDataFrame:
    """Gemeinsamer Kern von area/length (FA68): misst Objekte der Dimension
    ``dimension``, teilt durch ``factor`` und schreibt ``output_field`` in eine
    Kopie. Andere Objekte erhalten NaN und stehen in einer Sammelwarnung."""
    require_projected_crs(gdf, op=op)
    require_new_column(gdf, output_field, op=op)
    result = gdf.copy()
    if gdf.empty:
        result[output_field] = pd.Series(dtype="float64", index=result.index)
        return result
    dims = geometry_dimensions(gdf)
    fits = (dims == dimension).to_numpy()
    values = np.full(len(gdf), np.nan, dtype="float64")
    if fits.any():
        values[fits] = measure(gdf[fits]).to_numpy(dtype="float64") / factor
    if decimals is not None:
        values = np.round(values, decimals)
    result[output_field] = values
    if not fits.all():
        others = gdf.geometry[~fits]
        types = Counter(
            "ohne Geometrie" if (geom is None or geom.is_empty) else geom.geom_type
            for geom in others
        )
        listed = ", ".join(f"{name}: {count}" for name, count in sorted(types.items()))
        warnings.warn(
            f"{op}: {int((~fits).sum())} von {len(gdf)} Objekt(en) sind keine {expected} "
            f"({listed}) - '{output_field}' ist dort NaN",
            stacklevel=3,
        )
    return result
