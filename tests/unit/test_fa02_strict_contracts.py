"""Implements: FA2 (Konfiguration vor der Ausführung prüfen: strikte Verträge), FA40 (Params je Operation).

Contract-Tests der einheitlich strikten Konfigurationsverträge:

- unbekannte Schlüssel sind überall ein Fehler (Layer, Validierungsregeln,
  Meta, Schritt, Parameter, Ausgabe-Optionen) - mit Position;
- Parameter einer Operation stehen als ``class Params(ParamsBase)`` an der
  Operation: Typ, Auswahl, Grenzen, Pflicht, Defaults, Querprüfungen;
- jede Ausgabe hat eine Quelle, und das Format kann deren Datentyp schreiben -
  vor dem Lauf;
- der Katalog (Typ/Pflicht/Default/Auswahl/Grenze) wird aus ``Params``
  abgeleitet.

Alles offline: Region als BBox-Literal, es wird nichts geladen.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel, ValidationError

from geofact import api
from geofact.core.contracts import ParamsBase, Step
from geofact.core.errors import PluginError, PluginLoadWarning
from geofact.core.registry import register_operation
from geofact.core.scenario import Scenario
from geofact.core.types import DataType
from geofact.engine import catalog
from geofact.engine.discovery import discover

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
SRC_DIR = Path(__file__).resolve().parents[2] / "src" / "geofact"
BBOX_REGION = "13.60,51.01,13.94,51.15"

UNKNOWN_FIELD = "unbekanntes Feld (hier nicht erlaubt; Tippfehler?)"


def _config(**overrides) -> dict:
    config = {
        "scenario": {"name": "Strikt", "region": BBOX_REGION},
        "layers": [
            {
                "id": "umspannwerke",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "substations.geojson"),
            },
            {
                "id": "leitungen",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "power_lines.geojson"),
            },
            {
                "id": "bevoelkerung",
                "source": "file",
                "data_type": "raster",
                "path": str(FIXTURES_DIR / "population.tif"),
            },
        ],
        "steps": [
            {
                "id": "einzug",
                "op": "buffer",
                "inputs": {"geometry": "umspannwerke"},
                "params": {"radius_km": 1},
            },
        ],
        "output": [{"type": "geojson", "source": "einzug", "path": "einzug.geojson"}],
    }
    config.update(overrides)
    return config


def _issues(config: dict) -> list[tuple[str, str]]:
    report = api.validate(config)
    assert not report.valid, "die Konfiguration hätte abgelehnt werden müssen"
    return report.issues


def _step(**overrides) -> dict:
    step = {
        "id": "einzug",
        "op": "buffer",
        "inputs": {"geometry": "umspannwerke"},
        "params": {"radius_km": 1},
    }
    step.update(overrides)
    return step


# =====================================================================
# Unbekannte Schlüssel: Layer, Validierung, Meta, Wurzel
# =====================================================================


def test_fa02_unknown_layer_key_is_rejected_with_its_position():
    config = _config()
    config["layers"].append(
        {
            "id": "strassen",
            "source": "osm",
            "tags": {"highway": "primary"},
            "validaton": {"required_fields": ["name"]},
        }
    )  # Tippfehler
    assert _issues(config) == [("layers -> 3 -> validaton", UNKNOWN_FIELD)]


def test_fa02_unknown_key_on_every_layer_kind_is_rejected():
    for layer in (
        {"id": "x", "source": "osm", "tags": {"power": "line"}, "crss": "EPSG:4326"},
        {"id": "x", "source": "region", "lable": "Region"},
        {
            "id": "x",
            "source": "wfs",
            "url": "https://w.example/wfs",
            "typename": "a:b",
            "data_type": "vector",
            "verison": "2.0.0",
        },
    ):
        config = _config()
        config["layers"].append(layer)
        (location, message), *rest = _issues(config)
        assert location.startswith("layers -> 3 -> ") and message == UNKNOWN_FIELD, (
            layer
        )


def test_fa02_unknown_validation_rule_key_is_rejected():
    config = _config()
    config["layers"][0]["validation"] = {"required_feilds": ["voltage"]}
    assert _issues(config) == [
        ("layers -> 0 -> validation -> required_feilds", UNKNOWN_FIELD)
    ]


def test_fa02_unknown_nested_rule_key_is_rejected():
    config = _config()
    config["layers"][0]["validation"] = {"constraints": {"voltage": {"minimum": 1}}}
    assert _issues(config) == [
        (
            "layers -> 0 -> validation -> constraints -> voltage -> minimum",
            UNKNOWN_FIELD,
        )
    ]


def test_fa02_unknown_scenario_meta_step_and_root_keys_are_rejected():
    config = _config()
    config["scenario"]["regoin"] = "x"
    config["steps"][0]["parms"] = {}
    config["outputs"] = []
    issues = dict(_issues(config))
    assert issues["scenario -> regoin"] == UNKNOWN_FIELD
    assert issues["steps -> 0 -> parms"] == UNKNOWN_FIELD
    assert issues["outputs"] == UNKNOWN_FIELD


def test_fa02_unknown_geometry_mapping_and_column_spec_keys_are_rejected():
    config = _config()
    config["layers"].append(
        {
            "id": "tabelle",
            "source": "table",
            "path": "x.csv",
            "geometry": {"x_column": "lon", "y_column": "lat", "crss": "EPSG:4326"},
            "columns": {"name": {"tpye": "string"}},
        }
    )
    issues = dict(_issues(config))
    assert issues["layers -> 3 -> geometry -> crss"] == UNKNOWN_FIELD
    assert issues["layers -> 3 -> columns -> name -> tpye"] == UNKNOWN_FIELD


# =====================================================================
# Parameter: unbekannt, Typ, Auswahl, Pflicht, Querprüfung
# =====================================================================


def test_fa02_unknown_param_is_rejected_with_its_position():
    config = _config()
    config["steps"] = [
        {
            "id": "werte",
            "op": "zonal_stats",
            "inputs": {"zones": "einzug", "values": "bevoelkerung"},
            "params": {"stat": "sum", "decimls": 2},
        },
    ]
    config["steps"].insert(0, _step())
    config["output"] = [{"type": "geojson", "source": "werte"}]
    assert _issues(config) == [("steps -> 1 -> params -> decimls", UNKNOWN_FIELD)]


def test_fa02_params_of_an_operation_without_parameters_are_rejected():
    config = _config()
    config["steps"] = [
        _step(
            op="to_points", inputs={"features": "umspannwerke"}, params={"radius_km": 1}
        )
    ]
    assert _issues(config) == [("steps -> 0 -> params -> radius_km", UNKNOWN_FIELD)]


def test_fa02_bad_enum_param_names_the_allowed_values():
    config = _config()
    config["steps"] = [
        _step(),
        {
            "id": "werte",
            "op": "zonal_stats",
            "inputs": {"zones": "einzug", "values": "bevoelkerung"},
            "params": {"stat": "median"},
        },
    ]
    config["output"] = [{"type": "geojson", "source": "werte"}]
    ((location, message),) = _issues(config)
    assert location == "steps -> 1 -> params -> stat"
    assert message.startswith(
        "ungültiger Wert, erlaubt sind: 'sum', 'mean', 'max', 'min', 'count', 'std' or 'share'"
    )
    assert "(erhalten: 'median')" in message


def test_fa02_centrality_method_that_does_not_exist_is_rejected():
    """Vorher still ignoriert: 'closeness' wurde akzeptiert, gerechnet wurde Betweenness."""
    config = _config()
    config["steps"] = [
        {
            "id": "netz",
            "op": "build_network",
            "inputs": {"lines": "leitungen", "nodes": "umspannwerke"},
        },
        {
            "id": "zentral",
            "op": "centrality",
            "inputs": {"graph": "netz"},
            "params": {"method": "closeness"},
        },
        {"id": "knoten", "op": "graph_to_vector", "inputs": {"graph": "zentral"}},
    ]
    config["output"] = [{"type": "geojson", "source": "knoten"}]
    ((location, message),) = _issues(config)
    assert location == "steps -> 1 -> params -> method"
    assert "erlaubt sind: 'betweenness'" in message


def test_fa02_wrong_param_type_is_rejected_with_its_position_and_value():
    config = _config()
    config["steps"][0]["params"]["radius_km"] = "5km"
    assert _issues(config) == [
        ("steps -> 0 -> params -> radius_km", "muss eine Zahl sein (erhalten: '5km')")
    ]


def test_fa02_wrong_param_type_raises_a_located_validation_error_for_the_model():
    config = _config()
    config["steps"][0]["params"]["radius_km"] = "5km"
    with pytest.raises(ValidationError) as caught:
        Scenario(**config)
    ((error),) = caught.value.errors()
    assert error["type"] == "float_parsing"
    assert error["loc"] == ("steps", 0, "params", "radius_km")


def test_fa02_integer_param_rejects_floats_booleans_and_strings():
    for bad in (2.5, True, "3"):
        config = _config()
        config["steps"] = [
            {
                "id": "n",
                "op": "top_n",
                "inputs": {"features": "umspannwerke"},
                "params": {"by": "voltage", "n": bad},
            },
        ]
        config["output"] = [{"type": "geojson", "source": "n"}]
        ((location, message),) = _issues(config)
        assert location == "steps -> 0 -> params -> n", bad
        assert message.startswith("muss eine ganze Zahl sein"), (bad, message)


def test_fa02_numeric_strings_are_accepted_for_float_params():
    """Pydantic-Lax-Modus: '5' ist eine lesbare Zahl, kein stiller Datenverlust.
    Eine Angabe wie '5km' bleibt ein Fehler (siehe oben)."""
    config = _config()
    config["steps"][0]["params"]["radius_km"] = "5"
    document = api.load_scenario(config)
    assert document.scenario.steps[0].params["radius_km"] == 5.0


def test_fa02_bound_violation_names_the_bound():
    config = _config()
    config["steps"][0]["params"]["radius_km"] = 0
    assert _issues(config) == [
        ("steps -> 0 -> params -> radius_km", "muss größer als 0 sein (erhalten: 0)")
    ]


def test_fa02_missing_required_param_is_reported_at_the_param():
    config = _config()
    config["steps"][0]["params"] = {}
    assert _issues(config) == [
        ("steps -> 0 -> params -> radius_km", "Pflichtfeld fehlt")
    ]


def test_fa02_cross_field_rules_keep_german_messages():
    config = _config()
    config["steps"] = [
        {
            "id": "r",
            "op": "ranking",
            "inputs": {"features": "umspannwerke"},
            "params": {"by": ["a", "b"], "weights": [0.7, 0.7]},
        },
    ]
    config["output"] = [{"type": "geojson", "source": "r"}]
    assert _issues(config) == [
        ("steps -> 0 -> params", "weights müssen sich zu 1.0 summieren")
    ]

    config["steps"] = [
        {
            "id": "c",
            "op": "classify",
            "inputs": {"features": "umspannwerke"},
            "params": {"field": "voltage", "breaks": [1, 2], "labels": ["a", "b"]},
        },
    ]
    config["output"] = [{"type": "geojson", "source": "c"}]
    assert _issues(config) == [
        ("steps -> 0 -> params", "Anzahl labels (2) muss Anzahl breaks + 1 (3) sein")
    ]

    config["steps"] = [
        {
            "id": "a",
            "op": "aggregate",
            "inputs": {"features": "umspannwerke", "zones": "leitungen"},
            "params": {"statistic": "sum"},
        },
    ]
    config["output"] = [{"type": "geojson", "source": "a"}]
    assert _issues(config) == [
        ("steps -> 0 -> params", "statistic 'sum' benötigt value_field")
    ]


def test_fa02_isochrone_breaks_must_be_positive_and_ascending():
    base = {
        "id": "iso",
        "op": "isochrone",
        "inputs": {"graph": "netz", "origins": "umspannwerke"},
    }
    for breaks, text in (([500, 100], "aufsteigend"), ([0, 100], "> 0")):
        config = _config()
        config["steps"] = [
            {
                "id": "netz",
                "op": "build_network",
                "inputs": {"lines": "leitungen", "nodes": "umspannwerke"},
            },
            {**base, "params": {"breaks_m": breaks, "max_snap_m": 50}},
        ]
        config["output"] = [{"type": "geojson", "source": "iso"}]
        ((location, message),) = _issues(config)
        assert location == "steps -> 1 -> params -> breaks_m" and text in message


def test_fa02_validated_params_include_the_defaults():
    config = _config()
    config["steps"] = [
        _step(),
        {
            "id": "werte",
            "op": "zonal_stats",
            "inputs": {"zones": "einzug", "values": "bevoelkerung"},
        },
        {
            "id": "rang",
            "op": "ranking",
            "inputs": {"features": "werte"},
            "params": {"by": ["sum_value"], "weights": [1.0]},
        },
    ]
    config["output"] = [{"type": "geojson", "source": "rang"}]
    steps = {s.id: s for s in api.load_scenario(config).scenario.steps}
    assert steps["einzug"].params == {"radius_km": 1.0}
    # FA47: zonal_stats got output_field, band and class_value (all optional, None = not given)
    assert steps["werte"].params == {
        "stat": "sum",
        "decimals": 0,
        "output_field": None,
        "band": None,
        "class_value": None,
        # FA8 Nenner-Quote (W1-05): defaults keep the old behaviour
        "valid_share_field": None,
        "min_valid_share": 0.0,
    }
    assert steps["rang"].params == {
        "by": ["sum_value"],
        "weights": [1.0],
        "ascending": None,
        "output_field": "score",
    }
    # die Standard-Ausgabespalte kommt aus den validierten Parametern (FA12)
    assert steps["rang"].produced_fields() == {"score"}
    assert steps["rang"].params_model.output_field == "score"


def test_fa02_isochrone_breaks_stay_integers_for_integral_values():
    """Die validierten Parameter sind Gleitkommazahlen; die Spalte break_m trägt
    weiterhin 500 und nicht 500.0, wie vor dem Umbau."""
    import geopandas as gpd
    import networkx as nx
    from shapely.geometry import Point

    from geofact.builtin.operations.reachability import run_isochrone
    from geofact.core.types import Graph

    nx_graph = nx.Graph()
    nx_graph.add_node("a", geometry=Point(0, 0))
    nx_graph.add_node("b", geometry=Point(300, 0))
    nx_graph.add_edge("a", "b", length=300.0)
    origins = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs="EPSG:32633")
    result = run_isochrone(
        {"graph": Graph(nx_graph, crs="EPSG:32633"), "origins": origins},
        {"breaks_m": [100.0, 500.0], "max_snap_m": 10.0, "buffer_m": 50.0},
    )
    assert list(result["break_m"]) == [100, 500]
    assert all(isinstance(value, int) for value in result["break_m"].tolist())


# =====================================================================
# Ausgaben: Quelle Pflicht, Datentyp, Optionen
# =====================================================================


def test_fa02_graph_output_to_a_vector_only_format_is_rejected_before_the_run():
    """Ein Graph an ein Format, das nur Vektor-Layer schreibt (rdf): abgelehnt, bevor ein Layer
    geladen wird. Ersetzt die früheren Fälle 'Graph -> CSV' - seit FA53 schreiben geojson,
    csv und map auch Graphen (test_fa53_*)."""
    config = _config()
    config["steps"] = [
        {
            "id": "netz",
            "op": "build_network",
            "inputs": {"lines": "leitungen", "nodes": "umspannwerke"},
        },
    ]
    config["output"] = [
        {"type": "rdf", "source": "netz", "vocabulary": "geosparql", "path": "netz.ttl"}
    ]
    ((location, message),) = _issues(config)
    assert location == "output -> 0 -> source"
    assert (
        message
        == "Ausgabeformat 'rdf' schreibt nur Vektor, die Quelle 'netz' liefert aber Graph"
    )
    with pytest.raises(api.ConfigError, match="schreibt nur Vektor"):
        api.load_scenario(config)


def test_fa02_output_type_mismatch_is_rejected_before_the_run():
    """Ein Raster an ein Vektorformat: abgelehnt, bevor ein Layer geladen wird."""
    config = _config()
    config["output"] = [
        {"type": "geojson", "source": "bevoelkerung", "path": "raster.geojson"}
    ]
    ((location, message),) = _issues(config)
    assert location == "output -> 0 -> source"
    assert "liefert aber Raster" in message


def test_fa02_output_without_source_is_rejected():
    config = _config()
    config["output"] = [{"type": "geojson", "path": "x.geojson"}]
    assert _issues(config) == [("output -> 0 -> source", "Pflichtfeld fehlt")]


def test_fa02_output_of_an_unknown_source_is_located():
    config = _config()
    config["output"] = [{"type": "geojson", "source": "gibt_es_nicht"}]
    assert _issues(config) == [
        (
            "output -> 0 -> source",
            "Ausgabe referenziert unbekannte Quelle 'gibt_es_nicht'",
        )
    ]


def test_fa02_output_of_a_layer_is_allowed_when_the_format_accepts_its_type():
    config = _config()
    config["output"] = [
        {"type": "geojson", "source": "umspannwerke", "path": "u.geojson"}
    ]
    assert api.validate(config).valid


def test_fa02_output_options_are_typed_and_carry_the_format_defaults():
    config = _config()
    config["output"] = [
        {"type": "map", "source": "einzug", "color_field": "voltage"},
        {"type": "map", "source": "einzug", "path": "ohne.html"},
        {"type": "geojson", "source": "einzug"},
    ]
    colored, plain, geojson = api.load_scenario(config).scenario.output
    assert colored.options.color_field == "voltage"
    assert plain.options.color_field is None  # Default des Formats
    assert geojson.options.graph_part == "edges"  # Default des Formats (FA53)
    # flach in der YAML und im Dump, die Optionen selbst sind kein Feld
    assert colored.model_dump()["color_field"] == "voltage"
    assert "options" not in colored.model_dump()


def test_fa02_output_option_of_the_wrong_type_is_located():
    config = _config()
    config["output"] = [{"type": "rdf", "source": "einzug", "vocabulary": "skos"}]
    ((location, message),) = _issues(config)
    assert location == "output -> 0 -> vocabulary" and message.startswith(
        "ungültiger Wert"
    )


def test_fa02_unknown_output_option_names_the_allowed_ones():
    config = _config()
    config["output"] = [{"type": "map", "source": "einzug", "colour_field": "voltage"}]
    ((location, message),) = _issues(config)
    assert location == "output -> 0 -> colour_field"
    # FA49/FA53: the map output got raster options, normalize_by_area and graph_part next to color_field
    assert (
        "erlaubt sind ['band', 'color_field', 'colormap', 'graph_part', "
        "'normalize_by_area', 'vmax', 'vmin']"
    ) in message


# =====================================================================
# Position der Fehler: Elemente werden einzeln aufgelöst
# =====================================================================


def test_fa02_missing_layer_field_is_located_at_the_element():
    config = _config()
    config["layers"].append({"id": "strassen", "source": "osm", "data_type": "vector"})
    assert _issues(config) == [("layers -> 3 -> tags", "Pflichtfeld fehlt")]


def test_fa02_errors_of_layers_steps_and_outputs_are_collected_at_once():
    config = _config()
    config["layers"].append({"id": "strassen", "source": "osm", "data_type": "vector"})
    config["steps"][0]["params"]["radius_km"] = "5km"
    config["steps"].append(_step(id="zweiter", params={"radius": 1}))
    config["output"][0]["source"] = "nirgendwo"
    issues = dict(_issues(config))
    assert set(issues) == {
        "layers -> 3 -> tags",
        "steps -> 0 -> params -> radius_km",
        "steps -> 1 -> params -> radius",
        "steps -> 1 -> params -> radius_km",
    }


def test_fa02_unknown_operation_and_source_are_located_at_their_field():
    config = _config()
    config["layers"].append({"id": "x", "source": "nirgendwo"})
    config["steps"].append({"id": "y", "op": "gibt_es_nicht", "inputs": {}})
    issues = dict(_issues(config))
    assert "Unbekannte Quellart 'nirgendwo'" in issues["layers -> 3 -> source"]
    assert "Unbekannte Operation 'gibt_es_nicht'" in issues["steps -> 1 -> op"]


def test_fa02_missing_op_and_source_fields_are_located():
    config = _config()
    config["layers"].append({"id": "x"})
    config["steps"].append({"id": "y", "inputs": {}})
    issues = dict(_issues(config))
    assert "Feld 'source' fehlt" in issues["layers -> 3 -> source"]
    assert "Feld 'op' fehlt" in issues["steps -> 1 -> op"]


def test_fa02_port_type_mismatch_is_located_at_the_input():
    """Das Ergebnis einer Operation (Graph) passt nicht zum Eingang der nächsten (Vektor)."""
    config = _config()
    config["steps"] = [
        {
            "id": "netz",
            "op": "build_network",
            "inputs": {"lines": "leitungen", "nodes": "umspannwerke"},
        },
        {
            "id": "puffer",
            "op": "buffer",
            "inputs": {"geometry": "netz"},
            "params": {"radius_km": 1},
        },
    ]
    config["output"] = [{"type": "geojson", "source": "puffer"}]
    assert _issues(config) == [
        (
            "steps -> 1 -> inputs -> geometry",
            "Schritt 'puffer': Eingang 'geometry' erwartet vector, erhält aber graph von 'netz'",
        )
    ]


def test_fa02_unknown_input_source_is_located_at_the_port():
    config = _config()
    config["steps"][0]["inputs"] = {"geometry": "nirgendwo"}
    ((location, message),) = _issues(config)
    assert location == "steps -> 0 -> inputs -> geometry"
    assert "unbekannte Quelle 'nirgendwo'" in message


def test_fa02_spatial_check_reference_errors_are_located():
    config = _config()
    config["layers"][1]["validation"] = {"spatial_check": {"within": "gibt_es_nicht"}}
    ((location, message),) = _issues(config)
    assert location == "layers -> 1 -> validation -> spatial_check -> within"
    assert "unbekannten Layer 'gibt_es_nicht'" in message


# =====================================================================
# Operationen: Params statt REQUIRED_PARAMS/PARAMS
# =====================================================================


def test_fa02_legacy_param_declarations_are_rejected_at_registration():
    class LegacyRequired(Step):
        op: Literal["legacy_required"] = "legacy_required"
        INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
        REQUIRED_PARAMS: ClassVar[set[str]] = {"x"}

    class LegacyParams(Step):
        op: Literal["legacy_params"] = "legacy_params"
        INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
        PARAMS: ClassVar[dict[str, dict]] = {"x": {"type": "number"}}

    for step in (LegacyRequired, LegacyParams):
        with pytest.raises(
            PluginError, match=r"Parameter als class Params\(ParamsBase\) deklarieren"
        ):
            register_operation(step)


def test_fa02_params_must_be_a_params_base_subclass_that_forbids_unknown_keys():
    class NotParams(BaseModel):
        x: int = 1

    class Lax(ParamsBase):
        model_config = {"extra": "allow"}

    class WrongBase(Step):
        op: Literal["wrong_base"] = "wrong_base"
        Params: ClassVar[type] = NotParams

    class LaxParams(Step):
        op: Literal["lax_params"] = "lax_params"
        Params: ClassVar[type[ParamsBase]] = Lax

    with pytest.raises(PluginError, match="Unterklasse von ParamsBase"):
        register_operation(WrongBase)
    with pytest.raises(PluginError, match="extra='forbid'"):
        register_operation(LaxParams)


_PLUGIN_WITH_PARAMS = textwrap.dedent("""
    from typing import ClassVar, Literal

    from pydantic import Field, model_validator

    from geofact.plugin_api import DataType, ParamsBase, Step, register_operation


    class ScaleStep(Step):
        op: Literal["skalieren"] = "skalieren"
        INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
        OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

        class Params(ParamsBase):
            faktor: float = Field(..., gt=0, description="Streckungsfaktor")
            modus: Literal["relativ", "absolut"] = "relativ"

            @model_validator(mode="after")
            def check(self):
                if self.modus == "absolut" and self.faktor > 100:
                    raise ValueError("faktor darf im Modus 'absolut' höchstens 100 sein")
                return self


    @register_operation(ScaleStep)
    def run(inputs, params):
        out = inputs["features"].copy()
        out["faktor"] = params["faktor"]
        out["modus"] = params["modus"]
        return out
