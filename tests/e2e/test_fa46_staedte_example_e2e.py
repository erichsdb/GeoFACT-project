"""Implements: FA46-FA49 (Szenario 'Gruenanteil der Staedte Leipzig, Dresden, Chemnitz' Ende zu Ende), FA3, FA7.

Runs the REAL example `examples/sachsen_staedte_gruenanteil_sentinel2.yaml` through `api.run`
with the real connectors `stac` and `osm` - only the network is replaced (recorded fake STAC
catalog pointing at tiny synthetic scenes in tmp_path, Overpass relations for three "cities"),
and two values of the example are scaled to the tiny world: the region (a bbox literal around
the synthetic cities instead of 128 x 87 km) and `resolution_m` (200 instead of 20, because the
synthetic cells are 100 m instead of 10 m).

What the example must show: the four-tile situation of the real catalog (no scene covers the
region, the tiles of the target zone are mosaicked, the tile of the neighbour zone is ignored),
the 2x decimation of the reflectance bands and the class band, the exact-name selector of the
three boundaries, and one share per city that agrees with an independent calculation on the
raster the scenario produced."""

from __future__ import annotations

import csv
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
import yaml
from rasterio.features import geometry_mask
from shapely.geometry import Polygon

from geofact import api
from geofact.builtin.sources import osm, stac
from osm_relations import relation, ring_ways
from stac_scenes import (
    EAST_FOOTPRINT,
    FINE,
    WEST_FOOTPRINT,
    FakeStac,
    aligned_ring,
    expected_bands,
    stac_item,
    write_scene,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "sachsen_staedte_gruenanteil_sentinel2.yaml"

RESOLUTION = (
    200  # the example's 20 m, scaled with the synthetic 100 m (instead of 10 m) cells
)
# Three "cities" side by side, 8 km x 18 km each, on the 200 m target grid of the scenes (their origins
# are multiples of 200 m) and shifted 1 cm into it: zonal_stats then takes exactly the cells whose centre
# lies inside, and an independent rasterisation gives the same cells (see tests/stac_scenes.py).
LEFT, BOTTOM, TOP = 339_600.01, 5_623_600.01, 5_641_399.99
CITY_WIDTH = 8_000.0
CITIES = {
    "Leipzig": (LEFT, BOTTOM, LEFT + CITY_WIDTH, TOP),
    "Dresden": (LEFT + CITY_WIDTH, BOTTOM, LEFT + 2 * CITY_WIDTH, TOP),
    "Chemnitz": (LEFT + 2 * CITY_WIDTH, BOTTOM, LEFT + 3 * CITY_WIDTH, TOP),
}
RINGS = {name: aligned_ring(box_utm) for name, box_utm in CITIES.items()}
ALL_CITIES = (
    Polygon(RINGS["Leipzig"])
    .union(Polygon(RINGS["Dresden"]))
    .union(Polygon(RINGS["Chemnitz"]))
)
MIN_LON, MIN_LAT, MAX_LON, MAX_LAT = ALL_CITIES.bounds
REGION = f"{MIN_LON - 0.01:.4f},{MIN_LAT - 0.01:.4f},{MAX_LON + 0.01:.4f},{MAX_LAT + 0.01:.4f}"

pytestmark = pytest.mark.e2e


def _relations() -> list[dict]:
    """The three boundary relations (level 6, exact names) the selector asks for. The fake answers
    with them whatever the query says; that the query is the exact-name one is asserted on its text."""
    return [
        relation(
            100 + index,
            ring_ways(RINGS[name], 3, 5000 + 10 * index),
            name=name,
            admin_level="6",
            **{"de:amtlicher_gemeindeschluessel": f"1{index}000000"},
        )
        for index, name in enumerate(CITIES)
    ]


class _Overpass:
    def __init__(self, elements):
        self.elements, self.requests = elements, []

    def post(self, url, data=None, headers=None, timeout=None):
        self.requests.append(data["data"])
        elements = self.elements

        class Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"elements": elements}

        return Response()


