"""Implements: FA4 (Lesefenster mit Rand, Zellbudget), FA46/FA49 (GeoTIFF lesen und schreiben), FA70 (Zellbudget, Umprojektion).

Gemeinsame Raster-Hilfen der Bausteine (Hilfsmodul, registriert nichts):

- ``region_window_bounds``  Region (EPSG:4326) um ``margin_km`` vergrößert (Lesefenster)
- ``write_geotiff`` / ``read_geotiff``  RasterLayer als GeoTIFF schreiben bzw. lesen
- ``check_cell_budget``     Zellgrenze ``MAX_CELLS`` vor dem Lesen/Anlegen eines Rasters
- ``warp_raster``           RasterLayer in ein anderes CRS bringen
- ``GDAL_OPTIONS``          GDAL-Einstellungen fürs Lesen von COGs per HTTP"""

from __future__ import annotations

import math
import warnings
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import rasterio
from pyproj import CRS
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject

from geofact.plugin_api import RasterLayer

KM_PER_DEGREE = 111.32
"""Kilometer je Breitengrad (näherungsweise), für ``margin_km``."""

MAX_CELLS = 250_000_000
"""Obergrenze der Zellen (Bänder x Zeilen x Spalten) eines Rasters, das ein
Baustein liest oder anlegt: ein zu großes Gebiet scheitert mit Meldung, statt den
Speicher zu füllen (NFA2). ``check_cell_budget`` liest den Wert zur Laufzeit
(Tests setzen ihn herab)."""

GDAL_OPTIONS: dict[str, str] = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}
"""GDAL-Einstellungen fürs Lesen von COGs per HTTP (ohne ``READDIR`` fragt GDAL
sonst das Verzeichnis der Datei ab; Wiederholung bei kurzen Netzfehlern)."""

RESAMPLING_METHODS: dict[str, Resampling] = {
    "nearest": Resampling.nearest,
    "bilinear": Resampling.bilinear,
    "average": Resampling.average,
    "sum": Resampling.sum,
}
"""Resampling-Verfahren von ``warp_raster`` (dieselben Namen wie ``resampling``
am Raster-Layer, FA5)."""


class RasterWarpWarning(UserWarning):
    """Umprojektion eines ganzzahligen Rasters ohne Nodata: Zellen außerhalb der
    Quelle sind 0 und nicht von einem Messwert 0 zu unterscheiden."""


def check_cell_budget(cells: int, *, context: str, hint: str) -> None:
    """Fehler, wenn ``cells`` (Bänder x Zeilen x Spalten) ``MAX_CELLS``
    übersteigt; die Meldung nennt ``context`` (wer liest/anlegt), die Zahl, die
    Grenze und ``hint`` (die Abhilfe). Vor dem Lesen bzw. Anlegen aufrufen."""
    if cells > MAX_CELLS:
        raise ValueError(
            f"{context}: {cells:,} Zellen, erlaubt sind {MAX_CELLS:,} - {hint}"
        )


def warp_raster(
    raster: RasterLayer, crs: CRS | str, resampling: str = "nearest"
) -> RasterLayer:
    """``raster`` im CRS ``crs`` (Gitter von ``calculate_default_transform``):
    alle Bänder, Bandnamen, Datentyp und ``path`` bleiben. Mit deklariertem
    Nodata sind Zellen außerhalb der Quelle Nodata (und Nodata-Zellen mischen
    sich nicht in ihre Nachbarn); ohne Nodata bekommt ein Gleitkomma-Raster
    Nodata NaN, ein ganzzahliges 0 für Zellen außerhalb der Quelle - dann mit
    ``RasterWarpWarning``. Gleiches CRS: unveränderte Kopie."""
    if resampling not in RESAMPLING_METHODS:
        raise ValueError(
            f"unbekanntes Resampling '{resampling}', erlaubt sind {sorted(RESAMPLING_METHODS)}"
        )
    if raster.crs is None:
        raise ValueError("Raster ohne CRS kann nicht umprojiziert werden")
    source_crs = CRS.from_user_input(raster.crs)
    target_crs = CRS.from_user_input(crs)
    if source_crs == target_crs:
        return RasterLayer(
            data=raster.data.copy(),
            transform=raster.transform,
            crs=target_crs,
            path=raster.path,
            nodata=raster.nodata,
            band_names=raster.band_names,
        )
    rows, cols = raster.shape
    transform, width, height = calculate_default_transform(
        source_crs, target_crs, cols, rows, *array_bounds(rows, cols, raster.transform)
    )
    dtype = raster.data.dtype
    nodata = raster.nodata
    if nodata is None and np.issubdtype(dtype, np.floating):
        nodata = float("nan")
    out_shape = (
        (height, width)
        if raster.data.ndim == 2
        else (raster.data.shape[0], height, width)
    )
    destination = np.full(out_shape, 0 if nodata is None else nodata, dtype=dtype)
    kwargs = {} if nodata is None else {"src_nodata": nodata, "dst_nodata": nodata}
    reproject(
        source=raster.data,
        destination=destination,
        src_transform=raster.transform,
        src_crs=source_crs,
        dst_transform=transform,
        dst_crs=target_crs,
        resampling=RESAMPLING_METHODS[resampling],
        **kwargs,
    )
    if nodata is None:
        footprint = np.zeros((height, width), dtype=np.uint8)
        reproject(
            source=np.ones((rows, cols), dtype=np.uint8),
            destination=footprint,
            src_transform=raster.transform,
            src_crs=source_crs,
            dst_transform=transform,
            dst_crs=target_crs,
            resampling=Resampling.nearest,
            dst_nodata=0,
        )
        outside = int((footprint == 0).sum())
        if outside:
            warnings.warn(
                f"Raster{f' {raster.path!r}' if raster.path else ''} ({dtype}) ohne Nodata nach "
                f"{target_crs.to_string()} umprojiziert: {outside} Zellen außerhalb der Quelle "
                "sind 0 - Nodata am Raster deklarieren, um sie zu unterscheiden",
                RasterWarpWarning,
                stacklevel=2,
            )
    return RasterLayer(
        data=destination,
        transform=transform,
        crs=target_crs,
        path=raster.path,
        nodata=nodata,
        band_names=raster.band_names,
    )


