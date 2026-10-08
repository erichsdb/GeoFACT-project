"""Implements: FA65 (Lizenzmodell, Default-Hook je Quellart, Attribution durch den DAG).

Contract-Tests für ``core/contracts.py::License``/``LayerBase.license``/
``effective_license`` und ``core/provenance.py::attribution_of``/``union``
sowie ``ExecutionPlan.attribution``."""

from __future__ import annotations

from typing import Literal, Optional

import pytest
from pydantic import ValidationError

from geofact import api
from geofact.core.contracts import LayerBase, License
from geofact.core.errors import ConfigError
from geofact.core.plan import ExecutionPlan
from geofact.core.provenance import NodeAttribution, attribution_of, union
from geofact.core.types import DataType

ODBL = {
    "name": "ODbL-1.0",
    "attribution": "OpenStreetMap contributors",
    "url": "https://opendatacommons.org/licenses/odbl/",
}
DL_DE = {"name": "dl-de/by-2-0", "attribution": "Stadt Leipzig"}


# ---------------------------------------------------------------- License


def test_fa65_license_requires_a_name():
    with pytest.raises(ValidationError):
        License()  # type: ignore[call-arg]


def test_fa65_license_name_must_not_be_blank():
    with pytest.raises(ValidationError, match="license.name darf nicht leer sein"):
        License(name="  ")


def test_fa65_license_url_must_be_http():
    with pytest.raises(ValidationError, match="http:// oder https://"):
        License(name="CC-BY-4.0", url="ftp://example.org/lizenz")


def test_fa65_license_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        License(name="CC-BY-4.0", lizenz="x")  # type: ignore[call-arg]


def test_fa65_license_is_frozen_and_hashable():
    license_ = License(**ODBL)
    with pytest.raises(ValidationError):
        license_.name = "x"  # type: ignore[misc]
    assert len({license_, License(**ODBL)}) == 1


def test_fa65_license_line():
    assert License(**ODBL).line() == (
        "© OpenStreetMap contributors (ODbL-1.0, https://opendatacommons.org/licenses/odbl/)"
    )
    assert License(**DL_DE).line() == "© Stadt Leipzig (dl-de/by-2-0)"
    assert License(name="CC0-1.0").line() == "CC0-1.0"
    assert (
        License(name="CC-BY-4.0", url="https://cc.org").line()
        == "CC-BY-4.0 (https://cc.org)"
    )


# ---------------------------------------------------------------- Hook


class _OsmLikeLayer(LayerBase):
    source: Literal["osm_like"] = "osm_like"
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR

    def default_license(self) -> Optional[License]:
        return License(**ODBL)


def test_fa65_effective_license_uses_default_hook():
    layer = _OsmLikeLayer(id="x")
    assert layer.license is None
    assert layer.effective_license() == License(**ODBL)


def test_fa65_declared_license_replaces_default_completely():
    layer = _OsmLikeLayer(id="x", license=DL_DE)
    assert layer.effective_license() == License(**DL_DE)


def test_fa65_base_layer_has_no_default_license():
    class Plain(LayerBase):
        source: Literal["plain"] = "plain"
        data_type: Literal[DataType.VECTOR] = DataType.VECTOR

    assert Plain(id="p").effective_license() is None
    assert Plain(id="p").check_before_run(None) == []


# ---------------------------------------------------------------- Attribution

SCENARIO = """
scenario:
  name: Lizenz
  region: "12.2,51.2,12.5,51.4"
layers:
  - id: baeume
    source: file
    path: data/baeume.geojson
    data_type: vector
    license: {name: dl-de/by-2-0, attribution: Stadt Leipzig}
  - id: wege
    source: file
    path: data/wege.geojson
    data_type: vector
    license: {name: ODbL-1.0, attribution: OpenStreetMap contributors}
  - id: ohne
    source: file
    path: data/ohne.geojson
    data_type: vector
  - id: ungenutzt
    source: file
    path: data/x.geojson
    data_type: vector
    license: {name: CC0-1.0}
steps:
  - id: puffer
    op: buffer
    inputs: {geometry: wege}
    params: {radius_km: 1}
  - id: naehe
    op: nearest_distance
    inputs: {from: puffer, to: baeume}
  - id: doppelt
    op: nearest_distance
    inputs: {from: naehe, to: wege}
  - id: mit_ohne
    op: nearest_distance
    inputs: {from: doppelt, to: ohne}
output:
  - type: geojson
    source: mit_ohne
"""


@pytest.fixture
def scenario():
    return api.load_scenario(SCENARIO).scenario


def test_fa65_attribution_union_order_and_undeclared(scenario):
    plan = ExecutionPlan.of(scenario)
    attribution = plan.attribution
    osm, stadt = (
        License(name="ODbL-1.0", attribution="OpenStreetMap contributors"),
        License(**DL_DE),
    )
    assert attribution["puffer"] == NodeAttribution(licenses=(osm,))
    # Ports alphabetisch: from (puffer -> OSM) vor to (baeume -> Stadt)
    assert attribution["naehe"] == NodeAttribution(licenses=(osm, stadt))
    # keine Duplikate
    assert attribution["doppelt"].licenses == (osm, stadt)
    # Layer ohne Lizenz erscheint unter undeclared
    assert attribution["mit_ohne"] == NodeAttribution(
        licenses=(osm, stadt), undeclared=("ohne",)
    )
    assert attribution["ohne"].empty and attribution["ohne"].undeclared == ("ohne",)


