"""Implements: FA40 (Datei- und Tabellenformate bringen ihre Felder mit: options_model), FA2 (Formatfelder strikt geprüft).

Contract-Tests der Formatoptionen: ``@register_file_format`` und
``@register_table_format`` nehmen ein ``options_model`` (``extra='forbid'``);
die Felder stehen in der YAML flach neben ``path`` und ``format``, der Datei-
bzw. Tabellen-Konnektor prüft die übrigen Schlüssel eines Layers gegen das
Modell des aufgelösten Formats (sql/sqlite: ``table``/``query``, xlsx:
``sheet``, fwf: ``colspecs``/``names``/``skiprows``, gpkg/gml: ``layer``) und
stellt sie als ``layer.options`` bereit. Kern und Engine kennen keinen
Formatnamen.

Alles offline: Region als BBox-Literal, es wird nichts geladen außer kleinen
Fixtures.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from geofact import api
from geofact.builtin.formats.tables import FwfOptions, SqlOptions, XlsxOptions
from geofact.builtin.sources.file import FileLayer
from geofact.builtin.sources.table import TableLayer
from geofact.core.errors import PluginLoadWarning
from geofact.core.scenario import Scenario
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


def _plugin_registry(tmp_path: Path, source: str):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "plugin.py").write_text(source, encoding="utf-8")
    return discover(plugin_paths=[plugins])


def test_fa02_unknown_key_of_a_file_layer_is_rejected_by_the_format_check():
    """Datei- und Tabellenlayer nehmen die Felder ihres Formats flach an; was weder
    dem Layer noch dem Format gehört, ist ebenso ein Fehler mit Position."""
    config = _config()
    config["layers"][1]["validaton"] = {"required_fields": ["voltage"]}  # Tippfehler
    ((location, message),) = _issues(config)
    assert location == "layers -> 1 -> validaton"
    assert message == (
        "unbekanntes Feld (kein Feld des Layers und kein Feld des Dateiformats 'geojson'; "
        "das Format hat keine eigenen Felder)"
    )


# =====================================================================
# Round-Trip: Dump und erneutes Laden
# =====================================================================


def test_fa02_dump_keeps_every_field_and_loads_again():
    config = _config()
    config["layers"].append(
        {
            "id": "stat",
            "source": "table",
            "path": "x.xlsx",
            "sheet": "Blatt 1",
            "delimiter": ";",
        }
    )
    config["output"] = [{"type": "map", "source": "einzug", "color_field": "voltage"}]
    scenario = api.load_scenario(config).scenario
    dumped = scenario.model_dump(mode="json")
    assert (
        dumped["layers"][3]["sheet"] == "Blatt 1"
    )  # flach, nicht unter einem Unterschlüssel
    rebuilt = Scenario(**dumped)
    assert rebuilt.model_dump(mode="json") == dumped
    assert rebuilt.layers[3].options.sheet == "Blatt 1"


def test_fa02_catalog_lists_the_format_options():
    listing = catalog.extension_catalog()
    tables = {entry["name"]: entry["options"] for entry in listing["table_formats"]}
    assert tables == {
        "csv": [],
        "fwf": ["colspecs", "names", "skiprows"],
        "sql": ["query", "table"],
        "sqlite": ["query", "table"],
        "xlsx": ["sheet"],
    }
    files = {entry["name"]: entry["options"] for entry in listing["file_formats"]}
    assert files["gpkg"] == ["layer"] and files["gml"] == ["layer"]
    assert files["geojson"] == [] and files["shp"] == []
    # FA4/FA5/FA77: the raster format carries band selection, window margin, resampling
    # and the target resolution with its aggregation
    assert files["tif"] == [
        "aggregation",
        "band_names",
        "bands",
        "margin_km",
        "resampling",
        "resolution_m",
    ]


# =====================================================================
# Format-Optionen (7b): die YAML bleibt flach, das Format besitzt seine Felder
# =====================================================================


def test_fa02_sql_formats_need_table_or_query():
    for path, fmt in (("netz.sqlite", None), ("netz.sql", None), ("x.dat", "sqlite")):
        with pytest.raises(ValidationError, match="braucht 'table' oder 'query'"):
            TableLayer(id="t", path=path, format=fmt)
    layer = TableLayer(id="t", path="netz.sqlite", table="stationen")
    assert isinstance(layer.options, SqlOptions) and layer.options.table == "stationen"
    assert (
        TableLayer(id="t", path="netz.sqlite", query="select 1").options.query
        == "select 1"
    )


def test_fa02_xlsx_sheet_is_an_option_of_the_xlsx_format_only():
    layer = TableLayer(id="t", path="x.xlsx", sheet="Blatt 2")
    assert isinstance(layer.options, XlsxOptions) and layer.options.sheet == "Blatt 2"
    assert TableLayer(id="t", path="x.xlsx").options.sheet is None
    with pytest.raises(ValidationError, match="kein Feld des Tabellenformats 'csv'"):
        TableLayer(id="t", path="x.csv", sheet="Blatt 2")


def test_fa02_fwf_options_are_required_and_checked():
    layer = TableLayer(
        id="t",
        path="x.txt",
        format="fwf",
        colspecs=[(0, 3), (3, 6)],
        names=["a", "b"],
        skiprows=2,
    )
    assert isinstance(layer.options, FwfOptions) and layer.options.skiprows == 2
    with pytest.raises(ValidationError) as caught:
        TableLayer(id="t", path="x.txt", format="fwf")
    assert {error["loc"] for error in caught.value.errors()} == {
        ("colspecs",),
        ("names",),
    }
    with pytest.raises(ValidationError, match="gleich lang"):
        TableLayer(
            id="t", path="x.txt", format="fwf", colspecs=[(0, 3)], names=["a", "b"]
        )


def test_fa02_misspelled_csv_field_is_rejected_with_its_position():
    config = _config()
    config["layers"].append(
        {"id": "t", "source": "table", "path": "x.csv", "delimeter": ";"}
    )
    ((location, message),) = _issues(config)
    assert location == "layers -> 3 -> delimeter"
    assert message == (
        "unbekanntes Feld (kein Feld des Layers und kein Feld des Tabellenformats 'csv'; "
        "das Format hat keine eigenen Felder)"
    )


def test_fa02_option_of_another_table_format_names_the_allowed_ones():
    config = _config()
    config["layers"].append(
        {"id": "t", "source": "table", "path": "x.xlsx", "tabel": "x"}
    )
    ((location, message),) = _issues(config)
    assert location == "layers -> 3 -> tabel" and "Formatfelder: ['sheet']" in message


def test_fa02_gpkg_and_gml_layer_is_a_format_option():
    for path in ("x.gpkg", "x.gml"):
        layer = FileLayer(id="f", path=path, data_type="vector", layer="stationen")
        assert layer.options.layer == "stationen"
        assert FileLayer(id="f", path=path, data_type="vector").options.layer is None
    with pytest.raises(ValidationError, match="kein Feld des Dateiformats 'geojson'"):
        FileLayer(id="f", path="x.geojson", data_type="vector", layer="stationen")
    with pytest.raises(ValidationError, match="kein Feld des Dateiformats 'tif'"):
        FileLayer(id="f", path="x.tif", data_type="raster", layer="stationen")


def test_fa02_zip_container_checks_option_names_at_validation_and_values_at_load():
    zipped = str(FIXTURES_DIR / "substations_container.zip")
    layer = FileLayer(
        id="z", path=zipped, data_type="vector", layer="x"
    )  # Name gehört gpkg/gml
    with pytest.raises(ValidationError, match="aus Zip-Containern liest") as caught:
        FileLayer(id="z", path=zipped, data_type="vector", layr="x")
    assert [error["loc"] for error in caught.value.errors()] == [("layr",)]
    # das Mitglied ist ein Shapefile: dessen Format kennt kein 'layer'
    from geofact.builtin.sources import file as file_source
    from geofact.core.contracts import LoadContext
    from geofact.core.registry import default_registry

    with pytest.raises(
        ValueError, match="Layer 'z'.*unbekanntes Feld.*substations.shp"
    ):
        file_source.load(layer, LoadContext(registry=default_registry()))


def test_fa02_file_and_table_extras_stay_flat_in_the_dump():
    layer = TableLayer(id="t", path="x.xlsx", sheet="Blatt 2", delimiter=";")
    dumped = layer.model_dump()
    assert dumped["sheet"] == "Blatt 2" and dumped["delimiter"] == ";"
    assert TableLayer(**dumped).options.sheet == "Blatt 2"


_FORMAT_PLUGIN = textwrap.dedent("""
    from pydantic import BaseModel, ConfigDict, Field

    import geopandas as gpd
    from shapely.geometry import Point

    from geofact.plugin_api import DataType, register_file_format


    class XyzOptions(BaseModel):
        model_config = ConfigDict(extra="forbid")

        skalierung: float = Field(1.0, gt=0)


    @register_file_format("xyz", extensions=("xyz",), data_type=DataType.VECTOR,
                          options_model=XyzOptions, description="XYZ")
    def read_xyz(path, layer, ctx):
        rows = [line.split() for line in path.read_text().splitlines() if line.strip()]
        factor = layer.options.skalierung
        return gpd.GeoDataFrame(
            {"z": [float(z) * factor for _, _, z in rows]},
            geometry=[Point(float(x), float(y)) for x, y, _ in rows], crs="EPSG:25833",
        )
