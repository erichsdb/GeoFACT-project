"""Implements: FA65 (Lizenzen und Namensnennung durch den DAG), FA73 (Ausgabelizenz), FA74 (PROV-O) - Schaufenster-Beispiel Ende zu Ende.

Runs the REAL example `examples/chemnitz_datenquellen_lizenzen.yaml` offline through
`api.plan`/`api.run`: the two network layers (OSM districts, Sentinel-2 scene) are
replaced by connector overrides (the synthetic scene of tests/stac_scenes.py and nine
rectangular districts), the region by the grid-aligned Chemnitz box. The two inline
point layers load for real - one declares CC0-1.0, the other declares nothing.

Checked: the plan attributes three licence kinds and one undeclared layer to the end
node; ATTRIBUTION.txt, the map footer, the GeoTIFF tags and the RDF dataset carry the
licences of exactly the sources behind each output.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rdflib import Graph, URIRef
from rdflib.namespace import DCTERMS
from shapely.geometry import Polygon, box, mapping

from geofact import api
from geofact.plugin_api import RasterLayer
from stac_scenes import CITY_BOX, aligned_ring, expected_bands

REPO = Path(__file__).resolve().parents[2]
EXAMPLE = REPO / "examples" / "chemnitz_datenquellen_lizenzen.yaml"

ODBL, COPERNICUS, CC0 = "ODbL-1.0", "CC-BY-SA-3.0-IGO", "CC0-1.0"

pytestmark = pytest.mark.e2e


def _region_fetcher(name: str) -> list[dict]:
    assert name == "Chemnitz, Deutschland"
    return [{"geojson": mapping(Polygon(aligned_ring(CITY_BOX)))}]


def _districts(layer) -> gpd.GeoDataFrame:
    """Nine rectangular districts over the inner city (all points of the example lie inside)."""
    minx, miny, maxx, maxy = 12.75, 50.76, 13.03, 50.88
    dx, dy = (maxx - minx) / 3, (maxy - miny) / 3
    geoms = [
        box(minx + i * dx, miny + j * dy, minx + (i + 1) * dx, miny + (j + 1) * dy)
        for i in range(3)
        for j in range(3)
    ]
    return gpd.GeoDataFrame(
        {
            "id": [f"z{k}" for k in range(9)],
            "name": [f"Stadtteil {k + 1}" for k in range(9)],
        },
        geometry=geoms,
        crs="EPSG:4326",
    )


def _scene(layer) -> RasterLayer:
    """Synthetic red/nir/scl scene over the Chemnitz box in UTM 33, clouds already nodata."""
    left, bottom, right, top = CITY_BOX
    rows, cols = int((top - bottom) / 100), int((right - left) / 100)
    transform = Affine(100, 0, left, 0, -100, top)
    bands = expected_bands(transform, rows, cols)
    data = np.stack([bands["red"], bands["nir"], bands["scl"]])
    data[:, np.isin(bands["scl"], [3, 8, 9, 10])] = 0
    return RasterLayer(
        data=data,
        transform=transform,
        crs=CRS.from_epsg(32633),
        path="synthetic",
        nodata=0,
        band_names=["red", "nir", "scl"],
    )


OVERRIDES = {"stadtteile": _districts, "sentinel2": _scene}


@pytest.fixture
def report(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    document = api.load_scenario(EXAMPLE)
    return api.run(
        document,
        out_dir=tmp_path / "out",
        connector_override=OVERRIDES,
        region_fetcher=_region_fetcher,
    )


def _names(attribution) -> list[str]:
    return [licence.name for licence in attribution.licenses]


def test_fa65_showcase_declares_three_licence_kinds_and_one_undeclared():
    plan = api.plan(api.load_scenario(EXAMPLE))
    end = plan.attribution["punkte_im_stadtteil"]
    # ports alphabetical: left (messpunkte CC0, hinweise undeclared) before right (values -> zones)
    assert _names(end) == [CC0, COPERNICUS, ODBL]
    assert end.undeclared == ("hinweise",)
    # the three kinds: a declared licence, a STAC default and an OSM default
    assert _names(plan.attribution["messpunkte"]) == [CC0]
    assert plan.attribution["messpunkte"].licenses[0].attribution == "Eigene Erhebung"
    assert _names(plan.attribution["sentinel2"]) == [COPERNICUS]
    assert _names(plan.attribution["stadtteile"]) == [ODBL]
    assert plan.attribution["hinweise"].empty and plan.attribution[
        "hinweise"
    ].undeclared == ("hinweise",)
    # every node carries only what flows into it
    assert (
        _names(plan.attribution["ndvi"]) == [COPERNICUS]
        and plan.attribution["ndvi"].undeclared == ()
    )
    assert _names(plan.attribution["anteil_stadtteile"]) == [COPERNICUS, ODBL]


def test_fa65_showcase_runs_offline_and_writes_every_output(report):
    assert report.outputs.skipped == []
    assert sorted(p.name for p in report.outputs.written) == sorted(
        [
            "punkte_im_stadtteil.html",
            "gruenanteil_stadtteile.html",
            "punkte_im_stadtteil.geojson",
            "ndvi.tif",
            "punkte_im_stadtteil.ttl",
        ]
    )
    points = report.result.store["punkte_im_stadtteil"]
    assert len(points) == 8 and points["gruenanteil"].notna().all()


def test_fa65_showcase_writes_attribution_txt(report, tmp_path):
    assert report.attribution_file == tmp_path / "out" / "ATTRIBUTION.txt"
    text = report.attribution_file.read_text(encoding="utf-8")
    blocks = {
        block.splitlines()[0]: block
        for block in text.strip().split("\n\n")
        if "(Quelle: " in block
    }
    assert len(blocks) == 5, text  # one block per written output
    final = next(
        block
        for head, block in blocks.items()
        if head.startswith("punkte_im_stadtteil.html")
    )
    assert "Eigene Erhebung (CC0-1.0)" in final
    assert "Contains modified Copernicus Sentinel data (CC-BY-SA-3.0-IGO" in final
    assert "OpenStreetMap contributors (ODbL-1.0" in final
    assert "ohne Lizenzangabe: hinweise" in final
    ndvi = next(block for head, block in blocks.items() if head.startswith("ndvi.tif"))
    assert (
        "Copernicus" in ndvi
        and "OpenStreetMap" not in ndvi
        and "ohne Lizenzangabe" not in ndvi
    )
    assert _names(report.attribution) == [CC0, COPERNICUS, ODBL]


def test_fa65_showcase_map_html_contains_footer(report, tmp_path):
    html = (tmp_path / "out" / "punkte_im_stadtteil.html").read_text(encoding="utf-8")
    assert "geofact-attribution" in html
    for text in (
        "Eigene Erhebung",
        "Copernicus Sentinel data",
        "OpenStreetMap contributors",
    ):
        assert text in html
    districts = (tmp_path / "out" / "gruenanteil_stadtteile.html").read_text(
        encoding="utf-8"
    )
    assert "geofact-attribution" in districts and "Eigene Erhebung" not in districts


def test_fa65_showcase_geotiff_carries_only_the_copernicus_licence(report, tmp_path):
    with rasterio.open(tmp_path / "out" / "ndvi.tif") as dataset:
        tags = dataset.tags()
    assert (
        "Copernicus" in tags["TIFFTAG_COPYRIGHT"]
        and "OpenStreetMap" not in tags["TIFFTAG_COPYRIGHT"]
    )
    assert [
        entry["name"] for entry in json.loads(tags["GEOFACT_ATTRIBUTION"])["licenses"]
    ] == [COPERNICUS]


def test_fa73_showcase_rdf_dataset_carries_the_output_licence_and_sources_all_three(
    report, tmp_path
):
    graph = Graph().parse(tmp_path / "out" / "punkte_im_stadtteil.ttl", format="turtle")
    datasets = set(graph.objects(None, DCTERMS.isPartOf))
    assert len(datasets) == 1
    (dataset,) = datasets
    # FA73: the result carries the user's output licence (ODbL, share-alike) ...
    assert set(graph.objects(dataset, DCTERMS.license)) == {
        URIRef("https://opendatacommons.org/licenses/odbl/1-0/")
    }
    [rights] = list(graph.objects(dataset, DCTERMS.rights))
    assert str(rights).startswith(
        "Lizenz dieses Ergebnisses: © Auswertung GeoFACT-Beispiel Chemnitz"
    )
    # ... and the three source licences sit on the source datasets (dcterms:source)
    sources = set(graph.objects(dataset, DCTERMS.source))
    licences = {
        str(value)
        for source in sources
        for value in graph.objects(source, DCTERMS.license)
    }
    rights = " ".join(
        str(value)
        for source in sources
        for value in graph.objects(source, DCTERMS.rights)
    )
    assert len(licences) == 3, licences
    for text in ("Eigene Erhebung", "Copernicus", "OpenStreetMap"):
        assert text in rights


def test_fa73_showcase_attribution_txt_names_the_output_licence(report):
    text = report.attribution_file.read_text(encoding="utf-8")
    assert (
        text.count("Lizenz dieses Ergebnisses: © Auswertung GeoFACT-Beispiel Chemnitz")
        == 5
    )
    assert "(vom Nutzer festgelegt, ungeprüft)" in text


def test_fa74_showcase_rdf_names_the_osm_tags_and_steps_behind_the_points(
    report, tmp_path
):
    graph = Graph().parse(tmp_path / "out" / "punkte_im_stadtteil.ttl", format="turtle")
    query = (REPO / "docs" / "provenienz_rdf.md").read_text(encoding="utf-8")
    query = (
        query.split("```sparql\n", 1)[1]
        .split("```", 1)[0]
        .replace("resource/schnitt/0>", "resource/punkte_im_stadtteil/0>")
    )
    rows = {
        (str(row.node), str(row.block)): json.loads(str(row.parameters))
        for row in graph.query(query)
    }
    assert rows[("stadtteile", "osm")]["tags"][0]["boundary"] == "administrative"
    assert rows[("sentinel2", "stac")]["collection"] == "sentinel-2-l2a"
    assert rows[("ndvi", "raster_calc")]["expression"] == "(nir - red) / (nir + red)"
    assert rows[("punkte_im_stadtteil", "spatial_join")]["predicate"] == "within"
    assert ("hinweise", "points") in rows


# Ausdehnung der OSM-Stadtteile (Snapshot des PostGIS-Imports, 02.10.2026), lon/lat
_DISTRICT_BOUNDS = {
    "Sonnenberg": (12.93, 50.8307, 12.961, 50.845),
    "Kaßberg": (12.8905, 50.8243, 12.9144, 50.8402),
    "Yorckgebiet": (12.9539, 50.8287, 12.983, 50.8418),
}


def test_fa65_showcase_hint_points_lie_in_the_district_they_are_named_after():
    """Nachtreview 02.10. (Runde 2): 'Hinweis Yorckgebiet' lag in Gablenz - jede
    Ausgabe zeigte einen nach einem Stadtteil benannten Punkt im anderen."""
    layers = api.load_scenario(EXAMPLE).scenario.layers
    layer = next(item for item in layers if item.id == "hinweise")
    for feature in layer.features:
        district = feature.name.removeprefix("Hinweis ")
        min_lon, min_lat, max_lon, max_lat = _DISTRICT_BOUNDS[district]
        assert (
            min_lon <= feature.lon <= max_lon and min_lat <= feature.lat <= max_lat
        ), feature