def region_window_bounds(
    region_bounds: tuple[float, float, float, float], margin_km: float
) -> tuple[float, float, float, float]:
    """Region (min_lon, min_lat, max_lon, max_lat, EPSG:4326) um margin_km
    vergrößert (Grad-Näherung: 111,32 km je Breitengrad, Längengrad mit
    cos(Breite)); ohne Rand unverändert."""
    min_lon, min_lat, max_lon, max_lat = region_bounds
    if margin_km <= 0:
        return region_bounds
    d_lat = margin_km / KM_PER_DEGREE
    mid_lat = math.radians((min_lat + max_lat) / 2)
    d_lon = margin_km / (KM_PER_DEGREE * max(math.cos(mid_lat), 0.01))
    return (
        max(min_lon - d_lon, -180.0),
        max(min_lat - d_lat, -90.0),
        min(max_lon + d_lon, 180.0),
        min(max_lat + d_lat, 90.0),
    )


def write_geotiff(
    raster: RasterLayer, path: str | Path, tags: Mapping[str, str] | None = None
) -> None:
    """Schreibt ``raster`` als deflate-komprimiertes GeoTIFF: alle Bänder, ihre
    Namen als Bandbeschreibung, CRS, Georeferenzierung und Nodata wie im Layer;
    ``tags`` landen als Datensatz-Metadaten (z. B. ``TIFFTAG_COPYRIGHT``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if raster.crs is None:
        raise ValueError("Raster ohne CRS kann nicht als GeoTIFF geschrieben werden")
    data = raster.data if raster.data.ndim == 3 else raster.data[np.newaxis]
    nodata = raster.nodata
    if (
        nodata is not None
        and np.isnan(nodata)
        and not np.issubdtype(data.dtype, np.floating)
    ):
        raise ValueError(
            f"Nodata NaN ist für den Datentyp {data.dtype} nicht darstellbar (Gleitkomma nötig)"
        )
    rows, cols = data.shape[1:]
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=data.shape[0],
        dtype=data.dtype,
        crs=raster.crs,
        transform=raster.transform,
        nodata=nodata,
        compress="deflate",
    ) as dataset:
        dataset.write(data)
        if raster.band_names is not None:
            for index, name in enumerate(raster.band_names, start=1):
                dataset.set_band_description(index, name)
        if tags:
            dataset.update_tags(**{str(key): str(value) for key, value in tags.items()})


def read_geotiff(path: str | Path) -> RasterLayer:
    """Liest ein mit ``write_geotiff`` geschriebenes GeoTIFF vollständig: alle
    Bänder als 3D-Array, Bandbeschreibungen als Namen (nur wenn alle gesetzt)."""
    with rasterio.open(path) as dataset:
        descriptions = list(dataset.descriptions)
        names = descriptions if all(descriptions) else None
        return RasterLayer(
            data=dataset.read(),
            transform=dataset.transform,
            crs=CRS.from_user_input(dataset.crs),
            path=str(path),
            nodata=dataset.nodata,
            band_names=names,
        )
