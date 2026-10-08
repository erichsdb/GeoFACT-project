"""Implements: FA8 (räumliche Operationen, Erweiterung hexbin).

Contract-Tests für src/geofact/builtin/operations/hexbin.py: Happy Path,
Typfehler-Fall, Randfall leerer Layer (Testregel für Operationen) plus
Schema-Validierung des HexbinStep.
"""

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, Polygon

from geofact.builtin.operations import hexbin as density
from geofact.builtin.operations.hexbin import HexbinStep


def empty_points() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")


# =====================================================================
# Happy Path
# =====================================================================


def test_fa08_hexbin_counts_cover_all_features():
    """Nachbedingung: jedes Objekt landet in genau einer Zelle -
    die Summe aller counts entspricht der Objektanzahl."""
    rng = np.random.default_rng(42)
    xs = rng.uniform(0, 5000, 500)
    ys = rng.uniform(0, 5000, 500)
    gdf = gpd.GeoDataFrame(
        {"id": range(500)},
        geometry=[Point(x, y) for x, y in zip(xs, ys)],
        crs="EPSG:32633",
    )
    result = density.run_hexbin({"features": gdf}, {"cell_km": 1})
    assert result["count"].sum() == 500
    assert (result.geometry.geom_type == "Polygon").all()
    assert result.crs == gdf.crs


def test_fa08_hexbin_dense_cluster_lands_in_one_cell():
    cluster = gpd.GeoDataFrame(
        {"id": range(10)},
        geometry=[Point(100 + i, 100 + i) for i in range(10)],
        crs="EPSG:32633",
    )
    result = density.run_hexbin({"features": cluster}, {"cell_km": 1})
    assert len(result) == 1
    assert result["count"].iloc[0] == 10


def test_fa08_hexbin_min_count_filters_sparse_cells():
    gdf = gpd.GeoDataFrame(
        {"id": [1, 2, 3]},
        # Zwei Punkte dicht beieinander, einer weit entfernt.
        geometry=[Point(0, 0), Point(10, 10), Point(50_000, 50_000)],
        crs="EPSG:32633",
    )
    result = density.run_hexbin({"features": gdf}, {"cell_km": 1, "min_count": 2})
    assert len(result) == 1
    assert result["count"].iloc[0] == 2


def test_fa08_hexbin_polygons_counted_via_representative_point():
    gdf = gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])],
        crs="EPSG:32633",
    )
    result = density.run_hexbin({"features": gdf}, {"cell_km": 1})
    assert result["count"].sum() == 1


# =====================================================================
# Typfehler-Fälle (Schema-Validierung)
# =====================================================================


def test_fa08_hexbin_step_rejects_zero_cell():
    with pytest.raises(ValueError, match="cell_km"):
        HexbinStep(id="s", op="hexbin", inputs={"features": "a"}, params={"cell_km": 0})


def test_fa08_hexbin_step_rejects_missing_cell_km():
    with pytest.raises(ValueError, match="cell_km"):
        HexbinStep(id="s", op="hexbin", inputs={"features": "a"}, params={})


def test_fa08_hexbin_step_rejects_negative_min_count():
    with pytest.raises(ValueError, match="min_count"):
        HexbinStep(
            id="s",
            op="hexbin",
            inputs={"features": "a"},
            params={"cell_km": 1, "min_count": -1},
        )


# =====================================================================
# Randfall leerer Layer
# =====================================================================


def test_fa08_hexbin_empty_layer():
    result = density.run_hexbin({"features": empty_points()}, {"cell_km": 1})
    assert len(result) == 0
    assert "count" in result.columns
