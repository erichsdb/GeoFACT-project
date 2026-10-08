"""Implements: FA71 (density-based clustering of points: cluster).

Contract tests of `cluster` (builtin/operations/cluster.py): DBSCAN in the
projected working CRS without a new dependency. Happy path (two clusters and
noise), border points, type error (raster on the vector port), empty layer,
geographic CRS, non-point input via centroid with one warning, parameter
bounds and catalog visibility.
"""

from __future__ import annotations

import warnings

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from geofact import api
from geofact.builtin.operations.cluster import ClusterStep, run_cluster
from geofact.core.types import DataType

CRS = "EPSG:32633"


def _points(coords, crs=CRS, **columns) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": list(range(len(coords))), **columns},
        geometry=[Point(x, y) for x, y in coords],
        crs=crs,
    )


def _two_clusters_and_noise() -> gpd.GeoDataFrame:
    # Cluster A: 4 points around (0, 0); cluster B: 3 points around (1000, 1000);
    # two isolated points far away (noise).
    coords = [
        (0, 0),
        (10, 0),
        (0, 10),
        (10, 10),
        (5000, 5000),
        (1000, 1000),
        (1010, 1000),
        (1000, 1010),
        (-5000, 3000),
    ]
    return _points(coords)


# =====================================================================
# Happy path
# =====================================================================


def test_fa71_cluster_finds_two_clusters_and_noise():
    gdf = _two_clusters_and_noise()
    result = run_cluster({"features": gdf}, {"eps_m": 20, "min_samples": 3})
    labels = result["cluster_id"].tolist()
    assert labels == [0, 0, 0, 0, -1, 1, 1, 1, -1]
    assert str(result["cluster_id"].dtype) == "int64"


def test_fa71_cluster_keeps_rows_order_geometry_and_attributes():
    gdf = _two_clusters_and_noise()
    result = run_cluster({"features": gdf}, {"eps_m": 20, "min_samples": 3})
    assert len(result) == len(gdf)
    assert result["id"].tolist() == gdf["id"].tolist()
    assert result.geometry.equals(gdf.geometry)
    assert result.crs == gdf.crs
    assert "cluster_id" not in gdf.columns  # input not modified


def test_fa71_cluster_size_field_is_optional():
    gdf = _two_clusters_and_noise()
    result = run_cluster(
        {"features": gdf}, {"eps_m": 20, "min_samples": 3, "size_field": "cluster_size"}
    )
    assert result["cluster_size"].tolist() == [4, 4, 4, 4, 0, 3, 3, 3, 0]
    plain = run_cluster({"features": gdf}, {"eps_m": 20, "min_samples": 3})
    assert "cluster_size" not in plain.columns


def test_fa71_cluster_border_point_joins_cluster_but_does_not_extend_it():
    # Core point C=(0,0) has the neighbours B1, B2, B3 (eps 6, 4 incl. itself).
    # B1..B3 have fewer than 4 neighbours -> border points of C's cluster.
    # X=(10,0) is within eps of the border point B1 only -> noise: border
    # points never extend a cluster.
    gdf = _points([(0, 0), (5, 0), (-5, 0), (0, 5), (10, 0)])
    result = run_cluster({"features": gdf}, {"eps_m": 6, "min_samples": 4})
    assert result["cluster_id"].tolist() == [0, 0, 0, 0, -1]


def test_fa71_cluster_border_point_joins_nearest_core_point():
    # Cores L=(0,0) and R=(9,0) are 9 m apart (eps 5, not neighbours). The
    # border point B=(4.8,0) has only 3 neighbours (itself, L, R) and lies
    # 4.8 m from L and 4.2 m from R: it joins R's cluster (nearest core),
    # although L's cluster comes first.
    left = [(0, 0), (-3, 0), (0, 3), (0, -3)]
    right = [(9, 0), (12, 0), (9, 3), (9, -3)]
    gdf = _points(left + [(4.8, 0)] + right)
    result = run_cluster({"features": gdf}, {"eps_m": 5, "min_samples": 4})
    assert result["cluster_id"].tolist() == [0, 0, 0, 0, 1, 1, 1, 1, 1]


def test_fa71_cluster_distance_equal_to_eps_counts_as_neighbour():
    gdf = _points([(0, 0), (10, 0)])
    result = run_cluster({"features": gdf}, {"eps_m": 10, "min_samples": 2})
    assert result["cluster_id"].tolist() == [0, 0]


def test_fa71_cluster_min_samples_one_makes_every_point_a_cluster():
    gdf = _points([(0, 0), (1000, 0), (2000, 0)])
    result = run_cluster({"features": gdf}, {"eps_m": 5, "min_samples": 1})
    assert result["cluster_id"].tolist() == [0, 1, 2]


def test_fa71_cluster_custom_output_field():
    gdf = _two_clusters_and_noise()
    result = run_cluster(
        {"features": gdf}, {"eps_m": 20, "min_samples": 3, "output_field": "gruppe"}
    )
    assert "gruppe" in result.columns and "cluster_id" not in result.columns


