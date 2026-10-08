"""Implements: FA64 (Region je Layer: Feld im Basisvertrag).

Contract-Tests für ``LayerBase.region``: Default None, nicht leer, ein
BBox-Literal wird wie ``scenario.region`` geprüft, und das Feld steht im
Schema jedes registrierten Layer-Modells. Die Auswertung in der Ladestrecke
folgt in Welle 2 (W2-04)."""

from __future__ import annotations

import pytest

from geofact import api
from geofact.core.errors import ConfigError

TEMPLATE = """
scenario:
  name: Region je Layer
  region: "12.2,51.2,12.5,51.4"
layers:
  - id: a
    source: file
    path: data/a.geojson
    data_type: vector
    {extra}
steps:
  - id: p
    op: buffer
    inputs: {{geometry: a}}
    params: {{radius_km: 1}}
output:
  - type: geojson
    source: p
"""


def _load(extra: str = ""):
    return api.load_scenario(TEMPLATE.format(extra=extra))


def test_fa64_layer_region_defaults_to_none():
    layer = _load().scenario.layers[0]
    assert layer.region is None
    assert layer.model_dump()["region"] is None


def test_fa64_layer_region_accepts_name_and_bbox():
    assert (
        _load('region: "Dresden, Deutschland"').scenario.layers[0].region
        == "Dresden, Deutschland"
    )
    assert (
        _load('region: "13.6,51.0,13.9,51.1"').scenario.layers[0].region
        == "13.6,51.0,13.9,51.1"
    )


def test_fa64_layer_region_must_not_be_empty():
    with pytest.raises(ConfigError) as exc:
        _load('region: "  "')
    assert [loc for loc, _ in exc.value.issues] == ["layers -> 0 -> region"]
    assert "darf nicht leer sein" in exc.value.issues[0][1]


def test_fa64_layer_region_bbox_is_checked_like_scenario_region():
    with pytest.raises(ConfigError) as exc:
        _load('region: "177,-19,-178,-16"')
    assert [loc for loc, _ in exc.value.issues] == ["layers -> 0 -> region"]
    assert "Antimeridian" in exc.value.issues[0][1]


def test_fa64_schema_shows_region_on_every_layer_model():
    registry = api.registry()
    names = registry.names("source")
    assert names
    for name in names:
        model = registry.source(name).layer_model
        assert "region" in model.model_json_schema()["properties"], name
        assert "license" in model.model_json_schema()["properties"], name