class World:
    def __init__(self, catalog, overpass, tiles):
        self.catalog, self.overpass, self.tiles = catalog, overpass, tiles


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv(
        "GEOFACT_OSM_BACKEND", "overpass"
    )  # a local .env with postgis must not win
    west = write_scene(tmp_path / "west", "west", footprint=WEST_FOOTPRINT)
    east = write_scene(tmp_path / "east", "east", footprint=EAST_FOOTPRINT)
    zone32 = write_scene(
        tmp_path / "zone32", "zone32", epsg=32632, footprint=WEST_FOOTPRINT
    )
    catalog = FakeStac(
        [
            stac_item(
                "S2A_33UUS_20250812_0_L2A",
                "2025-08-12",
                0.0,
                west,
                footprint=WEST_FOOTPRINT,
            ),
            stac_item(
                "S2A_33UVS_20250812_0_L2A",
                "2025-08-12",
                0.0,
                east,
                footprint=EAST_FOOTPRINT,
                grid_code="MGRS-33UVS",
            ),
            stac_item(
                "S2A_32UQB_20250812_0_L2A",
                "2025-08-12",
                0.0,
                zone32,
                epsg=32632,
                grid_code="MGRS-32UQB",
                footprint=WEST_FOOTPRINT,
            ),
        ]
    ).install(monkeypatch, stac)
    overpass = _Overpass(_relations())
    monkeypatch.setattr(osm.requests, "post", overpass.post)
    return World(catalog, overpass, (west, east))


def _document():
    """The example as it is, with the region and the cell size scaled to the synthetic world."""
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    config["scenario"]["region"] = REGION
    (layer,) = [layer for layer in config["layers"] if layer["id"] == "sentinel2"]
    assert layer["resolution_m"] == 20
    layer["resolution_m"] = RESOLUTION
    return api.load_scenario(config, base_dir=EXAMPLE.parent)


def _run(out_dir: Path, **options):
    return api.run(_document(), out_dir=out_dir, **options)


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _independent_shares(raster, geometries: list) -> list[float]:
    """Green share per geometry from the bands of the scene the scenario read, with the NDVI formula
    and a plain rasterisation - nothing of the scenario's own steps (the float32 rounding of
    raster_calc's result is part of its documented contract, see test_fa46_sentinel2_example_e2e)."""
    red, nir = raster.data[0].astype(np.float64), raster.data[1].astype(np.float64)
    valid = (red != 0) & (nir != 0)  # mask_clouds wrote nodata 0
    with np.errstate(invalid="ignore", divide="ignore"):
        ndvi = ((nir - red) / (nir + red)).astype(np.float32).astype(np.float64)
    green = ndvi > 0.3
    shares = []
    for geometry in geometries:
        inside = (
            ~geometry_mask(
                [geometry], out_shape=raster.shape, transform=raster.transform
            )
            & valid
        )
        shares.append(float(green[inside].mean()))
    return shares


def test_fa46_staedte_example_runs_end_to_end_and_writes_every_output(world, tmp_path):
    report = _run(tmp_path / "out")
    assert report.outputs.skipped == []
    assert sorted(p.name for p in report.outputs.written) == sorted(
        [
            "gruenanteil_staedte.html",
            "gruenanteil_staedte.geojson",
            "gruenanteil_staedte.csv",
        ]
    )
    assert all(p.stat().st_size > 0 for p in report.outputs.written)
    assert set(report.result.loaded_layers) == {"staedte", "sentinel2"}