def test_fa71_cluster_non_points_use_centroid_with_one_warning():
    square = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])  # centroid (5, 5)
    line = LineString([(0, 10), (10, 10)])  # centroid (5, 10)
    gdf = gpd.GeoDataFrame(
        {"id": [0, 1, 2]},
        geometry=[square, line, Point(5, 0)],
        crs=CRS,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_cluster({"features": gdf}, {"eps_m": 6, "min_samples": 2})
    messages = [str(w.message) for w in caught]
    assert len(messages) == 1
    assert "2" in messages[0] and "Zentroid" in messages[0]
    assert result["cluster_id"].tolist() == [0, 0, 0]
    assert result.geometry.equals(gdf.geometry)  # original geometry kept


def test_fa71_cluster_missing_geometry_is_noise_with_warning():
    gdf = gpd.GeoDataFrame(
        {"id": [0, 1, 2]}, geometry=[Point(0, 0), None, Point(1, 0)], crs=CRS
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_cluster({"features": gdf}, {"eps_m": 5, "min_samples": 2})
    assert result["cluster_id"].tolist() == [0, -1, 0]
    assert any("ohne Geometrie" in str(w.message) for w in caught)


# =====================================================================
# Type errors and preconditions
# =====================================================================


def test_fa71_cluster_contract_ports_and_types():
    assert ClusterStep.INPUT_PORTS == {"features": DataType.VECTOR}
    assert ClusterStep.OUTPUT_TYPE == DataType.VECTOR


def _scenario(layer: dict) -> dict:
    return {
        "scenario": {"name": "t", "region": "13.0,50.9,13.1,51.0"},
        "layers": [layer],
        "steps": [
            {
                "id": "c",
                "op": "cluster",
                "inputs": {"features": layer["id"]},
                "params": {"eps_m": 100, "min_samples": 3},
            }
        ],
        "output": [{"type": "geojson", "source": "c"}],
    }


def test_fa71_cluster_raster_input_is_a_type_error_at_config_time():
    issues = api.validate(
        _scenario(
            {
                "id": "r",
                "source": "file",
                "path": "x.tif",
                "data_type": "raster",
                "format": "tif",
            }
        )
    ).issues
    assert issues and issues[0][0] == "steps -> 0 -> inputs -> features"
    assert "vector" in issues[0][1]


def test_fa71_cluster_valid_scenario_validates():
    assert api.validate(
        _scenario(
            {"id": "v", "source": "file", "path": "x.geojson", "data_type": "vector"}
        )
    ).valid


def test_fa71_cluster_geographic_crs_is_an_error():
    gdf = _points([(13.0, 50.9), (13.0001, 50.9)], crs="EPSG:4326")
    with pytest.raises(ValueError, match="kein projiziertes CRS"):
        run_cluster({"features": gdf}, {"eps_m": 20, "min_samples": 2})


def test_fa71_cluster_missing_crs_is_an_error():
    gdf = _points([(0, 0), (1, 0)], crs=None)
    with pytest.raises(ValueError, match="kein CRS"):
        run_cluster({"features": gdf}, {"eps_m": 20, "min_samples": 2})


def test_fa71_cluster_existing_output_field_is_an_error():
    gdf = _points([(0, 0), (1, 0)], cluster_id=[7, 8])
    with pytest.raises(ValueError, match="cluster_id"):
        run_cluster({"features": gdf}, {"eps_m": 20, "min_samples": 2})


@pytest.mark.parametrize(
    "params, field",
    [
        ({"eps_m": 0, "min_samples": 3}, "eps_m"),
        ({"eps_m": -5, "min_samples": 3}, "eps_m"),
        ({"eps_m": 10, "min_samples": 0}, "min_samples"),
        ({"min_samples": 3}, "eps_m"),
        ({"eps_m": 10}, "min_samples"),
        ({"eps_m": 10, "min_samples": 3, "output_field": "1 bad"}, "output_field"),
        ({"eps_m": 10, "min_samples": 3, "size_field": "cluster_id"}, "size_field"),
    ],
)
def test_fa71_cluster_params_are_validated(params, field):
    with pytest.raises(ValueError, match=field):
        ClusterStep(id="c", op="cluster", inputs={"features": "a"}, params=params)


# =====================================================================
# Edge case: empty layer
# =====================================================================


def test_fa71_cluster_empty_layer():
    empty = gpd.GeoDataFrame({"id": []}, geometry=[], crs=CRS)
    result = run_cluster(
        {"features": empty}, {"eps_m": 20, "min_samples": 3, "size_field": "n"}
    )
    assert len(result) == 0
    assert "cluster_id" in result.columns and "n" in result.columns


# =====================================================================
# Catalog visibility
# =====================================================================


def test_fa71_cluster_is_visible_in_the_catalog():
    entry = {e["op"]: e for e in api.operation_catalog()}["cluster"]
    assert entry["input_ports"] == {"features": "vector"}
    assert entry["output_type"] == "vector"
    assert entry["required_params"] == ["eps_m", "min_samples"]
    assert entry["params"]["output_field"]["default"] == "cluster_id"


def test_fa71_cluster_produced_fields():
    step = ClusterStep(
        id="c",
        op="cluster",
        inputs={"features": "a"},
        params={"eps_m": 10, "min_samples": 2, "size_field": "n"},
    )
    assert step.produced_fields() == {"cluster_id", "n"}
