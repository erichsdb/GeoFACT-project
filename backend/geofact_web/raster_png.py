"""Raster-Layer -> PNG-Vorschau (nur Web-Schicht).

Bewusst hier statt im Kern, damit geofact keine Bild-Abhängigkeit (Pillow) bekommt.
Rendert ein RasterLayer als farbcodiertes PNG (Viridis-ähnliche LUT), No-Data/NaN
transparent, große Raster heruntergerechnet. Die Bounds liefert
presentation/serialize.py (raster_bounds_wgs84).
"""

from __future__ import annotations

import io

import numpy as np

from geofact.api import RasterLayer

# Kompakte Viridis-Stützstellen (RGB, 0..255) - linear interpoliert.
_VIRIDIS = np.array(
    [
        [68, 1, 84],
        [72, 40, 120],
        [62, 74, 137],
        [49, 104, 142],
        [38, 130, 142],
        [31, 158, 137],
        [53, 183, 121],
        [109, 205, 89],
        [180, 222, 44],
        [253, 231, 37],
    ],
    dtype=np.float64,
)


def _build_lut(n: int = 256) -> np.ndarray:
    xs = np.linspace(0, len(_VIRIDIS) - 1, n)
    lo = np.floor(xs).astype(int)
    hi = np.clip(lo + 1, 0, len(_VIRIDIS) - 1)
    frac = (xs - lo)[:, None]
    lut = _VIRIDIS[lo] * (1 - frac) + _VIRIDIS[hi] * frac
    return lut.astype(np.uint8)


_LUT = _build_lut()


def _first_band(data: np.ndarray) -> np.ndarray:
    if data.ndim == 3:
        return data[0]
    return data


def _downsample(band: np.ndarray, max_dim: int) -> np.ndarray:
    rows, cols = band.shape
    step = max(1, int(np.ceil(max(rows, cols) / max_dim)))
    if step == 1:
        return band
    return band[::step, ::step]


def render_png(raster: RasterLayer, max_dim: int = 1024) -> bytes:
    from PIL import Image

    data = np.asarray(raster.data)
    declared = _downsample(raster.valid_mask(1 if data.ndim == 3 else None), max_dim)
    band = _first_band(data).astype(np.float64)
    band = _downsample(band, max_dim)

    valid = np.isfinite(band)
    # Häufige No-Data-Sentinels und das deklarierte Nodata des Rasters ausblenden.
    valid &= band > -1e30
    valid &= declared
    if valid.any():
        vmin = np.percentile(band[valid], 2)
        vmax = np.percentile(band[valid], 98)
    else:
        vmin, vmax = 0.0, 1.0
    span = vmax - vmin if vmax > vmin else 1.0

    normalized = np.clip((band - vmin) / span, 0, 1)
    indices = (normalized * (len(_LUT) - 1)).astype(np.uint8)

    rgb = _LUT[indices]  # (rows, cols, 3)
    alpha = np.where(valid, 255, 0).astype(np.uint8)
    rgba = np.dstack([rgb, alpha])

    image = Image.fromarray(rgba, mode="RGBA")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
