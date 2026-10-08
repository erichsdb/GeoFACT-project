"""Implements: FA46 (Satellitenbilder aus einem STAC-Katalog), FA5 (Resampling/Ziel-CRS), FA4 (margin_km).

Contract tests of the source `stac` (builtin/sources/stac.py), all offline: the
catalog is a recorded fake (tests/stac_scenes.py, FakeStac), the COGs are tiny
GeoTIFFs written into tmp_path whose pixel value is a function of the map
coordinate - an off-by-one-cell alignment or a wrong offset therefore shows up as
a wrong value, not as a plausible-looking raster.
"""

from __future__ import annotations

import json
import re
import warnings

import geopandas as gpd
import numpy as np
import pytest
from pyproj import CRS
from rasterio.transform import Affine
from shapely.geometry import box

from geofact import api
from geofact.builtin.sources import stac
from geofact.builtin.sources.stac import (
    StacLayer,
    StacSceneNotice,
    load,
    select_scenes,
    snapshot_key,
)
from geofact.builtin.transforms.reproject import harmonize
from geofact.core.contracts import LoadContext
from geofact.core.types import DataType, RasterLayer
from stac_scenes import (
    COARSE,
    EAST_FOOTPRINT,
    FINE,
    REGION_BBOX,
    SCL_CLASSES,
    WEST_FOOTPRINT,
    FakeStac,
    add_overviews,
    expected_bands,
    search_response,
    stac_item,
    write_scene,
)

# every load announces its scene (that is tested explicitly); elsewhere the notice is just noise
pytestmark = pytest.mark.filterwarnings(
    "ignore::geofact.builtin.sources.stac.StacSceneNotice"
)

TARGET = CRS.from_epsg(32633)
ASSETS = {"red": "red", "nir": "nir", "scl": "scl"}


def _region(bbox=REGION_BBOX) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["r"]}, geometry=[box(*bbox)], crs="EPSG:4326")


def _ctx(refresh: bool = False, region=None, target=TARGET) -> LoadContext:
    return LoadContext(
        region=region if region is not None else _region(),
        target_crs=target,
        refresh=refresh,
    )


def _layer(**fields) -> StacLayer:
    config = {"id": "sentinel2", "assets": ASSETS, "datetime": "2024-08-01/2024-08-31"}
    config.update(fields)
    return StacLayer(**config)


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    return tmp_path / "snapshots"


@pytest.fixture
def scene(tmp_path):
    return write_scene(tmp_path / "s_a", "S2B_A")


def _single_scene_catalog(monkeypatch, scene, **item_fields):
    item = stac_item(
        "S2B_33UUS_20240813_0_L2A", "2024-08-13", 1.06, scene, **item_fields
    )
    return FakeStac([item]).install(monkeypatch, stac)


# =====================================================================
# Request: bbox, datetime, cloud filter, sort
# =====================================================================


def test_fa46_search_request_has_plain_bbox_datetime_and_query_filter(
    snapshots, scene, monkeypatch
):
    fake = _single_scene_catalog(monkeypatch, scene)
    load(_layer(max_cloud_cover=20), _ctx())

    ((url, body),) = fake.posts
    assert url == "https://earth-search.aws.element84.com/v1/search"
    assert body["collections"] == ["sentinel-2-l2a"]
    assert body["bbox"] == pytest.approx(list(REGION_BBOX))  # plain [W, S, E, N]
    assert body["datetime"] == "2024-08-01T00:00:00Z/2024-08-31T23:59:59Z"
    assert body["query"] == {"eo:cloud_cover": {"lte": 20.0}}
    assert body["sortby"] == [
        {"field": "properties.eo:cloud_cover", "direction": "asc"}
    ]
    # Earth Search silently ignores a CQL2 'filter' - it must not be used
    assert "filter" not in body


def test_fa46_no_cloud_filter_without_max_cloud_cover(snapshots, scene, monkeypatch):
    fake = _single_scene_catalog(monkeypatch, scene)
    load(_layer(), _ctx())
    assert "query" not in fake.posts[0][1]


# =====================================================================
# Scene selection
# =====================================================================


def test_fa46_least_cloudy_date_wins_and_target_epsg_is_preferred(
    snapshots, tmp_path, monkeypatch
):
    """Two dates, two tiles each: the least cloudy DATE is chosen; of that date's two
    scenes the one in the scenario's UTM zone (no reprojection needed)."""
    items = []
    for date, cloud_utm33, cloud_utm32 in (
        ("2024-08-01", 12.0, 11.0),
        ("2024-08-13", 1.06, 0.95),
    ):
        stamp = date.replace("-", "")
        paths33 = write_scene(tmp_path / f"a{stamp}", f"A{stamp}", epsg=32633)
        paths32 = write_scene(tmp_path / f"b{stamp}", f"B{stamp}", epsg=32632)
        items.append(
            stac_item(
                f"S2B_33UUS_{stamp}_0_L2A", date, cloud_utm33, paths33, epsg=32633
            )
        )
        items.append(
            stac_item(
                f"S2B_32UQB_{stamp}_0_L2A",
                date,
                cloud_utm32,
                paths32,
                epsg=32632,
                grid_code="MGRS-32UQB",
            )
        )
    FakeStac(items).install(monkeypatch, stac)

    raster = load(_layer(), _ctx())
    assert raster.crs.to_epsg() == 32633
    assert raster.path == "stac:sentinel-2-l2a/S2B_33UUS_20240813_0_L2A"


def test_fa46_cloudier_epsg_match_still_beats_a_cleaner_scene_in_another_zone(
    snapshots, tmp_path, monkeypatch
):
    """Preference order within a date: target EPSG first, then cloud cover."""
    paths33 = write_scene(tmp_path / "a", "A", epsg=32633)
    paths32 = write_scene(tmp_path / "b", "B", epsg=32632)
    FakeStac(
        [
            stac_item(
                "S2B_32UQB_20240813_0_L2A",
                "2024-08-13",
                0.5,
                paths32,
                epsg=32632,
                grid_code="MGRS-32UQB",
            ),
            stac_item(
                "S2B_33UUS_20240813_0_L2A", "2024-08-13", 3.0, paths33, epsg=32633
            ),
        ]
    ).install(monkeypatch, stac)
    assert load(_layer(), _ctx()).crs.to_epsg() == 32633


