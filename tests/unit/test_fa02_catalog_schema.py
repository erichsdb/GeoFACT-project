"""Implements: FA2, FA6, FA40 (Katalog und JSON-Schema bewerben nur, was die Prüfung annimmt).

Contract-Tests des Konfigurationskatalogs für Editor und LLM-Grounding
(engine/catalog.py):

- das JSON-Schema führt keine Regel auf, die die Prüfung ablehnt
  (``validation.precision``, FA6);
- ``FileLayer`` und ``TableLayer`` führen die formatspezifischen Felder ihrer
  Formate als optionale Eigenschaften auf, je Feld mit den Formaten, die es
  kennen (``x-formats``), und - wo ein Format es verlangt - ``x-required-for``;
  ebenso die Ausgabe-Deklaration (``OutputSpec``) die Felder der Ausgabeformate;
- ``extension_catalog()['outputs']`` nennt je Ausgabeformat seine ``options``
  wie die Datei- und Tabellenformate.

Alles offline, ohne Daten zu laden.
"""

from __future__ import annotations

import textwrap

import pytest

from geofact import api
from geofact.engine import catalog
from geofact.engine.discovery import discover

BBOX_REGION = "13.60,51.01,13.94,51.15"


@pytest.fixture(scope="module")
def schema() -> dict:
    return catalog.scenario_json_schema()


def _layer_props(schema: dict, name: str) -> dict:
    return schema["$defs"][name]["properties"]


def _config(validation: dict) -> dict:
    return {
        "scenario": {"name": "Schema", "region": BBOX_REGION},
        "layers": [{"id": "l", "source": "region", "validation": validation}],
        "steps": [
            {
                "id": "b",
                "op": "buffer",
                "inputs": {"geometry": "l"},
                "params": {"radius_km": 1},
            }
        ],
        "output": [{"type": "geojson", "source": "b"}],
    }


# =====================================================================
# validation.precision wird nicht beworben
# =====================================================================


def test_fa06_json_schema_does_not_advertise_the_rejected_precision_rule(schema):
    advertised = set(_layer_props(schema, "LayerValidation"))
    assert "precision" not in advertised
    assert "PrecisionRule" not in schema["$defs"]
    # Gegenprobe: ``precision`` lehnt die Prüfung weiterhin ab ...
    report = api.validate(_config({"precision": {"x": {"min_decimals": 4}}}))
    assert not report.valid and "precision" in report.issues[0][1]
    # ... und jede beworbene Regel nimmt sie an.
    accepted = {
        "required_fields": ["a"],
        "field_types": {"a": "integer"},
        "constraints": {"a": {"min": 0}},
        "spatial_check": {"within": "scenario_region"},
        "on_violation": "warn",
    }
    assert set(accepted) == advertised
    assert api.validate(_config(accepted)).valid


def test_fa06_the_precision_field_stays_on_the_model_for_validate():
    """Das Modellfeld bleibt (``validate()`` wertet es mit Roh-Strings aus);
    nur das Schema blendet es aus."""
    from geofact.core.contracts import LayerValidation

    assert "precision" in LayerValidation.model_fields
    assert "precision" not in LayerValidation.model_json_schema()["properties"]


def test_fa02_json_schema_keeps_no_unreferenced_definitions(schema):
    """Die offenen Basismodelle (``LayerBase``, ``Step``) sind durch die
    Modelle der Bausteine ersetzt; es bleiben keine verwaisten ``$defs``."""
    assert "LayerBase" not in schema["$defs"] and "Step" not in schema["$defs"]
    assert {"FileLayer", "TableLayer", "OutputSpec", "BufferStep"} <= set(
        schema["$defs"]
    )


# =====================================================================
# Formatfelder in den Layer-Schemata
# =====================================================================


