"""Implements: FA65 (default licence of the source `stac` for Sentinel collections).

A `stac` layer whose collection is a Copernicus Sentinel collection carries the
Copernicus licence by default (CC-BY-SA-3.0-IGO with the Copernicus attribution);
every other collection declares none - the catalog's own `license` field is often
`proprietary` and is not guessed from (limits report, licences section).
"""

from __future__ import annotations

import pytest

from geofact.builtin.sources.stac import COPERNICUS_LICENSE, StacLayer


def _layer(**fields) -> StacLayer:
    config = {"id": "s2", "assets": {"red": "red"}, "datetime": "2024-08-01/2024-08-31"}
    config.update(fields)
    return StacLayer(**config)


@pytest.mark.parametrize(
    "collection",
    ["sentinel-2-l2a", "sentinel-2-c1-l2a", "Sentinel-1-GRD", "sentinel-2-l1c"],
)
def test_fa65_stac_sentinel_collection_defaults_to_the_copernicus_licence(collection):
    licence = _layer(collection=collection).default_license()
    assert licence is not None
    assert licence.name == "CC-BY-SA-3.0-IGO"
    assert "Copernicus Sentinel" in licence.attribution
    assert licence.url.startswith("https://")
    assert licence == COPERNICUS_LICENSE


@pytest.mark.parametrize(
    "collection", ["io-10m-annual-lulc", "esa-worldcover", "landsat-c2-l2"]
)
def test_fa65_stac_other_collections_declare_no_default_licence(collection):
    assert _layer(collection=collection).default_license() is None


def test_fa65_stac_default_licence_is_the_default_collection_of_the_source():
    assert _layer().default_license() == COPERNICUS_LICENSE
