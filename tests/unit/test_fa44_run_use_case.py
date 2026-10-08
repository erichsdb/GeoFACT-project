"""Implements: FA44 (Szenario als ein Anwendungsfall: laden, prüfen, planen, ausführen).

Contract-Tests für geofact.api.load_scenario / validate / plan / run
(engine/run.py): Quelle (Datei, Text, dict), base_dir (FA4), Fehler mit
Position (nie ein roher TypeError), Ausgaben schreiben, Warnungen, Abbruch,
Zwischenergebnisse. Offline: Datei-Layer aus tests/fixtures und eine
BBox-Region.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal

from geofact import api
from geofact.engine.discovery import discover
from geofact.engine.run import issues_of

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _config(**overrides) -> dict:
    config = {
        "scenario": {"name": "FA44", "region": BBOX_REGION},
        "layers": [
            {
                "id": "substations",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "substations.geojson"),
            },
        ],
        "steps": [
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 1},
            },
        ],
        "output": [
            {"type": "geojson", "source": "catchment", "path": "catchment.geojson"},
            {"type": "csv", "source": "catchment", "path": "catchment.csv"},
        ],
    }
    config.update(overrides)
    return config


def _yaml_file(
    directory: Path, config: dict | None = None, name: str = "szenario.yaml"
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(
        yaml.safe_dump(config or _config(), allow_unicode=True), encoding="utf-8"
    )
    return target


@pytest.fixture(autouse=True)
def _snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))


# =====================================================================
# load_scenario: Quelle und base_dir (FA4)
# =====================================================================


def test_fa44_load_scenario_from_file_sets_base_dir_to_the_yaml_directory(tmp_path):
    path = _yaml_file(tmp_path / "konfig")
    document = api.load_scenario(path)
    assert document.base_dir == (tmp_path / "konfig").resolve()
    assert document.scenario.scenario.name == "FA44"


def test_fa44_load_scenario_keeps_the_original_text(tmp_path):
    path = _yaml_file(tmp_path)
    document = api.load_scenario(path)
    assert document.source_text == path.read_text(encoding="utf-8")
    assert yaml.safe_load(document.source_text)["scenario"]["name"] == "FA44"


def test_fa44_load_scenario_from_text_has_no_base_dir_unless_given(tmp_path):
    text = yaml.safe_dump(_config(), allow_unicode=True)
    assert api.load_scenario(text).base_dir is None
    anchored = api.load_scenario(text, base_dir=tmp_path)
    assert anchored.base_dir == tmp_path.resolve()
    assert anchored.source_text == text


def test_fa44_load_scenario_from_dict_has_no_base_dir_and_the_yaml_form_as_text():
    config = _config()
    document = api.load_scenario(config)
    assert document.base_dir is None
    assert (
        yaml.safe_load(document.source_text) == config
    )  # verlustfrei, nicht model_dump


def test_fa44_load_scenario_from_a_dict_that_is_not_yaml_has_no_text():
    config = _config()
    # Ein Path-Objekt in einem frei typisierten Feld (Anfrage-Vorlage von op: rest)
    # ist gültig, lässt sich aber nicht als YAML darstellen.
    config["steps"].append(
        {
            "id": "dienst",
            "op": "rest",
            "inputs": {"features": "catchment"},
            "params": {
                "url": "https://dienst.example/x",
                "input_types": {"features": "vector"},
                "body": {"inputs": {"g": "@features"}, "notiz": Path("irgendwo")},
            },
        }
    )
    document = api.load_scenario(config)
    assert document.source_text is None


def test_fa44_explicit_base_dir_wins_over_the_yaml_directory(tmp_path):
    path = _yaml_file(tmp_path / "konfig")
    other = tmp_path / "anderswo"
    other.mkdir()
    assert api.load_scenario(path, base_dir=other).base_dir == other.resolve()


def test_fa44_relative_layer_paths_resolve_against_the_document_base_dir(tmp_path):
    """FA4: ein relativer Pfad gilt gegen base_dir, nicht gegen das Arbeitsverzeichnis."""
    config = _config()
    config["layers"][0]["path"] = "substations.geojson"
    document = api.load_scenario(config, base_dir=FIXTURES_DIR)
    report = api.run(document, out_dir=tmp_path / "out")
    assert report.result.loaded_layers == ["substations"]

    unanchored = api.load_scenario(
        config
    )  # kein base_dir: relativ zum Arbeitsverzeichnis
    # Seit W2-02 meldet der Vorab-Check (FA44) die fehlende Datei vor dem Laden.
    with pytest.raises(api.ConfigError) as caught:
        api.run(unanchored)
    assert caught.value.issues[0][0] == "layers -> 0 (substations) -> path"


# =====================================================================
# load_scenario: Fehler mit Position, nie ein Traceback / roher TypeError
# =====================================================================


def test_fa44_load_scenario_missing_file_is_config_error(tmp_path):
    with pytest.raises(api.ConfigError, match="nicht gefunden") as caught:
        api.load_scenario(tmp_path / "gibt_es_nicht.yaml")
    assert caught.value.issues[0][0] == "(Datei)"


def test_fa44_load_scenario_yaml_syntax_error_names_line_and_column():
    with pytest.raises(api.ConfigError) as caught:
        api.load_scenario("scenario: [offen")
    location, message = caught.value.issues[0]
    assert location == "(YAML)"
    assert "Zeile" in message and "Spalte" in message


def test_fa44_load_scenario_non_mapping_is_config_error_with_path_hint():
    with pytest.raises(api.ConfigError, match="Mapping"):
        api.load_scenario("- a\n- b\n")
    with pytest.raises(api.ConfigError, match="Path"):
        api.load_scenario("examples/sachsen_nachthimmel.yaml")


def test_fa44_load_scenario_rejects_an_unsupported_source_type():
    with pytest.raises(TypeError, match="Path"):
        api.load_scenario(42)  # type: ignore[arg-type]


def test_fa44_load_scenario_never_raises_a_raw_type_error_for_a_string_radius():
    """Ein Validator, der `radius_km <= 0` vergleicht, wirft bei '5km' einen
    TypeError. load_scenario macht daraus einen ConfigError, der Schritt und
    Parameter nennt."""
    config = _config()
    config["steps"][0]["params"]["radius_km"] = "5km"
    with pytest.raises(api.ConfigError) as caught:
        api.load_scenario(config)
    assert caught.value.issues == [
        ("steps -> 0 -> params -> radius_km", "muss eine Zahl sein (erhalten: '5km')")
    ]


def test_fa44_type_error_in_a_validator_without_a_typed_param_names_the_step():
    """Auch ohne deklarierten Parametertyp bleibt es ein ConfigError mit Position."""
    config = _config()
    config["steps"][0]["params"] = {"radius_km": [1, 2]}  # Liste statt Zahl
    with pytest.raises(api.ConfigError) as caught:
        api.load_scenario(config)
    location, _ = caught.value.issues[0]
    assert location.startswith("steps -> 0")


def test_fa44_validate_reports_positions_instead_of_raising():
    config = _config()
    config["layers"].append({"id": "strassen", "source": "osm", "data_type": "vector"})
    config["steps"][0]["op"] = "gibt_es_nicht"
    report = api.validate(config)
    assert report.valid is False and report.plan is None and report.document is None
    locations = [location for location, _ in report.issues]
    assert "layers -> 1 -> tags" in locations
    assert any(location.startswith("steps -> 0") for location in locations)


def test_fa44_validate_missing_field_is_german_and_located():
    config = _config()
    del config["output"]
    report = api.validate(config)
    assert report.issues == [("output", "Pflichtfeld fehlt")]


def test_fa44_validate_valid_scenario_returns_plan_and_document():
    report = api.validate(_config())
    assert report.valid and report.issues == []
    assert report.plan is not None and list(report.plan.order) == ["catchment"]
    assert (
        report.document is not None and report.document.scenario.scenario.name == "FA44"
    )


def test_fa44_validate_accepts_a_loaded_document():
    document = api.load_scenario(_config())
    report = api.validate(document)
    assert report.valid and report.document is document


def test_fa44_plan_lists_only_what_the_outputs_need():
    config = _config()
    config["layers"].append(
        {
            "id": "ungenutzt",
            "source": "file",
            "data_type": "vector",
            "path": str(FIXTURES_DIR / "parks.geojson"),
        }
    )
    plan = api.plan(api.load_scenario(config))
    assert list(plan.order) == ["catchment"]
    assert plan.unused_layers == {"ungenutzt"}


# =====================================================================
# Der eine Fehlerformatierer: Pydantic-Fehlerarten auf Deutsch
# =====================================================================


class _Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zahl: float
    anzahl: int = Field(gt=0)
    mindestens: int = Field(ge=2)
    kleiner: int = Field(lt=10)
    hoechstens: int = Field(le=5)
    wahr: bool
    text: str
    liste: list[int]
    mapping: dict
    modus: Literal["a", "b"]


def _probe_issues(**values) -> dict[str, str]:
    base = {
        "zahl": 1.5,
        "anzahl": 1,
        "mindestens": 2,
        "kleiner": 1,
        "hoechstens": 1,
        "wahr": True,
        "text": "t",
        "liste": [1],
        "mapping": {},
        "modus": "a",
    }
    base.update(values)
    with pytest.raises(ValidationError) as caught:
        _Probe(**base)
    return dict(issues_of(caught.value))


def test_fa44_issue_formatter_translates_the_common_pydantic_types():
    issues = _probe_issues(
        zahl="x",
        anzahl=0,
        mindestens=1,
        kleiner=10,
        hoechstens=6,
        wahr="vielleicht",
        text=5,
        liste="a",
        mapping=[1],
        modus="c",
        unbekannt=1,
    )
    assert issues["zahl"] == "muss eine Zahl sein (erhalten: 'x')"
    assert issues["anzahl"] == "muss größer als 0 sein (erhalten: 0)"
    assert issues["mindestens"] == "muss mindestens 2 sein (erhalten: 1)"
    assert issues["kleiner"] == "muss kleiner als 10 sein (erhalten: 10)"
    assert issues["hoechstens"] == "darf höchstens 5 sein (erhalten: 6)"
    assert issues["wahr"] == "muss true oder false sein (erhalten: 'vielleicht')"
    assert issues["text"] == "muss ein Text sein (erhalten: 5)"
    assert issues["liste"] == "muss eine Liste sein (erhalten: 'a')"
    assert (
        issues["mapping"] == "muss ein Mapping (Schlüssel: Wert) sein (erhalten: [1])"
    )
    assert issues["modus"].startswith("ungültiger Wert, erlaubt sind: 'a' or 'b'")
    assert issues["unbekannt"].startswith("unbekanntes Feld")


def test_fa44_issue_formatter_translates_integer_parsing_and_missing_fields():
    with pytest.raises(ValidationError) as caught:
        _Probe(zahl=1.0, anzahl="viele")
    issues = dict(issues_of(caught.value))
    assert issues["anzahl"] == "muss eine ganze Zahl sein (erhalten: 'viele')"
    assert issues["mindestens"] == "Pflichtfeld fehlt"


def test_fa44_issue_formatter_keeps_the_pydantic_text_for_unknown_types():
    class _Short(BaseModel):
        text: str = Field(min_length=3)

    with pytest.raises(ValidationError) as caught:
        _Short(text="ab")
    ((location, message),) = issues_of(caught.value)
    assert location == "text" and "at least 3" in message


def test_fa44_issue_formatter_uses_the_message_of_a_value_error_validator():
    config = _config()
    config["steps"][0]["params"]["radius_km"] = -1
    report = api.validate(config)
    assert report.issues == [
        ("steps -> 0 -> params -> radius_km", "muss größer als 0 sein (erhalten: -1)")
    ]


# =====================================================================
# run: Ausgaben, Warnungen, Abbruch, Zwischenergebnisse
# =====================================================================


def test_fa44_run_writes_every_declared_output_into_out_dir(tmp_path):
    report = api.run(api.load_scenario(_config()), out_dir=tmp_path / "ergebnis")
    assert report.outputs is not None and report.skipped_outputs == []
    assert sorted(p.name for p in report.outputs.written) == [
        "catchment.csv",
        "catchment.geojson",
    ]
    for path in report.outputs.written:
        assert path.parent == tmp_path / "ergebnis" and path.stat().st_size > 0
    assert report.result.order == ["catchment"] and report.result.loaded_layers == [
        "substations"
    ]


def test_fa44_run_without_out_dir_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = api.run(api.load_scenario(_config()))
    assert report.outputs is None and report.skipped_outputs == []
    assert list(tmp_path.glob("*.geojson")) == []


def test_fa44_run_reports_a_skipped_output_with_its_reason(tmp_path):
    """Gültige Konfiguration, aber das Ergebnis lässt sich nicht schreiben: RDF
    verlangt ein CRS, eine Tabelle ohne Geometrie hat keins (FA13)."""
    config = _config()
    config["layers"].append(
        {
            "id": "statistik",
            "source": "table",
            "delimiter": ";",
            "path": str(FIXTURES_DIR / "bip_mini.csv"),
        }
    )
    config["output"].append(
        {"type": "rdf", "source": "statistik", "path": "statistik.ttl"}
    )
    report = api.run(api.load_scenario(config), out_dir=tmp_path / "out")
    assert len(report.outputs.written) == 2
    (skipped,) = report.skipped_outputs
    assert skipped.source == "statistik" and "CRS" in skipped.reason
    assert not (tmp_path / "out" / "statistik.ttl").exists()


def test_fa44_run_collects_layer_warnings_in_the_report_and_the_observer(tmp_path):
    config = _config()
    config["layers"][0]["validation"] = {
        "required_fields": ["gibt_es_nicht"],
        "on_violation": "warn",
    }
    seen = []
    report = api.run(api.load_scenario(config), out_dir=tmp_path, observer=seen.append)
    assert any(
        "gibt_es_nicht" in w.message and w.layer == "substations"
        for w in report.warnings
    )
    assert any(
        e.type == api.events.LAYER_WARNING and e.layer == "substations" for e in seen
    )
    assert report.warnings == report.result.warnings


def test_fa44_run_cancelled_token_ends_the_run_with_run_cancelled():
    token = api.CancelToken()
    token.cancel()
    seen = []
    with pytest.raises(api.RunCancelled):
        api.run(api.load_scenario(_config()), cancel=token, observer=seen.append)
    assert [e.type for e in seen].count(api.events.RUN_CANCELLED) == 1


def test_fa44_run_layer_failure_is_a_layer_load_error_with_context(tmp_path):
    # Eine fehlende Datei fängt der Vorab-Check (W2-02); ein Ladefehler entsteht
    # durch eine vorhandene, unlesbare Datei.
    broken = tmp_path / "kaputt.geojson"
    broken.write_text("kein GeoJSON", encoding="utf-8")
    config = _config()
    config["layers"][0]["path"] = str(broken)
    with pytest.raises(api.LayerLoadError) as caught:
        api.run(api.load_scenario(config))
    assert caught.value.layer_id == "substations" and caught.value.source == "file"


def test_fa44_run_keeps_finished_results_in_the_callers_store_after_a_failure():
    config = _config()
    config["steps"].append(
        {
            "id": "kaputt",
            "op": "filter",
            "inputs": {"features": "catchment"},
            "params": {"condition": "spalte_gibt_es_nicht > 1"},
        }
    )
    config["output"] = [
        {"type": "geojson", "source": "kaputt", "path": "kaputt.geojson"}
    ]
    store = api.LayerStore()
    with pytest.raises(api.StepExecutionError) as caught:
        api.run(api.load_scenario(config), store=store)
    assert caught.value.step_id == "kaputt"
    assert "catchment" in store and "kaputt" not in store


def test_fa44_run_unresolvable_region_is_a_config_error_with_position():
    config = _config()
    config["scenario"]["region"] = "Nirgendwostadt"
    with pytest.raises(api.ConfigError) as caught:
        api.run(api.load_scenario(config), region_fetcher=lambda name: [])
    assert caught.value.issues[0][0] == "scenario -> region"


def test_fa44_run_connector_override_replaces_the_connector(tmp_path):
    import geopandas as gpd

    called = []

    def loader(layer):
        called.append(layer.id)
        return gpd.read_file(FIXTURES_DIR / "substations.geojson")

    config = _config()
    config["layers"][0]["path"] = "/gibt/es/nicht.geojson"
    report = api.run(
        api.load_scenario(config),
        out_dir=tmp_path,
        connector_override={"substations": loader},
    )
    assert called == ["substations"] and len(report.outputs.written) == 2


def test_fa44_run_rejects_anything_but_a_document():
    with pytest.raises(TypeError, match="ScenarioDocument"):
        api.run(api.load_scenario(_config()).scenario)  # type: ignore[arg-type]


def test_fa44_run_dump_intermediate_writes_vector_steps_as_geojson(tmp_path):
    report = api.run(
        api.load_scenario(_config()), dump_intermediate=tmp_path / "zwischen"
    )
    assert [p.name for p in report.intermediates] == ["catchment.geojson"]
    assert (tmp_path / "zwischen" / "catchment.geojson").stat().st_size > 0
    assert report.intermediates_skipped == []


def test_fa44_run_dump_intermediate_names_the_steps_it_cannot_write(tmp_path):
    config = _config(
        layers=[
            {
                "id": "substations",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "substations.geojson"),
            },
            {
                "id": "power_lines",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "power_lines.geojson"),
            },
        ],
        steps=[
            {
                "id": "netz",
                "op": "build_network",
                "inputs": {"lines": "power_lines", "nodes": "substations"},
                "params": {"tolerance_m": 100},
            },
            {"id": "knoten", "op": "graph_to_vector", "inputs": {"graph": "netz"}},
        ],
        output=[{"type": "geojson", "source": "knoten", "path": "knoten.geojson"}],
    )
    report = api.run(api.load_scenario(config), dump_intermediate=tmp_path / "zwischen")
    # der Graph hat kein GeoJSON-Format: er wird mit Grund gemeldet, der Vektor abgelegt
    assert [path.name for path in report.intermediates] == ["knoten.geojson"]
    assert [step for step, _ in report.intermediates_skipped] == ["netz"]


# =====================================================================
# Ein Schreiber, der abstürzt, bricht die übrigen Ausgaben nicht ab (FA12)
# =====================================================================

_CRASHING_WRITER = textwrap.dedent("""
    from geofact.plugin_api import register_output
    from geofact.core.types import DataType


    @register_output("abstuerzer", extension="txt", accepts=(DataType.VECTOR,))
    def write_crash(data, target, spec):
        target.write_text("halb", encoding="utf-8")
        raise OSError("Platte voll")
