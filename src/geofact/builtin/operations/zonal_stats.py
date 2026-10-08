"""Implements: FA8 (räumliche Operationen: zonal_stats, Nenner-Quote je Zone), FA47 (Anteil je Zone aus Raster).

Aggregatstatistik eines Rasters je Vektorzone.

Statistiken: ``sum``, ``mean``, ``max``, ``min``, ``count`` (gültige Zellen),
``std`` (Standardabweichung der Grundgesamtheit) und ``share`` (Anteil der
gültigen Zellen, die gleich ``class_value`` sind, Default 1; bei einem 0/1-Raster
der Mittelwert, z. B. Grünflächenanteil je Stadtteil). ``output_field`` benennt
die Ergebnisspalte (Default ``<stat>_value``), ``band`` wählt bei einem
Mehrband-Raster das Band (ohne Angabe nur bei genau einem Band erlaubt).

Eine Zone ohne Wert (außerhalb des Rasters, nur Nodata-Zellen oder leere
Geometrie) bekommt NaN; eine Sammelwarnung (``RasterCoverageWarning``) nennt
Anzahl und die ersten Zonen-Indizes.

Nenner-Quote (FA8): die Statistik stützt sich nur auf die gültigen Zellen einer
Zone. Der gültige Anteil ist ``gültige Zellen / Zellen der Zone``; Zellen
außerhalb des Rasters zählen im Nenner mit (das Zellgitter wird fortgesetzt).
``valid_share_field`` schreibt ihn als Spalte, eine ``RasterValidShareWarning``
nennt die Zonen unter 1,0, und ``min_valid_share`` setzt die Statistik einer Zone
unter der Schwelle auf NaN (Default 0). ``statistic_for_geometry`` rechnet eine
einzelne Zone."""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal, Optional, Self, Union

import numpy as np
from geopandas import GeoDataFrame
from pydantic import Field, model_validator
from rasterio.errors import WindowError
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds

from geofact.plugin_api import (
    DataType,
    ParamsBase,
    RasterLayer,
    register_operation,
    Step,
)

SHARE_DEFAULT_DECIMALS = 3
"""Nachkommastellen von ``share``, wenn ``decimals`` fehlt: bei Default 0 wären
alle Anteile 0 oder 1."""


class RasterCoverageWarning(UserWarning):
    """Zonen ohne Rasterwert (außerhalb des Rasters, nur Nodata, leere Geometrie)."""


class RasterValidShareWarning(UserWarning):
    """Zonen, deren Statistik sich nicht auf alle ihre Zellen stützt (Nodata
    oder außerhalb des Rasters), bzw. unter ``min_valid_share`` auf NaN gesetzt."""


class ZonalStatsStep(Step):
    op: Literal["zonal_stats"] = "zonal_stats"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "zones": DataType.VECTOR,
        "values": DataType.RASTER,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        stat: Literal["sum", "mean", "max", "min", "count", "std", "share"] = Field(
            "sum",
            description="Aggregatstatistik der Rasterwerte je Zone. 'count' zählt die "
            "gültigen Zellen, 'std' ist die Standardabweichung, 'share' der "
            "Anteil der gültigen Zellen mit dem Wert class_value.",
        )
        decimals: int = Field(
            0,
            strict=True,
            description="Nachkommastellen des Statistik-Attributs (Rundung). Default 0 "
            "(z. B. Bevölkerungssumme als Ganzzahl), bei stat: share 3; bei Bedarf "
            "explizit erhöhen (z. B. für mean auf einem Kontinuumsraster).",
        )
        output_field: Optional[str] = Field(
            None, description="Name der Ergebnisspalte (Default '<stat>_value')."
        )
        band: Optional[Union[int, str]] = Field(
            None,
            description="Band eines Mehrband-Rasters: Bandname oder 1-basierte Nummer. "
            "Ohne Angabe nur bei einem Ein-Band-Raster erlaubt.",
        )
        class_value: Optional[float] = Field(
            None,
            description="Nur für stat: share - der Rasterwert, dessen Anteil gesucht ist "
            "(Default 1, d. h. der Anteil der 1-Zellen eines 0/1-Rasters).",
        )
        valid_share_field: Optional[str] = Field(
            None,
            description="Spalte für den gültigen Anteil je Zone (gültige Zellen / "
            "Zellen der Zone, Zellen außerhalb des Rasters zählen mit). "
            "Ohne Angabe keine Spalte (die Warnung kommt trotzdem).",
        )
        min_valid_share: float = Field(
            0.0,
            ge=0.0,
            le=1.0,
            description="Mindestanteil gültiger Zellen (0..1); darunter wird die Statistik der "
            "Zone NaN. Default 0 = jede Zone mit einer gültigen Zelle behält "
            "ihren Wert.",
        )

        @model_validator(mode="before")
        @classmethod
        def share_needs_fractions(cls, data):
            """Ohne ``decimals`` rundet ``share`` auf 3 Stellen statt auf 0/1."""
            if (
                isinstance(data, dict)
                and data.get("stat") == "share"
                and "decimals" not in data
            ):
                return {**data, "decimals": SHARE_DEFAULT_DECIMALS}
            return data

        @model_validator(mode="after")
        def class_value_only_for_share(self) -> Self:
            if self.class_value is not None and self.stat != "share":
                raise ValueError(
                    f"class_value gilt nur für stat: share (hier stat: {self.stat})"
                )
            return self

        @model_validator(mode="after")
        def valid_share_field_is_distinct(self) -> Self:
            result_field = self.output_field or f"{self.stat}_value"
            if (
                self.valid_share_field is not None
                and self.valid_share_field == result_field
            ):
                raise ValueError(
                    f"valid_share_field '{self.valid_share_field}' ist schon die Ergebnisspalte "
                    "(output_field) - anderen Namen wählen"
                )
            return self

    def produced_fields(self) -> set[str]:
        output_field = self.params.get("output_field")
        fields = (
            {output_field}
            if output_field
            else {f"{self.params.get('stat', 'sum')}_value"}
        )
        if self.params.get("valid_share_field"):
            fields.add(self.params["valid_share_field"])
        return fields