def test_fa65_unreferenced_layers_do_not_contribute(scenario):
    plan = ExecutionPlan.of(scenario)
    assert "ungenutzt" not in plan.attribution
    all_names = {
        lic.name for node in plan.attribution.values() for lic in node.licenses
    }
    assert "CC0-1.0" not in all_names


def test_fa65_attribution_of_matches_plan(scenario):
    plan = ExecutionPlan.of(scenario)
    assert attribution_of(scenario, plan.order, plan.required_layers) == dict(
        plan.attribution
    )


def test_fa65_node_attribution_lines_and_dict():
    node = NodeAttribution(licenses=(License(**DL_DE),), undeclared=("ohne",))
    assert node.lines() == ["© Stadt Leipzig (dl-de/by-2-0)"]
    assert node.to_dict() == {
        "licenses": [
            {"name": "dl-de/by-2-0", "attribution": "Stadt Leipzig", "url": None}
        ],
        "undeclared": ["ohne"],
        "lines": ["© Stadt Leipzig (dl-de/by-2-0)"],
    }
    assert NodeAttribution().empty


def test_fa65_union_keeps_first_occurrence():
    a, b = License(name="A"), License(name="B")
    merged = union([NodeAttribution((b,), ("x",)), NodeAttribution((a, b), ("y", "x"))])
    assert merged == NodeAttribution((b, a), ("x", "y"))


def test_fa65_invalid_license_in_yaml_has_position():
    text = SCENARIO.replace(
        "license: {name: CC0-1.0}", "license: {name: CC0-1.0, url: www.x.de}"
    )
    with pytest.raises(ConfigError) as exc:
        api.load_scenario(text)
    assert any(
        loc.startswith("layers -> 3 -> license -> url") for loc, _ in exc.value.issues
    )


def test_fa65_scenario_without_licenses_has_empty_attribution():
    text = "\n".join(line for line in SCENARIO.splitlines() if "license:" not in line)
    plan = ExecutionPlan.of(api.load_scenario(text).scenario)
    assert all(node.empty for node in plan.attribution.values())


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/lizenz bedingungen",
        "https://example.org/<lizenz>",
        'https://example.org/"x"',
        "https://example.org/a|b",
        "https://example.org/a\tb",
    ],
)
def test_fa65_license_url_must_be_a_valid_iri(url):
    """Nachtreview 25: a url with a space passed validation and then broke the RDF
    export ('does not look like a valid URI') - the whole Turtle file was lost."""
    with pytest.raises(ValidationError, match="license.url"):
        License(name="X", url=url)


def test_fa65_license_url_with_non_ascii_characters_serialises_to_turtle():
    from rdflib import Graph, Namespace, URIRef

    license_ = License(name="X", url="https://example.org/lizenz-bedingungen-für-daten")
    graph = Graph()
    dcterms = Namespace("http://purl.org/dc/terms/")
    graph.add((URIRef("https://example.org/ds"), dcterms.license, URIRef(license_.url)))
    assert "lizenz-bedingungen" in graph.serialize(format="turtle")


REST_SCENARIO = """
scenario:
  name: Dienst
  region: "12.30,51.30,12.40,51.36"
layers:
  - id: messpunkte
    source: file
    path: data/messpunkte.geojson
    data_type: vector
    license: {name: CC0-1.0}
steps:
  - id: iso
    op: rest
    inputs: {features: messpunkte}
    params:
      url: https://dienst.example/isochronen
      input_types: {features: vector}
      output_type: vector
output:
  - type: geojson
    source: iso
"""


def test_fa65_rest_step_without_licence_is_marked_undeclared():
    """Nachtreview 26: data returned by an external service (op: rest) was credited
    only with the licences of the inputs, without any marker for the foreign data."""
    scenario = api.load_scenario(REST_SCENARIO).scenario
    attribution = ExecutionPlan.of(scenario).attribution["iso"]
    assert attribution.licenses == (License(name="CC0-1.0"),)
    assert attribution.undeclared == ("iso",)


def test_fa65_rest_step_can_declare_the_licence_of_the_service_data():
    text = REST_SCENARIO.replace(
        "      output_type: vector\n",
        "      output_type: vector\n"
        "      license: {name: CC-BY-4.0, attribution: Dienst GmbH}\n",
    )
    scenario = api.load_scenario(text).scenario
    attribution = ExecutionPlan.of(scenario).attribution["iso"]
    assert attribution.licenses == (
        License(name="CC0-1.0"),
        License(name="CC-BY-4.0", attribution="Dienst GmbH"),
    )
    assert attribution.undeclared == ()


def test_fa65_ordinary_steps_bring_no_external_data(scenario):
    assert not any(step.brings_external_data() for step in scenario.steps)
