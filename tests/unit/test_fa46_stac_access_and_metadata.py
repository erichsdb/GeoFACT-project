"""Implements: FA46 (STAC access and metadata: signing, s3, gdal_options, proj:code, scene_order, mask band).

Contract tests of the additions to the source `stac` (builtin/sources/stac.py) from the
limits report (G3, G5, G15, G16), all offline with the fake catalog of tests/stac_scenes.py:

- `asset_signing: planetary_computer` rewrites the href to GDAL's signing `/vsicurl?` form
  (percent-encoded), `s3://` hrefs become `/vsis3/`;
- `gdal_options` reach GDAL, secrets are refused (they belong into the environment);
- `proj:code`, `proj:wkt2` and `proj:projjson` count like `proj:epsg`;
- a catalog without `eo:cloud_cover` is an error under the default `scene_order: cloud_cover`,
  `latest`/`earliest` choose by date;
- `mask_clouds` only trusts a band that is a Sentinel-2 scene classification, or explicit
  `mask_classes`;
- the sampled class band is a copy, not a view on the full source window.
"""

from __future__ import annotations

import urllib.parse

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from pyproj import CRS
from shapely.geometry import box

from geofact import api
from geofact.builtin.sources import stac
from geofact.builtin.sources.stac import StacLayer, load, select_scenes, snapshot_key
from geofact.core.contracts import LoadContext
from stac_scenes import REGION_BBOX, FakeStac, stac_item, write_scene

pytestmark = pytest.mark.filterwarnings(
    "ignore::geofact.builtin.sources.stac.StacSceneNotice"
)

TARGET = CRS.from_epsg(32633)
ASSETS = {"red": "red", "nir": "nir", "scl": "scl"}


def _region(bbox=REGION_BBOX) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": ["r"]}, geometry=[box(*bbox)], crs="EPSG:4326")


def _ctx() -> LoadContext:
    return LoadContext(region=_region(), target_crs=TARGET, refresh=False)


def _layer(**fields) -> StacLayer:
    config = {"id": "sentinel2", "assets": ASSETS, "datetime": "2024-08-01/2024-08-31"}
    config.update(fields)
    return StacLayer(**config)