def _stat_functions(class_value: float):
    return {
        "sum": np.sum,
        "mean": np.mean,
        "max": np.max,
        "min": np.min,
        "count": lambda pixels: float(pixels.size),
        "std": np.std,
        "share": lambda pixels: float(np.mean(pixels == class_value)),
    }


def _describe_indices(indices: list[int]) -> str:
    shown = ", ".join(str(i) for i in indices[:5])
    return f"[{shown}{', ...' if len(indices) > 5 else ''}]"


def _warn_missing(
    total: int, outside: list[int], no_valid: list[int], empty: list[int]
) -> None:
    """Eine Sammelwarnung für alle Zonen ohne Wert."""
    parts = []
    if outside:
        parts.append(
            f"{len(outside)} außerhalb des Rasters (Zonen {_describe_indices(outside)})"
        )
    if no_valid:
        parts.append(
            f"{len(no_valid)} nur mit Nodata-Zellen (Zonen {_describe_indices(no_valid)})"
        )
    if empty:
        parts.append(
            f"{len(empty)} mit leerer Geometrie (Zonen {_describe_indices(empty)})"
        )
    if parts:
        warnings.warn(
            f"zonal_stats: {len(outside) + len(no_valid) + len(empty)} von {total} Zonen ohne "
            f"Wert (NaN): " + "; ".join(parts),
            RasterCoverageWarning,
            stacklevel=2,
        )


def _warn_share(
    total: int,
    partial: list[tuple[int, float]],
    below: list[int],
    min_valid_share: float,
) -> None:
    """Eine Sammelwarnung für Zonen mit gültigem Anteil unter 1,0."""
    if not partial:
        return
    lowest = min(share for _, share in partial)
    message = (
        f"zonal_stats: {len(partial)} von {total} Zonen stützen sich nicht auf alle ihre "
        f"Zellen (Nodata oder außerhalb des Rasters; kleinster gültiger Anteil "
        f"{lowest:.3g}, Zonen {_describe_indices([i for i, _ in partial])})"
    )
    if below:
        noun = "Zone" if len(below) == 1 else "Zonen"
        message += (
            f"; {len(below)} {noun} unter min_valid_share {min_valid_share:g} auf NaN gesetzt "
            f"(Zonen {_describe_indices(below)})"
        )
    warnings.warn(message, RasterValidShareWarning, stacklevel=3)


def _zone_cells(
    geometry, raster: RasterLayer, band_data: np.ndarray, valid_cells: np.ndarray
) -> tuple[np.ndarray | None, int, str | None]:
    """(gültige Zellwerte, Zellen der Zone insgesamt, Grund für "kein Wert")
    einer Zone; Grund ist ``empty``, ``outside``, ``no_valid`` oder None.

    Die Maske wird nur über das Fenster der Zonen-BBox berechnet, nicht über das
    ganze Raster (sonst O(Zonen x Rastergröße) bei vielen kleinen Zonen). Ragt
    die Zone über das Raster hinaus, zählt der Nenner auch ihre Zellen auf dem
    fortgesetzten Gitter."""
    if geometry is None or geometry.is_empty:
        return None, 0, "empty"
    raster_height, raster_width = raster.shape
    full_window = from_bounds(
        *geometry.bounds, transform=raster.transform
    ).round_lengths(pixel_precision=0)
    try:
        window = full_window.crop(raster_height, raster_width)
    except WindowError:
        # ganz außerhalb des Rasters: kein Wert, die Sammelwarnung nennt sie
        return None, 0, "outside"
    if window.width <= 0 or window.height <= 0:
        return None, 0, "outside"

    window_transform = raster.transform * raster.transform.translation(
        window.col_off, window.row_off
    )
    row_slice = slice(int(window.row_off), int(window.row_off + window.height))
    col_slice = slice(int(window.col_off), int(window.col_off + window.width))
    windowed_data = band_data[row_slice, col_slice]

    inside = geometry_mask(
        [geometry],
        out_shape=windowed_data.shape,
        transform=window_transform,
        invert=True,
    )
    zone_cells = int(inside.sum())
    if (window.col_off, window.row_off, window.width, window.height) != (
        full_window.col_off,
        full_window.row_off,
        full_window.width,
        full_window.height,
    ):
        full_transform = raster.transform * raster.transform.translation(
            full_window.col_off, full_window.row_off
        )
        full_shape = (max(int(full_window.height), 1), max(int(full_window.width), 1))
        zone_cells = max(
            zone_cells,
            int(
                geometry_mask(
                    [geometry],
                    out_shape=full_shape,
                    transform=full_transform,
                    invert=True,
                ).sum()
            ),
        )
    mask = inside & valid_cells[row_slice, col_slice]
    pixels = windowed_data[mask]
    if not pixels.size:
        return None, zone_cells, "no_valid"
    return pixels, zone_cells, None


