"""Implements: FA46-FA49 (Szenario 'Gruenflaechenanteil von Chemnitz aus Sentinel-2' Ende zu Ende), FA3, FA7.

Runs the REAL example `examples/chemnitz_gruenflaechenanteil_sentinel2.yaml` through
`api.run` with the real connectors `stac` and `osm` - only the network is replaced:
the STAC catalog by a recorded fake that points at tiny synthetic scenes in tmp_path,
Overpass by relation elements for nine districts, Nominatim by a fixture polygon.

The synthetic scene has a known pixel value at every place, so the numbers the
scenario produces (NDVI, cloud mask, green share per district and for the city)
are recomputed here independently with plain NumPy/rasterio and must agree.
"""

from __future__ import annotations

import csv
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.features import geometry_mask
from shapely.geometry import Polygon, mapping

from geofact import api
from geofact.builtin.sources import osm, stac
from osm_relations import relation, ring_ways
from stac_scenes import (
    CITY_BOX,
    FakeStac,
    aligned_ring,
    expected_bands,
    stac_item,
    write_scene,
)

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "chemnitz_gruenflaechenanteil_sentinel2.yaml"

# "Chemnitz" of this test: a rectangle on the grid of the synthetic scenes, nine districts of 8 km x 6 km
# inside it (see tests/stac_scenes.py: zones along the grid lines make the comparison exact).
CITY = Polygon(aligned_ring(CITY_BOX))
REGION_ANSWER = [{"geojson": mapping(CITY)}]
MIN_LON, MIN_LAT, MAX_LON, MAX_LAT = CITY.bounds

pytestmark = pytest.mark.e2e


def _region_fetcher(name: str) -> list[dict]:
    assert name == "Chemnitz, Deutschland"
    return REGION_ANSWER


def _districts() -> list[dict]:
    """Nine districts tiling the city rectangle: level 9 for the first three, level 10 for the rest."""
    left, bottom, right, top = CITY_BOX
    width, height = (right - left) / 3, (top - bottom) / 3
    elements = []
    for index in range(9):
        col, row = index % 3, index // 3
        box_utm = (
            left + col * width,
            bottom + row * height,
            left + (col + 1) * width,
            bottom + (row + 1) * height,
        )
        elements.append(
            relation(
                1000 + index,
                ring_ways(aligned_ring(box_utm), 3, 5000 + 10 * index),
                name=f"Stadtteil {index + 1}",
                admin_level="9" if index < 3 else "10",
                ref=str(index + 1),
                **{"name:prefix": "Stadtteil"},
            )
        )
    return elements


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


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Fake Overpass + fake STAC with a decoy scene; snapshots in tmp_path."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    # the fake below answers as Overpass: a local .env with GEOFACT_OSM_BACKEND=postgis (which
    # the web backend's settings load into the process) must not send the run to a real database
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "overpass")
    good = write_scene(tmp_path / "good", "good")
    decoy_cloudy = write_scene(tmp_path / "cloudy", "cloudy")
    decoy_other_tile = write_scene(tmp_path / "other", "other", epsg=32632)
    catalog = FakeStac(
        [
            stac_item("S2B_33UUS_20240813_0_L2A", "2024-08-13", 1.06, good),
            stac_item("S2B_33UUS_20240812_0_L2A", "2024-08-12", 14.0, decoy_cloudy),
            stac_item(
                "S2B_32UQB_20240813_0_L2A",
                "2024-08-13",
                0.9,
                decoy_other_tile,
                epsg=32632,
                grid_code="MGRS-32UQB",
            ),
        ]
    ).install(monkeypatch, stac)
    overpass = _Overpass(_districts())
    monkeypatch.setattr(osm.requests, "post", overpass.post)
    return SimpleWorld(catalog, overpass)


class SimpleWorld:
    def __init__(self, catalog, overpass):
        self.catalog, self.overpass = catalog, overpass