def test_fa46_staedte_example_mosaics_the_target_zone_tiles_at_the_target_cell_size(
    world, tmp_path
):
    report = _run(tmp_path / "out")
    notices = [w for w in report.warnings if w.category == "StacSceneNotice"]
    assert len(notices) == 1 and notices[0].layer == "sentinel2"
    message = notices[0].message
    assert (
        "S2A_33UUS_20250812_0_L2A" in message and "S2A_33UVS_20250812_0_L2A" in message
    )
    assert (
        "S2A_32UQB" not in message
        and "2025-08-12" in message
        and f"Zellgröße {RESOLUTION} m" in message
    )
    # the search: the pinned day, the cloud limit, the bbox of the region
    ((_, body),) = world.catalog.posts
    assert body["datetime"] == "2025-08-12T00:00:00Z/2025-08-12T23:59:59Z"
    assert body["query"] == {"eo:cloud_cover": {"lte": 10.0}}
    assert body["bbox"] == pytest.approx([float(v) for v in REGION.split(",")])

    raster = report.result.store["sentinel2"]
    assert raster.crs.to_epsg() == 32633 and raster.band_names == ["red", "nir", "scl"]
    assert raster.transform.a == RESOLUTION and raster.transform.e == -RESOLUTION
    # the reflectance bands are the 2 x 2 means of the fine cells, the class band the sampled class
    rows, cols = raster.shape
    fine = rasterio.transform.Affine(
        FINE, 0, raster.transform.c, 0, -FINE, raster.transform.f
    )
    for index, name in ((0, "red"), (1, "nir")):
        cells = expected_bands(fine, rows * 2, cols * 2)[name].astype(np.float64)
        mean = cells.reshape(rows, 2, cols, 2).mean(axis=(1, 3))
        cloudy = raster.data[index] == 0
        assert np.abs(raster.data[index][~cloudy] - mean[~cloudy]).max() <= 1.0, name
    assert (
        0.05 < (raster.data[0] == 0).mean() < 0.6
    )  # cloud cells (SCL 3, 8, 9, 10) are nodata


def test_fa46_staedte_example_selects_the_three_boundaries_by_exact_name(
    world, tmp_path
):
    report = _run(tmp_path / "out")
    cities = report.result.store["staedte"]
    assert sorted(cities["name"]) == ["Chemnitz", "Dresden", "Leipzig"]
    assert cities.crs.to_epsg() == 32633
    assert set(cities.geometry.geom_type) <= {"Polygon", "MultiPolygon"}
    (query,) = world.overpass.requests
    for name in CITIES:
        assert (
            f'["boundary"="administrative"]["admin_level"="6"]["name"="{name}"]'
            in query
        )
    assert "~" not in query  # no pattern match: "Landkreis Leipzig" stays out


def test_fa46_staedte_example_one_share_per_city_matches_an_independent_calculation(
    world, tmp_path
):
    report = _run(tmp_path / "out")
    rows = _read_csv(tmp_path / "out" / "gruenanteil_staedte.csv")
    assert sorted(r["name"] for r in rows) == ["Chemnitz", "Dresden", "Leipzig"]
    raster = report.result.store["sentinel2"]
    geometries = [
        gpd.GeoSeries([Polygon(RINGS[r["name"]])], crs="EPSG:4326")
        .to_crs(raster.crs)
        .iloc[0]
        for r in rows
    ]
    expected = _independent_shares(raster, geometries)
    # the scenario rounds to 3 decimals (decimals: 3): equal up to that rounding
    assert [float(r["gruenanteil"]) for r in rows] == pytest.approx(
        expected, abs=5.1e-4
    )
    shares = [float(r["gruenanteil"]) for r in rows]
    assert len(set(shares)) == 3 and min(shares) < 0.5 < 0.9 < max(
        shares
    )  # the cities differ: no mix-up passes


def test_fa46_staedte_example_map_colours_the_share_not_the_share_per_km2(
    world, tmp_path
):
    _run(tmp_path / "out")
    html = (tmp_path / "out" / "gruenanteil_staedte.html").read_text(encoding="utf-8")
    assert "gruenanteil" in html and "pro km" not in html


def test_fa46_staedte_example_second_run_needs_no_network_and_gives_identical_results(
    world, tmp_path, monkeypatch
):
    _run(tmp_path / "first")

    def forbidden(*args, **kwargs):
        raise AssertionError("network access although snapshots exist")

    monkeypatch.setattr(stac.http, "post_json", forbidden)
    monkeypatch.setattr(stac.http, "get_json", forbidden)
    monkeypatch.setattr(osm.requests, "post", forbidden)
    second = _run(tmp_path / "second")
    notices = [w for w in second.warnings if w.category == "StacSceneNotice"]
    assert len(notices) == 1 and "aus Snapshot" in notices[0].message
    for name in ("gruenanteil_staedte.csv", "gruenanteil_staedte.geojson"):
        assert (tmp_path / "first" / name).read_bytes() == (
            tmp_path / "second" / name
        ).read_bytes(), name
