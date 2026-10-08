"""Implements: FA70 (Flächenanteil: area_share).

Contract-Tests für src/geofact/builtin/operations/area_share.py. Der Rasterteil
von FA70 (rasterize) gehört zu W1-05 und hat eigene Tests.
"""

from __future__ import annotations

import math
import warnings
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon, box

from geofact import api
from geofact.builtin.operations.area_share import AreaShareStep, run_area_share
from geofact.core.scenario import Scenario
from geofact.core.types import DataType

UTM = "EPSG:25833"
BBOX_REGION = "12.8,50.7,13.1,50.9"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def gdf(geoms, crs=UTM, **columns) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(columns, geometry=list(geoms), crs=crs)


def share(zones, features, **params):
    full = AreaShareStep.Params(**params).model_dump()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = run_area_share({"zones": zones, "features": features}, full)
    return result, [str(w.message) for w in caught]


def two_zones() -> gpd.GeoDataFrame:
    return gdf([box(0, 0, 100, 100), box(100, 0, 200, 100)], name=["West", "Ost"])


# =====================================================================
# Happy Path
# =====================================================================


def test_fa70_area_share_is_covered_fraction_per_zone():
    features = gdf([box(0, 0, 50, 100)])  # halbe Westzone
    result, _ = share(two_zones(), features)
    assert result["area_share"].tolist() == [0.5, 0.0]


def test_fa70_area_share_features_are_clipped_at_zone_border():
    features = gdf([box(50, 0, 150, 100)])  # je halbe Zone
    result, _ = share(two_zones(), features, absolute_field="gruen_m2")
    assert result["area_share"].tolist() == [0.5, 0.5]
    assert result["gruen_m2"].tolist() == pytest.approx([5000.0, 5000.0])


def test_fa70_area_share_overlapping_features_count_once():
    """G8: überlappende Features werden mit dissolve einmal gezählt - nie > 1."""
    features = gdf([box(0, 0, 80, 100), box(20, 0, 100, 100), box(0, 0, 100, 100)])
    result, _ = share(two_zones(), features)
    assert result["area_share"].iloc[0] == 1.0
    partial = gdf([box(0, 0, 60, 100), box(40, 0, 80, 100)])
    result, _ = share(two_zones(), partial)
    assert result["area_share"].iloc[0] == 0.8


def test_fa70_area_share_without_dissolve_sums_overlaps():
    features = gdf([box(0, 0, 60, 100), box(40, 0, 80, 100)])
    result, _ = share(two_zones(), features, dissolve=False)
    assert result["area_share"].iloc[0] == 1.0  # 0.6 + 0.4, Überlappung doppelt


def test_fa70_area_share_decimals_and_output_field():
    features = gdf([box(0, 0, 100 / 3, 100)])
    result, _ = share(two_zones(), features, output_field="gruenanteil", decimals=2)
    assert result["gruenanteil"].iloc[0] == 0.33
    assert "area_share" not in result.columns


def test_fa70_area_share_keeps_zone_rows_order_and_input():
    zones = two_zones()
    result, _ = share(zones, gdf([box(0, 0, 10, 10)]))
    assert result["name"].tolist() == ["West", "Ost"]
    assert "area_share" not in zones.columns
    assert result.geometry.equals(zones.geometry)


def test_fa70_area_share_matches_manual_overlay_on_fixture():
    """Vergleich mit einem manuellen Overlay (union -> intersection -> area) auf
    den Park-Fixtures, mit Zonen, die park_a schneiden."""
    parks = gpd.read_file(FIXTURES / "parks.geojson").to_crs(UTM)
    minx, miny, maxx, maxy = parks.total_bounds
    mid_x = (minx + maxx) / 2
    zones = gdf(
        [
            box(minx - 50, miny - 50, mid_x, maxy + 50),
            box(mid_x, miny - 50, maxx + 50, maxy + 50),
            box(
                parks.geometry.iloc[0].centroid.x - 20,
                miny - 50,
                parks.geometry.iloc[0].centroid.x + 20,
                maxy + 50,
            ),
        ],
        zone=["a", "b", "streifen"],
    )
    result, _ = share(zones, parks, absolute_field="m2", decimals=None)
    dissolved = gpd.GeoDataFrame(geometry=[parks.union_all()], crs=UTM)
    manual = gpd.overlay(zones, dissolved, how="intersection")
    covered = manual.assign(m2=manual.geometry.area).groupby("zone")["m2"].sum()
    expected = [
        covered.get(z, 0.0) / zones.geometry.iloc[i].area
        for i, z in enumerate(zones["zone"])
    ]
    assert result["area_share"].tolist() == pytest.approx(expected, rel=1e-9)
    assert result["m2"].tolist() == pytest.approx(
        [covered.get(z, 0.0) for z in zones["zone"]]
    )
    assert 0 < result["area_share"].iloc[2] <= 1


# =====================================================================
# Randfälle
# =====================================================================


def test_fa70_area_share_empty_features_give_zero():
    result, _ = share(two_zones(), gdf([]))
    assert result["area_share"].tolist() == [0.0, 0.0]


def test_fa70_area_share_empty_zones_give_empty_result_with_columns():
    result, _ = share(gdf([]), gdf([box(0, 0, 1, 1)]), absolute_field="m2")
    assert result.empty
    assert {"area_share", "m2"} <= set(result.columns)


def test_fa70_area_share_zero_area_zone_is_nan_with_warning():
    zones = gdf([box(0, 0, 10, 10), Polygon([(0, 0), (1, 1), (2, 2), (0, 0)])])
    result, messages = share(zones, gdf([box(0, 0, 5, 10)]))
    assert result["area_share"].iloc[0] == 0.5
    assert math.isnan(result["area_share"].iloc[1])
    assert any("Fläche 0" in m for m in messages)