def test_fa46_grid_code_pins_the_tile(snapshots, tmp_path, monkeypatch):
    paths33 = write_scene(tmp_path / "a", "A", epsg=32633)
    paths32 = write_scene(tmp_path / "b", "B", epsg=32632)
    FakeStac(
        [
            stac_item(
                "S2B_32UQB_20240813_0_L2A",
                "2024-08-13",
                0.5,
                paths32,
                epsg=32632,
                grid_code="MGRS-32UQB",
            ),
            stac_item(
                "S2B_33UUS_20240813_0_L2A", "2024-08-13", 3.0, paths33, epsg=32633
            ),
        ]
    ).install(monkeypatch, stac)
    raster = load(_layer(grid_code="MGRS-32UQB"), _ctx())
    assert raster.path.endswith("S2B_32UQB_20240813_0_L2A")
    assert raster.crs.to_epsg() == 32632


def test_fa46_unknown_grid_code_names_the_tiles_found(snapshots, scene, monkeypatch):
    _single_scene_catalog(monkeypatch, scene)
    with pytest.raises(ValueError, match=r"MGRS-99XXX.*\['MGRS-33UUS'\]"):
        load(_layer(grid_code="MGRS-99XXX"), _ctx())


def test_fa46_item_id_fetches_that_scene_without_a_search(
    snapshots, scene, monkeypatch
):
    item = stac_item("S2B_33UUS_20240813_0_L2A", "2024-08-13", 1.06, scene)
    fake = FakeStac(item_by_id={"S2B_33UUS_20240813_0_L2A": item}).install(
        monkeypatch, stac
    )
    raster = load(
        StacLayer(id="s", assets=ASSETS, item_id="S2B_33UUS_20240813_0_L2A"), _ctx()
    )
    assert fake.posts == []
    assert fake.gets == [
        "https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/S2B_33UUS_20240813_0_L2A"
    ]
    assert raster.path.endswith("S2B_33UUS_20240813_0_L2A")


def test_fa46_empty_search_result_is_an_explicit_error(snapshots, monkeypatch):
    """Edge case 'empty layer': no scene -> error naming collection, period and cloud limit."""
    FakeStac([]).install(monkeypatch, stac)
    with pytest.raises(ValueError, match=r"2024-08-01/2024-08-31.*keine Szene.*<= 5"):
        load(_layer(max_cloud_cover=5), _ctx())


def test_fa46_scene_that_does_not_cover_the_region_is_an_explicit_error(
    snapshots, tmp_path, monkeypatch
):
    paths = write_scene(tmp_path / "west", "W", footprint=WEST_FOOTPRINT)
    FakeStac(
        [stac_item("W_only", "2024-08-13", 1.0, paths, footprint=WEST_FOOTPRINT)]
    ).install(monkeypatch, stac)
    with pytest.raises(ValueError, match="deckt"):
        load(_layer(), _ctx())


def test_fa46_mosaic_of_the_scenes_of_one_date_when_none_covers_the_region(
    snapshots, tmp_path, monkeypatch
):
    """The region straddles the seam of two scenes: both are merged and the result is
    identical to a scene read at once (the pixel value is a function of the place)."""
    west = write_scene(tmp_path / "west", "W", footprint=WEST_FOOTPRINT)
    east = write_scene(tmp_path / "east", "E", footprint=EAST_FOOTPRINT)
    FakeStac(
        [
            stac_item("S2_WEST", "2024-08-13", 2.0, west, footprint=WEST_FOOTPRINT),
            stac_item("S2_EAST", "2024-08-13", 4.0, east, footprint=EAST_FOOTPRINT),
        ]
    ).install(monkeypatch, stac)

    raster = load(_layer(), _ctx())
    rows, cols = raster.shape
    expected = expected_bands(raster.transform, rows, cols)
    for index, name in enumerate(("red", "nir", "scl")):
        assert np.array_equal(raster.data[index], expected[name]), name
    assert raster.path == "stac:sentinel-2-l2a/S2_EAST+S2_WEST"
    sidecar = json.loads(next(snapshots.glob("*.json")).read_text(encoding="utf-8"))
    assert [i["id"] for i in sidecar["items"]] == ["S2_EAST", "S2_WEST"]


# =====================================================================
# Reading: window, grid alignment, band order, dtype, nodata
# =====================================================================


def test_fa46_reads_the_region_window_with_the_coarse_band_aligned_to_the_fine_grid(
    snapshots, scene, monkeypatch
):
    """Postcondition: bands in the order of `assets`, named like their keys; every cell
    holds the value belonging to ITS place, also the 200 m scl band on the 100 m grid."""
    _single_scene_catalog(monkeypatch, scene)
    raster = load(_layer(), _ctx())

    assert raster.data.ndim == 3 and raster.band_names == ["red", "nir", "scl"]
    rows, cols = raster.shape
    assert (
        0 < rows < 200 and 0 < cols < 200
    )  # a window of the scene, not the whole 450 x 500 tile
    expected = expected_bands(raster.transform, rows, cols)
    for index, name in enumerate(("red", "nir", "scl")):
        assert np.array_equal(raster.data[index], expected[name]), (
            f"{name} is misaligned"
        )
    # the window covers the region
    left, top = raster.transform.c, raster.transform.f
    right, bottom = left + cols * raster.transform.a, top + rows * raster.transform.e
    from rasterio.warp import transform_bounds

    region = transform_bounds("EPSG:4326", "EPSG:32633", *REGION_BBOX, densify_pts=21)
    assert (
        left <= region[0]
        and bottom <= region[1]
        and right >= region[2]
        and top >= region[3]
    )


def test_fa46_default_keeps_raw_digital_numbers_with_nodata_zero(
    snapshots, scene, monkeypatch
):
    _single_scene_catalog(monkeypatch, scene)
    raster = load(_layer(), _ctx())
    assert raster.data.dtype == np.uint16
    assert raster.nodata == 0
    assert raster.crs.to_epsg() == 32633


def test_fa46_band_selection_follows_the_assets_mapping(snapshots, scene, monkeypatch):
    """Only the requested assets are read, in the order given."""
    _single_scene_catalog(monkeypatch, scene)
    raster = load(_layer(assets={"nir": "nir", "red": "red"}), _ctx())
    assert raster.band_names == ["nir", "red"]
    assert raster.data.shape[0] == 2
    expected = expected_bands(raster.transform, *raster.shape)
    assert np.array_equal(raster.data[0], expected["nir"])


def test_fa46_missing_asset_error_lists_the_assets_of_the_scene(
    snapshots, scene, monkeypatch
):
    _single_scene_catalog(monkeypatch, scene)
    with pytest.raises(
        ValueError, match=r"kein Asset 'swir16'.*\['nir', 'red', 'scl'\]"
    ):
        load(_layer(assets={"swir": "swir16"}), _ctx())


def test_fa46_margin_km_widens_the_window(snapshots, scene, monkeypatch):
    _single_scene_catalog(monkeypatch, scene)
    plain = load(_layer(), _ctx())
    widened = load(_layer(margin_km=2), _ctx())
    assert widened.shape[0] > plain.shape[0] and widened.shape[1] > plain.shape[1]


