"""Implements: FA5 (CRS-Harmonisierer, Raster-Resampling), FA55 (crs_override, densify_km),
FA57 (Nachbedingung der Reprojektion), Transformationen für FA40.

Überführt einen Layer in das Arbeits-CRS des Laufs (projiziert, Meter; ohne
Angabe die UTM-Zone der Region, FA5). Ein Layer ohne CRS ist ein Fehler, außer
die Konfiguration liefert ein Override-CRS. Registriert 'reproject' (Standard
jeder Quelle) und 'none' (Daten ohne Raumbezug); wer anderes braucht, bringt eine
eigene Transformation mit.

Raster: Mehrband-Raster werden band-weise reprojiziert, die Bandnamen bleiben.
``resampling: nearest|bilinear|average|sum`` (Default ``nearest``): ``sum`` erhält
die Gesamtsumme eines Zählrasters beim Wechsel der Zellgröße, ``nearest``
verfälscht sie. Ein Raster im Ziel-CRS bleibt unverändert (10-m-Raster eines
Sentinel-2-Bildes in der UTM-Zone der Region).

Ablauf von ``harmonize`` (FA57), für Vektor und Raster gleich bis (2):

1. Quell-CRS: das der Daten, sonst das Override-CRS (``crs``). Bei Abweichung
   gewinnt die Datei (FA4) mit Warnung; mit ``crs_override: true`` (FA55) gewinnt
   die Konfiguration mit Warnung.
2. ``coordinate_plausibility`` der Rohausdehnung: metrische Koordinaten unter einem
   geographischen CRS sind ein Fehler, außerhalb des Gültigkeitsbereichs eine Warnung.
3. ``densify_km``: Linien und Flächen werden vor der Reprojektion verdichtet.
4. ``to_crs``.
5. Nachbedingung: nicht endliche Stützpunkte sind ein Fehler (Hinweis
   ``scenario.crs: equal_area``), neu ungültige Geometrien eine Warnung. Raster:
   Warnung, wenn das Ziel komplett Nodata ist, die Quelle aber gültige Zellen hatte.

Ein leerer Layer besteht alle Prüfungen. Liegt ein Vektor-Layer schon im
Ziel-CRS, entfallen (3) und (5).
"""

from __future__ import annotations

import warnings

import numpy as np
import rasterio
import shapely
from geopandas import GeoDataFrame
from pyproj import CRS
from rasterio.warp import Resampling, calculate_default_transform, reproject

from geofact.core.crs import (
    coordinate_plausibility,
    crs_label,
    epsg_code,
    nonfinite_vertices,
)
from geofact.plugin_api import LayerData, LoadContext, RasterLayer, register_transform


class SourceCrsWarning(UserWarning):
    """FA55/FA57: Quell-CRS aus Datei und Konfiguration weichen ab, oder die
    Rohkoordinaten liegen außerhalb des Gültigkeitsbereichs des Quell-CRS."""


class ReprojectionWarning(UserWarning):
    """FA57: die Reprojektion hat Geometrien ungültig gemacht bzw. ein
    gültiges Raster komplett auf Nodata abgebildet."""


_KM_PER_DEGREE = 111.32
"""Länge eines Breitengrads in km - Umrechnung von ``densify_km`` für ein
geographisches Quell-CRS (konservativ: ein Längengrad ist höchstens so lang)."""

_HINT_EQUAL_AREA = (
    "die Daten liegen außerhalb des Bereichs, den das Arbeits-CRS abbilden kann - "
    "scenario.crs: equal_area deklarieren oder die Daten auf die Region beschränken"
)


def harmonize(
    layer_id: str,
    data: LayerData,
    target_crs: CRS,
    override_crs: str | CRS | None = None,
    resampling: str = "nearest",
    *,
    force: bool = False,
    densify_km: float | None = None,
) -> LayerData:
    """Reprojiziert data nach target_crs. Fehlt das Quell-CRS, gilt override_crs,
    sonst Fehler mit Layer-id. ``force`` erzwingt override_crs auch gegen ein
    vorhandenes CRS (FA55). ``densify_km`` verdichtet Vektorkanten vorher;
    resampling gilt nur für Raster."""
    if isinstance(data, GeoDataFrame):
        return _harmonize_vector(
            layer_id, data, target_crs, override_crs, force, densify_km
        )
    return _harmonize_raster(
        layer_id, data, target_crs, override_crs, resampling, force
    )


def _same_crs(a: CRS, b: CRS) -> bool:
    if a == b or a.equals(b, ignore_axis_order=True):
        return True
    epsg = epsg_code(a)
    return epsg is not None and epsg == epsg_code(b)


