"""Implements: FA73 (Ausgabelizenz: Deklaration und Darstellung in jeder Ausgabe).

Contract tests: ``output[i].license`` / ``scenario.output_license`` are bound to
each output as ``spec.output_license`` and appear as their own line in
ATTRIBUTION.txt, in the map footer, in the GeoTIFF tag ``GEOFACT_OUTPUT_LICENSE``
and as ``dcterms:license`` of the RDF dataset (source licences move to source
datasets). The note says "ungeprüft" unless a cached DALICC check of exactly this
licence set exists. Without an output licence nothing changes. Offline.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
import rasterio
from _cli_doubles import base_config
from affine import Affine
from pyproj import CRS
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import DCAT, DCTERMS, PROV

from geofact import api
from geofact.builtin.outputs.rdf import GEOFACT, build_graph
from geofact.core.provenance import NodeAttribution, OutputLicense
from geofact.core.types import RasterLayer
from geofact.support.dalicc import LIBRARY_PREFIX, DaliccClient

CC_BY = {
    "name": "CC-BY-4.0",
    "attribution": "Stadt Beispiel",
    "url": "https://creativecommons.org/licenses/by/4.0/",
}
ODBL = {
    "name": "ODbL-1.0",
    "attribution": "Eigene Auswertung",
    "url": "https://opendatacommons.org/licenses/odbl/1-0/",
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.chdir(tmp_path)


def _document(
    outputs: list[dict],
    *,
    source_license: dict | None = CC_BY,
    scenario_license: dict | None = None,
) -> api.ScenarioDocument:
    raw = base_config()
    if source_license is not None:
        raw["layers"][0]["license"] = source_license
    if scenario_license is not None:
        raw["scenario"]["output_license"] = scenario_license
    raw["output"] = outputs
    return api.load_scenario(raw)


def _run(document, tmp_path):
    return api.run(document, out_dir=tmp_path / "out")


# --- declaration -------------------------------------------------------------------


def test_fa73_output_license_is_validated_like_a_layer_license():
    with pytest.raises(api.ConfigError) as info:
        _document([{"type": "geojson", "source": "einzug", "license": {"name": " "}}])
    assert any(
        "output -> 0 -> license" in location for location, _ in info.value.issues
    )
    with pytest.raises(api.ConfigError) as info:
        _document(
            [{"type": "geojson", "source": "einzug"}], scenario_license={"nam": "x"}
        )
    assert any(
        "scenario -> output_license" in location for location, _ in info.value.issues
    )


def test_fa73_output_license_line_names_the_user_and_the_check_state():
    license_ = api.License(**ODBL)
    assert OutputLicense(license_).line() == (
        "Lizenz dieses Ergebnisses: © Eigene Auswertung (ODbL-1.0, "
        "https://opendatacommons.org/licenses/odbl/1-0/) (vom Nutzer festgelegt, ungeprüft)"
    )
    checked = OutputLicense(license_, check="geprüft am 2026-10-03: vereinbar (DALICC)")
    assert checked.line().endswith(
        "(vom Nutzer festgelegt, geprüft am 2026-10-03: vereinbar (DALICC))"
    )


# --- ATTRIBUTION.txt ---------------------------------------------------------------


def test_fa73_attribution_file_names_the_output_license_on_its_own_line(tmp_path):
    document = _document(
        [
            {
                "type": "geojson",
                "source": "einzug",
                "path": "a.geojson",
                "license": ODBL,
            },
            {"type": "geojson", "source": "einzug", "path": "b.geojson"},
        ]
    )
    report = _run(document, tmp_path)
    text = report.attribution_file.read_text(encoding="utf-8")
    block_a, block_b = text.split("\n\n")[1:3]
    assert block_a.splitlines() == [
        "a.geojson (Quelle: einzug)",
        "  © Stadt Beispiel (CC-BY-4.0, https://creativecommons.org/licenses/by/4.0/)",
        "  Lizenz dieses Ergebnisses: © Eigene Auswertung (ODbL-1.0, "
        "https://opendatacommons.org/licenses/odbl/1-0/) (vom Nutzer festgelegt, ungeprüft)",
    ]
    assert "Lizenz dieses Ergebnisses" not in block_b


def test_fa73_scenario_default_applies_to_every_output_without_own_license(tmp_path):
    document = _document(
        [
            {"type": "geojson", "source": "einzug", "path": "a.geojson"},
            {
                "type": "csv",
                "source": "einzug",
                "path": "b.csv",
                "license": {"name": "CC0-1.0"},
            },
        ],
        scenario_license=ODBL,
    )
    text = _run(document, tmp_path).attribution_file.read_text(encoding="utf-8")
    assert "Lizenz dieses Ergebnisses: © Eigene Auswertung (ODbL-1.0" in text
    assert (
        "Lizenz dieses Ergebnisses: CC0-1.0 (vom Nutzer festgelegt, ungeprüft)" in text
    )


def test_fa73_output_license_alone_creates_the_attribution_file(tmp_path):
    document = _document(
        [{"type": "geojson", "source": "einzug", "path": "a.geojson", "license": ODBL}],
        source_license=None,
    )
    text = _run(document, tmp_path).attribution_file.read_text(encoding="utf-8")
    assert "ohne Lizenzangabe: umspannwerke" in text
    assert "Lizenz dieses Ergebnisses" in text


def test_fa73_without_output_license_the_attribution_file_is_unchanged(tmp_path):
    document = _document([{"type": "geojson", "source": "einzug", "path": "a.geojson"}])
    text = _run(document, tmp_path).attribution_file.read_text(encoding="utf-8")
    assert text == (
        "Namensnennung und Lizenzen der Ausgaben dieses Laufs (GeoFACT, FA65)\n\n"
        "a.geojson (Quelle: einzug)\n"
        "  © Stadt Beispiel (CC-BY-4.0, https://creativecommons.org/licenses/by/4.0/)\n"
    )


def test_fa73_run_marks_the_output_license_checked_from_a_cached_dalicc_answer(
    tmp_path,
):
    odbl_uri, cc_by_uri = (
        LIBRARY_PREFIX + "OdcOpenDatabaseLicense",
        LIBRARY_PREFIX + "CC-BY-4.0",
    )
    library = {
        "results": {
            "bindings": [{"id": {"value": odbl_uri}}, {"id": {"value": cc_by_uri}}]
        }
    }
    client = DaliccClient(
        get=lambda *a, **k: library,
        post=lambda *a, **k: {"conflicting_statements": {"direct": {}, "derived": {}}},
    )
    document = _document(
        [{"type": "geojson", "source": "einzug", "path": "a.geojson", "license": ODBL}]
    )
    from geofact.engine.licenses import check_licenses

    assert (
        check_licenses(document.scenario, api.plan(document), client=client).status
        == "compatible"
    )
    text = _run(document, tmp_path).attribution_file.read_text(encoding="utf-8")
    assert "(vom Nutzer festgelegt, geprüft am " in text
    assert ": vereinbar (DALICC))" in text


# --- map, GeoTIFF, RDF -------------------------------------------------------------


def test_fa73_map_footer_shows_the_output_license_in_its_own_line(tmp_path):
    document = _document(
        [{"type": "map", "source": "einzug", "path": "k.html", "license": ODBL}]
    )
    _run(document, tmp_path)
    html = (tmp_path / "out" / "k.html").read_text(encoding="utf-8")
    assert 'class="geofact-output-license"' in html
    assert "Lizenz dieses Ergebnisses: © Eigene Auswertung (ODbL-1.0" in html
    assert "Daten: © Stadt Beispiel" in html


def test_fa73_map_with_output_license_but_no_source_license_still_has_a_footer(
    tmp_path,
):
    document = _document(
        [{"type": "map", "source": "einzug", "path": "k.html", "license": ODBL}],
        source_license=None,
    )
    _run(document, tmp_path)
    html = (tmp_path / "out" / "k.html").read_text(encoding="utf-8")
    assert 'class="geofact-attribution"' in html and "Daten:" not in html
    assert "Lizenz dieses Ergebnisses" in html


def _raster_spec(output: dict):
    head = """
