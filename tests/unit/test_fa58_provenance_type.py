"""Implements: FA58 (Datentyp LayerProvenance).

Contract-Tests fuer ``core/types.py::LayerProvenance``: unveraenderlich,
``to_dict`` JSON-tauglich, ``from_dict(to_dict())`` verlustfrei; Erfassung in
der Ladestrecke folgt in Welle 2 (W2-04)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from geofact import api, plugin_api
from geofact.core.types import LayerProvenance


def _provenance(**changes) -> LayerProvenance:
    values = dict(
        layer="baeume",
        source="file",
        source_crs="EPSG:31468",
        source_crs_label="EPSG:31468 (DHDN / 3-degree Gauss-Kruger zone 4)",
        crs_override=True,
        target_crs="EPSG:25833",
        target_crs_label="EPSG:25833 (ETRS89 / UTM zone 33N)",
        raw_bounds=(4520000.0, 5680000.0, 4540000.0, 5700000.0),
        bounds=(310000.0, 5680000.0, 330000.0, 5700000.0),
        feature_count=3,
        raw_sample=[(4520000.0, 5680000.0), (4530000.5, 5690000.25)],
        sample=[(310000.0, 5680000.0), (320000.5, 5690000.25)],
    )
    values.update(changes)
    return LayerProvenance(**values)


def test_fa58_provenance_to_dict_round_trip():
    provenance = _provenance()
    raw = provenance.to_dict()
    assert json.loads(json.dumps(raw)) == raw
    assert raw["raw_bounds"] == [4520000.0, 5680000.0, 4540000.0, 5700000.0]
    assert raw["sample"][1] == [320000.5, 5690000.25]
    assert LayerProvenance.from_dict(raw) == provenance


def test_fa58_provenance_without_geometry_round_trips():
    provenance = _provenance(
        source="table",
        source_crs=None,
        source_crs_label="ohne CRS",
        raw_bounds=None,
        bounds=None,
        feature_count=None,
        raw_sample=[],
        sample=[],
    )
    raw = provenance.to_dict()
    assert (
        raw["source_crs"] is None and raw["raw_bounds"] is None and raw["sample"] == []
    )
    assert LayerProvenance.from_dict(raw) == provenance


def test_fa58_provenance_shapes_stay_out_of_to_dict():
    shapes = (("point", (((310000.0, 5680000.0),),)),)
    provenance = _provenance(shapes=shapes, raw_shapes=shapes)
    raw = provenance.to_dict()
    assert "shapes" not in raw and "raw_shapes" not in raw
    restored = LayerProvenance.from_dict(raw)
    assert restored.shapes == () and restored.raw_shapes == ()
    assert restored == _provenance()


def test_fa58_provenance_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _provenance().layer = "x"  # type: ignore[misc]


def test_fa58_bounds_need_four_values():
    raw = _provenance().to_dict()
    raw["bounds"] = [1.0, 2.0]
    with pytest.raises(ValueError, match="vier Werte"):
        LayerProvenance.from_dict(raw)


def test_fa58_types_are_reexported_by_both_facades():
    from geofact.core import contracts, crs, provenance, types

    for facade in (api, plugin_api):
        assert facade.LayerProvenance is types.LayerProvenance
        assert facade.License is contracts.License
        assert facade.NodeAttribution is provenance.NodeAttribution
        assert facade.crs_label is crs.crs_label
        assert facade.CRS_PRESETS is crs.CRS_PRESETS
        for name in (
            "LayerProvenance",
            "License",
            "NodeAttribution",
            "crs_label",
            "CRS_PRESETS",
        ):
            assert name in facade.__all__
