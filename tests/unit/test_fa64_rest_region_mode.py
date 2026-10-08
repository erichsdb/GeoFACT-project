"""Implements: FA64 (Region je Layer) für die Quellart rest, FA42 (Rückwärtskompatibilität).

Seit FA64 ist ``region`` das Feld aller Layer für eine eigene Region; die
BBox-Mitsendung der REST-Quelle heißt ``region_mode``. Alte YAML mit
``region: bbox|none`` lädt weiter (mit Veraltungshinweis), der
Snapshot-Schlüssel bleibt bytegleich, und die Ladestrecke löst nur echte
Regionsangaben auf."""

from __future__ import annotations

import pytest

from _loading_helpers import context, layer_from
from geofact.builtin.sources import rest as rest_module
from geofact.engine.loading import _layer_context
from geofact.support import snapshot as snapshot_module

URL = "https://portal.example/api/messstellen"


def _rest(**extra):
    return {"id": "messstellen", "source": "rest", "url": URL, **extra}


def test_fa64_rest_region_mode_defaults_to_bbox_and_region_is_unset():
    layer = layer_from(_rest())

    assert layer.region_mode == "bbox"
    assert layer.region is None


@pytest.mark.parametrize("mode", ["bbox", "none"])
def test_fa64_rest_legacy_region_value_maps_to_region_mode_with_deprecation_note(mode):
    with pytest.warns(FutureWarning, match=f"region_mode: {mode}"):
        layer = layer_from(_rest(region=mode))

    assert layer.region_mode == mode
    assert layer.region is None


def test_fa64_rest_legacy_region_together_with_region_mode_is_an_error():
    with pytest.raises(ValueError, match="nur region_mode angeben"):
        layer_from(_rest(region="bbox", region_mode="none"))


def test_fa64_rest_region_name_is_a_layer_region():
    layer = layer_from(_rest(region="12.30,51.30,12.45,51.38", region_mode="none"))

    assert layer.region == "12.30,51.30,12.45,51.38"
    assert layer.region_mode == "none"


def test_fa64_loader_resolves_only_real_region_specs():
    ctx = context()
    legacy = layer_from(_rest(region_mode="bbox"))

    assert _layer_context(legacy, ctx, None) is ctx

    own = layer_from(_rest(region="12.30,51.30,12.45,51.38"))
    replaced = _layer_context(own, ctx, None)
    assert replaced is not ctx
    assert tuple(replaced.region.total_bounds) == pytest.approx(
        (12.30, 51.30, 12.45, 51.38)
    )
    assert replaced.target_crs == ctx.target_crs


def test_fa64_rest_snapshot_key_is_unchanged_by_the_rename():
    ctx = context()
    payload = rest_module._snapshot_payload(layer_from(_rest(region_mode="none")), ctx)

    assert payload["region"] == "none"
    assert "region_mode" not in payload
    assert payload["region_wkt"] is None


# Key of the same layer ({..., region: none}) computed with the code before
# FA64/FA65 (commit 35f9970); existing REST snapshots are stored under it.
PRE_FA64_KEY = "afd400c5eaa2d9617d7853a7bc9f82e810782ec4143a61e9db5c9bd834f87208"


def test_fa64_rest_snapshot_key_is_byte_identical_to_before_fa64():
    ctx = context()
    payload = rest_module._snapshot_payload(layer_from(_rest(region_mode="none")), ctx)

    assert snapshot_module.payload_key(payload) == PRE_FA64_KEY


def test_fa65_rest_license_does_not_change_the_snapshot_key():
    ctx = context()
    licensed = layer_from(_rest(region_mode="none", license={"name": "CC0-1.0"}))
    payload = rest_module._snapshot_payload(licensed, ctx)

    assert "license" not in payload
    assert snapshot_module.payload_key(payload) == PRE_FA64_KEY