def _resolve_source_crs(
    layer_id: str,
    source_crs: CRS | None,
    override_crs: str | CRS | None,
    force: bool = False,
) -> CRS:
    """Quell-CRS nach FA4/FA55: Datei, sonst Konfiguration; ``force`` lässt
    die Konfiguration gewinnen. Jede Abweichung ist eine Warnung."""
    declared = CRS.from_user_input(override_crs) if override_crs is not None else None
    if source_crs is not None:
        actual = CRS.from_user_input(source_crs)
        if declared is None or _same_crs(actual, declared):
            return actual
        if force:
            warnings.warn(
                f"Layer '{layer_id}': Datei deklariert {crs_label(actual)}, Konfiguration "
                f"erzwingt {crs_label(declared)} (crs_override: true)",
                SourceCrsWarning,
                stacklevel=4,
            )
            return declared
        warnings.warn(
            f"Layer '{layer_id}': Datei deklariert {crs_label(actual)}, Konfiguration nennt "
            f"{crs_label(declared)} - die Datei gewinnt (FA4); zum Erzwingen "
            "crs_override: true setzen",
            SourceCrsWarning,
            stacklevel=4,
        )
        return actual
    if declared is not None:
        return declared
    raise ValueError(
        f"Layer '{layer_id}': kein CRS erkennbar und kein Override in der "
        "Konfiguration angegeben"
    )


def _check_plausibility(layer_id: str, bounds, source_crs: CRS) -> None:
    errors, notes = coordinate_plausibility(bounds, source_crs)
    if errors:
        raise ValueError(f"Layer '{layer_id}': {errors[0]}")
    for note in notes:
        warnings.warn(f"Layer '{layer_id}': {note}", SourceCrsWarning, stacklevel=4)


def _densify(gdf: GeoDataFrame, source_crs: CRS, densify_km: float) -> GeoDataFrame:
    """Linien und Flächen auf Segmente <= densify_km verdichten (in den
    Einheiten des Quell-CRS); Punkte bleiben unverändert (FA57 (4))."""
    if source_crs.is_geographic:
        max_length = densify_km / _KM_PER_DEGREE
    else:
        factor = source_crs.axis_info[0].unit_conversion_factor or 1.0
        max_length = densify_km * 1000.0 / factor
    geometry = gdf.geometry
    mask = geometry.geom_type.isin(
        ["LineString", "MultiLineString", "LinearRing", "Polygon", "MultiPolygon"]
    ).to_numpy()
    if not mask.any():
        return gdf
    values = geometry.to_numpy().copy()
    values[mask] = shapely.segmentize(values[mask], max_length)
    result = gdf.copy()
    result[gdf.geometry.name] = values
    return result.set_crs(source_crs, allow_override=True)


def _check_finite(layer_id: str, result: GeoDataFrame, target_crs: CRS) -> None:
    vertex_count, rows = nonfinite_vertices(result)
    if vertex_count == 0:
        return
    coords = result.geometry.reset_index(drop=True).get_coordinates()
    bad = ~np.isfinite(coords.to_numpy(dtype=float)).all(axis=1)
    objects = int(coords.index[bad].nunique())
    raise ValueError(
        f"Layer '{layer_id}': {vertex_count} nicht endliche Stützpunkte in {objects} "
        f"Objekt(en) nach der Reprojektion nach {crs_label(target_crs)} (Zeilen {rows}) - "
        f"{_HINT_EQUAL_AREA}"
    )


def _harmonize_vector(
    layer_id: str,
    gdf: GeoDataFrame,
    target_crs: CRS,
    override_crs: str | CRS | None,
    force: bool = False,
    densify_km: float | None = None,
) -> GeoDataFrame:
    source_crs = _resolve_source_crs(layer_id, gdf.crs, override_crs, force)
    if gdf.crs is None or not _same_crs(CRS.from_user_input(gdf.crs), source_crs):
        gdf = gdf.set_crs(source_crs, allow_override=True)
    if gdf.empty:
        return gdf.to_crs(target_crs)
    _check_plausibility(layer_id, gdf.total_bounds, source_crs)
    if _same_crs(source_crs, target_crs):
        return gdf.to_crs(target_crs)
    if densify_km:
        gdf = _densify(gdf, source_crs, densify_km)
    valid_before = gdf.geometry.is_valid.to_numpy()
    result = gdf.to_crs(target_crs)
    _check_finite(layer_id, result, target_crs)
    newly_invalid = int((valid_before & ~result.geometry.is_valid.to_numpy()).sum())
    if newly_invalid:
        warnings.warn(
            f"Layer '{layer_id}': {newly_invalid} Geometrie(n) nach der Reprojektion nach "
            f"{crs_label(target_crs)} ungültig (vorher gültig) - Operation repair oder "
            "ein passenderes Arbeits-CRS (scenario.crs) verwenden",
            ReprojectionWarning,
            stacklevel=3,
        )
    return result


RESAMPLING_METHODS: dict[str, Resampling] = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "average": Resampling.average,
    "sum": Resampling.sum,
}
"""Namen des deklarierbaren Raster-Resamplings (``resampling`` am Raster-Layer)."""


def _has_valid_cells(data: np.ndarray, nodata: float) -> bool:
    if np.isnan(nodata):
        return bool((~np.isnan(data)).any())
    return bool((data != nodata).any())