def _value_and_share(
    pixels: np.ndarray | None, zone_cells: int, stat_fn, reason
) -> tuple[float, float]:
    """(Statistik, gültiger Anteil) aus dem Ergebnis von ``_zone_cells``."""
    if reason == "empty":
        return np.nan, np.nan
    if pixels is None:
        return np.nan, 0.0
    share = min(1.0, pixels.size / zone_cells) if zone_cells else np.nan
    return stat_fn(pixels), share


def statistic_for_geometry(
    geometry,
    raster: RasterLayer,
    stat: str,
    class_value: float | None,
    *,
    band: int | str | None = None,
) -> tuple[float, float]:
    """(Statistik, gültiger Anteil) EINER Zone: Statistik über ihre gültigen
    Zellen (NaN ohne gültige Zelle), Anteil = gültige Zellen / Zellen der Zone
    (0.0 außerhalb des Rasters oder nur Nodata, NaN bei leerer Geometrie)."""
    class_value = 1.0 if class_value is None else class_value
    pixels, zone_cells, reason = _zone_cells(
        geometry, raster, raster.band_data(band), raster.valid_mask(band)
    )
    value, share = _value_and_share(
        pixels, zone_cells, _stat_functions(class_value)[stat], reason
    )
    return float(value), share


@register_operation(ZonalStatsStep)
def run_zonal_stats(inputs: dict, params: dict) -> GeoDataFrame:
    """Zonen werden unabhängig behandelt (Fensterrechnung je Zone in
    ``_zone_cells``), überlappende Zonen schließen sich also nicht aus.
    Nodata-Zellen (raster.nodata, z. B. der negative GHS-POP-Sentinel) werden vor
    der Statistik ausgeschlossen, sonst flosse der Sentinel als Messwert ein."""
    zones: GeoDataFrame = inputs["zones"]
    raster: RasterLayer = inputs["values"]
    stat = params.get("stat", "sum")
    decimals = params.get("decimals", SHARE_DEFAULT_DECIMALS if stat == "share" else 0)
    class_value = params.get("class_value")
    class_value = 1.0 if class_value is None else class_value
    explicit_field = params.get("output_field")
    share_field = params.get("valid_share_field")
    min_valid_share = float(params.get("min_valid_share") or 0.0)

    stat_fn = _stat_functions(class_value)[stat]
    field_name = explicit_field or f"{stat}_value"
    for label, name in (
        ("output_field", explicit_field),
        ("valid_share_field", share_field),
    ):
        if name and name in zones.columns:
            raise ValueError(
                f"zonal_stats: {label} '{name}' ist schon eine Spalte der Zonen "
                f"({sorted(c for c in zones.columns if c != zones.geometry.name)}) - anderen Namen wählen"
            )

    band_data = raster.band_data(params.get("band"))
    valid_cells = raster.valid_mask(params.get("band"))

    values: list[float] = []
    shares: list[float] = []
    missing: dict[str, list[int]] = {"outside": [], "no_valid": [], "empty": []}
    partial: list[tuple[int, float]] = []
    below: list[int] = []
    for position, geometry in enumerate(zones.geometry):
        pixels, zone_cells, reason = _zone_cells(
            geometry, raster, band_data, valid_cells
        )
        value, share = _value_and_share(pixels, zone_cells, stat_fn, reason)
        if reason is not None:
            missing[reason].append(position)
        elif share < 1.0:
            partial.append((position, share))
            if share < min_valid_share:
                below.append(position)
                value = np.nan
        values.append(value)
        shares.append(share)

    _warn_missing(len(zones), missing["outside"], missing["no_valid"], missing["empty"])
    _warn_share(len(zones), partial, below, min_valid_share)
    result = zones.copy()
    result[field_name] = np.round(values, decimals=decimals)
    if share_field:
        result[share_field] = shares
    return result
