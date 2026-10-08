"""Implements: FA12 (example sachsen_nachthimmel of the web demo, offline end to end).

The example `examples/sachsen_nachthimmel.yaml` without network and without the large raster
downloads: the four layers are replaced by the synthetic stand-ins of
`tests/nachthimmel_scenes.py` (25 viewpoints, 25 parking lots, a night-light raster with a
bright south and a dark north, an elevation plane); filter, top_n, buffers and zonal
statistics run unchanged through `api.run`. The expected selection is written down from the
construction of the data, not taken from a pipeline run.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
import yaml
from shapely.geometry import mapping

from geofact import api

import nachthimmel_scenes as scenes

pytestmark = pytest.mark.e2e

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "sachsen_nachthimmel.yaml"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def region_fetcher(name: str) -> list[dict]:
    assert name == "Sachsen, Deutschland"
    geometry = gpd.read_file(FIXTURES / "sachsen_region.geojson").geometry.iloc[0]
    return [{"geojson": mapping(geometry)}]


def run(tmp_path: Path) -> api.RunReport:
    document = api.load_scenario(EXAMPLE)
    return api.run(
        document,
        out_dir=tmp_path / "out",
        connector_override=scenes.nachthimmel_overrides(),
        region_fetcher=region_fetcher,
    )


@pytest.fixture(autouse=True)
def _snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))


def test_fa12_nachthimmel_returns_the_ten_highest_dark_reachable_sites_in_rank_order(
    tmp_path,
):
    best = run(tmp_path).result.store["beste"]
    assert list(best["id"]) == scenes.expected_ranking()
    assert list(best["rang"]) == list(range(1, scenes.TOP_N + 1))
    heights = list(best["max_value"])
    assert heights == sorted(heights, reverse=True) and len(set(heights)) == len(
        heights
    )
    # the height is the maximum within 100 m of the site: the terrain height plus the rise of the
    # plane over the circle (about 2 m), give or take one 90 m cell
    for row in best.itertuples():
        lon, lat = (float(part) for part in row.id.split("/"))
        assert row.max_value == pytest.approx(scenes.elevation_m(lon, lat), abs=5)
    assert (best["distance"] <= 1000).all() and (best["mean_value"] <= 5).all()


def test_fa12_nachthimmel_filter_drops_bright_and_unreachable_sites(tmp_path):
    store = run(tmp_path).result.store
    lit = store["mit_licht"].set_index("id")
    assert len(lit) == 25
    for lon, lat in scenes.all_sites():
        row = lit.loc[scenes.site_id(lon, lat)]
        assert row["mean_value"] == (
            scenes.DARK if lat in scenes.DARK_LATS else scenes.BRIGHT
        )
        beyond = lon == scenes.UNREACHABLE_LON
        assert (row["distance"] > 1000) == beyond, (
            f"{scenes.site_id(lon, lat)}: {row['distance']:.0f} m"
        )
    suitable = set(store["geeignet"]["id"])
    assert suitable == {scenes.site_id(*site) for site in scenes.suitable_sites()}
    assert (
        len(suitable) == 12 > scenes.TOP_N
    )  # top_n really cuts: the two lowest sites drop out
    assert suitable - set(store["beste"]["id"]) == {
        scenes.site_id(13.64, 51.08),
        scenes.site_id(13.64, 51.11),
    }


def test_fa12_nachthimmel_outputs_are_the_map_and_the_geojson_in_rank_order(tmp_path):
    report = run(tmp_path)
    assert report.outputs.skipped == []
    out = tmp_path / "out"
    assert (out / "sachsen_nachthimmel.html").stat().st_size > 0
    collection = json.loads(
        (out / "sachsen_nachthimmel_beste_orte.geojson").read_text(encoding="utf-8")
    )
    assert [
        feature["properties"]["id"] for feature in collection["features"]
    ] == scenes.expected_ranking()
    assert [
        feature["properties"]["rang"] for feature in collection["features"]
    ] == list(range(1, 11))
    assert collection["crs"]["properties"]["name"].endswith("CRS84")


def test_fa12_nachthimmel_keeps_the_parameters_of_the_reference_evaluation():
    """The example is the s3 configuration of the reference evaluation with German ids; the analysis
    parameters (and with them the published numbers: 250 suitable sites, top 10 between 771 and
    1018 m) must not drift."""
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    params = {step["id"]: step.get("params") for step in raw["steps"]}
    assert params == {
        "orte": None,
        "mit_parkplatz": None,
        "umfeld_hoehe": {"radius_km": 0.1},
        "mit_hoehe": {"stat": "max"},
        "umfeld_licht": {"radius_km": 1.9},
        "mit_licht": {"stat": "mean", "decimals": 2},
        "geeignet": {"condition": "distance <= 1000 and mean_value <= 5"},
        "beste": {"by": "max_value", "n": 10, "rank_field": "rang"},
    }
    assert raw["scenario"]["region"] == "Sachsen, Deutschland"
    assert {
        layer["id"]: layer.get("tags") or layer["path"] for layer in raw["layers"]
    } == {
        "aussichtspunkte": {"tourism": "viewpoint"},
        "parkplaetze": {"amenity": "parking"},
        "nachtlicht": "data/ntl_2024_simviirs.tif",
        "hoehe": "data/dem_sachsen_glo90.tif",
    }