def _harmonize_raster(
    layer_id: str,
    layer: RasterLayer,
    target_crs: CRS,
    override_crs: str | CRS | None,
    resampling: str = "nearest",
    force: bool = False,
) -> RasterLayer:
    source_crs = _resolve_source_crs(layer_id, layer.crs, override_crs, force)
    if resampling not in RESAMPLING_METHODS:
        raise ValueError(
            f"Layer '{layer_id}': unbekanntes Resampling '{resampling}', "
            f"erlaubt sind {sorted(RESAMPLING_METHODS)}"
        )
    bounds = rasterio.transform.array_bounds(
        layer.shape[0], layer.shape[1], layer.transform
    )
    _check_plausibility(layer_id, bounds, source_crs)
    if source_crs == target_crs:
        # Schon im Ziel-CRS: Zelle für Zelle erhalten, ein Resampling auf das
        # vorgeschlagene Gitter würde es nur verschieben.
        return RasterLayer(
            data=layer.data,
            transform=layer.transform,
            crs=target_crs,
            path=layer.path,
            nodata=layer.nodata,
            band_names=layer.band_names,
        )

    transform, width, height = calculate_default_transform(
        source_crs,
        target_crs,
        layer.shape[1],
        layer.shape[0],
        *bounds,
    )
    out_shape = (
        (height, width)
        if layer.data.ndim == 2
        else (layer.data.shape[0], height, width)
    )
    # Nodata durchreichen, sonst mischt reproject() Sentinelzellen (z. B. GHS-POPs
    # negativer Wert) in die Nachbarzellen. Ohne deklariertes Nodata bekommt ein
    # Gleitkomma-Raster NaN (wie _raster_io.warp_raster), ein ganzzahliges behält 0
    # und es gibt eine Warnung.
    nodata = layer.nodata
    if nodata is None and np.issubdtype(layer.data.dtype, np.floating):
        nodata = float("nan")
    reproject_kwargs = {}
    if nodata is not None:
        reproject_kwargs["src_nodata"] = nodata
        reproject_kwargs["dst_nodata"] = nodata
        destination = np.full(out_shape, nodata, dtype=layer.data.dtype)
    else:
        destination = np.zeros(out_shape, dtype=layer.data.dtype)
    reproject(
        source=layer.data,
        destination=destination,
        src_transform=layer.transform,
        src_crs=source_crs,
        dst_transform=transform,
        dst_crs=target_crs,
        resampling=RESAMPLING_METHODS[resampling],
        **reproject_kwargs,
    )
    if nodata is None:
        footprint = np.zeros((height, width), dtype=np.uint8)
        reproject(
            source=np.ones(layer.shape, dtype=np.uint8),
            destination=footprint,
            src_transform=layer.transform,
            src_crs=source_crs,
            dst_transform=transform,
            dst_crs=target_crs,
            resampling=Resampling.nearest,
            dst_nodata=0,
        )
        outside = int((footprint == 0).sum())
        if outside:
            warnings.warn(
                f"Layer '{layer_id}' ({layer.data.dtype}) ohne Nodata nach "
                f"{crs_label(target_crs)} umprojiziert: {outside} Zellen außerhalb der Quelle "
                "sind 0 und von echten Nullwerten nicht zu unterscheiden - Nodata am Raster "
                "deklarieren",
                ReprojectionWarning,
                stacklevel=3,
            )
    if (
        nodata is not None
        and _has_valid_cells(layer.data, float(nodata))
        and not _has_valid_cells(destination, float(nodata))
    ):
        warnings.warn(
            f"Layer '{layer_id}': das Raster ist nach der Reprojektion nach "
            f"{crs_label(target_crs)} komplett Nodata, obwohl die Quelle gültige Zellen hat - "
            "Quell-CRS (crs/crs_override) und Arbeits-CRS (scenario.crs) prüfen",
            ReprojectionWarning,
            stacklevel=3,
        )
    return RasterLayer(
        data=destination,
        transform=transform,
        crs=target_crs,
        path=layer.path,
        nodata=nodata,
        band_names=layer.band_names,
    )


def declared_resampling(layer) -> str:
    """Das am Raster-Layer deklarierte Resampling: ein Feld des Layers
    (``source: stac``) oder eine Formatoption (``source: file``, Format tif,
    ``layer.options``); ohne Angabe ``nearest``."""
    value = getattr(layer, "resampling", None)
    if value is None:
        value = getattr(getattr(layer, "options", None), "resampling", None)
    return value or "nearest"


@register_transform(
    "reproject",
    description="In das Arbeits-CRS des Laufs überführen; Quell-CRS aus layer.crs (crs_override erzwingt es)",
)
def reproject_to_target(layer, data: LayerData, ctx: LoadContext) -> LayerData:
    # getattr, weil nur manche Layer-Modelle crs (FA4) und crs_override (FA55) deklarieren.
    return harmonize(
        layer.id,
        data,
        ctx.target_crs,
        override_crs=getattr(layer, "crs", None),
        resampling=declared_resampling(layer),
        force=bool(getattr(layer, "crs_override", False)),
        densify_km=getattr(ctx, "densify_km", None),
    )


@register_transform("none", description="Daten unverändert lassen (kein Raumbezug)")
def keep_as_is(layer, data: LayerData, ctx: LoadContext) -> LayerData:
    return data
