"""Implements: FA56 (Regionsplausibilität: BBox-Literal schon bei validate).

Contract-Tests für ``core/crs.py::parse_bbox_literal``/``check_bbox`` und den
Validator an ``scenario.region``: NaN/inf, Bereich, Reihenfolge, Antimeridian
und Nullfläche werden mit Position ``scenario -> region`` abgelehnt; ein
gültiges FA3-Literal und ein Ortsname bleiben unverändert."""

from __future__ import annotations

import pytest

from geofact import api
from geofact.core.crs import check_bbox, parse_bbox_literal
from geofact.core.errors import ConfigError

TEMPLATE = """
scenario:
  name: BBox
  region: "{region}"
layers:
  - id: a
    source: file
    path: data/a.geojson
    data_type: vector
steps:
  - id: p
    op: buffer
    inputs: {{geometry: a}}
    params: {{radius_km: 1}}
output:
  - type: geojson
    source: p
"""


def _load(region: str):
    return api.load_scenario(TEMPLATE.format(region=region))


REJECTED = [
    (
        "nan,12,51,13",
        "BBox 'nan,12,51,13': alle vier Werte müssen endliche Zahlen sein",
    ),
    ("-200,-100,200,100", "BBox '-200,-100,200,100': Werte außerhalb des Bereichs"),
    (
        "13,51.4,14,51.1",
        "BBox '13,51.4,14,51.1': maxlat (51.1) ist kleiner als minlat (51.4)",
    ),
    (
        "177,-19,-178,-16",
        "minlon > maxlon - Regionen über den Antimeridian werden nicht unterstützt",
    ),
    ("13,51,13,51", "BBox '13,51,13,51' hat keine Ausdehnung"),
]


@pytest.mark.parametrize(("region", "message"), REJECTED, ids=[r for r, _ in REJECTED])
def test_fa56_check_bbox_rejects(region, message):
    values = parse_bbox_literal(region)
    assert values is not None
    with pytest.raises(ValueError) as exc:
        check_bbox(values, text=region)
    assert message in str(exc.value)


@pytest.mark.parametrize(("region", "message"), REJECTED, ids=[r for r, _ in REJECTED])
def test_fa56_scenario_region_rejected_at_validate(region, message):
    with pytest.raises(ConfigError) as exc:
        _load(region)
    assert [loc for loc, _ in exc.value.issues] == ["scenario -> region"]
    assert message in exc.value.issues[0][1]


def test_fa56_check_bbox_formats_values_without_text():
    with pytest.raises(ValueError, match=r"BBox 'inf,12,51,13'"):
        check_bbox((float("inf"), 12.0, 51.0, 13.0))


def test_fa56_valid_fa3_literal_is_unchanged():
    region = "12.2292,51.2378,12.5467,51.4501"
    assert _load(region).scenario.scenario.region == region
    check_bbox(parse_bbox_literal(region))


def test_fa56_place_name_is_not_a_bbox():
    assert parse_bbox_literal("Leipzig, Deutschland") is None
    assert parse_bbox_literal("1,2,3") is None
    assert (
        _load("Leipzig, Deutschland").scenario.scenario.region == "Leipzig, Deutschland"
    )
