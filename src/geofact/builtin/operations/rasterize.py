"""Implements: FA70 (Rasterisierung: rasterize).

Brennt die Objekte eines Vektorlayers in ein Raster im Arbeits-CRS:

    op: rasterize   params: {cell_size_m: 10}                       -> 0/1-Maske (uint8)
    op: rasterize   params: {cell_size_m: 100, value_field: dichte} -> Werte (float32)

Gitter: die Ausdehnung der Objekte, nach außen auf Vielfache von ``cell_size_m``
gerundet (gleiche Zellgröße = gleiches Gitter); ein Punkt oder eine Linie ohne
Ausdehnung bekommt eine Zelle. Vor dem Anlegen prüft
``_raster_io.check_cell_budget`` die Zellzahl (``MAX_CELLS``, NFA2).

Werte: ohne ``value_field`` 1 für jede Zelle, deren Mittelpunkt im Objekt liegt
(``all_touched: true``: jede berührte Zelle), sonst ``fill`` - Ergebnis ``uint8``.
Mit ``value_field`` der Spaltenwert als ``float32``; bei Überlappung gilt das
spätere Objekt (rasterio ``replace``). ``nodata`` deklariert den Nodata-Wert des
Ergebnisses (Default keiner).

Fehler: leerer Layer (kein Gitter ableitbar), geographisches CRS, unbekannte oder
nicht numerische ``value_field``-Spalte. Objekte ohne Geometrie, mit NaN im
``value_field`` und (ohne ``all_touched``) Flächen ohne Zellmittelpunkt werden mit
einer Warnung ausgelassen. Ein Punkt oder eine Linie genau auf dem rechten/unteren
Gitterrand bekommt eine Zelle (das Gitter wächst dort um eine Zelle)."""

from __future__ import annotations

import math
import warnings
from typing import ClassVar, Literal, Optional, Self

import numpy as np
import pandas as pd
import shapely
from geopandas import GeoDataFrame
from pydantic import Field, field_validator, model_validator
from rasterio.features import rasterize as burn
from rasterio.transform import from_origin

from geofact.builtin import _raster_io
from geofact.builtin.operations._geometry import require_projected_crs
from geofact.plugin_api import (
    DataType,
    ParamsBase,
    RasterLayer,
    register_operation,
    Step,
)

UINT8_MAX = 255


class RasterizeWarning(UserWarning):
    """Objekte, die ``rasterize`` ausgelassen hat (ohne Geometrie, NaN-Wert)."""


class RasterizeStep(Step):
    op: Literal["rasterize"] = "rasterize"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.RASTER

    class Params(ParamsBase):
        cell_size_m: float = Field(
            ...,
            gt=0,
            description="Zellgröße des Ergebnisrasters in Metern (Arbeits-CRS).",
        )
        value_field: Optional[str] = Field(
            None,
            description="Numerische Spalte, deren Wert eingebrannt wird (Ergebnis float32). "
            "Ohne Angabe eine 0/1-Maske (uint8).",
        )
        fill: float = Field(
            0,
            description="Wert der Zellen ohne Objekt (Default 0; ohne value_field eine ganze "
            "Zahl 0..255).",
        )
        nodata: Optional[float] = Field(
            None,
            description="Nodata-Wert des Ergebnisses (Default keiner); z. B. gleich fill, "
            "wenn leere Zellen kein Messwert sind.",
        )
        all_touched: bool = Field(
            False,
            description="true: jede vom Objekt berührte Zelle; false (Default): nur "
            "Zellen, deren Mittelpunkt im Objekt liegt.",
        )
        output_band: str = Field(
            "result",
            description="Name des Ergebnisbandes (in raster_calc als Bandname "
            "verwendbar).",
        )

        @field_validator("output_band")
        @classmethod
        def output_band_is_identifier(cls, value: str) -> str:
            if not value.isidentifier():
                raise ValueError(
                    f"output_band '{value}' muss ein Name aus Buchstaben, Ziffern und Unterstrich sein "
                    "(er wird in Ausdrücken als Bandname verwendet)"
                )
            return value

        @model_validator(mode="after")
        def mask_values_fit_uint8(self) -> Self:
            if self.value_field is not None:
                return self
            for name in ("fill", "nodata"):
                value = getattr(self, name)
                if value is None:
                    continue
                if (
                    math.isnan(value)
                    or value != int(value)
                    or not 0 <= value <= UINT8_MAX
                ):
                    raise ValueError(
                        f"{name} {value:g}: ohne value_field ist das Ergebnis eine uint8-Maske - "
                        f"{name} muss eine ganze Zahl von 0 bis {UINT8_MAX} sein"
                    )
            return self