def test_fa46_window_beyond_the_scene_is_an_error_not_a_silent_cut(
    snapshots, tmp_path, monkeypatch
):
    """A margin that reaches over the edge of the only scene must not shorten zones silently."""
    small = (12.86, 50.80, 12.99, 50.88)
    paths = write_scene(tmp_path / "small", "S", footprint=small)
    FakeStac([stac_item("S_small", "2024-08-13", 1.0, paths, footprint=small)]).install(
        monkeypatch, stac
    )
    load(_layer(), _ctx())  # without margin the region is covered
    with pytest.raises(ValueError, match="deckt die Region|nur teilweise|deckt"):
        load(_layer(margin_km=20), _ctx())


def test_fa46_nodata_mismatch_between_bands_is_an_error(
    snapshots, tmp_path, monkeypatch
):
    import rasterio

    paths = write_scene(tmp_path / "s", "S")
    with rasterio.open(paths["scl"], "r+") as dataset:
        dataset.nodata = 255
    FakeStac([stac_item("S", "2024-08-13", 1.0, paths)]).install(monkeypatch, stac)
    with pytest.raises(ValueError, match="verschiedenes Nodata"):
        load(_layer(), _ctx())


def test_fa46_too_large_window_is_refused_before_reading(snapshots, scene, monkeypatch):
    _single_scene_catalog(monkeypatch, scene)
    monkeypatch.setattr(stac, "MAX_CELLS", 100)
    with pytest.raises(ValueError, match="Zellen.*erlaubt"):
        load(_layer(), _ctx())


# =====================================================================
# Clouds and scale
# =====================================================================


def test_fa46_mask_clouds_turns_scl_3_8_9_10_into_nodata_in_every_band(
    snapshots, scene, monkeypatch
):
    _single_scene_catalog(monkeypatch, scene)
    raster = load(_layer(mask_clouds=True), _ctx())
    plain_expected = expected_bands(raster.transform, *raster.shape)
    cloudy = np.isin(plain_expected["scl"], [3, 8, 9, 10])
    assert cloudy.any() and not cloudy.all()  # the synthetic scene has both
    for index, name in enumerate(("red", "nir", "scl")):
        assert np.all(raster.data[index][cloudy] == 0), (
            f"{name}: cloud cells must be nodata"
        )
        assert np.array_equal(
            raster.data[index][~cloudy], plain_expected[name][~cloudy]
        ), name


def test_fa46_without_mask_clouds_the_cloud_cells_keep_their_values(
    snapshots, scene, monkeypatch
):
    _single_scene_catalog(monkeypatch, scene)
    raster = load(_layer(), _ctx())
    expected = expected_bands(raster.transform, *raster.shape)
    assert np.array_equal(raster.data[0], expected["red"])


@pytest.mark.parametrize(
    "offset_applied, factor, shift",
    [
        (
            True,
            0.0001,
            0.0,
        ),  # earthsearch:boa_offset_applied -> the offset is NOT subtracted again
        (False, 0.0001, -0.1),  # offset not yet removed -> DN * scale + offset
    ],
)
def test_fa46_apply_scale_uses_the_offset_only_when_it_was_not_applied_already(
    snapshots, tmp_path, monkeypatch, offset_applied, factor, shift
):
    paths = write_scene(tmp_path / "s", "S")
    FakeStac(
        [stac_item("S", "2024-08-13", 1.0, paths, boa_offset_applied=offset_applied)]
    ).install(monkeypatch, stac)
    raster = load(_layer(apply_scale=True), _ctx())
    expected = expected_bands(raster.transform, *raster.shape)
    assert raster.data.dtype == np.float32 and np.isnan(raster.nodata)
    assert np.allclose(raster.data[0], expected["red"] * factor + shift, atol=1e-5)
    assert np.allclose(raster.data[1], expected["nir"] * factor + shift, atol=1e-5)
    # a band without scale (the class band) stays what it was
    assert np.array_equal(raster.data[2], expected["scl"].astype(np.float32))
    assert np.nanmin(raster.data[0]) > -0.1 if offset_applied else True


def test_fa46_apply_scale_turns_nodata_into_nan(snapshots, tmp_path, monkeypatch):
    hole = (
        350_000.0,
        5_630_000.0,
        354_000.0,
        5_640_000.0,
    )  # inside the region, map coordinates
    paths = write_scene(tmp_path / "s", "S", hole=hole)
    FakeStac([stac_item("S", "2024-08-13", 1.0, paths)]).install(monkeypatch, stac)
    raster = load(_layer(apply_scale=True), _ctx())
    assert np.isnan(raster.data[0]).any() and np.isnan(raster.data[1]).any()
    assert not np.isinf(raster.data).any()


# =====================================================================
# Snapshot, provenance
# =====================================================================


def test_fa46_second_run_reads_the_snapshot_without_network_and_is_identical(
    snapshots, scene, monkeypatch
):
    fake = _single_scene_catalog(monkeypatch, scene)
    first = load(_layer(mask_clouds=True), _ctx())
    assert len(fake.posts) == 1
    tifs = list(snapshots.glob("*.tif"))
    sidecars = list(snapshots.glob("*.json"))
    assert len(tifs) == 1 and len(sidecars) == 1

    def forbidden(*args, **kwargs):
        raise AssertionError("network access although a snapshot exists")

    monkeypatch.setattr(stac.http, "post_json", forbidden)
    monkeypatch.setattr(stac.http, "get_json", forbidden)
    second = load(_layer(mask_clouds=True), _ctx())

    assert np.array_equal(first.data, second.data)
    assert first.transform == second.transform and first.band_names == second.band_names
    assert first.nodata == second.nodata and first.crs == second.crs
    assert second.path == first.path


def test_fa46_refresh_snapshots_fetches_again(snapshots, scene, monkeypatch):
    fake = _single_scene_catalog(monkeypatch, scene)
    load(_layer(), _ctx())
    load(_layer(), _ctx())
    assert len(fake.posts) == 1
    load(_layer(), _ctx(refresh=True))
    assert len(fake.posts) == 2


def test_fa46_snapshot_sidecar_records_the_chosen_scene(snapshots, scene, monkeypatch):
    _single_scene_catalog(monkeypatch, scene)
    load(_layer(mask_clouds=True, max_cloud_cover=20), _ctx())
    sidecar = json.loads(next(snapshots.glob("*.json")).read_text(encoding="utf-8"))
    assert sidecar["date"] == "2024-08-13"
    assert sidecar["items"] == [
        {
            "id": "S2B_33UUS_20240813_0_L2A",
            "datetime": "2024-08-13T10:26:44.168000Z",
            "cloud_cover": 1.06,
            "grid_code": "MGRS-33UUS",
            "epsg": 32633,
        }
    ]
    assert sidecar["assets"] == ASSETS and sidecar["mask_clouds"] is True
    assert sidecar["cloud_classes_masked"] == [3, 8, 9, 10]


