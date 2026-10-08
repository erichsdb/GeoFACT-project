"""Implements: FA12 (example data of the web demo: elevation model for sachsen_nachthimmel).

Builds examples/data/dem_sachsen_glo90.tif, the elevation raster of the example
examples/sachsen_nachthimmel.yaml. GeoFACT reads exactly one file per raster layer,
so the ten 1-degree tiles of the Copernicus DEM GLO-90 that cover Saxony (N50/N51,
E011 to E015) are downloaded from the public AWS bucket copernicus-dem-90m (no
account needed) and merged into one GeoTIFF (deflate, about 33 MB).

Usage:
    uv run python scripts/prepare_dem_sachsen.py
    uv run python scripts/prepare_dem_sachsen.py --tiles-dir tiles   # keep the tiles for a rerun

Copernicus DEM GLO-90: free to use with attribution (licence of the Copernicus DEM).
"""

from __future__ import annotations

import argparse
import tempfile
import urllib.request
from pathlib import Path

import rasterio
from rasterio.merge import merge

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "examples" / "data" / "dem_sachsen_glo90.tif"
BUCKET_URL = "https://copernicus-dem-90m.s3.amazonaws.com"

# 1-degree tiles named by their lower-left corner; together they cover Saxony
# (11.87-15.04 E, 50.17-51.69 N).
SAXONY_LATITUDES = (50, 51)
SAXONY_LONGITUDES = (11, 12, 13, 14, 15)


def tile_names(
    latitudes: tuple[int, ...] = SAXONY_LATITUDES,
    longitudes: tuple[int, ...] = SAXONY_LONGITUDES,
) -> list[str]:
    """Tile names in the naming scheme of the bucket, e.g. Copernicus_DSM_COG_30_N50_00_E011_00_DEM."""
    return [
        f"Copernicus_DSM_COG_30_N{lat:02d}_00_E{lon:03d}_00_DEM"
        for lat in latitudes
        for lon in longitudes
    ]


def download_tile(name: str, directory: Path) -> Path:
    """Fetch one tile into the directory (an existing file is reused)."""
    target = directory / f"{name}.tif"
    if target.exists():
        return target
    url = f"{BUCKET_URL}/{name}/{name}.tif"
    partial = target.with_suffix(".part")
    print(f"download {url}")
    urllib.request.urlretrieve(url, partial)
    partial.replace(target)
    return target


def merge_tiles(tiles: list[Path], out: Path) -> tuple[int, ...]:
    """Merge the tiles into one deflate-compressed GeoTIFF; returns the mosaic shape."""
    if not tiles:
        raise ValueError("no tiles to merge")
    sources = [rasterio.open(path) for path in tiles]
    try:
        mosaic, transform = merge(sources)
        profile = sources[0].profile | {
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": transform,
            "compress": "deflate",
        }
    finally:
        for source in sources:
            source.close()
    for key in ("blockxsize", "blockysize", "tiled"):
        profile.pop(key, None)
    out.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(mosaic)
    return mosaic.shape


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="target GeoTIFF")
    parser.add_argument(
        "--tiles-dir",
        type=Path,
        default=None,
        help="directory for the downloaded tiles (kept; default: a temporary directory)",
    )
    args = parser.parse_args()

    def build(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        tiles = [download_tile(name, directory) for name in tile_names()]
        shape = merge_tiles(tiles, args.out)
        print(f"{args.out}: {len(tiles)} tiles, mosaic {shape}")

    if args.tiles_dir is not None:
        build(args.tiles_dir)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            build(Path(tmp))


if __name__ == "__main__":
    main()