""")


def _plugin_registry(tmp_path: Path, source: str):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "skalieren.py").write_text(source, encoding="utf-8")
    return discover(plugin_paths=[plugins])


def test_fa02_plugin_operation_declares_params_like_a_core_operation(tmp_path):
    registry = _plugin_registry(tmp_path, _PLUGIN_WITH_PARAMS)
    config = _config()
    config["steps"] = [
        {
            "id": "s",
            "op": "skalieren",
            "inputs": {"features": "umspannwerke"},
            "params": {"faktor": "2"},
        }
    ]
    config["output"] = [{"type": "geojson", "source": "s"}]
    document = api.load_scenario(config, registry=registry)
    assert document.scenario.steps[0].params == {"faktor": 2.0, "modus": "relativ"}

    entry = next(
        e for e in catalog.operation_catalog(registry) if e["op"] == "skalieren"
    )
    assert entry["required_params"] == ["faktor"]
    assert entry["params"]["faktor"] == {
        "type": "number",
        "required": True,
        "description": "Streckungsfaktor",
        "minimum": 0,
        "exclusive_minimum": True,
    }

    for params, location, text in (
        ({"faktor": 0}, "steps -> 0 -> params -> faktor", "muss größer als 0 sein"),
        (
            {"faktor": 1, "modus": "x"},
            "steps -> 0 -> params -> modus",
            "ungültiger Wert",
        ),
        ({"faktor": 1, "tipp": 1}, "steps -> 0 -> params -> tipp", UNKNOWN_FIELD),
        ({"faktor": 500, "modus": "absolut"}, "steps -> 0 -> params", "höchstens 100"),
    ):
        config["steps"][0]["params"] = params
        report = api.validate(config, registry=registry)
        ((found_location, message),) = report.issues
        assert found_location == location and text in message, params


def test_fa02_plugin_with_legacy_params_is_skipped_with_a_warning_and_named(tmp_path):
    legacy = _PLUGIN_WITH_PARAMS.replace(
        "    class Params(ParamsBase):",
        '    REQUIRED_PARAMS: ClassVar[set[str]] = {"faktor"}\n\n'
        "    class Params(ParamsBase):",
    )
    with pytest.warns(PluginLoadWarning, match="class Params"):
        registry = _plugin_registry(tmp_path, legacy)
    assert "skalieren" not in registry.names("operation")
    assert any("skalieren.py" in origin for origin in registry.failed)


def test_fa02_a_type_error_in_a_plugin_validator_is_located_at_the_step(tmp_path):
    broken = _PLUGIN_WITH_PARAMS.replace(
        'if self.modus == "absolut" and self.faktor > 100:', 'if self.faktor > "100":'
    )
    registry = _plugin_registry(tmp_path, broken)
    config = _config()
    config["steps"] = [
        {
            "id": "s",
            "op": "skalieren",
            "inputs": {"features": "umspannwerke"},
            "params": {"faktor": 2},
        }
    ]
    config["output"] = [{"type": "geojson", "source": "s"}]
    ((location, message),) = api.validate(config, registry=registry).issues
    assert location == "steps -> 0" and message.startswith("TypeError")


# =====================================================================
# Katalog: aus Params abgeleitet
# =====================================================================


def _catalog() -> dict[str, dict]:
    return {entry["op"]: entry for entry in catalog.operation_catalog()}


def test_fa02_catalog_params_carry_type_default_choices_and_bounds():
    entries = _catalog()
    buffer = entries["buffer"]["params"]["radius_km"]
    assert buffer == {
        "type": "number",
        "required": True,
        "description": "Pufferradius in Kilometern.",
        "minimum": 0,
        "exclusive_minimum": True,
    }
    stat = entries["zonal_stats"]["params"]["stat"]
    assert stat["type"] == "enum"
    assert stat["choices"] == [
        "sum",
        "mean",
        "max",
        "min",
        "count",
        "std",
        "share",
    ]  # FA47
    assert stat["default"] == "sum" and stat["required"] is False
    ranking = entries["ranking"]["params"]
    assert (
        ranking["by"]["item_type"] == "string"
        and ranking["weights"]["item_type"] == "number"
    )
    assert ranking["ascending"]["item_type"] == "boolean"
    assert ranking["output_field"]["default"] == "score"
    assert entries["network_nodes"]["params"]["tolerance_m"]["minimum"] == 0
    assert entries["hexbin"]["params"]["min_count"]["type"] == "integer"
    # ein Optional-Feld ohne Wert trägt keinen Default (der Editor schriebe sonst null)
    assert "default" not in entries["dissolve"]["params"]["by"]


def test_fa02_catalog_rest_params_are_typed_not_all_strings():
    """Die handgeschriebene Beschreibung nannte jeden Parameter von op: rest 'string';
    abgeleitet aus RestCall sind es Objekte, Zahlen und Auswahlen."""
    params = _catalog()["rest"]["params"]
    assert (
        params["input_types"]["type"] == "object" and params["input_types"]["required"]
    )
    assert params["body"]["type"] == "object" and params["headers"]["type"] == "object"
    assert params["method"] == {
        "type": "enum",
        "choices": ["POST", "GET"],
        "required": False,
        "default": "POST",
    }
    assert params["timeout_s"]["type"] == "number"
    assert _catalog()["rest"]["required_params"] == ["input_types", "url"]


def test_fa02_json_schema_expands_the_steps_through_their_params():
    schema = catalog.scenario_json_schema()
    variants = {
        ref["$ref"].rsplit("/", 1)[-1]
        for ref in schema["properties"]["steps"]["items"]["anyOf"]
    }
    assert {"BufferStep", "ZonalStatsStep", "RestStep"} <= variants
    buffer = schema["$defs"]["BufferStep"]
    params = schema["$defs"][buffer["properties"]["params"]["$ref"].rsplit("/", 1)[-1]]
    assert params["properties"]["radius_km"]["exclusiveMinimum"] == 0
    assert params["required"] == ["radius_km"] and "params" in buffer["required"]
