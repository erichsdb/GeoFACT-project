"""Test helper for FA46 (source: stac): synthetic Sentinel-2-like scenes as tiny
local GeoTIFFs plus canned STAC responses. Nothing here touches the network and
no binary fixture is checked in - the scenes are written into tmp_path by the
tests (cell sizes 100 m / 200 m instead of the real 10 m / 20 m keep them small).

The pixel values are a function of the MAP COORDINATE of a cell, not of its
row/column, so every scene shows the same value at the same place: a mosaic of
two scenes, a window read at any offset and the 200 m class band resampled to
the 100 m grid can all be checked against ``expected_*`` without knowing how the
scene was cut. That is what makes an off-by-one-cell alignment error visible."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine
from rasterio.warp import transform_bounds

FINE = 100.0
COARSE = 200.0
SCL_CLASSES = np.array(
    [4, 5, 6, 7, 2, 11, 3, 8, 9, 10], dtype=np.uint8
)  # includes 3, 8, 9, 10

# Region used by the tests: a few km around the Chemnitz Hauptbahnhof (WGS84).
REGION_BBOX = (12.90, 50.82, 12.95, 50.86)
# One big "tile" footprint that covers the region comfortably (WGS84).
TILE_FOOTPRINT = (12.60, 50.60, 13.30, 51.05)
WEST_FOOTPRINT = (12.60, 50.60, 12.925, 51.05)
EAST_FOOTPRINT = (12.915, 50.60, 13.30, 51.05)


# A "city" for the end-to-end test: a rectangle that follows the 100 m grid of the scenes in UTM
# zone 33 (about the extent of Chemnitz), shifted 1 cm into the grid (right/up by +0.01 m on the
# left/right edges and -0.01 m on the top/bottom edges). Zonal statistics then take exactly the
# cells whose centre lies inside, and the result can be compared EXACTLY with an independent
# calculation: `zonal_stats` positions its window at the fractional offset of the zone's bounds
# and a zone edge that lies on a cell boundary ends up one cell off by rounding noise alone.
CITY_BOX = (339_600.01, 5_623_499.99, 363_600.01, 5_641_499.99)  # 24 km x 18 km


def aligned_ring(
    box_utm: tuple[float, float, float, float], epsg: int = 32633, steps: int = 40
):
    """Closed (lon, lat) ring of a UTM rectangle, every side densified to `steps` vertices so that
    its edges stay on the UTM grid lines after the pipeline projects it back."""
    from pyproj import Transformer

    left, bottom, right, top = box_utm
    corners = [(left, bottom), (right, bottom), (right, top), (left, top)]
    points = []
    for (x0, y0), (x1, y1) in zip(corners, corners[1:] + corners[:1]):
        for i in range(steps):
            points.append((x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps))
    points.append(points[0])
    to_lonlat = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    return [to_lonlat.transform(x, y) for x, y in points]


def red_value(x: np.ndarray, y: np.ndarray, fine: float = FINE) -> np.ndarray:
    col, row = np.floor(x / fine).astype(np.int64), np.floor(y / fine).astype(np.int64)
    return (1000 + (row * 7 + col * 3) % 2000).astype(np.uint16)


def nir_value(x: np.ndarray, y: np.ndarray, fine: float = FINE) -> np.ndarray:
    col, row = np.floor(x / fine).astype(np.int64), np.floor(y / fine).astype(np.int64)
    return (3000 + (row * 5 + col * 11) % 3000).astype(np.uint16)


def scl_value(x: np.ndarray, y: np.ndarray, coarse: float = COARSE) -> np.ndarray:
    col, row = (
        np.floor(x / coarse).astype(np.int64),
        np.floor(y / coarse).astype(np.int64),
    )
    return SCL_CLASSES[(row + 2 * col) % len(SCL_CLASSES)]


def cell_centres(
    transform: Affine, rows: int, cols: int
) -> tuple[np.ndarray, np.ndarray]:
    """Map coordinates (x, y) of every cell centre, shape (rows, cols)."""
    xs = transform.c + (np.arange(cols) + 0.5) * transform.a
    ys = transform.f + (np.arange(rows) + 0.5) * transform.e
    return np.meshgrid(xs, ys)


def expected_bands(transform: Affine, rows: int, cols: int) -> dict[str, np.ndarray]:
    """What a correctly read window with this transform must contain."""
    x, y = cell_centres(transform, rows, cols)
    return {"red": red_value(x, y), "nir": nir_value(x, y), "scl": scl_value(x, y)}


def write_scene(
    directory: Path,
    scene_id: str,
    *,
    epsg: int = 32633,
    footprint: tuple[float, float, float, float] = TILE_FOOTPRINT,
    nodata: int | None = 0,
    with_scl: bool = True,
    hole: tuple | None = None,
) -> dict[str, str]:
    """Writes red/nir (100 m, uint16) and scl (200 m, uint8) for a scene whose grid
    covers ``footprint`` in the CRS ``epsg``; returns asset key -> file path.
    ``hole``: (x0, y0, x1, y1) in map coordinates set to nodata in red/nir (tests)."""
    directory.mkdir(parents=True, exist_ok=True)
    left, bottom, right, top = transform_bounds(
        "EPSG:4326", f"EPSG:{epsg}", *footprint, densify_pts=21
    )
    left, top = np.floor(left / COARSE) * COARSE, np.ceil(top / COARSE) * COARSE
    right, bottom = np.ceil(right / COARSE) * COARSE, np.floor(bottom / COARSE) * COARSE
    fine_cols, fine_rows = int((right - left) / FINE), int((top - bottom) / FINE)
    paths: dict[str, str] = {}
    for key, name, resolution, maker, dtype in (
        ("red", "B04", FINE, red_value, "uint16"),
        ("nir", "B08", FINE, nir_value, "uint16"),
        ("scl", "SCL", COARSE, scl_value, "uint8"),
    ):
        if key == "scl" and not with_scl:
            continue
        transform = Affine(resolution, 0, left, 0, -resolution, top)
        rows, cols = int((top - bottom) / resolution), int((right - left) / resolution)
        x, y = cell_centres(transform, rows, cols)
        data = maker(x, y, resolution).astype(dtype)
        if hole is not None and key in ("red", "nir"):
            x0, y0, x1, y1 = hole
            data[(x >= x0) & (x < x1) & (y >= y0) & (y < y1)] = 0
        path = directory / f"{scene_id}_{name}.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=rows,
            width=cols,
            count=1,
            dtype=dtype,
            crs=f"EPSG:{epsg}",
            transform=transform,
            nodata=nodata,
        ) as dataset:
            dataset.write(data, 1)
        paths[key] = str(path)
    assert fine_cols > 0 and fine_rows > 0
    return paths


def add_overviews(paths: dict[str, str], factors: tuple[int, ...] = (2, 4)) -> None:
    """Builds internal overviews (average) into the reflectance files of ``write_scene``, like
    the Cloud Optimized GeoTIFFs of Earth Search carry them. The class band stays without:
    the source must not depend on how a provider resampled class codes."""
    from rasterio.enums import Resampling

    for key in ("red", "nir"):
        with rasterio.open(paths[key], "r+") as dataset:
            dataset.build_overviews(list(factors), Resampling.average)


def footprint_geometry(footprint: tuple[float, float, float, float]) -> dict:
    west, south, east, north = footprint
    return {
        "type": "Polygon",
        "coordinates": [
            [[west, south], [east, south], [east, north], [west, north], [west, south]]
        ],
    }


def stac_item(
    scene_id: str,
    date: str,
    cloud: float,
    paths: dict[str, str],
    *,
    epsg: int = 32633,
    grid_code: str | None = "MGRS-33UUS",
    footprint: tuple = TILE_FOOTPRINT,
    boa_offset_applied: bool = True,
    scale: float | None = 0.0001,
    offset: float = -0.1,
) -> dict:
    """A STAC item as Earth Search returns it (only the fields the source reads)."""
    properties = {
        "datetime": f"{date}T10:26:44.168000Z",
        "eo:cloud_cover": cloud,
        "proj:epsg": epsg,
        "earthsearch:boa_offset_applied": boa_offset_applied,
    }
    if grid_code is not None:
        properties["grid:code"] = grid_code
    assets = {}
    for key, href in paths.items():
        raster_band: dict = {"nodata": 0}
        if scale is not None and key != "scl":
            raster_band.update({"scale": scale, "offset": offset})
        assets[key] = {"href": href, "raster:bands": [raster_band]}
    return {
        "type": "Feature",
        "id": scene_id,
        "properties": properties,
        "geometry": footprint_geometry(footprint),
        "bbox": list(footprint),
        "assets": assets,
    }


def search_response(items: list[dict], **extra) -> dict:
    return {"type": "FeatureCollection", "features": items, "links": [], **extra}


class FakeStac:
    """Stands in for ``http.post_json`` / ``http.get_json`` of the STAC source and
    records the requests. ``pages``: successive responses of a paged search."""

    def __init__(
        self,
        items: list[dict] | None = None,
        pages: list[dict] | None = None,
        item_by_id: dict[str, dict] | None = None,
    ):
        self.pages = pages if pages is not None else [search_response(items or [])]
        self.item_by_id = item_by_id or {}
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    def post_json(self, url, *, body, context, max_retries=3):
        self.posts.append((url, body))
        return self.pages[min(len(self.posts), len(self.pages)) - 1]

    def get_json(self, url, *, params=None, context, max_retries=3):
        self.gets.append(url)
        item_id = url.rsplit("/", 1)[-1]
        if item_id not in self.item_by_id:
            raise RuntimeError(f"unexpected GET {url}")
        return self.item_by_id[item_id]

    def install(self, monkeypatch, module) -> "FakeStac":
        monkeypatch.setattr(module.http, "post_json", self.post_json)
        monkeypatch.setattr(module.http, "get_json", self.get_json)
        return self