def test_fa46_scene_is_announced_fresh_and_from_snapshot(snapshots, scene, monkeypatch):
    """Provenance is never lost: item id, date and cloud cover are reported on every load."""
    _single_scene_catalog(monkeypatch, scene)
    for expected_origin in ("frisch bezogen", "aus Snapshot"):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            load(_layer(), _ctx())
        notices = [w for w in caught if issubclass(w.category, StacSceneNotice)]
        assert len(notices) == 1
        text = str(notices[0].message)
        assert (
            "S2B_33UUS_20240813_0_L2A" in text
            and "2024-08-13" in text
            and "1.06" in text
        )
        assert expected_origin in text


@pytest.mark.parametrize(
    "change",
    [
        {"datetime": "2024-08-02/2024-08-31"},
        {"max_cloud_cover": 30},
        {"mask_clouds": True},
        {"apply_scale": True},
        {"assets": {"red": "red", "nir": "nir"}},
        {"grid_code": "MGRS-33UUS"},
        {"collection": "sentinel-2-l1c"},
        {"url": "https://example.org/stac"},
        {"margin_km": 1},
        {"resolution_m": 200},
    ],
)
def test_fa46_snapshot_key_depends_on_every_field_that_shapes_the_result(change):
    base = snapshot_key(_layer(), _region(), 32633)
    assert snapshot_key(_layer(**change), _region(), 32633) != base


def test_fa46_snapshot_key_depends_on_region_and_utm_zone_and_is_stable():
    base = snapshot_key(_layer(), _region(), 32633)
    assert snapshot_key(_layer(), _region(), 32633) == base
    assert snapshot_key(_layer(), _region((12.90, 50.82, 12.96, 50.86)), 32633) != base
    assert snapshot_key(_layer(), _region(), 32632) != base
    assert snapshot_key(_layer(item_id="x", datetime=None), _region(), 32633) != base


# =====================================================================
# Paging of the search
# =====================================================================


def test_fa46_follows_the_next_link_of_a_paged_search(snapshots, tmp_path, monkeypatch):
    paths = write_scene(tmp_path / "a", "A")
    cloudy = stac_item("S_cloudy", "2024-08-01", 15.0, paths)
    clean = stac_item("S_clean", "2024-08-13", 1.0, paths)
    page1 = search_response(
        [cloudy],
        links=[
            {
                "rel": "next",
                "method": "POST",
                "href": "https://earth-search.aws.element84.com/v1/search",
                "body": {"next": "token-2"},
                "merge": True,
            }
        ],
    )
    page2 = search_response([clean])
    fake = FakeStac(pages=[page1, page2]).install(monkeypatch, stac)
    raster = load(_layer(), _ctx())
    assert len(fake.posts) == 2
    assert fake.posts[1][1]["next"] == "token-2" and fake.posts[1][1][
        "collections"
    ] == ["sentinel-2-l2a"]
    assert raster.path.endswith("S_clean")  # found on page 2


def test_fa46_truncated_result_list_is_an_error(snapshots, scene, monkeypatch):
    item = stac_item("S", "2024-08-13", 1.0, scene)
    FakeStac(pages=[search_response([item], numberMatched=250)]).install(
        monkeypatch, stac
    )
    with pytest.raises(ValueError, match="250 Treffer.*unvollständig"):
        load(_layer(), _ctx())


def test_fa46_endless_paging_is_stopped(snapshots, scene, monkeypatch):
    item = stac_item("S", "2024-08-13", 1.0, scene)
    again = search_response(
        [item], links=[{"rel": "next", "method": "POST", "href": "x", "body": {}}]
    )
    FakeStac(pages=[again]).install(monkeypatch, stac)
    with pytest.raises(ValueError, match="Seiten"):
        load(_layer(), _ctx())


# =====================================================================
# FA5: target CRS - a scene in the working CRS is not resampled
# =====================================================================


def test_fa05_raster_in_the_target_crs_is_not_resampled(snapshots, scene, monkeypatch):
    _single_scene_catalog(monkeypatch, scene)
    raster = load(_layer(), _ctx())
    harmonized = harmonize("sentinel2", raster, TARGET)
    assert np.array_equal(harmonized.data, raster.data)
    assert (
        harmonized.transform == raster.transform
        and harmonized.band_names == raster.band_names
    )


def test_fa05_scene_in_another_utm_zone_is_reprojected_band_wise(
    snapshots, tmp_path, monkeypatch
):
    paths32 = write_scene(tmp_path / "b", "B", epsg=32632)
    FakeStac(
        [
            stac_item(
                "S2B_32UQB",
                "2024-08-13",
                0.5,
                paths32,
                epsg=32632,
                grid_code="MGRS-32UQB",
            )
        ]
    ).install(monkeypatch, stac)
    raster = load(_layer(), _ctx())
    assert raster.crs.to_epsg() == 32632
    harmonized = harmonize("sentinel2", raster, TARGET)
    assert harmonized.crs.to_epsg() == 32633
    assert harmonized.data.shape[0] == 3 and harmonized.band_names == [
        "red",
        "nir",
        "scl",
    ]
    assert harmonized.data.dtype == raster.data.dtype


# =====================================================================
# Config contract (FA2): errors with position, before any network access
# =====================================================================


def _config(layer: dict) -> dict:
    return {
        "scenario": {"name": "t", "region": "12.9,50.8,12.95,50.86"},
        "layers": [layer, {"id": "stadt", "source": "region"}],
        "steps": [
            {
                "id": "z",
                "op": "zonal_stats",
                "inputs": {"zones": "stadt", "values": layer["id"]},
                "params": {"stat": "mean"},
            }
        ],
        "output": [{"type": "csv", "source": "z"}],
    }


def _issues(layer: dict) -> list[tuple[str, str]]:
    return api.validate(_config(layer)).issues


def test_fa46_valid_layer_validates():
    layer = {
        "id": "s",
        "source": "stac",
        "data_type": "raster",
        "assets": ASSETS,
        "datetime": "2024-08-12/2024-08-14",
        "grid_code": "MGRS-33UUS",
        "max_cloud_cover": 20,
        "mask_clouds": True,
    }
    assert _issues(layer) == []