""")


def test_fa02_a_plugin_file_format_brings_its_own_flat_fields(tmp_path):
    registry = _plugin_registry(tmp_path, _FORMAT_PLUGIN)
    layer = FileLayer.model_validate(
        {"id": "p", "path": "x.xyz", "data_type": "vector", "skalierung": 2},
        context={"registry": registry},
    )
    assert layer.options.skalierung == 2.0
    with pytest.raises(ValidationError) as caught:
        FileLayer.model_validate(
            {"id": "p", "path": "x.xyz", "data_type": "vector", "skalierung": -1},
            context={"registry": registry},
        )
    assert [error["loc"] for error in caught.value.errors()] == [("skalierung",)]
    with pytest.raises(ValidationError, match="Formatfelder: \\['skalierung'\\]"):
        FileLayer.model_validate(
            {"id": "p", "path": "x.xyz", "data_type": "vector", "skalierng": 2},
            context={"registry": registry},
        )

    data = tmp_path / "punkte.xyz"
    data.write_text("1 2 3\n4 5 6\n", encoding="utf-8")
    from geofact.builtin.sources import file as file_source
    from geofact.core.contracts import LoadContext

    layer = FileLayer.model_validate(
        {"id": "p", "path": str(data), "data_type": "vector", "skalierung": 10},
        context={"registry": registry},
    )
    result = file_source.load(layer, LoadContext(registry=registry))
    assert list(result["z"]) == [30.0, 60.0]


def test_fa02_a_format_options_model_must_forbid_unknown_fields(tmp_path):
    lax = _FORMAT_PLUGIN.replace(
        '    model_config = ConfigDict(extra="forbid")\n\n', ""
    )
    with pytest.warns(PluginLoadWarning, match="extra='forbid'"):
        registry = _plugin_registry(tmp_path, lax)
    assert "xyz" not in registry.names("file_format")


# =====================================================================
# Kern kennt keine Formatnamen
# =====================================================================

_FORMAT_NAMES = {
    "sqlite",
    "sql",
    "fwf",
    "xlsx",
    "csv",
    "gpkg",
    "gml",
    "shp",
    "geojson",
    "tif",
}


@pytest.mark.parametrize("package", ["core", "engine"])
def test_fa02_core_and_engine_have_no_format_name_branches(package):
    """Formatspezifisches steht in der Registrierung des Formats (builtin/formats/);
    Kern und Engine vergleichen mit keinem Formatnamen."""
    offenders = []
    for path in sorted((SRC_DIR / package).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                for operand in operands:
                    constants = (
                        [operand]
                        if isinstance(operand, ast.Constant)
                        else list(operand.elts)
                        if isinstance(operand, (ast.Tuple, ast.Set, ast.List))
                        else []
                    )
                    for constant in constants:
                        if (
                            isinstance(constant, ast.Constant)
                            and constant.value in _FORMAT_NAMES
                        ):
                            offenders.append(
                                f"{path.relative_to(SRC_DIR)}:{node.lineno}"
                            )
    assert not offenders, offenders