scenario: {name: Lizenzen, region: "13.0,50.9,13.1,51.0"}
layers:
  - {id: r, source: file, path: data/r.tif, data_type: raster, format: tif}
steps:
  - {id: n, op: raster_calc, inputs: {raster: r}, params: {expression: "b1 * 2"}}
output:
"""
    return api.load_scenario(head + "  - " + json.dumps(output) + "\n").scenario.output[
        0
    ]


def test_fa73_geotiff_carries_the_output_license_tag_next_to_the_source_tags(tmp_path):
    spec = _raster_spec({"type": "geotiff", "source": "n"})
    spec.bind_attribution(NodeAttribution(licenses=(api.License(**CC_BY),)))
    spec.bind_output_license(OutputLicense(api.License(**ODBL)))
    raster = RasterLayer(
        data=np.ones((1, 2, 2), dtype="float32"),
        transform=Affine(10, 0, 0, 0, -10, 20),
        crs=CRS.from_epsg(25833),
        path="mem",
        nodata=None,
        band_names=["b1"],
    )
    target = tmp_path / "o.tif"
    api.registry().output("geotiff").write(raster, target, spec)
    with rasterio.open(target) as src:
        tags = src.tags()
    assert tags["GEOFACT_OUTPUT_LICENSE"].startswith(
        "Lizenz dieses Ergebnisses: © Eigene Auswertung"
    )
    assert (
        tags["TIFFTAG_COPYRIGHT"]
        == "© Stadt Beispiel (CC-BY-4.0, https://creativecommons.org/licenses/by/4.0/)"
    )


def test_fa73_rdf_dataset_carries_the_output_license_and_sources_move_to_source_datasets(
    tmp_path,
):
    document = _document(
        [{"type": "rdf", "source": "einzug", "path": "o.ttl", "license": ODBL}]
    )
    _run(document, tmp_path)
    graph = Graph().parse(tmp_path / "out" / "o.ttl", format="turtle")
    dataset = GEOFACT["dataset/einzug"]
    assert set(graph.objects(dataset, DCTERMS.license)) == {URIRef(ODBL["url"])}
    [rights] = list(graph.objects(dataset, DCTERMS.rights))
    assert str(rights).startswith("Lizenz dieses Ergebnisses: © Eigene Auswertung")
    source = GEOFACT["source/umspannwerke"]
    assert (dataset, DCTERMS.source, source) in graph
    assert (dataset, PROV.wasDerivedFrom, source) in graph
    assert (source, RDF.type, DCAT.Dataset) in graph
    assert set(graph.objects(source, DCTERMS.license)) == {URIRef(CC_BY["url"])}


def test_fa73_rdf_without_lineage_puts_source_licenses_on_numbered_source_datasets():
    import geopandas as gpd
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame({"a": [1]}, geometry=[Point(13, 51)], crs="EPSG:4326")
    graph = build_graph(
        gdf,
        "x",
        NodeAttribution(licenses=(api.License(**CC_BY),)),
        output_license=OutputLicense(api.License(name="CC0-1.0")),
    )
    dataset, source = GEOFACT["dataset/x"], GEOFACT["dataset/x/source/1"]
    assert set(graph.objects(dataset, DCTERMS.license)) == {Literal("CC0-1.0")}
    assert (dataset, DCTERMS.source, source) in graph
    assert set(graph.objects(source, DCTERMS.license)) == {URIRef(CC_BY["url"])}


def test_fa73_rdf_without_output_license_keeps_the_fa65_dataset_licenses():
    import geopandas as gpd
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame({"a": [1]}, geometry=[Point(13, 51)], crs="EPSG:4326")
    attribution = NodeAttribution(licenses=(api.License(**CC_BY),))
    graph = build_graph(gdf, "x", attribution, output_license=None)
    assert isomorphic(graph, build_graph(gdf, "x", attribution))
    assert set(graph.objects(GEOFACT["dataset/x"], DCTERMS.license)) == {
        URIRef(CC_BY["url"])
    }