def _aligned_grid(
    bounds: tuple[float, float, float, float],
    cell: float,
    open_right: bool = False,
    open_bottom: bool = False,
) -> tuple[float, float, int, int]:
    """(links, oben, Zeilen, Spalten) eines an Vielfachen von ``cell`` ausgerichteten
    Gitters, das ``bounds`` überdeckt (mindestens eine Zelle je Richtung).

    ``open_right``/``open_bottom``: ein Punkt oder eine Linie erreicht den rechten
    bzw. unteren Rand. Liegt dieser genau auf einer Gitterlinie, gehört er zur
    Zelle dahinter (GDAL: Spalte = floor((x - links) / cell)) - das Gitter wächst
    dann um eine Zelle, sonst fiele das Objekt still aus dem Array."""
    min_x, min_y, max_x, max_y = bounds
    left, bottom = math.floor(min_x / cell) * cell, math.floor(min_y / cell) * cell
    right, top = math.ceil(max_x / cell) * cell, math.ceil(max_y / cell) * cell
    if open_right and right <= max_x:
        right += cell
    if open_bottom and bottom >= min_y:
        bottom -= cell
    cols = max(1, round((right - left) / cell))
    rows = max(1, round((top - bottom) / cell))
    # Oberkante immer die aufgerundete y: ohne Ausdehnung (ein Punkt genau auf
    # einer Gitterlinie) liegt die eine Zelle darunter, und GDAL brennt den Punkt
    # in Zeile 0 ein (mit Ausdehnung ist top == bottom + rows * cell).
    return left, top, rows, cols


_AREAL = ("Polygon", "MultiPolygon")
_SMALL_CANDIDATES = 64
"""Flächen mit höchstens so vielen Zellmittelpunkten in ihrer BBox werden
vektorisiert geprüft, größere einzeln (erst um einen inneren Punkt, dann
mit einem Fenster-Burn)."""


def _polygons_without_cell(
    geometries, left: float, top: float, rows: int, cols: int, cell: float
) -> int:
    """Anzahl der Flächen, die bei ``all_touched: false`` keine Zelle belegen
    (GDAL brennt nur Zellen, deren Mittelpunkt im Objekt liegt)."""
    polygons = np.asarray(
        geometries[geometries.geom_type.isin(_AREAL)].to_numpy(), dtype=object
    )
    if polygons.size == 0:
        return 0
    bounds = shapely.bounds(polygons)
    col_lo = np.clip(np.ceil((bounds[:, 0] - left) / cell - 0.5), 0, cols - 1).astype(
        int
    )
    col_hi = np.clip(np.floor((bounds[:, 2] - left) / cell - 0.5), 0, cols - 1).astype(
        int
    )
    row_lo = np.clip(np.ceil((top - bounds[:, 3]) / cell - 0.5), 0, rows - 1).astype(
        int
    )
    row_hi = np.clip(np.floor((top - bounds[:, 1]) / cell - 0.5), 0, rows - 1).astype(
        int
    )
    n_cols = np.maximum(col_hi - col_lo + 1, 0)
    n_rows = np.maximum(row_hi - row_lo + 1, 0)
    candidates = n_cols * n_rows  # 0: kein Zellmittelpunkt in der BBox
    covered = np.zeros(polygons.size, dtype=bool)
    small = np.flatnonzero((candidates > 0) & (candidates <= _SMALL_CANDIDATES))
    if small.size:
        counts = candidates[small]
        index_all = np.repeat(small, counts)
        offset = np.arange(counts.sum()) - np.repeat(np.cumsum(counts) - counts, counts)
        cols_all = col_lo[index_all] + offset % n_cols[index_all]
        rows_all = row_lo[index_all] + offset // n_cols[index_all]
        hits = shapely.intersects_xy(
            polygons[index_all],
            left + (cols_all + 0.5) * cell,
            top - (rows_all + 0.5) * cell,
        )
        np.logical_or.at(covered, index_all, hits)
    for index in np.flatnonzero(candidates > _SMALL_CANDIDATES):
        covered[index] = _large_polygon_covers_a_cell(
            polygons[index],
            left,
            top,
            cell,
            (
                int(col_lo[index]),
                int(col_hi[index]),
                int(row_lo[index]),
                int(row_hi[index]),
            ),
        )
    return int((~covered).sum())