@pytest.mark.parametrize(
    "fields, fragment",
    [
        ({"mask_clouds": True, "assets": {"red": "red"}}, "'scl'"),
        ({"datetime": None}, "datetime"),
        ({"datetime": "13.08.2024"}, "YYYY-MM-DD"),
        ({"datetime": "2024-08-31/2024-08-01"}, "vor dem Anfang"),
        (
            {"item_id": "S2B_X", "datetime": "2024-08-01/2024-08-31"},
            "item_id legt die Szene fest",
        ),
        ({"assets": {"red-band": "red"}}, "red-band"),
        ({"assets": {}}, "assets"),
        ({"max_cloud_cover": 120}, "max_cloud_cover"),
        ({"max_cloud_cover": "viel"}, "max_cloud_cover"),
        ({"assets": ["red", "nir"]}, "assets"),
        ({"url": "ftp://example.org"}, "http"),
        ({"resampling": "cubic"}, "resampling"),
        ({"margin_km": -1}, "margin_km"),
        ({"resolution_m": 0}, "resolution_m"),
        ({"resolution_m": -20}, "resolution_m"),
        ({"resolution_m": "grob"}, "resolution_m"),
        ({"resolution_m": float("inf")}, "resolution_m"),
        ({"typo_field": 1}, "typo_field"),
    ],
)
def test_fa46_invalid_layer_config_is_rejected_with_position(fields, fragment):
    layer = {
        "id": "s",
        "source": "stac",
        "data_type": "raster",
        "assets": ASSETS,
        "datetime": "2024-08-01/2024-08-31",
    }
    for key, value in fields.items():
        if value is None:
            layer.pop(key)
        else:
            layer[key] = value
    issues = _issues(layer)
    assert issues, f"{fields} was accepted"
    assert all(location.startswith("layers -> 0") for location, _ in issues)
    assert any(
        fragment in location or fragment in message for location, message in issues
    ), issues


def test_fa46_layer_cannot_declare_a_non_raster_type():
    issues = _issues(
        {
            "id": "s",
            "source": "stac",
            "data_type": "vector",
            "assets": ASSETS,
            "datetime": "2024-08-01/2024-08-31",
        }
    )
    assert issues and issues[0][0].startswith("layers -> 0")


def test_fa46_stac_layer_is_a_raster_layer_and_validation_is_rejected():
    layer = _layer()
    assert layer.data_type == DataType.RASTER
    issues = _issues(
        {
            "id": "s",
            "source": "stac",
            "data_type": "raster",
            "assets": ASSETS,
            "datetime": "2024-08-01/2024-08-31",
            "validation": {"required_fields": ["a"]},
        }
    )
    assert issues and "Raster" in issues[0][1] or "Vektor" in issues[0][1]


def test_fa46_empty_region_is_an_explicit_error(snapshots, scene, monkeypatch):
    """Edge case 'empty layer': a region without geometry has no bbox to search."""
    _single_scene_catalog(monkeypatch, scene)
    empty = gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:4326")
    with pytest.raises(ValueError, match="keine endliche BBox"):
        load(_layer(), _ctx(region=empty))


def test_fa46_source_needs_the_region():
    with pytest.raises(ValueError, match="Region"):
        load(_layer(), LoadContext(region=None))


def test_fa46_is_registered_as_a_source_with_a_raster_result():
    entry = api.extension_catalog()["sources"]
    assert "stac" in {e["name"] for e in entry}
    assert isinstance(RasterLayer, type)


# =====================================================================
# Mosaic in the target zone (FA46): tiles of the zone of the region win over the neighbour zone
# =====================================================================


def test_fa46_mosaic_in_the_target_zone_ignores_the_scenes_of_the_neighbour_zone(
    snapshots, tmp_path, monkeypatch
):
    """Three scenes on one date: two tiles of the scenario's UTM zone that cover the region
    TOGETHER, plus a (clean) tile of the neighbour zone that only reaches into the west. The
    zone-33 tiles are mosaicked as they are - no CRS error, no reprojection of the other zone."""
    west = write_scene(tmp_path / "west", "W", footprint=WEST_FOOTPRINT)
    east = write_scene(tmp_path / "east", "E", footprint=EAST_FOOTPRINT)
    other = write_scene(tmp_path / "zone32", "Z", epsg=32632, footprint=WEST_FOOTPRINT)
    FakeStac(
        [
            stac_item("S2_WEST", "2024-08-13", 2.0, west, footprint=WEST_FOOTPRINT),
            stac_item("S2_EAST", "2024-08-13", 4.0, east, footprint=EAST_FOOTPRINT),
            stac_item(
                "S2_ZONE32",
                "2024-08-13",
                0.5,
                other,
                epsg=32632,
                grid_code="MGRS-32UQB",
                footprint=WEST_FOOTPRINT,
            ),
        ]
    ).install(monkeypatch, stac)

    raster = load(_layer(), _ctx())
    assert raster.crs.to_epsg() == 32633
    assert raster.path == "stac:sentinel-2-l2a/S2_EAST+S2_WEST"
    expected = expected_bands(raster.transform, *raster.shape)
    for index, name in enumerate(("red", "nir", "scl")):
        assert np.array_equal(raster.data[index], expected[name]), name
    sidecar = json.loads(next(snapshots.glob("*.json")).read_text(encoding="utf-8"))
    assert [i["id"] for i in sidecar["items"]] == ["S2_EAST", "S2_WEST"]


def test_fa46_mixed_zones_that_only_cover_the_region_together_stay_an_explicit_error(
    snapshots, tmp_path, monkeypatch
):
    """The zone-33 scenes alone do NOT cover the region, all scenes of the date together do:
    the old explicit error about the different CRS stays (no silent mixing of zones)."""
    west = write_scene(tmp_path / "west", "W", footprint=WEST_FOOTPRINT)
    east32 = write_scene(tmp_path / "east32", "E", epsg=32632, footprint=EAST_FOOTPRINT)
    FakeStac(
        [
            stac_item("S2_WEST", "2024-08-13", 2.0, west, footprint=WEST_FOOTPRINT),
            stac_item(
                "S2_EAST32",
                "2024-08-13",
                4.0,
                east32,
                epsg=32632,
                grid_code="MGRS-32UQB",
                footprint=EAST_FOOTPRINT,
            ),
        ]
    ).install(monkeypatch, stac)
    with pytest.raises(ValueError, match=r"verschiedenen CRS.*grid_code"):
        load(_layer(), _ctx())