""")


def test_fa44_a_crashing_writer_does_not_abort_the_other_outputs(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "abstuerzer.py").write_text(_CRASHING_WRITER, encoding="utf-8")
    registry = discover(plugin_paths=[plugins])
    config = _config()
    config["output"] = [
        {"type": "geojson", "source": "catchment", "path": "vorher.geojson"},
        {"type": "abstuerzer", "source": "catchment", "path": "kaputt.txt"},
        {"type": "csv", "source": "catchment", "path": "nachher.csv"},
    ]
    document = api.load_scenario(config, registry=registry)
    report = api.run(document, out_dir=tmp_path / "out", registry=registry)

    assert sorted(p.name for p in report.outputs.written) == [
        "nachher.csv",
        "vorher.geojson",
    ]
    (skipped,) = report.skipped_outputs
    assert skipped.type == "abstuerzer" and "Platte voll" in skipped.reason
    assert not (
        tmp_path / "out" / "kaputt.txt"
    ).exists()  # halb geschriebene Datei entfernt


@pytest.mark.parametrize("ids", [("Puffer", "puffer"), ("a b", "a_b")])
def test_fa44_run_dump_intermediate_colliding_step_ids_get_distinct_files(
    tmp_path, ids
):
    """Nachtreview 02.10. (Runde 2): zwei Schritt-ids, die auf denselben
    Dateinamen fallen (Groß-/Kleinschreibung oder ersetzte Zeichen), über-
    schrieben sich still, beide standen als geschrieben im Bericht."""
    first, second = ids
    config = _config(
        steps=[
            {
                "id": first,
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 1},
            },
            {
                "id": second,
                "op": "buffer",
                "inputs": {"geometry": "substations"},
                "params": {"radius_km": 5},
            },
        ],
        output=[
            {"type": "geojson", "source": first, "path": "o1.geojson"},
            {"type": "geojson", "source": second, "path": "o2.geojson"},
        ],
    )
    report = api.run(api.load_scenario(config), dump_intermediate=tmp_path / "zwischen")

    names = [p.name.casefold() for p in report.intermediates]
    assert len(names) == 2 and len(set(names)) == 2
    assert all(p.is_file() for p in report.intermediates)
    assert len(list((tmp_path / "zwischen").iterdir())) == 2
    assert report.intermediates_skipped == []


def test_fa44_run_dump_directory_is_checked_before_the_analysis(tmp_path):
    """Nachtreview 02.10. (Runde 2): ein Zwischenergebnis-Pfad, der eine Datei
    ist, scheitert vor dem ersten Ladevorgang statt nach Lauf und Ausgaben."""
    import geopandas as gpd

    called = []

    def loader(layer):
        called.append(layer.id)
        return gpd.read_file(FIXTURES_DIR / "substations.geojson")

    blocker = tmp_path / "zwischen"
    blocker.write_text("Datei", encoding="utf-8")
    with pytest.raises(OSError):
        api.run(
            api.load_scenario(_config()),
            out_dir=tmp_path / "out",
            dump_intermediate=blocker,
            connector_override={"substations": loader},
        )
    assert called == []
    assert not (tmp_path / "out" / "catchment.geojson").exists()


def test_fa44_precheck_position_of_an_instance_layer_is_the_domain_position(tmp_path):
    """Nachtreview 02.10. (Runde 2), FA75: der Vorab-Check meldete den Index im
    ausgedehnten Szenario (``layers -> 2``), den es in der YAML nicht gibt."""
    config = _config(
        layers=[
            {
                "id": "substations",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "substations.geojson"),
            },
        ],
        steps=[
            {
                "id": "catchment",
                "op": "buffer",
                "inputs": {"geometry": "f[b].datei"},
                "params": {"radius_km": 1},
            }
        ],
        output=[{"type": "geojson", "source": "catchment"}],
    )
    config["modules"] = {
        "datei": {
            "args": {"name": {"type": "string"}},
            "layers": [
                {
                    "id": "datei",
                    "source": "file",
                    "data_type": "vector",
                    "path": str(tmp_path / "fehlt_${name}.geojson"),
                }
            ],
            "exports": ["datei"],
        }
    }
    config["instances"] = [
        {
            "id": "f",
            "module": "datei",
            "with": {"name": "${n}"},
            "foreach": {"var": "n", "in": ["a", "b"]},
        }
    ]
    with pytest.raises(api.ConfigError) as caught:
        api.run(api.load_scenario(config))
    [(location, message)] = caught.value.issues
    assert location == "instances -> f[b] -> layers -> datei (f[b].datei) -> path"
    assert "nicht gefunden" in message


def test_fa07_run_refresh_re_resolves_the_scenario_region():
    """Nachtreview 02.10. (Runde 2): ``api.run(refresh=True)`` löste die
    Szenario-Region bisher aus dem Cache, ohne Nominatim zu fragen."""
    import json

    calls = []
    answer = json.loads(
        (FIXTURES_DIR / "nominatim_dresden.json").read_text(encoding="utf-8")
    )

    def fetcher(name):
        calls.append(name)
        return answer

    config = _config(scenario={"name": "FA44", "region": "Dresden"})
    api.run(api.load_scenario(config), region_fetcher=fetcher)
    api.run(api.load_scenario(config), region_fetcher=fetcher)
    assert calls == ["Dresden"]
    api.run(api.load_scenario(config), region_fetcher=fetcher, refresh=True)
    assert calls == ["Dresden", "Dresden"]
