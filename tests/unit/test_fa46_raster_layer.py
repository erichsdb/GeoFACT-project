"""Implements: FA46 (mehrbandiger RasterLayer), FA4 (Rasteroptionen bands/band_names/margin_km), FA5 (Resampling).

Contract tests of the multi-band `RasterLayer` (core/types.py), the tif format
options (builtin/formats/raster.py) and the declarable resampling of the
transformation `reproject` (builtin/transforms/reproject.py). The reference results on
single-band rasters must stay identical: every new option defaults to the old
behaviour (tests `..._unchanged`).
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import CRS
from shapely.geometry import box

from geofact import api
from geofact.builtin.sources import file as files
from geofact.builtin.sources.file import FileLayer
from geofact.builtin.transforms.reproject import harmonize
from geofact.core.types import RasterLayer
from geofact.plugin_api import LoadContext

TRANSFORM = Affine(0.01, 0, 13.0, 0, -0.01, 51.1)
WGS84 = CRS.from_epsg(4326)


def _layer(data, **fields) -> RasterLayer:
    return RasterLayer(data=data, transform=TRANSFORM, crs=WGS84, **fields)


# =====================================================================
# RasterLayer: 2D = one band, 3D = several named bands
# =====================================================================


def test_fa46_single_band_raster_behaves_as_before():
    layer = _layer(np.ones((4, 5), dtype="int32"))
    assert layer.shape == (4, 5) and layer.band_count == 1
    assert layer.band_names is None and layer.names == ["band1"]
    assert layer.nodata is None and layer.path is None


def test_fa46_multi_band_raster_has_a_shape_per_band_and_named_bands():
    layer = _layer(np.zeros((3, 4, 5)), band_names=["red", "nir", "scl"])
    assert (
        layer.shape == (4, 5)
        and layer.band_count == 3
        and layer.names == ["red", "nir", "scl"]
    )


def test_fa46_unnamed_multi_band_raster_has_implicit_names():
    assert _layer(np.zeros((2, 3, 3))).names == ["band1", "band2"]


@pytest.mark.parametrize("selector, expected", [("nir", 1), ("red", 0), (1, 0), (2, 1)])
def test_fa46_band_index_accepts_a_name_or_a_one_based_number(selector, expected):
    assert (
        _layer(np.zeros((2, 3, 3)), band_names=["red", "nir"]).band_index(selector)
        == expected
    )


@pytest.mark.parametrize("selector", ["swir", 0, 3, True, 1.5])
def test_fa46_unknown_band_error_lists_the_bands(selector):
    layer = _layer(np.zeros((2, 3, 3)), band_names=["red", "nir"], path="szene.tif")
    with pytest.raises(ValueError, match=r"\['red', 'nir'\]"):
        layer.band_index(selector)


def test_fa46_band_data_returns_the_2d_array_of_one_band():
    data = np.arange(18).reshape(2, 3, 3)
    layer = _layer(data, band_names=["a", "b"])
    assert np.array_equal(layer.band_data("b"), data[1])
    assert np.array_equal(layer.band_data(1), data[0])


def test_fa46_band_data_without_selector_needs_exactly_one_band():
    """No silent choice of the first band of a multi-band raster (rule 7)."""
    layer = _layer(np.zeros((2, 3, 3)), band_names=["red", "nir"])
    with pytest.raises(ValueError, match=r"2 Bänder.*'band'"):
        layer.band_data()
    assert _layer(np.zeros((3, 3))).band_data().shape == (3, 3)
    assert _layer(np.zeros((1, 3, 3))).band_data().shape == (3, 3)


def test_fa46_valid_mask_follows_the_declared_nodata():
    data = np.array([[1.0, -200.0], [np.nan, 5.0]])
    assert _layer(data).valid_mask().all()  # no nodata declared: all valid
    assert _layer(data, nodata=-200.0).valid_mask().tolist() == [
        [True, False],
        [True, True],
    ]
    assert _layer(data, nodata=float("nan")).valid_mask().tolist() == [
        [True, True],
        [False, True],
    ]


def test_fa46_valid_mask_of_a_band_and_of_the_whole_array():
    data = np.stack([np.array([[0, 1], [1, 1]]), np.array([[1, 1], [1, 0]])])
    layer = _layer(data, nodata=0, band_names=["a", "b"])
    assert layer.valid_mask().shape == (2, 2, 2)
    assert layer.valid_mask("a").tolist() == [[False, True], [True, True]]
    assert layer.valid_mask("b").tolist() == [[True, True], [True, False]]


@pytest.mark.parametrize(
    "data, names, message",
    [
        (np.zeros(5), None, "2D"),
        (np.zeros((1, 2, 3, 4)), None, "Dimensionen"),
        (np.zeros((2, 3, 3)), ["only_one"], "Bandnamen"),
        (np.zeros((2, 3, 3)), ["same", "same"], "eindeutig"),
    ],
)
def test_fa46_inconsistent_raster_is_refused(data, names, message):
    with pytest.raises(ValueError, match=message):
        _layer(data, band_names=names)


# =====================================================================
# FA4: tif options bands / band_names / margin_km
# =====================================================================


@pytest.fixture
def three_band_tif(tmp_path):
    path = tmp_path / "szene.tif"
    data = np.stack(
        [np.full((20, 20), value, dtype="uint16") for value in (10, 20, 30)]
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=20,
        width=20,
        count=3,
        dtype="uint16",
        crs="EPSG:4326",
        transform=TRANSFORM,
        nodata=0,
    ) as dataset:
        dataset.write(data)
        for index, name in enumerate(("blau", "gruen", "rot"), start=1):
            dataset.set_band_description(index, name)
    return path


def _scenario(raster_layer: dict) -> dict:
    """A valid scenario around one raster layer: mean of the raster per region polygon."""
    return {
        "scenario": {"name": "t", "region": "13.0,50.9,13.1,51.0"},
        "layers": [raster_layer, {"id": "zonen", "source": "region"}],
        "steps": [
            {
                "id": "z",
                "op": "zonal_stats",
                "inputs": {"zones": "zonen", "values": raster_layer["id"]},
                "params": {"stat": "sum"},
            }
        ],
        "output": [{"type": "csv", "source": "z"}],
    }


def _file_layer(path, **fields) -> FileLayer:
    return FileLayer(
        id="szene", path=str(path), data_type="raster", format="tif", **fields
    )


def _region(bounds=None):
    bounds = bounds or (13.04, 50.94, 13.12, 51.02)
    return gpd.GeoDataFrame(geometry=[box(*bounds)], crs="EPSG:4326")


def test_fa04_tif_without_options_still_reads_band_one_as_2d_unnamed(three_band_tif):
    raster = files.load(_file_layer(three_band_tif), LoadContext())
    assert raster.data.ndim == 2 and raster.band_names is None
    assert raster.data[0, 0] == 10


def test_fa04_tif_bands_by_number_read_those_bands_with_the_file_descriptions(
    three_band_tif,
):
    raster = files.load(_file_layer(three_band_tif, bands=[3, 1]), LoadContext())
    assert raster.data.shape == (2, 20, 20)
    assert raster.band_names == ["rot", "blau"]
    assert raster.data[:, 0, 0].tolist() == [30, 10]


def test_fa04_tif_bands_by_description(three_band_tif):
    raster = files.load(_file_layer(three_band_tif, bands=["gruen"]), LoadContext())
    assert raster.band_names == ["gruen"] and raster.data.shape == (1, 20, 20)
    assert raster.data[0, 0, 0] == 20


def test_fa04_tif_band_names_rename_the_bands(three_band_tif):
    raster = files.load(
        _file_layer(three_band_tif, bands=[1, 3], band_names=["blue", "red"]),
        LoadContext(),
    )
    assert raster.band_names == ["blue", "red"]


def test_fa04_tif_band_names_alone_name_all_bands(three_band_tif):
    raster = files.load(
        _file_layer(three_band_tif, band_names=["b", "g", "r"]), LoadContext()
    )
    assert raster.band_names == ["b", "g", "r"] and raster.data.shape[0] == 3


def test_fa04_tif_file_without_descriptions_gets_band_numbers(tmp_path):
    path = tmp_path / "ohne.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=5,
        width=5,
        count=2,
        dtype="uint8",
        crs="EPSG:4326",
        transform=TRANSFORM,
    ) as dataset:
        dataset.write(np.ones((2, 5, 5), dtype="uint8"))
    raster = files.load(_file_layer(path, bands=[1, 2]), LoadContext())
    assert raster.band_names == ["band1", "band2"]


@pytest.mark.parametrize(
    "fields, fragment",
    [
        ({"bands": [4]}, "Band 4"),
        ({"bands": ["nirgends"]}, "nirgends"),
        ({"bands": [1], "band_names": ["a", "b"]}, "gleich viele"),
    ],
)
def test_fa04_tif_wrong_band_selection_is_an_explicit_error(
    three_band_tif, fields, fragment
):
    with pytest.raises(Exception, match=fragment):
        files.load(_file_layer(three_band_tif, **fields), LoadContext())


def test_fa04_tif_band_names_count_must_match_when_bands_are_not_given(three_band_tif):
    with pytest.raises(ValueError, match="band_names"):
        files.load(_file_layer(three_band_tif, band_names=["a", "b"]), LoadContext())


def test_fa04_tif_unknown_option_is_rejected_with_position():
    issues = api.validate(
        _scenario(
            {
                "id": "r",
                "source": "file",
                "path": "x.tif",
                "data_type": "raster",
                "format": "tif",
                "bandz": [1],
            }
        )
    ).issues
    assert issues and issues[0][0] == "layers -> 0 -> bandz"


def test_fa04_tif_margin_km_widens_the_window(three_band_tif):
    ctx = LoadContext(region=_region())
    plain = files.load(_file_layer(three_band_tif), ctx)
    widened = files.load(_file_layer(three_band_tif, margin_km=3), ctx)
    assert widened.shape[0] > plain.shape[0] and widened.shape[1] > plain.shape[1]
    # the original window sits inside the widened one: 3 km are about 0.027 degrees of latitude
    assert (
        widened.transform.f > plain.transform.f
        and widened.transform.c < plain.transform.c
    )


def test_fa04_tif_margin_zero_is_the_unchanged_window(three_band_tif):
    ctx = LoadContext(region=_region())
    default = files.load(_file_layer(three_band_tif), ctx)
    explicit = files.load(_file_layer(three_band_tif, margin_km=0), ctx)
    assert default.shape == explicit.shape and default.transform == explicit.transform


def test_fa04_tif_negative_margin_is_rejected():
    issues = api.validate(
        _scenario(
            {
                "id": "r",
                "source": "file",
                "path": "x.tif",
                "data_type": "raster",
                "format": "tif",
                "margin_km": -1,
            }
        )
    ).issues
    assert issues and "margin_km" in issues[0][0]


def test_fa04_tif_empty_window_outside_the_file_has_no_cells(three_band_tif):
    """Edge case: a region far outside the raster reads an empty window (as before)."""
    ctx = LoadContext(region=_region((20.0, 40.0, 20.1, 40.1)))
    raster = files.load(_file_layer(three_band_tif, bands=[1]), ctx)
    assert raster.data.size == 0 or raster.data.sum() == 0


# =====================================================================
# FA5: declarable resampling
# =====================================================================


def _count_raster() -> RasterLayer:
    data = np.random.default_rng(1).integers(0, 100, (20, 20)).astype("float32")
    return RasterLayer(data=data, transform=TRANSFORM, crs=WGS84, nodata=-1.0)


def _total(raster: RasterLayer) -> float:
    return float(raster.data[raster.data != -1.0].sum())


def test_fa05_resampling_sum_preserves_the_total_of_a_count_raster_nearest_does_not():
    source = _count_raster()
    target = CRS.from_epsg(32633)
    total = float(source.data.sum())
    summed = harmonize("pop", source, target, resampling="sum")
    nearest = harmonize("pop", source, target)
    assert abs(_total(summed) - total) / total < 0.02
    assert (
        abs(_total(nearest) - total) / total > 0.02
    )  # the reference finding, now declarable


def test_fa05_resampling_default_is_nearest_as_before():
    source = _count_raster()
    target = CRS.from_epsg(32633)
    default = harmonize("pop", source, target)
    explicit = harmonize("pop", source, target, resampling="nearest")
    assert (
        np.array_equal(default.data, explicit.data)
        and default.transform == explicit.transform
    )


@pytest.mark.parametrize("method", ["bilinear", "average"])
def test_fa05_other_resampling_methods_differ_from_nearest(method):
    source = _count_raster()
    target = CRS.from_epsg(32633)
    assert not np.array_equal(
        harmonize("pop", source, target, resampling=method).data,
        harmonize("pop", source, target).data,
    )


def test_fa05_unknown_resampling_is_an_explicit_error():
    with pytest.raises(
        ValueError, match=r"Layer 'pop'.*unbekanntes Resampling 'cubic'"
    ):
        harmonize("pop", _count_raster(), CRS.from_epsg(32633), resampling="cubic")


def test_fa05_resampling_is_declared_in_the_file_layer_and_read_by_the_transform(
    three_band_tif,
):
    from geofact.builtin.transforms.reproject import declared_resampling

    assert declared_resampling(_file_layer(three_band_tif)) == "nearest"
    # the option travels through the format options (validated with the layer)
    layer = api.load_scenario(
        _scenario(
            {
                "id": "r",
                "source": "file",
                "path": str(three_band_tif),
                "data_type": "raster",
                "format": "tif",
                "resampling": "sum",
            }
        )
    ).scenario.layers[0]
    assert declared_resampling(layer) == "sum"


def test_fa05_invalid_resampling_in_the_layer_is_rejected():
    issues = api.validate(
        _scenario(
            {
                "id": "r",
                "source": "file",
                "path": "x.tif",
                "data_type": "raster",
                "format": "tif",
                "resampling": "cubic",
            }
        )
    ).issues
    assert issues and "resampling" in issues[0][0]


def test_fa05_multi_band_raster_is_reprojected_with_names_and_every_band():
    data = np.stack(
        [np.full((10, 10), 1.0, "float32"), np.full((10, 10), 2.0, "float32")]
    )
    source = RasterLayer(
        data=data, transform=TRANSFORM, crs=WGS84, nodata=-9.0, band_names=["a", "b"]
    )
    result = harmonize("m", source, CRS.from_epsg(32633))
    assert (
        result.data.ndim == 3
        and result.data.shape[0] == 2
        and result.band_names == ["a", "b"]
    )
    assert result.nodata == -9.0
    assert set(np.unique(result.data[0])) <= {1.0, -9.0} and set(
        np.unique(result.data[1])
    ) <= {2.0, -9.0}


def test_fa05_raster_already_in_the_target_crs_is_returned_cell_for_cell():
    target = CRS.from_epsg(32633)
    source = RasterLayer(
        data=np.arange(12, dtype="float32").reshape(3, 4),
        transform=Affine(10, 0, 350000, 0, -10, 5640000),
        crs=target,
        nodata=-1.0,
    )
    result = harmonize("same", source, target)
    assert (
        np.array_equal(result.data, source.data)
        and result.transform == source.transform
    )
    assert result.nodata == -1.0