def _selection_items() -> list[dict]:
    """2024-08-01: two zone-33 tiles that cover the region together (3 % / 4 %) and a zone-32
    tile with 50 % clouds that only reaches into the west; 2024-08-13: one scene with 5 %."""
    return [
        stac_item("S2_WEST", "2024-08-01", 3.0, {}, footprint=WEST_FOOTPRINT),
        stac_item("S2_EAST", "2024-08-01", 4.0, {}, footprint=EAST_FOOTPRINT),
        stac_item(
            "S2_ZONE32",
            "2024-08-01",
            50.0,
            {},
            epsg=32632,
            grid_code="MGRS-32UQB",
            footprint=WEST_FOOTPRINT,
        ),
        stac_item("S2_SINGLE", "2024-08-13", 5.0, {}),
    ]


def test_fa46_cloud_cover_of_a_zone_mosaic_counts_only_the_scenes_in_the_zone():
    date, chosen = select_scenes(_layer(), _selection_items(), REGION_BBOX, 32633)
    assert date == "2024-08-01" and [i["id"] for i in chosen] == ["S2_EAST", "S2_WEST"]


def test_fa46_without_a_target_zone_the_mosaic_still_takes_every_scene_of_the_date():
    """Nothing tells the zones apart without a target CRS: as before, all scenes of the date
    form the mosaic (and count with their 50 %), so the single clean scene of the next date wins."""
    date, chosen = select_scenes(_layer(), _selection_items(), REGION_BBOX, None)
    assert date == "2024-08-13" and [i["id"] for i in chosen] == ["S2_SINGLE"]


def test_fa46_single_scene_choice_still_prefers_the_target_zone():
    """A scene that covers the region alone is chosen as before: target zone first, then clouds."""
    items = [
        stac_item(
            "S2_ZONE32", "2024-08-13", 0.5, {}, epsg=32632, grid_code="MGRS-32UQB"
        ),
        stac_item("S2_ZONE33", "2024-08-13", 3.0, {}),
    ]
    _, chosen = select_scenes(_layer(), items, REGION_BBOX, 32633)
    assert [i["id"] for i in chosen] == ["S2_ZONE33"]


# =====================================================================
# resolution_m: target cell size, per-band resampling, alignment
# =====================================================================


def _scene_origin(paths: dict[str, str]) -> tuple[float, float]:
    import rasterio

    with rasterio.open(paths["red"]) as dataset:
        return dataset.transform.c, dataset.transform.f


def _fine_cells(band: str, transform, rows: int, cols: int, step: int) -> np.ndarray:
    """The 100 m cells (step x step of them per target cell) of ``band`` under a window with
    this transform: shape (rows * step, cols * step)."""
    fine = Affine(FINE, 0, transform.c, 0, -FINE, transform.f)
    return expected_bands(fine, rows * step, cols * step)[band]


def _block_mean(band: str, transform, rows: int, cols: int, step: int) -> np.ndarray:
    values = _fine_cells(band, transform, rows, cols, step).astype(np.float64)
    return values.reshape(rows, step, cols, step).mean(axis=(1, 3))


