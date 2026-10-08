"""Implements: FA55, FA56, FA57, FA58 (example examples/crs_showcase/drei_crs_eine_stadt.yaml, offline end to end).

Three local files of the same part of Chemnitz in three CRS (WGS 84 degrees,
UTM 33 metres without a GeoJSON ``crs`` member, Gauss-Krueger zone 4 metres in a
shapefile; built by examples/crs_showcase/make_data.py). The run needs no
network: the region is a bbox literal and every layer is a local file.
"""

from __future__ import annotations

import re
from pathlib import Path

import geopandas as gpd
import pytest
import yaml
from pyproj import CRS

from geofact import api

pytestmark = pytest.mark.e2e

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "crs_showcase"
EXAMPLE = EXAMPLE_DIR / "drei_crs_eine_stadt.yaml"
WORKING_CRS = CRS.from_epsg(
    32633
)  # crs: auto -> UTM zone of the region centroid (12.93 E)


@pytest.fixture(scope="module")
def showcase_run(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("crs_showcase")
    document = api.load_scenario(EXAMPLE)
    report = api.run(document, out_dir=out_dir)
    return document.scenario, report, out_dir


def _written(report, name: str) -> Path:
    matches = [path for path in report.outputs.written if path.name == name]
    assert matches, f"{name} not written: {[p.name for p in report.outputs.written]}"
    return matches[0]


def test_fa58_showcase_example_validates_and_runs_offline(showcase_run):
    scenario, report, out_dir = showcase_run
    assert api.validate(api.load_scenario(EXAMPLE)) is not None
    assert report.outputs.skipped == []
    assert len(report.outputs.written) == len(scenario.output) == 5
    svg = _written(report, "drei_crs_eine_stadt_crs_plot.svg").read_text(
        encoding="utf-8"
    )
    assert svg.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns=')
    html_page = _written(report, "drei_crs_eine_stadt_crs_plot.html").read_text(
        encoding="utf-8"
    )
    assert svg.split("<svg", 1)[1].rstrip() in html_page  # same figure, same bytes
    zones = report.result.store["je_stadtteil"]
    assert len(zones) == 9
    assert sorted(zones.columns.drop(zones.geometry.name)) == ["count", "id", "name"]
    # 18 stops beside a street are counted, the 6 in block centres are not.
    assert int(zones["count"].sum()) == 18
    assert len(report.result.store["utm"]) == 24


def test_fa57_showcase_without_crs_override_fails_with_coordinate_range_error(tmp_path):
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    for layer in raw["layers"]:
        layer["path"] = str(EXAMPLE_DIR / layer["path"])
        if layer["id"] == "utm":
            layer.pop("crs")
            layer.pop("crs_override")
    config = tmp_path / "ohne_override.yaml"
    config.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(Exception, match=r"passen nicht zu EPSG:4326") as excinfo:
        api.run(api.load_scenario(config), out_dir=tmp_path / "out")
    assert "utm" in str(excinfo.value)


def test_fa55_showcase_layers_share_the_working_crs(showcase_run):
    _, report, _ = showcase_run
    result = report.result
    for layer in ("wgs", "utm", "gk4"):
        assert CRS(result.store[layer].crs) == WORKING_CRS, layer
    provenance = result.provenance
    assert set(provenance) == {"wgs", "utm", "gk4"}
    sources = {
        layer: CRS.from_user_input(provenance[layer].source_crs).to_epsg()
        for layer in provenance
    }
    assert sources == {"wgs": 4326, "utm": 25833, "gk4": 31468}
    assert provenance["utm"].crs_override is True
    assert {
        CRS.from_user_input(p.target_crs).to_epsg() for p in provenance.values()
    } == {32633}
    # Raw extents lie far apart: degrees, UTM metres, Gauss-Krueger metres.
    assert provenance["wgs"].raw_bounds[0] == pytest.approx(12.922, abs=1e-6)
    assert 3.4e5 < provenance["utm"].raw_bounds[0] < 3.6e5
    assert 4.5e6 < provenance["gk4"].raw_bounds[0] < 4.6e6


def test_fa58_showcase_crs_plot_names_three_source_crs(showcase_run):
    _, report, _ = showcase_run
    page = _written(report, "drei_crs_eine_stadt_crs_plot.html").read_text(
        encoding="utf-8"
    )
    for label in ("EPSG:4326", "EPSG:25833", "EPSG:31468", "EPSG:32633"):
        assert label in page, label
    assert "erzwungen (crs_override)" in page
    raw_panel = page.split('data-panel="raw"', 1)[1].split("</svg>", 1)[0]
    for layer in ("gk4", "utm", "wgs"):
        assert f'data-layer="{layer}"' in raw_panel
    ticks = [
        float(value.replace(" ", ""))
        for value in re.findall(r'class="tick"[^>]*>([^<]+)<', raw_panel)
    ]
    # One linear axis from the origin up to the Gauss-Krueger metres.
    assert 0.0 in ticks and max(ticks) >= 4_000_000, ticks
    # Each layer's raw extent is annotated in its own unit: degrees next to the
    # origin, UTM and Gauss-Krueger metres millions of units away.
    extents = dict(
        re.findall(
            r'<g class="label" data-layer="([^"]+)">.*?class="extent-label"[^>]*>x ([0-9. ]+) …',
            raw_panel,
        )
    )
    assert set(extents) == {"gk4", "utm", "wgs"}
    assert 12 < float(extents["wgs"]) < 14
    assert 3.4e5 < float(extents["utm"].replace(" ", "")) < 3.7e5
    assert 4.5e6 < float(extents["gk4"].replace(" ", "")) < 4.6e6
    assert "Abstand" in raw_panel
    # every layer collapses to a dot on the shared axis -> one inset each
    insets = re.findall(r'<svg data-panel="inset" data-layer="([^"]+)"', page)
    assert insets == ["gk4", "utm", "wgs"]
    harmonized = page.split('data-panel="harmonized"', 1)[1].split("</svg>", 1)[0]
    assert 'class="polygon"' in harmonized and 'class="line"' in harmonized
    assert harmonized.count('class="point"') == 24


def test_fa55_showcase_geojson_output_is_in_epsg_25833(showcase_run):
    _, report, _ = showcase_run
    written = gpd.read_file(_written(report, "drei_crs_eine_stadt_utm33.geojson"))
    assert written.crs.to_epsg() == 25833
    minx, miny, maxx, maxy = written.total_bounds
    assert 3.4e5 < minx < maxx < 3.7e5
    assert 5.6e6 < miny < maxy < 5.7e6
    assert len(written) == 9
