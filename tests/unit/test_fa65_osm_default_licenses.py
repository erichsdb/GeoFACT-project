"""Implements: FA65 (Default-Lizenz der OSM-basierten Quellarten osm, region, geocode).

Daten aus OpenStreetMap stehen unter der ODbL-1.0; die drei Quellarten, die
OSM-Daten liefern (Overpass/PostGIS, Nominatim-Regionen, Nominatim-Orte), tragen
deshalb ohne Angabe in der YAML diese Lizenz (Hook ``default_license()``).
"""

from __future__ import annotations

from geofact.builtin._licenses import OSM_LICENSE
from geofact.builtin.sources.geocode import GeocodeLayer
from geofact.builtin.sources.osm import OsmLayer
from geofact.builtin.sources.region import RegionLayer


def test_fa65_osm_license_constant():
    assert OSM_LICENSE.name == "ODbL-1.0"
    assert OSM_LICENSE.attribution == "OpenStreetMap contributors"
    assert OSM_LICENSE.url == "https://www.openstreetmap.org/copyright"
    assert OSM_LICENSE.line() == (
        "© OpenStreetMap contributors (ODbL-1.0, https://www.openstreetmap.org/copyright)"
    )


def test_fa65_osm_region_geocode_default_to_odbl():
    layers = [
        OsmLayer(id="a", tags={"power": "substation"}),
        RegionLayer(id="b"),
        GeocodeLayer(id="c", query="Chemnitz Hauptbahnhof"),
    ]
    for layer in layers:
        assert layer.default_license() == OSM_LICENSE, layer.id


def test_fa65_default_license_is_not_part_of_the_dump():
    """Der Default ist ein Hook, kein Feld: bestehende Dumps bleiben gleich."""
    dumped = OsmLayer(id="a", tags={"power": "substation"}).model_dump()
    assert "default_license" not in dumped


def _region_scenario(region: str, layer_region: str | None = None) -> dict:
    layer = {"id": "gebiet", "source": "region"}
    if layer_region is not None:
        layer["region"] = layer_region
    return {
        "scenario": {"name": "Gebiet", "region": region},
        "layers": [layer],
        "steps": [
            {
                "id": "raster",
                "op": "hex_grid",
                "inputs": {"region": "gebiet"},
                "params": {"cell_km": 0.5},
            }
        ],
        "output": [{"type": "geojson", "source": "raster"}],
    }


def test_fa65_region_layer_from_a_bbox_literal_has_no_odbl_default():
    """Nachtreview 24: a bbox literal contains no OSM data, but source: region always
    claimed ODbL-1.0 (ATTRIBUTION.txt, map footer, GeoTIFF tags, RDF)."""
    from geofact import api

    document = api.load_scenario(_region_scenario("12.30,51.30,12.40,51.36"))
    assert document.scenario.layers[0].effective_license() is None
    attribution = api.output_attribution(document)
    assert not any("OpenStreetMap" in line for line in attribution.lines())
    assert attribution.undeclared == ("gebiet",)


def test_fa65_region_layer_from_a_name_keeps_the_odbl_default():
    from geofact import api

    document = api.load_scenario(_region_scenario("Leipzig, Deutschland"))
    assert document.scenario.layers[0].effective_license() == OSM_LICENSE


def test_fa65_region_layer_with_its_own_region_uses_that_for_the_default():
    from geofact import api

    named = api.load_scenario(
        _region_scenario("12.30,51.30,12.40,51.36", "Leipzig, Deutschland")
    )
    assert named.scenario.layers[0].effective_license() == OSM_LICENSE
    literal = api.load_scenario(
        _region_scenario("Leipzig, Deutschland", "12.30,51.30,12.40,51.36")
    )
    assert literal.scenario.layers[0].effective_license() is None