def test_fa40_file_layer_schema_lists_the_format_fields(schema):
    props = _layer_props(schema, "FileLayer")
    assert props["layer"]["x-formats"] == ["gml", "gpkg"]
    for name in ("bands", "band_names", "margin_km", "resampling"):
        assert props[name]["x-formats"] == ["tif"], name
    assert props["resampling"]["enum"] == ["nearest", "bilinear", "average", "sum"]
    assert props["margin_km"]["minimum"] == 0
    # Beschreibung mit Formatpräfix, auch für Leser ohne ``x-``-Schlüsselwörter
    assert props["layer"]["description"].startswith("Formatfeld (gml, gpkg): ")
    # die eigenen Felder des Layers bleiben unverändert
    assert {
        "id",
        "source",
        "data_type",
        "path",
        "format",
        "member",
        "crs",
        "validation",
    } <= set(props)


def test_fa40_table_layer_schema_lists_the_format_fields(schema):
    props = _layer_props(schema, "TableLayer")
    assert props["table"]["x-formats"] == ["sql", "sqlite"]
    assert props["query"]["x-formats"] == ["sql", "sqlite"]
    assert props["sheet"]["x-formats"] == ["xlsx"]
    for name in ("colspecs", "names"):
        assert props[name]["x-formats"] == ["fwf"] and props[name][
            "x-required-for"
        ] == ["fwf"], name
        assert "Pflicht" in props[name]["description"]
    assert (
        props["skiprows"]["x-formats"] == ["fwf"]
        and "x-required-for" not in props["skiprows"]
    )
    assert props["skiprows"]["minimum"] == 0


def test_fa40_format_fields_are_optional_properties_not_required_ones(schema):
    for name, formats in (
        ("FileLayer", {"layer", "bands", "band_names", "margin_km", "resampling"}),
        ("TableLayer", {"table", "query", "sheet", "colspecs", "names", "skiprows"}),
    ):
        model = schema["$defs"][name]
        assert not formats & set(model["required"]), name
        assert (
            model["additionalProperties"] is True
        )  # flach: weitere Schlüssel gehen an das Format


def test_fa40_schema_format_fields_match_the_extension_catalog():
    """Das Schema und der Katalog nennen dieselben Felder je Format (eine
    Quelle: das ``options_model``)."""
    schema = catalog.scenario_json_schema()
    listing = catalog.extension_catalog()
    for key, layer in (("file_formats", "FileLayer"), ("table_formats", "TableLayer")):
        props = _layer_props(schema, layer)
        for entry in listing[key]:
            for option in entry["options"]:
                assert entry["name"] in props[option]["x-formats"], (
                    entry["name"],
                    option,
                )
    for entry in listing["outputs"]:
        props = _layer_props(schema, "OutputSpec")
        for option in entry["options"]:
            assert entry["name"] in _formats_of(props[option]), (entry["name"], option)


def _formats_of(prop: dict) -> list[str]:
    """Formate eines Feldes: direkt (``x-formats``) oder - bei strukturell
    verschiedenen Varianten - über die Varianten der ``anyOf``."""
    if "x-formats" in prop:
        return prop["x-formats"]
    return [name for variant in prop["anyOf"] for name in variant["x-formats"]]


# =====================================================================
# Ausgabe-Deklaration und Katalog der Ausgaben
# =====================================================================


def test_fa40_output_spec_schema_lists_the_output_options(schema):
    props = _layer_props(schema, "OutputSpec")
    assert {"type", "source", "path"} <= set(props)
    assert props["color_field"]["x-formats"] == ["map"]
    # ein Feld mehrerer Formate steht einmal, mit allen Formaten
    assert props["graph_part"]["x-formats"] == ["csv", "geojson", "map"]
    assert props["graph_part"]["enum"] == ["edges", "nodes"]
    assert props["extension"]["x-required-for"] == ["rest"]


def test_fa40_extension_catalog_outputs_carry_their_options():
    outputs = {entry["name"]: entry for entry in catalog.extension_catalog()["outputs"]}
    assert all("options" in entry for entry in outputs.values())
    assert "color_field" in outputs["map"]["options"]
    assert outputs["map"]["options"] == sorted(outputs["map"]["options"])
    assert outputs["geojson"]["options"] == ["crs", "graph_part"]  # FA55: Ziel-CRS
    assert outputs["geotiff"]["options"] == ["crs", "resampling"]  # FA55
    # wie bei den Formaten: genau die Felder des options_model
    registry = api.load_extensions()
    for name, entry in outputs.items():
        model = registry.output(name).options_model
        assert entry["options"] == (
            sorted(model.model_fields) if model is not None else []
        )


