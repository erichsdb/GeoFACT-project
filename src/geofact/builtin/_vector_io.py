"""Implements: FA4 (Zusatz region_filter und Datei-URL, Changelog 83), FA44 (Pfad-Hook).

Gemeinsame Hilfen des Datei-Konnektors (builtin/sources/file.py) und der
Vektorformate (builtin/formats/vector.py) für das Lesen mit Regionsbezug
(Hilfsmodul, registriert nichts):

- ``region_bbox_in``       Region (EPSG:4326, optional um ``margin_km`` vergrößert)
                           als Rechteck im CRS der Datei, das pyogrio als ``bbox``
                           an den Treiber reicht (Pushdown)
- ``read_vector``          liest eine Vektorquelle, bei ``region_filter``
                           ``bbox``/``intersects`` mit Pushdown
- ``refine_to_region``     filtert einen geladenen Layer generisch auf Rechteck bzw.
                           Geometrie der Region, auch für Formate ohne Pushdown
- ``count_outside_region`` zählt Objekte außerhalb der Region (Grundlage der
                           Warnung bei ``region_filter: none``)
- ``gdal_path``            ``http(s)://`` -> ``/vsicurl/<url>``
- ``missing_local_file``   FA44-Hook ``check_before_run`` der Quellarten file und table

Ohne Region wird nichts gefiltert. Ein Layer ohne bestimmbares CRS kann nicht
gefiltert werden: ``bbox``/``intersects`` sind dann ein Fehler, ``none`` zählt
nicht (das fehlende CRS meldet die Harmonisierung, FA5)."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Literal, Optional, Union

import geopandas as gpd
import numpy as np
import pyogrio
from pyproj import CRS, Transformer

from geofact.builtin._raster_io import region_window_bounds
from geofact.plugin_api import LoadContext

RegionFilter = Literal["none", "bbox", "intersects"]

_URL_PREFIXES = ("http://", "https://")


class OutsideRegionWarning(UserWarning):
    """Ein Datei-Layer mit ``region_filter: none`` enthält Objekte außerhalb
    der Szenario-Region (FA4) - sie werden geladen und mitgerechnet."""


# --- Pfade ---


def is_remote(path: str) -> bool:
    """URL (``http(s)://``) oder GDAL-Pfad (``/vsi...``): keine lokale Datei."""
    return path.startswith(_URL_PREFIXES) or path.startswith("/vsi")


def gdal_path(path: str) -> str:
    """GDAL-Pfad einer entfernten Quelle: ``http(s)://`` wird über
    ``/vsicurl/`` gelesen (GDAL, pyogrio und rasterio verstehen das gleich),
    ein bereits virtueller Pfad bleibt unverändert."""
    if path.startswith(_URL_PREFIXES):
        return f"/vsicurl/{path}"
    return path


def missing_local_file(raw: str, base_dir: Optional[Path]) -> list[tuple[str, str]]:
    """FA44-Hook der Quellarten file und table: eine fehlende lokale Datei als
    (feld, meldung); URLs und GDAL-Pfade werden übersprungen, relative Pfade
    gelten gegen base_dir."""
    if is_remote(raw):
        return []
    path = Path(raw)
    if base_dir is not None and not path.is_absolute():
        path = Path(base_dir) / path
    if path.is_file():
        return []
    return [("path", f"Datei nicht gefunden: {path}")]


def strip_query(path: str) -> str:
    """Pfad ohne Query-String und Fragment (nur für die Endung einer URL)."""
    if is_remote(path):
        return path.split("?", 1)[0].split("#", 1)[0]
    return path


# --- Region im CRS der Daten ---


def _as_crs(crs: Any) -> Optional[CRS]:
    if crs is None:
        return None
    return CRS.from_user_input(crs)


def region_bbox_in(
    crs: Any, ctx: LoadContext, margin_km: float = 0.0
) -> Optional[tuple[float, float, float, float]]:
    """Rechteck der Region (``ctx.region``, EPSG:4326, um ``margin_km``
    vergrößert) im CRS ``crs``; None ohne Region. Die Kanten werden beim
    Transformieren verdichtet, damit das Rechteck die Region ganz enthält."""
    if ctx.region is None or len(ctx.region) == 0:
        return None
    target = _as_crs(crs)
    if target is None:
        raise ValueError(
            "region_bbox_in: ohne CRS lässt sich die Region nicht abbilden"
        )
    bounds = region_window_bounds(
        tuple(float(v) for v in ctx.region.total_bounds), margin_km
    )
    if target.equals(CRS.from_epsg(4326)):
        return bounds
    transformer = Transformer.from_crs("EPSG:4326", target, always_xy=True)
    return tuple(
        float(v) for v in transformer.transform_bounds(*bounds, densify_pts=21)
    )


def _region_in(crs: CRS, ctx: LoadContext) -> gpd.GeoSeries:
    return ctx.region.geometry.to_crs(crs)


# --- Lesen mit Pushdown ---


def _file_crs(
    source: Union[str, Path], read_kwargs: dict[str, Any], fallback: Any
) -> Any:
    info_kwargs = {"layer": read_kwargs["layer"]} if read_kwargs.get("layer") else {}
    crs = pyogrio.read_info(source, **info_kwargs).get("crs")
    return crs if crs is not None else fallback


def read_vector(
    source: Union[str, Path], layer: Any, ctx: LoadContext, /, **read_kwargs: Any
) -> gpd.GeoDataFrame:
    """Liest eine Vektorquelle (Datei, /vsizip/-, /vsicurl/-Pfad). Bei
    ``region_filter`` ``bbox``/``intersects`` und vorhandener Region liest der
    Treiber nur das Rechteck der Region (``bbox`` im CRS der Datei); die
    Verfeinerung auf die Geometrie (``intersects``) macht der Konnektor
    danach für alle Formate gleich (``refine_to_region``)."""
    mode = getattr(layer, "region_filter", "none")
    if mode != "none" and ctx.region is not None:
        declared = getattr(layer, "crs", None)
        # FA55: crs_override erzwingt das konfigurierte CRS auch gegen die Datei.
        if getattr(layer, "crs_override", False) and declared is not None:
            crs = declared
        else:
            crs = _file_crs(source, read_kwargs, declared)
        if crs is None:
            raise ValueError(
                f"Layer '{layer.id}': region_filter '{mode}' braucht das CRS der Datei - "
                "die Datei deklariert keines; 'crs' in der Konfiguration angeben"
            )
        read_kwargs["bbox"] = region_bbox_in(crs, ctx)
    return gpd.read_file(source, **read_kwargs)


# --- Generische Verfeinerung und Zählung ---


def _data_crs(
    gdf: gpd.GeoDataFrame, fallback: Any, override: bool = False
) -> Optional[CRS]:
    """CRS der Daten: das der Daten, ohne eines ``fallback``; mit ``override``
    (FA55 ``crs_override``) gewinnt ``fallback`` auch gegen das Datei-CRS."""
    if override and fallback is not None:
        return _as_crs(fallback)
    return _as_crs(gdf.crs if gdf.crs is not None else fallback)


def refine_to_region(
    gdf: gpd.GeoDataFrame,
    ctx: LoadContext,
    mode: RegionFilter,
    *,
    crs: Any = None,
    layer_id: str = "?",
    override: bool = False,
) -> gpd.GeoDataFrame:
    """Filtert ``gdf`` auf die Region: ``bbox`` = Objekte, deren Rechteck das
    Rechteck der Region schneidet; ``intersects`` = Objekte, die die
    Regionsgeometrie schneiden; ``none`` = unverändert. ``crs`` gilt, wenn
    ``gdf`` selbst keines trägt, mit ``override`` (FA55) immer."""
    if mode == "none" or ctx.region is None or len(gdf) == 0:
        return gdf
    data_crs = _data_crs(gdf, crs, override)
    if data_crs is None:
        raise ValueError(
            f"Layer '{layer_id}': region_filter '{mode}' braucht ein CRS - die Daten "
            "deklarieren keines; 'crs' in der Konfiguration angeben"
        )
    if mode == "bbox":
        keep = _intersects_rect(gdf, region_bbox_in(data_crs, ctx))
    else:
        region = _region_in(data_crs, ctx).union_all()
        keep = np.asarray(gdf.geometry.intersects(region), dtype=bool)
    return gdf.loc[keep].copy()


def _intersects_rect(
    gdf: gpd.GeoDataFrame, rect: tuple[float, float, float, float]
) -> np.ndarray:
    bounds = gdf.geometry.bounds.to_numpy()
    minx, miny, maxx, maxy = rect
    with np.errstate(invalid="ignore"):
        hit = (
            (bounds[:, 0] <= maxx)
            & (bounds[:, 2] >= minx)
            & (bounds[:, 1] <= maxy)
            & (bounds[:, 3] >= miny)
        )
    return hit


def count_outside_region(
    gdf: gpd.GeoDataFrame, ctx: LoadContext, *, crs: Any = None, override: bool = False
) -> int:
    """Anzahl Objekte, deren Rechteck das Rechteck der Region (im CRS der
    Daten) nicht schneidet. Leere/fehlende Geometrien zählen nicht; ohne
    Region oder ohne CRS ist das Ergebnis 0."""
    if ctx.region is None or len(gdf) == 0:
        return 0
    data_crs = _data_crs(gdf, crs, override)
    if data_crs is None:
        return 0
    region_rect = tuple(float(v) for v in _region_in(data_crs, ctx).total_bounds)
    present = ~(gdf.geometry.isna() | gdf.geometry.is_empty).to_numpy()
    inside = _intersects_rect(gdf, region_rect)
    return int((present & ~inside).sum())


def warn_outside_region(
    gdf: gpd.GeoDataFrame,
    ctx: LoadContext,
    *,
    layer_id: str,
    crs: Any = None,
    override: bool = False,
) -> None:
    """Die eine Warnung bei ``region_filter: none``: wie viele Objekte liegen
    außerhalb der Region (keine, wenn alle innerhalb liegen)."""
    outside = count_outside_region(gdf, ctx, crs=crs, override=override)
    if outside:
        warnings.warn(
            f"Layer '{layer_id}': {outside} von {len(gdf)} Objekten liegen außerhalb der "
            "Region und werden mitgeladen (region_filter: none) - 'region_filter: bbox' "
            "oder 'intersects' begrenzt das Laden auf die Region",
            OutsideRegionWarning,
            stacklevel=2,
        )
