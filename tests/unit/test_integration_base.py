"""Implements: Grundlage für FA3-FA7, FA14 (LayerStore, RasterLayer).

Contract-Tests für src/geofact/core/types.py.
"""

import geopandas as gpd
import numpy as np
import pytest
from affine import Affine
from pyproj import CRS
from shapely.geometry import Point

from geofact.core.store import LayerStore
from geofact.core.types import RasterLayer


def test_layerstore_set_and_get_roundtrip():
    store = LayerStore()
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[Point(13.7, 51.0)], crs="EPSG:4326")
    store["substations"] = gdf
    assert store["substations"] is gdf
    assert "substations" in store
    assert len(store) == 1


def test_layerstore_missing_key_raises_with_layer_id():
    store = LayerStore()
    with pytest.raises(KeyError, match="not_loaded"):
        store["not_loaded"]


def test_layerstore_get_returns_none_for_missing_key():
    store = LayerStore()
    assert store.get("absent") is None


def test_rasterlayer_shape_from_2d_array():
    layer = RasterLayer(
        data=np.zeros((10, 10), dtype="int32"),
        transform=Affine.identity(),
        crs=CRS.from_epsg(4326),
    )
    assert layer.shape == (10, 10)
