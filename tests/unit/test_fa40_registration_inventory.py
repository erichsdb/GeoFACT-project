"""Implements: FA40 (inventory of all registered blocks as a safety net).

Golden list of every extension point as the core reports it BEFORE the
restructuring (architecture B, docs/architektur.md):
sources, file formats, table formats, transforms, operations and output
formats. The restructuring moves contracts, registration and discovery
(one registry, one loader, blocks as files of their own) and must not lose
a single block on the way. Later stages may ADD blocks; they leave the
golden list untouched and only add their new names next to it (subset
comparison).

`_inventory()` is the only place that knows where the inventory is read
from: it reads the catalog (`extension_catalog()`), because the catalog keeps
the same shape in every stage. When the catalog moves (engine/catalog.py,
geofact.api), only that one function is adapted, never the golden list.
"""

from __future__ import annotations

import pytest

from geofact.engine import catalog

# (kind, name) exactly as the pre-restructuring state reports them.
GOLDEN_INVENTORY: frozenset[tuple[str, str]] = frozenset(
    [
        ("source", name)
        for name in (
            "ckan",
            "file",
            "gtfs",
            "osm",
            "region",
            "rest",
            "table",
            "wfs",
        )
    ]
    + [("file_format", name) for name in ("geojson", "gml", "gpkg", "shp", "tif")]
    + [("table_format", name) for name in ("csv", "fwf", "sql", "sqlite", "xlsx")]
    + [("transform", name) for name in ("none", "reproject")]
    + [
        ("operation", name)
        for name in (
            "aggregate",
            "attribute_join",
            "buffer",
            "build_network",
            "centrality",
            "classify",
            "clip",
            "dissolve",
            "filter",
            "graph_to_vector",
            "hex_grid",
            "hexbin",
            "homogenize_geometry",
            "isochrone",
            "nearest_distance",
            "network_nodes",
            "overlay",
            "power_grid_topology",
            "ranking",
            "repair_geometry",
            "rest",
            "shortest_path",
            "spatial_join",
            "to_points",
            "top_n",
            "vector_union",
            "zonal_stats",
        )
    ]
    + [("output", name) for name in ("csv", "geojson", "map", "rdf", "rest")]
)

# Contract of every operation (input ports, output type, required params) as
# the catalog hands it to the web editor and the LLM grounding. The params
# rework (Params models instead of REQUIRED_PARAMS) must keep this shape.
GOLDEN_OPERATION_CONTRACTS: dict[str, tuple[dict[str, str], str, list[str]]] = {
    "aggregate": ({"features": "vector", "zones": "vector"}, "vector", []),
    "attribute_join": ({"left": "vector", "right": "vector"}, "vector", ["left_on"]),
    "buffer": ({"geometry": "vector"}, "vector", ["radius_km"]),
    "build_network": ({"lines": "vector", "nodes": "vector"}, "graph", []),
    "centrality": ({"graph": "graph"}, "graph", []),
    "classify": ({"features": "vector"}, "vector", ["breaks", "field", "labels"]),
    "clip": ({"features": "vector", "mask": "vector"}, "vector", []),
    "dissolve": ({"features": "vector"}, "vector", []),
    "filter": ({"features": "vector"}, "vector", ["condition"]),
    "graph_to_vector": ({"graph": "graph"}, "vector", []),
    "hex_grid": ({"region": "vector"}, "vector", ["cell_km"]),
    "hexbin": ({"features": "vector"}, "vector", ["cell_km"]),
    "homogenize_geometry": ({"features": "vector"}, "vector", []),
    "isochrone": (
        {"graph": "graph", "origins": "vector"},
        "vector",
        ["breaks_m", "max_snap_m"],
    ),
    "nearest_distance": ({"from": "vector", "to": "vector"}, "vector", []),
    "network_nodes": ({"lines": "vector"}, "vector", []),
    "overlay": ({"left": "vector", "right": "vector"}, "vector", ["method"]),
    "power_grid_topology": (
        {"substations": "vector", "power_lines": "vector"},
        "graph",
        ["min_voltage"],
    ),
    "ranking": ({"features": "vector"}, "vector", ["by", "weights"]),
    "repair_geometry": ({"features": "vector"}, "vector", []),
    "rest": ({}, "vector", ["input_types", "url"]),
    "shortest_path": (
        {"graph": "graph", "origins": "vector", "destinations": "vector"},
        "vector",
        ["max_snap_m"],
    ),
    "spatial_join": ({"left": "vector", "right": "vector"}, "vector", ["predicate"]),
    "to_points": ({"features": "vector"}, "vector", []),
    "top_n": ({"features": "vector"}, "vector", ["by", "n"]),
    "vector_union": ({"a": "vector", "b": "vector"}, "vector", []),
    "zonal_stats": ({"zones": "vector", "values": "raster"}, "vector", []),
}

# Key of extension_catalog() -> kind in the inventory.
_CATALOG_KINDS = {
    "sources": "source",
    "file_formats": "file_format",
    "table_formats": "table_format",
    "transforms": "transform",
    "operations": "operation",
    "outputs": "output",
}


def _inventory() -> set[tuple[str, str]]:
    """All registered (kind, name) pairs."""
    listing = catalog.extension_catalog()
    return {
        (kind, entry["name"])
        for key, kind in _CATALOG_KINDS.items()
        for entry in listing[key]
    }


def _operation_contracts() -> dict[str, tuple[dict[str, str], str, list[str]]]:
    return {
        entry["op"]: (
            entry["input_ports"],
            entry["output_type"],
            entry["required_params"],
        )
        for entry in catalog.operation_catalog()
    }


