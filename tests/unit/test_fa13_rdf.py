"""Implements: FA13 (RDF/GeoSPARQL-Export).

Contract-Tests für src/geofact/builtin/outputs/rdf.py.
"""

import geopandas as gpd
import pytest
import rdflib
from rdflib.namespace import GEO
from shapely.geometry import Point

from geofact.builtin.outputs import rdf as rdf_module


def sample_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": ["a"], "risk_score": [0.75], "name": ["Umspannwerk Nord"]},
        geometry=[Point(13.72, 51.04)],
        crs="EPSG:4326",
    )


def test_fa13_turtle_parses_with_rdflib(tmp_path):
    path = tmp_path / "result.ttl"
    rdf_module.write_turtle(sample_gdf(), path)

    graph = rdflib.Graph()
    graph.parse(str(path), format="turtle")
    assert len(graph) > 0


def test_fa13_geometry_as_wkt_literal():
    graph = rdf_module.build_graph(sample_gdf())
    wkt_values = list(graph.objects(predicate=GEO.asWKT))
    assert len(wkt_values) == 1
    assert wkt_values[0].datatype == GEO.wktLiteral
    assert str(wkt_values[0]).startswith(f"<{rdf_module.CRS84_URI}> POINT")


def test_fa13_wkt_literal_declares_crs84():
    """Bugfix: ein geo:wktLiteral ohne CRS-Präfix gilt nach GeoSPARQL
    implizit als CRS84 - bisher wurde das Präfix nie geschrieben, obwohl
    stets nach EPSG:4326 reprojiziert wird. Jedes Literal muss das
    Präfix jetzt explizit tragen (GeoSPARQL 11-052r4, 8.3.2)."""
    graph = rdf_module.build_graph(sample_gdf())
    wkt_values = list(graph.objects(predicate=GEO.asWKT))
    assert str(wkt_values[0]).startswith(
        "<http://www.opengis.net/def/crs/OGC/1.3/CRS84>"
    )


def test_fa13_missing_crs_raises_instead_of_silent_wkt():
    """Randfall (Goldene Regel 7): ein Layer ohne CRS darf keine WKT-
    Geometrie ohne verlässliche CRS-Zuordnung erzeugen - Fehler statt
    stillschweigend falsch beschrifteter Koordinaten."""
    gdf = gpd.GeoDataFrame({"id": ["a"]}, geometry=[Point(13.72, 51.04)], crs=None)
    with pytest.raises(ValueError, match="CRS"):
        rdf_module.build_graph(gdf)


def test_fa13_attributes_as_properties():
    graph = rdf_module.build_graph(sample_gdf(), layer_name="substations")
    feature_uri = rdf_module.GEOFACT["substations/0"]

    risk_score = list(
        graph.objects(subject=feature_uri, predicate=rdf_module.GEOFACT_PROP.risk_score)
    )
    assert len(risk_score) == 1
    assert float(risk_score[0]) == 0.75

    name = list(
        graph.objects(subject=feature_uri, predicate=rdf_module.GEOFACT_PROP.name)
    )
    assert str(name[0]) == "Umspannwerk Nord"


def test_fa13_geosparql_types_present():
    graph = rdf_module.build_graph(sample_gdf())
    assert (None, rdflib.RDF.type, GEO.Feature) in graph
    assert (None, rdflib.RDF.type, GEO.Geometry) in graph


def test_fa13_empty_layer_produces_empty_graph():
    empty = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:4326")
    graph = rdf_module.build_graph(empty)
    assert len(graph) == 0