def _expected_scl(transform, rows: int, cols: int, resolution: int) -> np.ndarray:
    """The class band the source must give: of the 200 m cells inside every target cell the one
    at the centre (``step // 2``) - never a mixture of class codes."""
    step = resolution // int(COARSE)
    native = Affine(COARSE, 0, transform.c, 0, -COARSE, transform.f)
    scl = expected_bands(native, rows * step, cols * step)["scl"]
    return scl[step // 2 :: step, step // 2 :: step]


@pytest.mark.parametrize(
    "resolution, overviews", [(200, False), (200, True), (400, False), (400, True)]
)
def test_fa46_resolution_m_averages_reflectance_bands_and_samples_class_bands(
    snapshots, tmp_path, monkeypatch, resolution, overviews
):
    """Postcondition: the result has the cell size resolution_m; a reflectance band holds the mean
    of its source cells (up to the rounding of a COG overview), the class band only codes that
    exist in the source and the one of the cell at the centre of the target cell."""
    paths = write_scene(tmp_path / "s", "S")
    if overviews:
        add_overviews(paths)
    FakeStac(
        [stac_item("S2B_33UUS_20240813_0_L2A", "2024-08-13", 1.06, paths)]
    ).install(monkeypatch, stac)

    raster = load(_layer(resolution_m=resolution), _ctx())
    rows, cols = raster.shape
    step = resolution // int(FINE)
    assert raster.transform.a == resolution and raster.transform.e == -resolution
    assert raster.band_names == ["red", "nir", "scl"] and raster.data.dtype == np.uint16
    assert rows > 3 and cols > 3
    for index, name in ((0, "red"), (1, "nir")):
        mean = _block_mean(name, raster.transform, rows, cols, step)
        deviation = np.abs(raster.data[index].astype(np.float64) - mean)
        assert deviation.max() <= 1.0, (
            f"{name}: not the mean of its source cells ({deviation.max()})"
        )
    assert np.array_equal(
        raster.data[2], _expected_scl(raster.transform, rows, cols, resolution)
    )
    assert set(np.unique(raster.data[2]).tolist()) <= set(SCL_CLASSES.tolist())


@pytest.mark.parametrize("resolution", [200, 400])
def test_fa46_resolution_m_window_lies_on_the_target_grid_and_covers_the_region(
    snapshots, tmp_path, monkeypatch, resolution
):
    """The window starts on a multiple of the target cell from the origin of the scene, so no
    cell of the result straddles two source cells; it still covers the region."""
    from rasterio.warp import transform_bounds

    paths = write_scene(tmp_path / "s", "S")
    FakeStac([stac_item("S", "2024-08-13", 1.0, paths)]).install(monkeypatch, stac)
    raster = load(_layer(resolution_m=resolution), _ctx())
    origin_x, origin_y = _scene_origin(paths)
    assert (raster.transform.c - origin_x) % resolution == 0
    assert (origin_y - raster.transform.f) % resolution == 0
    rows, cols = raster.shape
    region = transform_bounds("EPSG:4326", "EPSG:32633", *REGION_BBOX, densify_pts=21)
    assert raster.transform.c <= region[0] and raster.transform.f >= region[3]
    assert raster.transform.c + cols * resolution >= region[2]
    assert raster.transform.f - rows * resolution <= region[1]


def test_fa46_resolution_m_equal_to_the_native_cell_size_changes_nothing(
    snapshots, scene, monkeypatch
):
    _single_scene_catalog(monkeypatch, scene)
    native = load(_layer(), _ctx())
    explicit = load(_layer(resolution_m=100), _ctx())
    assert (
        np.array_equal(native.data, explicit.data)
        and native.transform == explicit.transform
    )


def test_fa46_resolution_m_finer_than_a_coarse_band_repeats_its_cells(
    snapshots, scene, monkeypatch
):
    """The 200 m class band on the 100 m grid (resolution_m = the finest cell) is the exact
    repetition: every 2 x 2 block of fine cells holds the value of its source cell."""
    _single_scene_catalog(monkeypatch, scene)
    raster = load(_layer(resolution_m=100), _ctx())
    expected = expected_bands(raster.transform, *raster.shape)
    assert np.array_equal(raster.data[2], expected["scl"])
    assert np.array_equal(raster.data[2][::2, ::2], raster.data[2][1::2, 1::2])


def test_fa46_mask_clouds_works_on_the_resampled_class_band(
    snapshots, tmp_path, monkeypatch
):
    """The cloud mask is taken from the SCL on the grid of the result: the cells whose (sampled)
    class is 3, 8, 9 or 10 are nodata in every band, all others keep their values."""
    paths = write_scene(tmp_path / "s", "S")
    FakeStac([stac_item("S", "2024-08-13", 1.0, paths)]).install(monkeypatch, stac)
    raster = load(_layer(resolution_m=400, mask_clouds=True), _ctx())
    rows, cols = raster.shape
    scl = _expected_scl(raster.transform, rows, cols, 400)
    cloudy = np.isin(scl, [3, 8, 9, 10])
    assert cloudy.any() and not cloudy.all()
    for index in range(3):
        assert np.all(raster.data[index][cloudy] == 0), (
            f"band {index}: cloud cells must be nodata"
        )
    assert np.array_equal(raster.data[2][~cloudy], scl[~cloudy])
    mean = _block_mean("red", raster.transform, rows, cols, 4)
    assert np.abs(raster.data[0][~cloudy].astype(float) - mean[~cloudy]).max() <= 1.0


def test_fa46_a_band_without_scale_is_a_class_band_and_is_sampled_not_averaged(
    snapshots, tmp_path, monkeypatch
):
    """The rule: a class band is `scl` or a band whose asset declares no `scale` - here the
    reflectance bands of an item without `raster:bands` scale are sampled like class codes."""
    paths = write_scene(tmp_path / "s", "S")
    FakeStac([stac_item("S", "2024-08-13", 1.0, paths, scale=None)]).install(
        monkeypatch, stac
    )
    raster = load(_layer(resolution_m=200), _ctx())
    rows, cols = raster.shape
    sampled = _fine_cells("red", raster.transform, rows, cols, 2)[1::2, 1::2]
    assert np.array_equal(raster.data[0], sampled)
    mean = _block_mean("red", raster.transform, rows, cols, 2)
    assert not np.array_equal(
        raster.data[0].astype(float), np.round(mean)
    )  # the two rules differ here


@pytest.mark.parametrize(
    "resolution, fragment",
    [
        (50, "feiner als die Zellgröße des feinsten Bandes 'red' (100 m)"),
        (
            150,
            "kein ganzzahliges Vielfaches der Zellgröße des feinsten Bandes 'red' (100 m)",
        ),
        (
            300,
            "Band 'scl' (200 m) und resolution_m 300 m stehen nicht im ganzzahligen Verhältnis",
        ),
    ],
)
def test_fa46_resolution_m_that_does_not_fit_the_bands_is_an_error_before_reading(
    snapshots, scene, monkeypatch, resolution, fragment
):
    _single_scene_catalog(monkeypatch, scene)

    def forbidden(*args, **kwargs):
        raise AssertionError("a band was read although resolution_m does not fit")

    monkeypatch.setattr(stac, "_read_band", forbidden)
    with pytest.raises(ValueError, match=re.escape(fragment)):
        load(_layer(resolution_m=resolution), _ctx())


def test_fa46_resolution_m_error_names_the_layer(snapshots, scene, monkeypatch):
    _single_scene_catalog(monkeypatch, scene)
    with pytest.raises(ValueError, match="Layer 'sentinel2'.*resolution_m 150"):
        load(_layer(resolution_m=150), _ctx())


def test_fa46_resolution_m_mosaic_across_the_seam_is_the_mean_everywhere(
    snapshots, tmp_path, monkeypatch
):
    west = write_scene(tmp_path / "west", "W", footprint=WEST_FOOTPRINT)
    east = write_scene(tmp_path / "east", "E", footprint=EAST_FOOTPRINT)
    FakeStac(
        [
            stac_item("S2_WEST", "2024-08-13", 2.0, west, footprint=WEST_FOOTPRINT),
            stac_item("S2_EAST", "2024-08-13", 4.0, east, footprint=EAST_FOOTPRINT),
        ]
    ).install(monkeypatch, stac)
    raster = load(_layer(resolution_m=200), _ctx())
    rows, cols = raster.shape
    assert (
        raster.path == "stac:sentinel-2-l2a/S2_EAST+S2_WEST"
        and raster.transform.a == 200
    )
    for index, name in ((0, "red"), (1, "nir")):
        mean = _block_mean(name, raster.transform, rows, cols, 2)
        assert np.abs(raster.data[index].astype(float) - mean).max() <= 1.0, name
    assert np.array_equal(
        raster.data[2], _expected_scl(raster.transform, rows, cols, 200)
    )


def test_fa46_mosaic_scenes_on_different_cell_grids_are_an_error_not_a_shifted_mosaic(
    snapshots, tmp_path, monkeypatch
):
    """The two fixture scenes have origins 600 m apart in y: a multiple of 200 m, not of 400 m.
    At 400 m the cells of the east scene would sit half a cell off the west scene."""
    west = write_scene(tmp_path / "west", "W", footprint=WEST_FOOTPRINT)
    east = write_scene(tmp_path / "east", "E", footprint=EAST_FOOTPRINT)
    (west_x, west_y), (east_x, east_y) = _scene_origin(west), _scene_origin(east)
    assert (east_x - west_x) % 400 == 0 and (
        east_y - west_y
    ) % 400 == 200  # the premise of this test
    FakeStac(
        [
            stac_item("S2_WEST", "2024-08-13", 2.0, west, footprint=WEST_FOOTPRINT),
            stac_item("S2_EAST", "2024-08-13", 4.0, east, footprint=EAST_FOOTPRINT),
        ]
    ).install(monkeypatch, stac)
    with pytest.raises(
        ValueError, match=r"S2_EAST.*S2_WEST.*nicht auf demselben Zellgitter.*400 m"
    ):
        load(_layer(resolution_m=400), _ctx())


def test_fa46_resolution_m_brings_a_too_large_window_under_the_cell_limit(
    snapshots, scene, monkeypatch
):
    _single_scene_catalog(monkeypatch, scene)
    native = load(_layer(), _ctx())
    monkeypatch.setattr(stac, "MAX_CELLS", native.data.size - 1)
    with pytest.raises(ValueError, match=r"Zellen.*erlaubt.*resolution_m"):
        load(_layer(), _ctx(refresh=True))
    coarse = load(_layer(resolution_m=200), _ctx(refresh=True))
    assert coarse.data.size <= stac.MAX_CELLS


def test_fa46_resolution_m_is_part_of_the_snapshot_and_the_notice(
    snapshots, scene, monkeypatch
):
    fake = _single_scene_catalog(monkeypatch, scene)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = load(_layer(resolution_m=200), _ctx())
    sidecar = json.loads(next(snapshots.glob("*.json")).read_text(encoding="utf-8"))
    assert sidecar["resolution_m"] == 200
    assert any(
        "Zellgröße 200 m" in str(w.message)
        for w in caught
        if issubclass(w.category, StacSceneNotice)
    )

    other = load(
        _layer(resolution_m=400), _ctx()
    )  # another cell size: another snapshot
    assert len(fake.posts) == 2 and len(list(snapshots.glob("*.tif"))) == 2
    assert other.transform.a == 400 and first.transform.a == 200

    def forbidden(*args, **kwargs):
        raise AssertionError("network access although a snapshot exists")

    monkeypatch.setattr(stac.http, "post_json", forbidden)
    again = load(_layer(resolution_m=200), _ctx())
    assert np.array_equal(first.data, again.data) and first.transform == again.transform


def test_fa46_snapshot_key_depends_on_resolution_m():
    keys = {
        snapshot_key(_layer(), _region(), 32633),
        snapshot_key(_layer(resolution_m=200), _region(), 32633),
        snapshot_key(_layer(resolution_m=400), _region(), 32633),
    }
    assert len(keys) == 3
    assert snapshot_key(_layer(resolution_m=200), _region(), 32633) == snapshot_key(
        _layer(resolution_m=200.0), _region(), 32633
    )


def test_fa46_valid_layer_with_resolution_m_validates():
    layer = {
        "id": "s",
        "source": "stac",
        "data_type": "raster",
        "assets": ASSETS,
        "datetime": "2025-08-12",
        "max_cloud_cover": 10,
        "mask_clouds": True,
        "resolution_m": 20,
    }
    assert _issues(layer) == []


def _plain_tif(path, rows: int, cols: int, overviews: tuple[int, ...] = ()):
    import rasterio
    from rasterio.enums import Resampling

    data = (np.arange(rows * cols, dtype=np.uint16).reshape(rows, cols) % 500) + 1
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=rows,
        width=cols,
        count=1,
        dtype="uint16",
        crs="EPSG:32633",
        transform=Affine(10, 0, 300_000, 0, -10, 5_700_000),
        nodata=0,
    ) as dataset:
        dataset.write(data, 1)
        if overviews:
            dataset.build_overviews(list(overviews), Resampling.average)
    return path


