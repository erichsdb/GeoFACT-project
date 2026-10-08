"""Implements: FA4 (Dateiformate des Datei-Konnektors: Raster, Zellbudget), FA5 (Resampling), FA40, FA77 (Zielauflösung mit Aggregation).

GeoTIFF (tif, tiff) über rasterio. Mit Szenario-Region wird nur der von der
BBox (EPSG:4326) überdeckte Ausschnitt gelesen, nicht die ganze Datei; das
macht globale Raster (z. B. GHS-POP) praktikabel. Das Fenster wird an der Datei
beschnitten (ein Raster kleiner als die Region behält seine Lage) und im
Quell-CRS nach FA4/FA55 berechnet (Datei, ohne CRS ``crs``, mit
``crs_override: true`` immer ``crs``). Passt schon der Ausschnitt nicht in den
Speicher, hilft das nicht: Out-of-Core-Verarbeitung (Kacheln) gibt es nicht.
Der Nodata-Wert der Datei wird unverändert übernommen, damit zonal_stats (FA8)
und die CRS-Harmonisierung (FA5) Sentinelzellen nicht als Messwerte behandeln.

Formatoptionen (``RasterOptions``, flach in der YAML neben ``path``; Details in
den Feldbeschreibungen): ``bands``/``band_names`` wählen und benennen Bänder
(ohne beides wird nur Band 1 gelesen, 2D, unbenannt), ``margin_km`` weitet das
Lesefenster (Grad-Umrechnung näherungsweise, 111,32 km je Breitengrad),
``resampling`` wirkt beim Wechsel ins Arbeits-CRS und wird von der Transformation
'reproject' gelesen. ``resolution_m`` + ``aggregation`` (FA77) fassen in voller
Auflösung streifenweise gelesene Zellen zu einem Zielgitter zusammen (nie aus den
gemittelten Übersichten der Datei); das Zellbudget gilt für das Zielraster.

Zellbudget (FA4/NFA2): vor ``dataset.read`` wird die Zellzahl des Lesefensters
(Bänder x Zeilen x Spalten) gegen ``_raster_io.MAX_CELLS`` geprüft; ein zu
großes Fenster scheitert mit Layer, Zellzahl und Abhilfe. ``path`` darf ein
``str`` sein (GDAL-Pfade wie ``/vsizip/...``)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional, Union

import numpy as np
import rasterio
import rasterio.crs
from affine import Affine
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rasterio.warp import transform_bounds
from rasterio.errors import WindowError
from rasterio.windows import Window, from_bounds

from geofact.builtin import _raster_io
from geofact.builtin._raster_io import region_window_bounds
from geofact.plugin_api import (
    DataType,
    LayerData,
    LoadContext,
    RasterLayer,
    register_file_format,
)

if TYPE_CHECKING:
    from geofact.builtin.sources.file import FileLayer


class RasterOptions(BaseModel):
    """Formatspezifische Felder eines Rasters (GeoTIFF) in der Szenario-YAML."""

    model_config = ConfigDict(extra="forbid")

    bands: Optional[list[Union[int, str]]] = Field(
        None,
        min_length=1,
        description="Zu lesende Bänder: 1-basierte Nummern oder Bandbeschreibungen der Datei. "
        "Ohne Angabe wird nur Band 1 gelesen.",
    )
    band_names: Optional[list[str]] = Field(
        None,
        min_length=1,
        description="Namen der gelesenen Bänder (für raster_calc); sonst die Beschreibung "
        "der Datei bzw. band<Nummer>.",
    )
    margin_km: float = Field(
        0,
        ge=0,
        description="Rand in Kilometern um die Region für das Lesefenster (Default 0).",
    )
    resampling: Literal["nearest", "bilinear", "average", "sum"] = Field(
        "nearest",
        description="Resampling beim Wechsel ins Arbeits-CRS; 'sum' erhält die Summe eines "
        "Zählrasters, 'nearest' (Default) nicht.",
    )
    resolution_m: Optional[float] = Field(
        None,
        gt=0,
        description="FA77: Zielauflösung in Metern (Quell-CRS); ganzzahliges Vielfaches der "
        "Quellzelle. Gelesen wird in voller Auflösung, zusammengefasst nach "
        "'aggregation' (nie aus Übersichten).",
    )
    aggregation: Optional[Literal["sum", "mean"]] = Field(
        None,
        description="FA77: Zusammenfassung je Zielzelle - 'sum' für Zählgrößen "
        "(Einwohner), 'mean' für Intensitäten; Pflicht mit resolution_m.",
    )

    @model_validator(mode="after")
    def aggregation_is_explicit(self) -> "RasterOptions":
        """FA77: Zielauflösung nur mit ausdrücklicher Zusammenfassung, und das
        Resampling ins Arbeits-CRS darf ihr nicht widersprechen."""
        if (self.resolution_m is None) != (self.aggregation is None):
            raise ValueError(
                "resolution_m und aggregation gehören zusammen: resolution_m braucht "
                "aggregation: sum (Zählgrößen) oder mean (Intensitäten), und umgekehrt"
            )
        if self.aggregation == "sum" and self.resampling != "sum":
            raise ValueError(
                f"aggregation: sum mit resampling: {self.resampling} verliert die Summe beim "
                "Wechsel ins Arbeits-CRS - resampling: sum angeben"
            )
        if self.aggregation == "mean" and self.resampling == "sum":
            raise ValueError(
                "aggregation: mean mit resampling: sum summiert Mittelwerte - resampling: "
                "average, bilinear oder nearest angeben"
            )
        return self

    @model_validator(mode="after")
    def names_match_bands(self) -> "RasterOptions":
        if (
            self.bands is not None
            and self.band_names is not None
            and len(self.bands) != len(self.band_names)
        ):
            raise ValueError(
                f"band_names ({len(self.band_names)}) und bands ({len(self.bands)}) "
                "müssen gleich viele Einträge haben"
            )
        return self


def _resolve_bands(
    dataset, layer_id: str, options: RasterOptions
) -> tuple[list[int], list[str]]:
    """(1-basierte Bandnummern, Namen) der zu lesenden Bänder."""
    descriptions = list(dataset.descriptions)
    if options.bands is None:
        indices = list(range(1, dataset.count + 1))
    else:
        indices = []
        for selector in options.bands:
            if isinstance(selector, str):
                if selector not in descriptions:
                    raise ValueError(
                        f"Layer '{layer_id}': Band '{selector}' nicht in der Datei "
                        f"'{dataset.name}'; Bandbeschreibungen: {descriptions}, "
                        f"{dataset.count} Bänder (Nummern 1..{dataset.count})"
                    )
                indices.append(descriptions.index(selector) + 1)
            elif 1 <= selector <= dataset.count:
                indices.append(selector)
            else:
                raise ValueError(
                    f"Layer '{layer_id}': Band {selector} nicht in der Datei '{dataset.name}' "
                    f"({dataset.count} Bänder, Nummern 1..{dataset.count})"
                )
    if options.band_names is not None:
        if len(options.band_names) != len(indices):
            raise ValueError(
                f"Layer '{layer_id}': {len(options.band_names)} band_names {options.band_names} "
                f"für {len(indices)} gelesene Bänder"
            )
        names = list(options.band_names)
    else:
        names = [descriptions[i - 1] or f"band{i}" for i in indices]
    return indices, names


def _window_cells(dataset, window, band_count: int) -> int:
    """Zellen, die ``dataset.read`` für ``window`` (None = ganze Datei) liest:
    das an der Datei beschnittene Fenster, aufgerundet, mal Bandzahl."""
    if window is None:
        return dataset.height * dataset.width * band_count
    row_start = max(0, math.floor(window.row_off))
    row_end = min(dataset.height, math.ceil(window.row_off + window.height))
    col_start = max(0, math.floor(window.col_off))
    col_end = min(dataset.width, math.ceil(window.col_off + window.width))
    return max(0, row_end - row_start) * max(0, col_end - col_start) * band_count


def _window_crs(dataset, layer: FileLayer, path: str | Path):
    """CRS, in dem das Lesefenster berechnet wird - dieselbe Regel wie bei der
    Harmonisierung (FA4/FA55): die Datei, ohne CRS in der Datei die Angabe
    ``crs``, mit ``crs_override: true`` immer ``crs``."""
    declared = getattr(layer, "crs", None)
    if declared is not None and (
        dataset.crs is None or getattr(layer, "crs_override", False)
    ):
        return declared
    if dataset.crs is None:
        raise ValueError(
            f"Layer '{layer.id}': '{path}' hat kein CRS - das Lesefenster der Region lässt "
            "sich nicht bestimmen; crs: EPSG:... am Layer angeben"
        )
    return dataset.crs


_CELL_EPS = 1e-6
"""Toleranz (in Zellen) beim Runden des Fensters: Gleitkommareste der
Transformation (4.9999999 statt 5) sollen keine zusätzliche Zelle ergeben."""


def _whole_cells(window: Window) -> Window:
    """``window`` nach außen auf ganze Zellen gerundet. ``dataset.read`` liest ab
    der ganzen Zelle, ``window_transform`` setzt den Ursprung aber an den
    Bruchteil-Versatz; ohne Rundung lägen die Daten bis zu einer Zelle verschoben."""
    col_start = math.floor(window.col_off + _CELL_EPS)
    row_start = math.floor(window.row_off + _CELL_EPS)
    col_end = math.ceil(window.col_off + window.width - _CELL_EPS)
    row_end = math.ceil(window.row_off + window.height - _CELL_EPS)
    return Window(
        col_start, row_start, max(0, col_end - col_start), max(0, row_end - row_start)
    )


def _clipped(window: Window, dataset) -> Window:
    """``window`` an der Datei beschnitten. ``dataset.read`` beschneidet ohnehin,
    ``window_transform`` rechnet aber mit dem unbeschnittenen Versatz - ein
    Raster kleiner als die Region läge sonst an der falschen Stelle. Ohne
    Überlappung bleibt das Fenster (leeres Ergebnis, das die Ladeschicht meldet)."""
    try:
        return window.intersection(Window(0, 0, dataset.width, dataset.height))
    except WindowError:
        return window


STRIP_SOURCE_CELLS = 16_000_000
"""FA77: source cells per band read at once while aggregating (float64: 128 MB)."""

_FACTOR_EPS = 1e-6


def _block_factor(resolution_m: float, cell: float, axis: str, layer_id: str) -> int:
    """Source cells per target cell along one axis; must be a whole number."""
    factor = resolution_m / abs(cell)
    whole = round(factor)
    if whole < 1 or abs(factor - whole) > _FACTOR_EPS * max(1.0, factor):
        raise ValueError(
            f"Layer '{layer_id}': resolution_m {resolution_m:g} ist kein ganzzahliges Vielfaches "
            f"der Quellzelle ({abs(cell):g} m in {axis}) - z. B. {abs(cell) * max(1, whole):g} "
            f"oder {abs(cell) * (max(1, whole) + 1):g} angeben"
        )
    return whole


def _read_aggregated(
    dataset,
    layer: FileLayer,
    path,
    read_args: tuple,
    window,
    band_count: int,
    multiband: bool,
):
    """FA77: read ``window`` at full resolution in row strips and combine
    ``fy x fx`` source cells into one target cell (sum or mean of the valid
    cells). The target grid is aligned to whole blocks of the file's grid, so the
    result does not depend on the region window. Full-resolution reads avoid the
    file's (averaged) overviews, which GDAL would pick for a downsampled read."""
    options: RasterOptions = layer.options
    crs = _window_crs(dataset, layer, path)
    crs = (
        crs
        if isinstance(crs, rasterio.crs.CRS)
        else rasterio.crs.CRS.from_user_input(crs)
    )
    if not crs.is_projected or (crs.linear_units or "").lower() not in (
        "metre",
        "meter",
        "m",
    ):
        raise ValueError(
            f"Layer '{layer.id}': resolution_m braucht ein projiziertes Quell-CRS in Metern, "
            f"'{path}' hat {crs.to_string()} - eine Datei in einem metrischen CRS verwenden"
        )
    fx = _block_factor(options.resolution_m, dataset.transform.a, "x", layer.id)
    fy = _block_factor(options.resolution_m, dataset.transform.e, "y", layer.id)
    if window is None:
        window = Window(0, 0, dataset.width, dataset.height)
    # Clamped to the file: a region without overlap (``_clipped`` keeps the
    # unclipped window) gives an empty raster like the plain read, which the
    # loading layer reports - not a broadcast error from negative offsets.
    col0 = min(max(0, int(window.col_off)), dataset.width) // fx * fx
    row0 = min(max(0, int(window.row_off)), dataset.height) // fy * fy
    col1 = min(dataset.width, -(-int(window.col_off + window.width) // fx) * fx)
    row1 = min(dataset.height, -(-int(window.row_off + window.height) // fy) * fy)
    width, height = max(0, col1 - col0), max(0, row1 - row0)
    if width == 0 or height == 0:
        width = height = 0
    out_cols, out_rows = -(-width // fx), -(-height // fy)
    _raster_io.check_cell_budget(
        out_rows * out_cols * band_count,
        context=f"Layer '{layer.id}': das Zielraster von '{path}' bei {options.resolution_m:g} m "
        f"({band_count} Band/Baender)",
        hint="Region verkleinern oder resolution_m vergrößern",
    )
    nodata = dataset.nodata
    total = np.zeros((band_count, out_rows, out_cols), dtype="float64")
    count = np.zeros((band_count, out_rows, out_cols), dtype="int64")
    strip_rows = max(1, STRIP_SOURCE_CELLS // max(1, width * fy)) if width else 1
    for out_start in range(0, out_rows, strip_rows):
        out_n = min(strip_rows, out_rows - out_start)
        src_row = row0 + out_start * fy
        src_n = min(out_n * fy, row1 - src_row)
        block = dataset.read(
            read_args[0] if multiband else [1],
            window=Window(col0, src_row, width, src_n),
        ).astype("float64")
        valid = ~np.isnan(block)
        if nodata is not None and not np.isnan(nodata):
            valid &= block != nodata
        padded = np.zeros((band_count, out_n * fy, out_cols * fx), dtype="float64")
        padded_valid = np.zeros(padded.shape, dtype=bool)
        padded[:, :src_n, :width] = np.where(valid, block, 0.0)
        padded_valid[:, :src_n, :width] = valid
        shape = (band_count, out_n, fy, out_cols, fx)
        total[:, out_start : out_start + out_n] = padded.reshape(shape).sum(axis=(2, 4))
        count[:, out_start : out_start + out_n] = padded_valid.reshape(shape).sum(
            axis=(2, 4)
        )
    empty = count == 0
    if options.aggregation == "mean":
        with np.errstate(invalid="ignore", divide="ignore"):
            total = total / count
    out_nodata = nodata if nodata is not None else (np.nan if empty.any() else None)
    if (
        out_nodata is not None
        and not np.isnan(out_nodata)
        and bool((total[~empty] == out_nodata).any())
    ):
        # A block sum/mean of valid cells hit the file's nodata value (e.g. a
        # count raster with nodata 65535); keeping that sentinel would turn the
        # cell into nodata downstream. The float64 result uses NaN instead.
        out_nodata = np.nan
    if empty.any():
        total[empty] = np.nan if out_nodata is None else out_nodata
    transform = (
        dataset.transform * Affine.translation(col0, row0) * Affine.scale(fx, fy)
    )
    return (total if multiband else total[0]), transform, out_nodata


@register_file_format(
    "tif",
    extensions=("tif", "tiff"),
    zip_extensions=("tif", "tiff"),
    data_type=DataType.RASTER,
    options_model=RasterOptions,
    description="GeoTIFF; nur der Ausschnitt der Region wird gelesen (optional mehrere Bänder)",
)
def _read_geotiff(path: str | Path, layer: FileLayer, ctx: LoadContext) -> LayerData:
    options: RasterOptions = layer.options
    multiband = options.bands is not None or options.band_names is not None
    with rasterio.open(path) as dataset:
        if multiband:
            indices, names = _resolve_bands(dataset, layer.id, options)
            read_args: tuple = (indices,)
        else:
            names = None
            read_args = (1,)
        band_count = len(read_args[0]) if multiband else 1
        window = None
        if ctx.region is not None:
            bounds = region_window_bounds(
                tuple(ctx.region.total_bounds), options.margin_km
            )
            source_crs = _window_crs(dataset, layer, path)
            left, bottom, right, top = transform_bounds(
                "EPSG:4326", source_crs, *bounds
            )
            window = _clipped(
                _whole_cells(
                    from_bounds(left, bottom, right, top, transform=dataset.transform)
                ),
                dataset,
            )
        if options.resolution_m is not None:
            data, transform, nodata = _read_aggregated(
                dataset, layer, path, read_args, window, band_count, multiband
            )
            return RasterLayer(
                data=data,
                transform=transform,
                crs=dataset.crs,
                path=str(path),
                nodata=nodata,
                band_names=names,
            )
        _raster_io.check_cell_budget(
            _window_cells(dataset, window, band_count),
            context=f"Layer '{layer.id}': das Lesefenster von '{path}' ({band_count} Band/Baender)",
            hint="Region verkleinern, weniger Bänder lesen, resolution_m mit aggregation "
            "angeben (FA77) oder eine gröbere Datei verwenden",
        )
        if window is None:
            data = dataset.read(*read_args)
            transform = dataset.transform
        else:
            data = dataset.read(*read_args, window=window)
            transform = dataset.window_transform(window)
        return RasterLayer(
            data=data,
            transform=transform,
            crs=dataset.crs,
            path=str(path),
            nodata=dataset.nodata,
            band_names=names,
        )