def _run(out_dir: Path, **options):
    document = api.load_scenario(EXAMPLE)
    return api.run(document, out_dir=out_dir, region_fetcher=_region_fetcher, **options)


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _independent_shares(
    tif_path: Path, zones: gpd.GeoDataFrame, mask_clouds: bool
) -> list[float]:
    """Green share per zone recomputed from the formula, the scene's known pixel values
    and a plain rasterisation of the zones - nothing of the scenario's own pipeline."""
    with rasterio.open(tif_path) as dataset:
        transform, shape_, crs = dataset.transform, dataset.shape, dataset.crs
    bands = expected_bands(transform, *shape_)
    red, nir = bands["red"].astype(float), bands["nir"].astype(float)
    # raster_calc stores a float result as float32 (documented), so the next step compares THAT
    # value with 0.3: a cell whose NDVI is exactly 0.3 (3/10, easy to hit with integer digital
    # numbers) becomes 0.30000001 in float32 and counts as green.
    ndvi = ((nir - red) / (nir + red)).astype(np.float32).astype(np.float64)
    green = ndvi > 0.3
    valid = np.ones(shape_, dtype=bool)
    if mask_clouds:
        valid &= ~np.isin(bands["scl"], [3, 8, 9, 10])
    shares = []
    for geometry in zones.to_crs(crs).geometry:
        inside = (
            ~geometry_mask([geometry], out_shape=shape_, transform=transform) & valid
        )
        shares.append(float(green[inside].mean()))
    return shares


def test_fa46_example_runs_end_to_end_and_writes_every_output(world, tmp_path):
    report = _run(tmp_path / "out")
    assert report.outputs.skipped == []
    assert sorted(p.name for p in report.outputs.written) == sorted(
        [
            "gruenanteil_stadtteile.html",
            "gruenanteil_stadtteile.geojson",
            "gruenanteil_stadtteile.csv",
            "gruenanteil_stadt.csv",
            "ndvi.tif",
            "ndvi.html",
        ]
    )
    assert all(p.stat().st_size > 0 for p in report.outputs.written)
    assert set(report.result.loaded_layers) == {"stadtteile", "sentinel2", "stadt"}


def test_fa46_example_selects_the_pinned_scene_and_reports_it(world, tmp_path):
    report = _run(tmp_path / "out")
    notices = [w for w in report.warnings if w.category == "StacSceneNotice"]
    assert len(notices) == 1 and notices[0].layer == "sentinel2"
    assert (
        "S2B_33UUS_20240813_0_L2A" in notices[0].message
        and "2024-08-13" in notices[0].message
    )
    # the search asked for the pinned period and the cloud limit of the example
    ((_, body),) = world.catalog.posts
    assert body["datetime"] == "2024-08-12T00:00:00Z/2024-08-14T23:59:59Z"
    assert body["query"] == {"eo:cloud_cover": {"lte": 20.0}}
    assert body["bbox"] == pytest.approx(list(CITY.bounds))


def test_fa46_example_ndvi_geotiff_is_the_ndvi_formula_on_the_scene(world, tmp_path):
    _run(tmp_path / "out")
    with rasterio.open(tmp_path / "out" / "ndvi.tif") as dataset:
        assert (
            dataset.count == 1
            and dataset.descriptions == ("ndvi",)
            and dataset.dtypes == ("float32",)
        )
        assert dataset.crs.to_epsg() == 32633 and np.isnan(dataset.nodata)
        ndvi = dataset.read(1)
        bands = expected_bands(dataset.transform, *dataset.shape)
    cloudy = np.isin(bands["scl"], [3, 8, 9, 10])
    red, nir = bands["red"].astype(float), bands["nir"].astype(float)
    expected = (nir - red) / (nir + red)
    assert np.isnan(ndvi[cloudy]).all() and not np.isnan(ndvi[~cloudy]).any()
    assert np.allclose(ndvi[~cloudy], expected[~cloudy], atol=1e-6)
    assert not np.isinf(ndvi).any()


def test_fa46_example_green_share_per_district_matches_an_independent_calculation(
    world, tmp_path
):
    _run(tmp_path / "out")
    rows = _read_csv(tmp_path / "out" / "gruenanteil_stadtteile.csv")
    assert len(rows) == 9
    assert sorted(r["name"] for r in rows) == sorted(
        f"Stadtteil {i}" for i in range(1, 10)
    )
    zones = gpd.read_file(tmp_path / "out" / "gruenanteil_stadtteile.geojson")
    expected = _independent_shares(
        tmp_path / "out" / "ndvi.tif", zones, mask_clouds=True
    )
    shares = [float(r["gruenanteil"]) for r in rows]
    by_name = dict(zip(zones["name"], expected))
    # the scenario rounds to 3 decimals (decimals: 3): equal up to that rounding
    assert {r["name"]: float(r["gruenanteil"]) for r in rows} == pytest.approx(
        by_name, abs=5.1e-4
    )
    assert (
        len(set(shares)) > 5 and min(shares) < 0.1 and max(shares) == 1.0
    )  # districts differ: no mix-up passes