@pytest.mark.parametrize(
    "rows, cols, overviews, step, exact",
    [
        (64, 48, (), 4, True),  # no overview: GDAL averages from the full resolution
        (
            64,
            48,
            (2, 4),
            2,
            True,
        ),  # the overview of exactly this factor, edges are multiples
        (64, 48, (2, 4), 4, True),
        (
            62,
            46,
            (2, 4),
            4,
            False,
        ),  # edge not a multiple of 4: the window drifts inside the overview
        (62, 46, (2,), 2, True),  # 31 x 23 overview cells: exact for factor 2
        (
            64,
            48,
            (2, 4),
            6,
            False,
        ),  # no overview of factor 6: GDAL would resample 4 -> 6 off the grid
        (64, 48, (4,), 2, True),  # only coarser overviews: GDAL does not use them
    ],
)
def test_fa46_gdal_decimation_is_used_only_where_it_cannot_shift_the_cell_blocks(
    tmp_path, rows, cols, overviews, step, exact
):
    import rasterio

    path = _plain_tif(tmp_path / "t.tif", rows, cols, overviews)
    with rasterio.open(path) as dataset:
        assert stac._gdal_decimates_exactly(dataset, step) is exact


def test_fa46_mean_blocks_skips_nodata_cells_and_rounds_integers():
    data = np.array(
        [
            [1000, 1001, 0, 0, 10, 0, 3, 4],
            [1000, 1000, 0, 0, 20, 0, 4, 4],
        ],
        dtype=np.uint16,
    )
    result = stac._mean_blocks(data, 2, 0)
    assert result.dtype == np.uint16 and result.tolist() == [[1000, 0, 15, 4]]
    # without a nodata value every cell counts
    assert stac._mean_blocks(data, 2, None).tolist() == [[1000, 0, 8, 4]]


def test_fa46_mean_blocks_of_floats_keeps_the_fraction():
    data = np.array([[1.0, 2.0], [2.0, 4.0]], dtype=np.float32)
    assert stac._mean_blocks(data, 2, None).tolist() == [[2.25]]


def test_fa46_mean_blocks_of_floats_skips_nan_nodata():
    data = np.array(
        [[1.0, np.nan, np.nan, np.nan], [3.0, 5.0, np.nan, np.nan]], dtype=np.float32
    )
    result = stac._mean_blocks(data, 2, float("nan"))
    assert result[0, 0] == 3.0 and np.isnan(
        result[0, 1]
    )  # NaN cells do not count; all-NaN stays NaN


def test_fa46_snapshot_of_a_layer_with_other_asset_order_keeps_this_layers_band_order(
    snapshots, monkeypatch
):
    """Nachtreview 02.10. (Runde 2): zwei Layer mit denselben Bändern in
    anderer Reihenfolge teilen den Snapshot-Schlüssel; der zweite bekam die
    Bandreihenfolge des ersten."""

    def fake_fetch(layer, ctx):
        names = list(layer.assets)
        data = np.stack(
            [
                np.full((2, 2), {"red": 1.0, "nir": 2.0}[n], dtype="float32")
                for n in names
            ]
        )
        raster = RasterLayer(
            data=data,
            transform=Affine(10, 0, 300000, 0, -10, 5650000),
            crs=TARGET,
            nodata=float("nan"),
            band_names=names,
        )
        provenance = {
            "date": "2024-08-13",
            "collection": layer.collection,
            "assets": dict(layer.assets),
            "items": [{"id": "S2B_X"}],
        }
        return raster, provenance

    monkeypatch.setattr(stac, "fetch", fake_fetch)
    first = _layer(id="a", assets={"red": "red", "nir": "nir"})
    second = _layer(id="b", assets={"nir": "nir", "red": "red"})
    assert snapshot_key(first, _region(), 32633) == snapshot_key(
        second, _region(), 32633
    )

    load(first, _ctx())
    result = load(second, _ctx())

    assert result.band_names == ["nir", "red"]
    assert float(result.data[0, 0, 0]) == 2.0 and float(result.data[1, 0, 0]) == 1.0