def test_fa40_golden_inventory_covers_all_six_kinds():
    """The golden list itself is complete: all six kinds, 52 entries (8
    sources, 5 file formats, 5 table formats, 2 transforms, 27 operations,
    5 outputs). It may only grow, never shrink."""
    kinds = {kind for kind, _ in GOLDEN_INVENTORY}
    assert kinds == {
        "source",
        "file_format",
        "table_format",
        "transform",
        "operation",
        "output",
    }
    assert len(GOLDEN_INVENTORY) == 52
    assert len(GOLDEN_OPERATION_CONTRACTS) == 27
    assert {name for kind, name in GOLDEN_INVENTORY if kind == "operation"} == set(
        GOLDEN_OPERATION_CONTRACTS
    )


def test_fa40_every_golden_block_is_still_registered():
    """Postcondition of the restructuring: no block is lost. New blocks are
    allowed (subset comparison), a missing one is an error."""
    missing = sorted(GOLDEN_INVENTORY - _inventory())
    assert not missing, (
        f"blocks from the golden list are no longer registered (kind, name): {missing}"
    )


def test_fa40_every_golden_operation_keeps_its_contract():
    """Input ports, output type and required params of every operation stay
    unchanged (the web editor and the LLM grounding read exactly this shape)."""
    actual = _operation_contracts()
    changed = {
        op: {"expected": expected, "actual": actual.get(op)}
        for op, expected in sorted(GOLDEN_OPERATION_CONTRACTS.items())
        if actual.get(op) != expected
    }
    assert not changed, f"operation contracts differ from the golden list: {changed}"


def test_fa40_inventory_has_no_duplicate_names_per_kind():
    """Edge case: per kind every name is listed exactly once (one directory
    per kind, no entry listed twice)."""
    listing = catalog.extension_catalog()
    for key in _CATALOG_KINDS:
        names = [entry["name"] for entry in listing[key]]
        assert len(names) == len(set(names)), (
            f"{key}: duplicate names in {sorted(names)}"
        )


@pytest.mark.parametrize("kind", sorted({kind for kind, _ in GOLDEN_INVENTORY}))
def test_fa40_inventory_reports_origin_for_every_entry(kind):
    """Every entry names its origin (module or plugin file); the visibility of
    `geofact plugins` must survive the restructuring."""
    key = next(k for k, v in _CATALOG_KINDS.items() if v == kind)
    for entry in catalog.extension_catalog()[key]:
        assert entry["origin"], f"{kind} '{entry['name']}' has no origin"


# ---------------------------------------------------------------------------
# Blocks added after the restructuring (feature stages). The golden list above
# stays untouched; every feature adds its new names here (subset comparison).
# ---------------------------------------------------------------------------

# FA50-FA53 (graphs and routing): line_network, route, geocode/points, graph outputs.
# FA46-FA49 (satellite raster analysis): stac, raster_calc, raster_classify, geotiff.
ADDED_INVENTORY: frozenset[tuple[str, str]] = frozenset(
    [
        ("operation", "line_network"),
        ("operation", "route"),
        ("source", "geocode"),
        ("source", "points"),
        ("source", "stac"),
        ("operation", "raster_calc"),
        ("operation", "raster_classify"),
        ("output", "geotiff"),
        ("operation", "area"),  # W1-02, FA68
        ("operation", "length"),  # W1-02, FA68
        ("operation", "calculate_field"),  # W1-02, FA68
        ("operation", "select_columns"),  # W1-02, FA69
        ("operation", "deduplicate"),  # W1-02, FA69
        ("operation", "area_share"),  # W1-02, FA70
        ("operation", "collect"),  # W1-03 (FA60)
        ("operation", "gate"),  # W1-03 (FA62)
        # FA70 (W1-05): rasterize
        ("operation", "rasterize"),
        ("operation", "cluster"),  # FA71
        ("operation", "edge_list_network"),  # FA76
        ("output", "crs_plot"),  # W2-05 (FA58)
    ]
)

ADDED_OPERATION_CONTRACTS: dict[str, tuple[dict[str, str], str, list[str]]] = {
    "raster_calc": ({"raster": "raster"}, "raster", ["expression"]),
    "raster_classify": ({"raster": "raster"}, "raster", ["breaks", "values"]),
    "rasterize": ({"features": "vector"}, "raster", ["cell_size_m"]),
    "cluster": ({"features": "vector"}, "vector", ["eps_m", "min_samples"]),  # FA71
    "edge_list_network": (
        {"nodes": "vector", "edges": "vector"},
        "graph",
        ["from_column", "node_key", "to_column"],
    ),  # FA76
    "line_network": ({"lines": "vector"}, "graph", []),
    "route": (
        {"graph": "graph", "origins": "vector", "destinations": "vector"},
        "vector",
        ["max_snap_m"],
    ),
    "collect": ({}, "vector", []),  # W1-03 (FA60): ports come from the step's inputs
    "gate": ({"features": "vector"}, "vector", []),  # W1-03 (FA62)
}


def test_fa50_added_blocks_are_registered():
    missing = sorted(ADDED_INVENTORY - _inventory())
    assert not missing, (
        f"blocks added by the feature stages are not registered (kind, name): {missing}"
    )


def test_fa50_added_operations_have_their_contract():
    actual = _operation_contracts()
    changed = {
        op: {"expected": expected, "actual": actual.get(op)}
        for op, expected in sorted(ADDED_OPERATION_CONTRACTS.items())
        if actual.get(op) != expected
    }
    assert not changed, f"contracts of added operations differ: {changed}"