def test_fa46_example_city_wide_share_matches_an_independent_calculation(
    world, tmp_path
):
    _run(tmp_path / "out")
    (row,) = _read_csv(tmp_path / "out" / "gruenanteil_stadt.csv")
    city = gpd.GeoDataFrame(geometry=[CITY], crs="EPSG:4326")
    (expected,) = _independent_shares(
        tmp_path / "out" / "ndvi.tif", city, mask_clouds=True
    )
    assert float(row["gruenanteil"]) == pytest.approx(expected, abs=5.1e-4)
    assert 0.3 < float(row["gruenanteil"]) < 0.95


def test_fa46_example_districts_are_relations_assembled_to_polygons(world, tmp_path):
    report = _run(tmp_path / "out")
    districts = report.result.store["stadtteile"]
    assert len(districts) == 9 and set(districts.geometry.geom_type) <= {
        "Polygon",
        "MultiPolygon",
    }
    assert districts.crs.to_epsg() == 32633
    assert districts.union_all().area == pytest.approx(
        24_000 * 18_000, rel=1e-6
    )  # they tile the city box
    # one Overpass statement per admin level, the colon tag quoted
    (query,) = world.overpass.requests
    assert '["admin_level"="9"]["name:prefix"="Stadtteil"]' in query
    assert '["admin_level"="10"]["name:prefix"="Stadtteil"]' in query


def test_fa46_example_map_of_the_districts_colours_the_share_not_the_share_per_km2(
    world, tmp_path
):
    _run(tmp_path / "out")
    html = (tmp_path / "out" / "gruenanteil_stadtteile.html").read_text(
        encoding="utf-8"
    )
    assert "gruenanteil" in html and "pro km" not in html


def test_fa46_example_ndvi_map_carries_the_overlay_with_alpha_and_legend(
    world, tmp_path
):
    import base64
    import re
    import struct
    import zlib

    _run(tmp_path / "out")
    html = (tmp_path / "out" / "ndvi.html").read_text(encoding="utf-8")
    match = re.search(r"data:image/png;base64,([A-Za-z0-9+/=]+)", html)
    assert match and "ndvi" in html
    png = base64.b64decode(match.group(1))
    width, height, _, color_type = struct.unpack(">IIBB", png[16:26])
    assert color_type == 6  # RGBA: nodata can be transparent
    chunks, position = [], 8
    while position < len(png):
        length, kind = struct.unpack(">I4s", png[position : position + 8])
        if kind == b"IDAT":
            chunks.append(png[position + 8 : position + 8 + length])
        position += 12 + length
    raw = zlib.decompress(b"".join(chunks))
    stride = 1 + width * 4
    first_row = raw[:stride]
    assert first_row[0] == 0  # filter type none
    alphas = {first_row[1 + 4 * i + 3] for i in range(width)}
    assert 0 in alphas  # the rotated corner of the grid is transparent


def test_fa46_second_run_needs_no_network_and_gives_identical_results(
    world, tmp_path, monkeypatch
):
    _run(tmp_path / "first")
    posts_before = len(world.catalog.posts)

    def forbidden(*args, **kwargs):
        raise AssertionError("network access although snapshots exist")

    monkeypatch.setattr(stac.http, "post_json", forbidden)
    monkeypatch.setattr(stac.http, "get_json", forbidden)
    monkeypatch.setattr(osm.requests, "post", forbidden)
    second = _run(tmp_path / "second")

    assert posts_before == 1
    notices = [w for w in second.warnings if w.category == "StacSceneNotice"]
    assert len(notices) == 1 and "aus Snapshot" in notices[0].message
    for name in (
        "gruenanteil_stadtteile.csv",
        "gruenanteil_stadt.csv",
        "gruenanteil_stadtteile.geojson",
        "ndvi.tif",
    ):
        assert (tmp_path / "first" / name).read_bytes() == (
            tmp_path / "second" / name
        ).read_bytes(), name


def test_fa46_refresh_snapshots_fetches_the_scene_again(world, tmp_path):
    _run(tmp_path / "first")
    _run(tmp_path / "again", refresh=True)
    assert len(world.catalog.posts) == 2 and len(world.overpass.requests) == 2