def _issues(layer: dict) -> list[tuple[str, str]]:
    config = {
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
    return api.validate(config).issues


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    return tmp_path / "snapshots"


@pytest.fixture
def scene(tmp_path):
    return write_scene(tmp_path / "s_a", "S2B_A")


# =====================================================================
# href: signing and s3
# =====================================================================


def test_fa46_planetary_computer_signing_rewrites_the_href_percent_encoded():
    layer = _layer(asset_signing="planetary_computer")
    href = "https://sentinel2l2a01.blob.core.windows.net/x/B04.tif?st=1&se=2"
    resolved = stac._resolve_href(layer, href)
    assert resolved.startswith("/vsicurl?pc_url_signing=yes&")
    query = urllib.parse.parse_qs(resolved.split("?", 1)[1])
    assert query["url"] == [href]
    # the raw '&' of the href must not leak into GDAL's option list
    assert "&se=2" not in resolved


def test_fa46_hrefs_stay_untouched_without_signing_and_s3_becomes_vsis3():
    layer = _layer()
    assert (
        stac._resolve_href(layer, "https://example.org/a.tif")
        == "https://example.org/a.tif"
    )
    assert (
        stac._resolve_href(layer, "s3://bucket/path/B04.tif")
        == "/vsis3/bucket/path/B04.tif"
    )
    signed = _layer(asset_signing="planetary_computer")
    # signing applies to http(s) only: an s3 href is read through /vsis3/ and the environment
    assert stac._resolve_href(signed, "s3://bucket/a.tif") == "/vsis3/bucket/a.tif"


def test_fa46_signed_hrefs_are_what_gdal_opens(snapshots, scene, monkeypatch):
    remote = {key: f"https://example.org/scene/{key}.tif" for key in scene}
    local = {remote[key]: path for key, path in scene.items()}
    FakeStac([stac_item("S", "2024-08-13", 1.0, remote)]).install(monkeypatch, stac)
    opened: list[str] = []
    real_open = rasterio.open

    def recording_open(href, *args, **kwargs):
        if not str(href).startswith("/vsicurl?"):  # snapshot writing etc.
            return real_open(href, *args, **kwargs)
        opened.append(href)
        url = urllib.parse.parse_qs(href.split("?", 1)[1])["url"][0]
        return real_open(local[url], *args, **kwargs)

    monkeypatch.setattr(stac.rasterio, "open", recording_open)
    raster = load(_layer(asset_signing="planetary_computer"), _ctx())
    assert raster.band_names == ["red", "nir", "scl"]
    assert len(opened) == 3 and all(
        h.startswith("/vsicurl?pc_url_signing=yes&url=https%3A") for h in opened
    )


# =====================================================================
# gdal_options
# =====================================================================


def test_fa46_gdal_options_reach_gdal_over_the_defaults(snapshots, scene, monkeypatch):
    FakeStac([stac_item("S", "2024-08-13", 1.0, scene)]).install(monkeypatch, stac)
    seen: list[dict] = []
    real_env = rasterio.Env

    def recording_env(**options):
        seen.append(options)
        return real_env(**options)

    monkeypatch.setattr(stac.rasterio, "Env", recording_env)
    load(
        _layer(gdal_options={"AWS_NO_SIGN_REQUEST": True, "GDAL_HTTP_MAX_RETRY": 5}),
        _ctx(),
    )
    assert seen, "rasterio.Env was not used"
    options = seen[-1]
    assert options["AWS_NO_SIGN_REQUEST"] == "YES"
    assert options["GDAL_HTTP_MAX_RETRY"] == "5"
    assert options["GDAL_DISABLE_READDIR_ON_OPEN"] == "EMPTY_DIR"


@pytest.mark.parametrize(
    "key",
    [
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SESSION_TOKEN",
        "GDAL_HTTP_USERPWD",
        "GDAL_HTTP_HEADERS",
        "AZURE_STORAGE_SAS_TOKEN",
        "GDAL_HTTP_BEARER",
    ],
)
def test_fa46_gdal_options_refuse_secrets_with_position(key):
    issues = _issues(
        {
            "id": "s2",
            "source": "stac",
            "assets": ASSETS,
            "datetime": "2024-08-01/2024-08-31",
            "gdal_options": {key: "x"},
        }
    )
    assert issues, f"{key} was accepted"
    location, message = issues[0]
    assert location.startswith("layers -> 0")
    assert key in message and "Umgebung" in message


def test_fa46_gdal_option_names_must_be_config_option_names():
    with pytest.raises(ValueError, match="GDAL-Konfigurationsoption"):
        _layer(gdal_options={"gdal http max retry": "3"})


# =====================================================================
# Projection extension: proj:code, proj:wkt2, proj:projjson
# =====================================================================


@pytest.mark.parametrize(
    "properties",
    [
        {"proj:epsg": 32633},
        {"proj:code": "EPSG:32633"},
        {"proj:wkt2": CRS.from_epsg(32633).to_wkt()},
        {"proj:projjson": CRS.from_epsg(32633).to_json_dict()},
    ],
)
def test_fa46_proj_code_wkt2_and_projjson_count_like_proj_epsg(properties):
    assert stac._epsg_of({"id": "x", "properties": properties}) == 32633


def test_fa46_proj_on_the_asset_counts_when_the_item_has_none():
    item = {
        "id": "x",
        "properties": {},
        "assets": {"red": {"href": "a", "proj:code": "EPSG:32632"}},
    }
    assert stac._epsg_of(item) == 32632
    assert stac._epsg_of({"id": "y", "properties": {}}) is None


def test_fa46_target_zone_preference_works_with_proj_code(tmp_path):
    def item(scene_id, cloud, epsg):
        it = stac_item(
            scene_id,
            "2024-08-13",
            cloud,
            {"red": "r", "nir": "n", "scl": "s"},
            epsg=epsg,
        )
        del it["properties"]["proj:epsg"]
        it["properties"]["proj:code"] = f"EPSG:{epsg}"
        return it

    items = [item("clean_32", 1.0, 32632), item("cloudy_33", 9.0, 32633)]
    _, chosen = select_scenes(_layer(), items, REGION_BBOX, 32633)
    assert [i["id"] for i in chosen] == ["cloudy_33"]


# =====================================================================
# scene_order and missing eo:cloud_cover (G5)
# =====================================================================


def _dated(scene_id: str, date: str, cloud: float | None) -> dict:
    it = stac_item(
        scene_id,
        date,
        cloud if cloud is not None else 0.0,
        {"red": "r", "nir": "n", "scl": "s"},
    )
    if cloud is None:
        del it["properties"]["eo:cloud_cover"]
    return it


def test_fa46_missing_cloud_cover_is_an_error_under_the_default_scene_order():
    items = [_dated("y2017", "2017-07-01", None), _dated("y2024", "2024-07-01", None)]
    with pytest.raises(ValueError, match="eo:cloud_cover") as excinfo:
        select_scenes(
            _layer(datetime="2017-01-01/2024-12-31"), items, REGION_BBOX, 32633
        )
    assert "scene_order" in str(excinfo.value) and "y2017" in str(excinfo.value)


@pytest.mark.parametrize(
    "order, expected", [("latest", "y2024"), ("earliest", "y2017")]
)
def test_fa46_scene_order_latest_and_earliest_choose_by_date(order, expected):
    items = [
        _dated("y2017", "2017-07-01", None),
        _dated("y2020", "2020-07-01", None),
        _dated("y2024", "2024-07-01", None),
    ]
    date, chosen = select_scenes(
        _layer(datetime="2017-01-01/2024-12-31", scene_order=order),
        items,
        REGION_BBOX,
        32633,
    )
    assert [i["id"] for i in chosen] == [expected]
    assert date == expected[1:] + "-07-01"


def test_fa46_scene_order_latest_ignores_cloud_cover_and_sorts_the_search_by_date(
    snapshots, scene, monkeypatch
):
    fake = FakeStac([stac_item("S", "2024-08-13", 1.0, scene)]).install(
        monkeypatch, stac
    )
    load(_layer(scene_order="latest"), _ctx())
    _, body = fake.posts[0]
    assert body["sortby"] == [{"field": "properties.datetime", "direction": "desc"}]
    items = [
        _dated("old_clean", "2024-08-02", 0.5),
        _dated("new_cloudy", "2024-08-20", 40.0),
    ]
    _, chosen = select_scenes(_layer(scene_order="latest"), items, REGION_BBOX, 32633)
    assert [i["id"] for i in chosen] == ["new_cloudy"]


def test_fa46_scene_order_enters_the_snapshot_key_only_when_not_default():
    base = snapshot_key(_layer(), _region(), 32633)
    assert snapshot_key(_layer(scene_order="cloud_cover"), _region(), 32633) == base
    assert snapshot_key(_layer(scene_order="latest"), _region(), 32633) != base
    assert snapshot_key(_layer(scene_order="earliest"), _region(), 32633) != base
    # access settings do not change the pixels and keep existing snapshots valid
    assert (
        snapshot_key(
            _layer(
                asset_signing="planetary_computer",
                gdal_options={"AWS_NO_SIGN_REQUEST": "YES"},
            ),
            _region(),
            32633,
        )
        == base
    )


def test_fa46_scene_order_does_not_combine_with_item_id():
    with pytest.raises(ValueError, match="scene_order"):
        _layer(item_id="S", datetime=None, scene_order="latest")


def test_fa46_max_cloud_cover_hint_when_the_catalog_returns_nothing():
    with pytest.raises(ValueError, match="eo:cloud_cover"):
        select_scenes(_layer(max_cloud_cover=10), [], REGION_BBOX, 32633)


# =====================================================================
# Mask band semantics (G15)
# =====================================================================


def _qa_item(scene: dict, **extra) -> dict:
    """The class band of the scene published under a non-SCL asset ('qa_pixel')."""
    paths = {"red": scene["red"], "nir": scene["nir"], "qa_pixel": scene["scl"]}
    item = stac_item("S", "2024-08-13", 1.0, paths)
    item["assets"]["qa_pixel"].update(extra)
    return item


def test_fa46_mask_clouds_refuses_a_band_that_is_not_a_scene_classification(
    snapshots, scene, monkeypatch
):
    FakeStac([_qa_item(scene, title="Pixel Quality Assessment Band")]).install(
        monkeypatch, stac
    )
    monkeypatch.setattr(
        stac, "_read_band", lambda *a, **k: pytest.fail("read before the mask check")
    )
    layer = _layer(
        assets={"red": "red", "nir": "nir", "scl": "qa_pixel"}, mask_clouds=True
    )
    with pytest.raises(ValueError, match="mask_classes") as excinfo:
        load(layer, _ctx())
    assert "qa_pixel" in str(excinfo.value)


def test_fa46_scl_title_or_classification_marks_a_scene_classification():
    item = {
        "id": "S",
        "properties": {},
        "assets": {
            "SCL_20m": {"href": "a"},
            "x": {"href": "b", "title": "Scene classification map (SCL)"},
            "y": {
                "href": "c",
                "classification:classes": [
                    {"value": 8, "description": "Cloud medium probability"},
                    {"value": 9, "description": "Cloud high probability"},
                ],
            },
            "qa": {"href": "d", "title": "Quality"},
        },
    }
    for key in ("SCL_20m", "x", "y"):
        assert stac._is_scene_classification(item, key), key
    assert not stac._is_scene_classification(item, "qa")


def test_fa46_mask_classes_mask_exactly_those_values(snapshots, scene, monkeypatch):
    FakeStac([_qa_item(scene)]).install(monkeypatch, stac)
    layer = _layer(
        assets={"red": "red", "nir": "nir", "scl": "qa_pixel"},
        mask_clouds=True,
        mask_classes=[4, 5],
    )
    raster = load(layer, _ctx())
    red, scl = raster.data[0], raster.data[2]
    assert np.all(red[np.isin(scl, [4, 5])] == 0)
    keep = ~np.isin(scl, [4, 5]) & (scl != 0)
    assert keep.any() and np.all(red[keep] > 0)
    # the SCL cloud classes are NOT masked when mask_classes names others
    assert np.any(red[np.isin(scl, [8, 9])] > 0)


def test_fa46_mask_classes_need_mask_clouds_and_are_part_of_the_snapshot_key():
    with pytest.raises(ValueError, match="mask_clouds"):
        _layer(mask_classes=[1])
    base = snapshot_key(_layer(mask_clouds=True), _region(), 32633)
    assert (
        snapshot_key(_layer(mask_clouds=True, mask_classes=[3, 8]), _region(), 32633)
        != base
    )


# =====================================================================
# Sampled class band is a copy (memory: no view on the full source window)
# =====================================================================


def test_fa46_sampled_class_band_is_a_copy_not_a_view(scene):
    layer = _layer(resolution_m=400)
    item = stac_item("S", "2024-08-13", 1.0, scene)
    with rasterio.open(scene["scl"]) as dataset:
        window = rasterio.windows.Window(
            0, 0, 16, 16
        )  # in 100 m cells; SCL factor 2, target 4
        data = stac._read_band(layer, item, "scl", dataset, window, 2, 4, is_class=True)
    assert data.shape == (4, 4)
    assert data.base is None and data.flags.owndata
