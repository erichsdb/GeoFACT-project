"""Implements: FA55 (Feld FileLayer.crs_override; Auswertung in reproject kommt mit W2-01).

``crs_override`` ist standardmäßig false; ``crs_override: true`` ohne
``crs`` ist ein Konfigurationsfehler mit Position.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from geofact import api
from geofact.builtin.sources.file import FileLayer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SUBSTATIONS = str(FIXTURES / "substations.geojson")


def test_fa55_crs_override_defaults_to_false():
    layer = FileLayer(id="a", path=SUBSTATIONS, data_type="vector")
    assert layer.crs_override is False
    forced = FileLayer(
        id="a",
        path=SUBSTATIONS,
        data_type="vector",
        crs="EPSG:25833",
        crs_override=True,
    )
    assert forced.crs_override is True and forced.crs == "EPSG:25833"


def test_fa55_crs_override_without_crs_is_rejected_with_position():
    with pytest.raises(ValidationError) as caught:
        FileLayer(id="a", path=SUBSTATIONS, data_type="vector", crs_override=True)
    errors = caught.value.errors()
    assert errors[0]["loc"][-1] == "crs_override"
    assert "crs_override: true braucht eine crs-Angabe" in errors[0]["msg"]


def test_fa55_crs_override_without_crs_scenario_position():
    config = {
        "scenario": {"name": "t", "region": "13.7,51.0,13.8,51.1"},
        "layers": [
            {
                "id": "a",
                "source": "file",
                "path": SUBSTATIONS,
                "data_type": "vector",
                "crs_override": True,
            }
        ],
        "steps": [],
        "output": [],
    }
    with pytest.raises(api.ConfigError) as caught:
        api.load_scenario(config)
    assert "crs_override" in str(caught.value)
    assert "layers" in str(caught.value)
