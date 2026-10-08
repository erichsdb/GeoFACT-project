"""Implements: FA65 (licenses and attribution in the output formats map, geotiff, rdf).

Contract tests: the formats read ``spec.attribution`` (bound before writing, the
writer signature ``(data, target, spec)`` stays). ``map`` gets a footer and the
Leaflet attribution, ``geotiff`` the tags ``TIFFTAG_COPYRIGHT`` and
``GEOFACT_ATTRIBUTION``, ``rdf`` a ``dcat:Dataset`` with ``dcterms:license`` and
``dcterms:rights`` that every feature is ``dcterms:isPartOf``; without
attribution the RDF graph is triple-identical to before (FA13)."""

from __future__ import annotations

import json

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from pyproj import CRS
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCAT, DCTERMS, GEO
from shapely.geometry import Point

from geofact import api
from geofact.builtin.outputs.rdf import build_graph
from geofact.core.contracts import License
from geofact.core.provenance import NodeAttribution
from geofact.core.types import RasterLayer

HEAD = """
scenario:
  name: Lizenzen
  region: "13.0,50.9,13.1,51.0"
layers:
  - {id: v, source: file, path: data/v.geojson, data_type: vector}
  - {id: r, source: file, path: data/r.tif, data_type: raster, format: tif}
steps:
  - {id: b, op: buffer, inputs: {geometry: v}, params: {radius_km: 1}}
output:
"""

OSM = License(
    name="ODbL-1.0",
    attribution="OpenStreetMap contributors",
    url="https://opendatacommons.org/licenses/odbl/",
)
DLDE = License(name="DL-DE-BY-2.0", attribution="GeoSN")
ATTRIBUTION = NodeAttribution(licenses=(OSM, DLDE))


def _spec(output: str, attribution: NodeAttribution | None = ATTRIBUTION):
    spec = api.load_scenario(HEAD + output).scenario.output[0]
    spec.bind_attribution(attribution)
    return spec


def _write(spec, data, target):
    api.registry().output(spec.type).write(data, target, spec)


def _points() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[Point(13.05, 50.95), Point(13.08, 50.97)],
        crs="EPSG:4326",
    )


def test_fa65_map_output_carries_the_attribution_footer_and_leaflet_attribution(
    tmp_path,
):
    target = tmp_path / "karte.html"
    _write(_spec("  - {type: map, source: v}\n"), _points(), target)
    html = target.read_text(encoding="utf-8")
    assert 'class="geofact-attribution"' in html
    assert OSM.line() in html and DLDE.line() in html
    # Leaflet attribution of the tile layer: the OSM tile credit stays, the data licenses follow
    tile_options = [line for line in html.splitlines() if '"attribution"' in line]
    assert any(
        "OpenStreetMap" in line and "DL-DE-BY-2.0" in line for line in tile_options
    )


def test_fa65_map_without_attribution_has_no_footer(tmp_path):
    target = tmp_path / "karte.html"
    _write(_spec("  - {type: map, source: v}\n", attribution=None), _points(), target)
    assert "geofact-attribution" not in target.read_text(encoding="utf-8")


def test_fa65_geotiff_output_carries_copyright_and_attribution_tags(tmp_path):
    raster = RasterLayer(
        data=np.ones((4, 4), dtype="float32"),
        transform=Affine(10, 0, 352000, 0, -10, 5636000),
        crs=CRS.from_epsg(32633),
    )
    target = tmp_path / "r.tif"
    _write(_spec("  - {type: geotiff, source: r}\n"), raster, target)
    with rasterio.open(target) as dataset:
        tags = dataset.tags()
    assert (
        OSM.line() in tags["TIFFTAG_COPYRIGHT"]
        and DLDE.line() in tags["TIFFTAG_COPYRIGHT"]
    )
    stored = json.loads(tags["GEOFACT_ATTRIBUTION"])
    assert [lic["name"] for lic in stored["licenses"]] == ["ODbL-1.0", "DL-DE-BY-2.0"]

    plain = tmp_path / "plain.tif"
    _write(_spec("  - {type: geotiff, source: r}\n", attribution=None), raster, plain)
    with rasterio.open(plain) as dataset:
        assert "GEOFACT_ATTRIBUTION" not in dataset.tags()


def test_fa65_rdf_output_links_features_to_a_dataset_with_dcterms_license_and_rights(
    tmp_path,
):
    target = tmp_path / "o.ttl"
    _write(_spec("  - {type: rdf, source: v}\n"), _points(), target)
    graph = Graph().parse(target, format="turtle")
    [dataset] = list(graph.subjects(RDF.type, DCAT.Dataset))
    assert set(graph.objects(dataset, DCTERMS.license)) == {
        URIRef(OSM.url),
        Literal("DL-DE-BY-2.0"),
    }
    assert set(graph.objects(dataset, DCTERMS.rights)) == {
        Literal(OSM.line()),
        Literal(DLDE.line()),
    }
    features = set(graph.subjects(RDF.type, GEO.Feature))
    assert len(features) == 2
    assert all((feature, DCTERMS.isPartOf, dataset) in graph for feature in features)


def test_fa65_rdf_without_attribution_is_unchanged(tmp_path):
    target = tmp_path / "o.ttl"
    _write(_spec("  - {type: rdf, source: v}\n", attribution=None), _points(), target)
    written = Graph().parse(target, format="turtle")
    assert isomorphic(written, build_graph(_points(), layer_name="v"))
    assert not list(written.subjects(RDF.type, DCAT.Dataset))
    empty = NodeAttribution(undeclared=("v",))
    assert isomorphic(
        build_graph(_points(), "v", attribution=empty), build_graph(_points(), "v")
    )