def test_fa70_area_share_non_polygon_features_are_skipped_with_one_warning():
    features = gdf([box(0, 0, 50, 100), Point(10, 10), LineString([(0, 0), (90, 90)])])
    result, messages = share(two_zones(), features)
    assert result["area_share"].tolist() == [0.5, 0.0]
    relevant = [m for m in messages if "keine Flächen" in m]
    assert len(relevant) == 1 and "2 Objekt(e)" in relevant[0]


def test_fa70_area_share_invalid_feature_is_repaired_and_reported():
    bowtie = Polygon([(0, 0), (100, 100), (100, 0), (0, 100), (0, 0)])
    result, messages = share(two_zones(), gdf([bowtie]))
    assert result["area_share"].iloc[0] == 0.5
    assert any("make_valid" in m for m in messages)


# =====================================================================
# Fehlerfälle
# =====================================================================


def test_fa70_area_share_line_zones_are_an_error():
    zones = gdf([LineString([(0, 0), (1, 1)])])
    with pytest.raises(ValueError, match="keine Flächen"):
        share(zones, gdf([box(0, 0, 1, 1)]))


@pytest.mark.parametrize("port", ["zones", "features"])
def test_fa70_area_share_requires_projected_crs(port):
    inputs = {"zones": two_zones(), "features": gdf([box(0, 0, 1, 1)])}
    inputs[port] = inputs[port].to_crs("EPSG:4326")
    with pytest.raises(ValueError, match=f"Eingang '{port}' hat kein projiziertes CRS"):
        run_area_share(inputs, AreaShareStep.Params().model_dump())


def test_fa70_area_share_crs_mismatch_is_an_error():
    features = gdf([box(0, 0, 1, 1)], crs="EPSG:25832")
    with pytest.raises(ValueError, match="verschiedene CRS"):
        share(two_zones(), features)


def test_fa70_area_share_existing_output_column_is_an_error():
    zones = two_zones().assign(area_share=[1, 2])
    with pytest.raises(ValueError, match="existiert bereits"):
        share(zones, gdf([box(0, 0, 1, 1)]))


def test_fa70_area_share_same_output_and_absolute_field_rejected():
    with pytest.raises(ValueError, match="verschieden"):
        AreaShareStep(
            id="s",
            inputs={"zones": "z", "features": "f"},
            params={"output_field": "x", "absolute_field": "x"},
        )


def test_fa70_area_share_type_error_raster_input_rejected_at_validation():
    with pytest.raises(ValueError, match="erwartet vector, erhält aber raster"):
        Scenario(
            **{
                "scenario": {"name": "t", "region": BBOX_REGION},
                "layers": [
                    {"id": "zonen", "source": "region", "data_type": "vector"},
                    {
                        "id": "dem",
                        "source": "file",
                        "path": "dem.tif",
                        "format": "tif",
                        "data_type": "raster",
                    },
                ],
                "steps": [
                    {
                        "id": "s",
                        "op": "area_share",
                        "inputs": {"zones": "zonen", "features": "dem"},
                    }
                ],
                "output": [{"type": "geojson", "source": "s"}],
            }
        )


# =====================================================================
# Contract / Katalog
# =====================================================================


def test_fa70_area_share_step_declares_contract():
    step = AreaShareStep(
        id="s", inputs={"zones": "z", "features": "f"}, params={"absolute_field": "m2"}
    )
    assert step.INPUT_PORTS == {"zones": DataType.VECTOR, "features": DataType.VECTOR}
    assert step.OUTPUT_TYPE == DataType.VECTOR
    assert step.produced_fields() == {"area_share", "m2"}


def test_fa70_area_share_scenario_validates():
    scenario = Scenario(
        **{
            "scenario": {"name": "t", "region": BBOX_REGION},
            "layers": [
                {"id": "zonen", "source": "region", "data_type": "vector"},
                {"id": "gruen", "source": "region", "data_type": "vector"},
            ],
            "steps": [
                {
                    "id": "s",
                    "op": "area_share",
                    "inputs": {"zones": "zonen", "features": "gruen"},
                }
            ],
            "output": [{"type": "geojson", "source": "s"}],
        }
    )
    assert scenario.execution_order() == ["s"]


def test_fa70_area_share_is_visible_in_the_catalog():
    entry = {e["op"]: e for e in api.operation_catalog()}["area_share"]
    assert entry["input_ports"] == {"zones": "vector", "features": "vector"}
    assert entry["required_params"] == []
    assert entry["params"]["decimals"]["default"] == 3


def test_fa70_area_share_repairs_an_invalid_zone_and_reports_it():
    """Nachtreview 18: a self-intersecting zone (bow-tie) raised a GEOSException
    TopologyException; its unrepaired area (0) would also be a wrong denominator."""
    bow_tie = Polygon([(0, 0), (10, 10), (10, 0), (0, 10)])
    zones = gpd.GeoDataFrame({"name": ["z"]}, geometry=[bow_tie], crs=UTM)
    features = gpd.GeoDataFrame(geometry=[box(-1, -1, 11, 11)], crs=UTM)
    with pytest.warns(UserWarning, match="ungültige Zone"):
        result = run_area_share({"zones": zones, "features": features}, {})
    assert result["area_share"].tolist() == [pytest.approx(1.0)]
    assert result.geometry.iloc[0].equals(bow_tie)  # Ergebnis behält die Originalzone