# =====================================================================
# Plugins: eigene Formatfelder erscheinen ohne Änderung am Katalog
# =====================================================================

_PLUGIN = textwrap.dedent("""
    from typing import Optional

    import geopandas as gpd
    from pydantic import BaseModel, ConfigDict, Field

    from geofact.plugin_api import DataType, register_file_format, register_output


    class XyzOptions(BaseModel):
        model_config = ConfigDict(extra="forbid")

        skalierung: float = Field(1.0, gt=0, description="Faktor auf z.")
        layer: int = Field(0, ge=0, description="Nummer der Schicht (anders als bei gpkg).")


    @register_file_format("xyz", extensions=("xyz",), data_type=DataType.VECTOR,
                          options_model=XyzOptions, description="XYZ")
    def read_xyz(path, layer, ctx):
        return gpd.GeoDataFrame({"z": []}, geometry=[], crs="EPSG:25833")


    class CountOptions(BaseModel):
        model_config = ConfigDict(extra="forbid")

        label: str = "Anzahl"


    @register_output("count_txt", extension="txt", options_model=CountOptions,
                     description="Anzahl der Objekte")
    def write_count(gdf, target, spec):
        target.write_text(spec.options.label)
""")


def test_fa40_plugin_format_fields_reach_schema_and_catalog_without_core_changes(
    tmp_path,
):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "xyz_plugin.py").write_text(_PLUGIN, encoding="utf-8")
    registry = discover(plugin_paths=[plugins])
    schema = catalog.scenario_json_schema(registry)
    listing = catalog.extension_catalog(registry)

    files = _layer_props(schema, "FileLayer")
    assert files["skalierung"]["x-formats"] == ["xyz"]
    # ``layer`` heißt auch bei gpkg/gml - aber mit anderem Typ: zwei Varianten
    variants = files["layer"]["anyOf"]
    assert {tuple(v["x-formats"]) for v in variants} == {("gml", "gpkg"), ("xyz",)}
    assert {v["type"] for v in variants if "type" in v} == {"integer"}

    assert _layer_props(schema, "OutputSpec")["label"]["x-formats"] == ["count_txt"]
    plugin_output = {e["name"]: e for e in listing["outputs"]}["count_txt"]
    assert plugin_output["options"] == ["label"]
    xyz = {e["name"]: e for e in listing["file_formats"]}["xyz"]
    assert xyz["options"] == ["layer", "skalierung"]


def test_fa40_a_layer_field_wins_over_a_format_field_of_the_same_name(tmp_path):
    """Ein Format, das ein eigenes Layer-Feld (``path``) noch einmal anbietet,
    überschreibt dessen Beschreibung nicht."""
    plugin = _PLUGIN.replace(
        'skalierung: float = Field(1.0, gt=0, description="Faktor auf z.")',
        'path: str = Field("x", description="Format-Pfad")',
    )
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "xyz_plugin.py").write_text(plugin, encoding="utf-8")
    schema = catalog.scenario_json_schema(discover(plugin_paths=[plugins]))
    path = _layer_props(schema, "FileLayer")["path"]
    assert "x-formats" not in path and path["title"] == "Path"


def test_fa02_json_schema_is_stable_across_calls():
    first = catalog.scenario_json_schema()
    second = catalog.scenario_json_schema()
    assert first == second


def test_fa02_schema_file_is_json_serialisable():
    import json

    text = json.dumps(catalog.scenario_json_schema())
    assert "x-formats" in text and "PrecisionRule" not in text


def test_fa40_scenario_json_schema_via_the_facade_is_the_same():
    assert api.scenario_json_schema() == catalog.scenario_json_schema(
        api.load_extensions()
    )