def _large_polygon_covers_a_cell(
    polygon, left: float, top: float, cell: float, span: tuple[int, int, int, int]
) -> bool:
    """Erst die Zellmittelpunkte um einen inneren Punkt, sonst ein Burn im Fenster
    der BBox (exakt nach GDAL)."""
    col_lo, col_hi, row_lo, row_hi = span
    inner = polygon.representative_point()
    col = math.floor((inner.x - left) / cell)
    row = math.floor((top - inner.y) / cell)
    near_c, near_r = np.meshgrid(
        np.arange(col - 1, col + 2), np.arange(row - 1, row + 2)
    )
    if shapely.intersects_xy(
        polygon,
        left + (near_c.ravel() + 0.5) * cell,
        top - (near_r.ravel() + 0.5) * cell,
    ).any():
        return True
    window = from_origin(left + col_lo * cell, top - row_lo * cell, cell, cell)
    burned = burn(
        [(polygon, 1)],
        out_shape=(row_hi - row_lo + 1, col_hi - col_lo + 1),
        transform=window,
        fill=0,
        all_touched=False,
        dtype=np.uint8,
    )
    return bool(burned.any())


def _values(features: GeoDataFrame, value_field: str) -> pd.Series:
    if value_field not in features.columns or value_field == features.geometry.name:
        columns = sorted(c for c in features.columns if c != features.geometry.name)
        raise ValueError(
            f"rasterize: value_field '{value_field}' ist keine Spalte des Layers; vorhanden: {columns}"
        )
    column = features[value_field]
    if pd.api.types.is_bool_dtype(column) or not pd.api.types.is_numeric_dtype(column):
        raise ValueError(
            f"rasterize: value_field '{value_field}' ist nicht numerisch (Typ {column.dtype}) - "
            "eine Zahlenspalte wählen oder vorher umrechnen"
        )
    return column.astype("float64")


@register_operation(RasterizeStep)
def run_rasterize(inputs: dict, params: dict) -> RasterLayer:
    features: GeoDataFrame = inputs["features"]
    require_projected_crs(features, op="rasterize")
    cell = float(params["cell_size_m"])
    value_field = params.get("value_field")
    fill = params.get("fill", 0)
    nodata = params.get("nodata")
    all_touched = bool(params.get("all_touched", False))
    output_band = params.get("output_band", "result")

    present = features.geometry.notna() & ~features.geometry.is_empty
    if not present.any():
        raise ValueError(
            f"rasterize: der Layer ist leer ({len(features)} Objekte, keines mit Geometrie) - "
            "aus ihm ist kein Gitter ableitbar; Filter davor prüfen"
        )
    values = _values(features, value_field) if value_field else None
    usable = present & (values.notna() if values is not None else True)

    drawn = features.geometry[present]
    bounds = tuple(drawn.total_bounds)
    linear = drawn[~drawn.geom_type.isin(_AREAL)]
    linear_bounds = linear.total_bounds if not linear.empty else None
    left, top, rows, cols = _aligned_grid(
        bounds,
        cell,
        open_right=linear_bounds is not None and linear_bounds[2] >= bounds[2],
        open_bottom=linear_bounds is not None and linear_bounds[1] <= bounds[1],
    )
    _raster_io.check_cell_budget(
        rows * cols,
        context=f"rasterize: Gitter {rows} x {cols} bei {cell:g} m",
        hint="cell_size_m vergrößern oder den Layer vorher zuschneiden (clip)",
    )
    transform = from_origin(left, top, cell, cell)
    geometries = features.geometry[usable]

    skipped = int((~usable).sum())
    uncovered = (
        0
        if all_touched
        else _polygons_without_cell(geometries, left, top, rows, cols, cell)
    )
    if skipped or uncovered:
        reasons = []
        if (~present).sum():
            reasons.append(f"{int((~present).sum())} ohne Geometrie")
        if values is not None and (present & values.isna()).sum():
            reasons.append(
                f"{int((present & values.isna()).sum())} mit NaN in '{value_field}'"
            )
        if uncovered:
            reasons.append(
                f"{uncovered} Fläche(n) belegen keine Zelle (kein Zellmittelpunkt im Objekt bei "
                f"{cell:g} m - all_touched: true oder kleinere Zellen)"
            )
        warnings.warn(
            f"rasterize: {skipped + uncovered} von {len(features)} Objekten ausgelassen "
            f"({', '.join(reasons)})",
            RasterizeWarning,
            stacklevel=2,
        )
    if values is None:
        dtype = np.uint8
        shapes = [(geometry, 1) for geometry in geometries]
    else:
        dtype = np.float32
        shapes = list(zip(geometries, values[usable]))
    data = (
        burn(
            shapes,
            out_shape=(rows, cols),
            transform=transform,
            fill=fill,
            all_touched=all_touched,
            dtype=dtype,
        )
        if shapes
        else np.full((rows, cols), fill, dtype=dtype)
    )
    return RasterLayer(
        data=np.ascontiguousarray(data),
        transform=transform,
        crs=features.crs,
        nodata=None if nodata is None else float(nodata),
        band_names=[output_band],
    )
