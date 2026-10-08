"""Implements: FA46-FA49 (Inventar der Raster-Bausteine), FA40 (Registrierung), FA45 (Schichtenregel).

Golden entries of the blocks added by the satellite raster feature (R), next to the
golden list of `test_fa40_registration_inventory.py` (which keeps its fixed count of
52 pre-restructuring blocks and may only grow in its own lines): a missing block, a
changed contract or a module that lost its `Implements: FAx` header fails here.
"""

from __future__ import annotations

import importlib

import pytest

from geofact.core.registry import default_registry
from geofact.engine import catalog

ADDED_INVENTORY = frozenset(
    {
        ("source", "stac"),
        ("operation", "raster_calc"),
        ("operation", "raster_classify"),
        ("output", "geotiff"),
    }
)

ADDED_OPERATION_CONTRACTS = {
    "raster_calc": ({"raster": "raster"}, "raster", ["expression"]),
    "raster_classify": ({"raster": "raster"}, "raster", ["breaks", "values"]),
}

ORIGINS = {
    ("source", "stac"): ("geofact.builtin.sources.stac", "FA46"),
    ("operation", "raster_calc"): ("geofact.builtin.operations.raster_calc", "FA47"),
    ("operation", "raster_classify"): (
        "geofact.builtin.operations.raster_classify",
        "FA48",
    ),
    ("output", "geotiff"): ("geofact.builtin.outputs.geotiff", "FA49"),
}


def _inventory() -> set[tuple[str, str]]:
    listing = catalog.extension_catalog()
    keys = {"sources": "source", "operations": "operation", "outputs": "output"}
    return {
        (kind, entry["name"]) for key, kind in keys.items() for entry in listing[key]
    }


def test_fa46_every_raster_block_is_registered():
    assert not sorted(ADDED_INVENTORY - _inventory())


def test_fa46_raster_operations_keep_their_contracts():
    actual = {
        entry["op"]: (
            entry["input_ports"],
            entry["output_type"],
            entry["required_params"],
        )
        for entry in catalog.operation_catalog()
    }
    for op, expected in ADDED_OPERATION_CONTRACTS.items():
        assert actual[op] == expected, op


def test_fa46_zonal_stats_keeps_its_ports_and_has_no_new_required_parameter():
    """Extended by share/min/count/std, output_field, band: old configurations stay valid."""
    entry = {e["op"]: e for e in catalog.operation_catalog()}["zonal_stats"]
    assert entry["input_ports"] == {"zones": "vector", "values": "raster"}
    assert entry["output_type"] == "vector" and entry["required_params"] == []


@pytest.mark.parametrize("block", sorted(ORIGINS))
def test_fa46_block_lives_in_its_package_and_names_its_requirement(block):
    kind, name = block
    module_name, requirement = ORIGINS[block]
    registry = default_registry()
    entry = {
        "source": registry.source,
        "operation": registry.operation,
        "output": registry.output,
    }[kind](name)
    assert entry.origin == module_name
    docstring = importlib.import_module(module_name).__doc__ or ""
    assert docstring.startswith(f"Implements: {requirement}"), docstring.splitlines()[0]


def test_fa46_stac_contract_lives_with_its_loader():
    entry = default_registry().source("stac")
    assert (
        entry.layer_model.__module__
        == entry.load.__module__
        == "geofact.builtin.sources.stac"
    )
    assert (
        entry.transform == "reproject"
    )  # FA5: the standard harmonisation (with the layer's resampling)


def test_fa46_map_accepts_vector_and_raster_and_geotiff_only_raster():
    """Since FA53 map, geojson and csv also take a graph; a raster only goes to map and geotiff."""
    outputs = {o["name"]: o["accepts"] for o in catalog.extension_catalog()["outputs"]}
    assert outputs["map"] == ["graph", "raster", "vector"] and outputs["geotiff"] == [
        "raster"
    ]
    assert outputs["geojson"] == ["graph", "vector"] and outputs["csv"] == [
        "graph",
        "vector",
    ]


def test_fa46_helper_modules_are_not_scanned_as_blocks():
    from geofact.engine import discovery

    found = discovery._module_names("geofact.builtin")
    assert "geofact.builtin._raster_io" not in found
    assert "geofact.builtin.sources.stac" in found
