"""Implements: FA40 (Einheitliche Erweiterungspunkte mit Plugin-Erkennung).

Contract-Tests für Registry (core/registry.py) und Erkennung
(engine/discovery.py). Jede Erweiterung wird als echte Datei in einen
temporären Plugin-Ordner gelegt - genau so, wie ein Nutzer sie ablegen würde
- und danach ohne Änderung am Kern verwendet. Jeder Test baut sich seine
EIGENE Registry per ``discover(plugin_paths=[...])``: es gibt keinen globalen
Zustand, der zurückgesetzt werden müsste. Region über BBox-Literal (kein
Netzzugriff).
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import geopandas as gpd
import pytest
from pydantic import ValidationError

from geofact.engine import catalog
from geofact.api import PLUGIN_PATH_ENV
from geofact.core.errors import PluginError, PluginLoadWarning
from geofact.core.registry import default_registry, register_file_format
from geofact.core.scenario import Scenario
from geofact.core.types import DataType
from geofact.engine.discovery import discover
from geofact.engine.executor import run_scenario
from geofact.engine.outputs import write_outputs

BBOX_REGION = "12.30,51.30,12.40,51.36"  # Leipzig, UTM-Zone 33N

# ---------------------------------------------------------------------------
# Plugin-Dateien (so, wie ein Nutzer sie ablegt)
# ---------------------------------------------------------------------------

SOURCE_PLUGIN = '''
"""Quellart mit eigener Transformation: Punkte ohne CRS-Angabe."""
from typing import Literal

import geopandas as gpd
from shapely.geometry import Point

from geofact.plugin_api import DataType, LayerBase, register_source, register_transform


class GridPointsLayer(LayerBase):
    source: Literal["grid_points"] = "grid_points"
    data_type: DataType = DataType.VECTOR
    count: int = 3


@register_transform("grid_points_from_wgs84", description="setzt WGS84 und projiziert")
def from_wgs84(layer, data, ctx):
    data = data.set_crs("EPSG:4326").to_crs(ctx.target_crs)
    data["transformed_by"] = "grid_points_from_wgs84"
    return data


@register_source("grid_points", GridPointsLayer, transform="grid_points_from_wgs84",
                 description="Gleichmaessige Punkte in der Region")
def load(layer, ctx):
    minx, miny, maxx, maxy = ctx.region.total_bounds
    step = (maxx - minx) / (layer.count + 1)
    points = [Point(minx + step * (i + 1), (miny + maxy) / 2) for i in range(layer.count)]
    return gpd.GeoDataFrame({"n": list(range(layer.count))}, geometry=points)
'''

FILE_FORMAT_PLUGIN = '''
"""Dateiformat .xyz (Leerzeichen-getrennte Koordinaten in ETRS89/UTM33,
ohne CRS-Metadaten) samt passender Transformation in derselben Datei."""
import geopandas as gpd
from shapely.geometry import Point

from geofact.plugin_api import DataType, register_file_format


def xyz_transform(layer, data, ctx):
    return data.set_crs("EPSG:25833").to_crs(ctx.target_crs)


@register_file_format("xyz", extensions=("xyz",), data_type=DataType.VECTOR,
                      transform=xyz_transform, description="XYZ-Punktliste in EPSG:25833")
def read_xyz(path, layer, ctx):
    rows = [line.split() for line in path.read_text().splitlines() if line.strip()]
    points = [Point(float(x), float(y)) for x, y, _ in rows]
    return gpd.GeoDataFrame({"z": [float(z) for _, _, z in rows]}, geometry=points)
'''

TABLE_FORMAT_PLUGIN = """
import pandas as pd

from geofact.plugin_api import register_table_format


@register_table_format("psv", extensions=("psv",), description="Pipe-getrennte Werte")
def read_psv(layer, path):
    return pd.read_csv(path, sep="|", dtype=str)
"""

OUTPUT_PLUGIN = """
from typing import Optional

from pydantic import BaseModel, ConfigDict

from geofact.plugin_api import register_output


class CountOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = "Anzahl"


@register_output("count_txt", extension="txt", options_model=CountOptions,
                 description="Anzahl der Objekte als Textdatei")
def write_count(gdf, target, spec):
    target.write_text(f"{spec.options.label}: {len(gdf)}", encoding="utf-8")
"""

OPERATION_PLUGIN = """
from typing import ClassVar, Literal

from geofact.plugin_api import DataType, register_operation, Step


class CentroidStep(Step):
    op: Literal["centroid_points"] = "centroid_points"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR


@register_operation(CentroidStep)
def run_centroid(inputs, params):
    gdf = inputs["features"].copy()
    gdf["geometry"] = gdf.geometry.centroid
    return gdf
"""


# ---------------------------------------------------------------------------
# Fixtures und Helfer
# ---------------------------------------------------------------------------


@pytest.fixture
def plugin_dir(tmp_path):
    """Leerer Plugin-Ordner. Jeder Test baut daraus seine eigene Registry."""
    directory = tmp_path / "plugins"
    directory.mkdir()
    return directory


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def _scenario(layers, steps, output, registry=None) -> Scenario:
    """Szenario gegen die Registry des Tests (None = Standard-Registry)."""
    raw = {
        "scenario": {"name": "Plugin-Test", "region": BBOX_REGION},
        "layers": layers,
        "steps": steps,
        "output": output,
    }
    if registry is None:
        return Scenario(**raw)
    return Scenario.model_validate(raw, context={"registry": registry})


def _buffer_step(source: str) -> dict:
    return {
        "id": "buffered",
        "op": "buffer",
        "inputs": {"geometry": source},
        "params": {"radius_km": 0.1},
    }


# ---------------------------------------------------------------------------
# Kern: alle Bausteine kommen aus der Registry
# ---------------------------------------------------------------------------


def test_fa40_core_building_blocks_are_registered_like_plugins():
    """Nachbedingung: Kernbausteine stehen in derselben Registry, in
    die sich Plugins eintragen - der Kern hat keinen Sonderweg."""
    registry = default_registry()
    names = {
        kind: {e.name for e in registry.entries(kind)}
        for kind in (
            "source",
            "file_format",
            "table_format",
            "transform",
            "output",
            "operation",
        )
    }
    assert {"osm", "file", "table", "wfs", "ckan", "gtfs", "region"} <= names["source"]
    assert {"geojson", "gpkg", "shp", "gml", "tif"} <= names["file_format"]
    assert {"csv", "fwf", "xlsx", "sqlite", "sql"} <= names["table_format"]
    assert {"reproject", "none"} <= names["transform"]
    assert {"map", "geojson", "csv", "rdf"} <= names["output"]
    # REST-Endpunkte sind Kernbausteine, in der Konfiguration deklariert (FA41-43)
    assert "rest" in names["source"] and "rest" in names["output"]
    assert "rest" in names["operation"]
    assert "power_grid_topology" in names["operation"]


def test_fa40_unknown_source_lists_registered_sources():
    with pytest.raises(ValidationError, match="Unbekannte Quellart 'nirgendwo'.*osm"):
        _scenario(
            [{"id": "x", "source": "nirgendwo"}],
            [_buffer_step("x")],
            [{"type": "geojson", "source": "buffered"}],
        )


def test_fa40_unknown_output_type_lists_registered_formats():
    with pytest.raises(
        ValidationError, match="Unbekannte Ausgabeformat 'pdf'.*geojson"
    ):
        _scenario(
            [{"id": "r", "source": "region"}],
            [_buffer_step("r")],
            [{"type": "pdf", "source": "buffered"}],
        )


def test_fa40_output_option_of_other_format_is_rejected():
    """Vorbedingung: color_field ist eine Option von map, nicht von geojson.
    Nachbedingung: expliziter Fehler statt stillem Ignorieren."""
    with pytest.raises(
        ValidationError, match="unbekanntes Feld für Ausgabeformat 'geojson'"
    ) as caught:
        _scenario(
            [{"id": "r", "source": "region"}],
            [_buffer_step("r")],
            [{"type": "geojson", "source": "buffered", "color_field": "x"}],
        )
    assert [error["loc"] for error in caught.value.errors()] == [
        ("output", 0, "color_field")
    ]


# ---------------------------------------------------------------------------
# Plugins je Erweiterungspunkt
# ---------------------------------------------------------------------------


def test_fa40_source_plugin_with_own_transform_runs_in_scenario(plugin_dir):
    """Happy Path Konnektor: eine neue Quellart samt eigener Transformation
    liegt als eine Datei im Plugin-Ordner und ist danach per YAML nutzbar."""
    _write(plugin_dir, "grid_points.py", SOURCE_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])

    scenario = _scenario(
        [{"id": "pts", "source": "grid_points", "count": 4}],
        [_buffer_step("pts")],
        [{"type": "geojson", "source": "buffered"}],
        registry=registry,
    )
    result = run_scenario(scenario, registry=registry)

    points = result.store["pts"]
    assert len(points) == 4
    assert (points["transformed_by"] == "grid_points_from_wgs84").all()
    assert points.crs.to_epsg() == 32633  # UTM-Zone der Region (FA5-Nachbedingung)
    assert len(result.store["buffered"]) == 4


def test_fa40_plugin_source_is_unknown_without_its_registry(plugin_dir):
    """Die Erkennung ist eine reine Funktion von (Code, Pfaden): dieselbe Datei
    macht die Quellart nur in der Registry bekannt, die sie entdeckt hat - die
    Standard-Registry bleibt unberührt."""
    _write(plugin_dir, "grid_points.py", SOURCE_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])

    assert "grid_points" in registry.names("source")
    assert "grid_points" not in default_registry().names("source")
    with pytest.raises(ValidationError, match="Unbekannte Quellart 'grid_points'"):
        _scenario(
            [{"id": "pts", "source": "grid_points"}],
            [_buffer_step("pts")],
            [{"type": "geojson", "source": "buffered"}],
        )


def test_fa40_file_format_plugin_brings_its_own_transform(plugin_dir, tmp_path):
    """Happy Path Dateiformat: ein Format ohne CRS-Metadaten bringt seine
    Harmonisierung in derselben Datei mit (statt 'reproject')."""
    _write(plugin_dir, "xyz_format.py", FILE_FORMAT_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])
    data_file = tmp_path / "punkte.xyz"
    data_file.write_text(
        "318000 5690000 101.5\n319000 5691000 99.0\n", encoding="utf-8"
    )

    scenario = _scenario(
        [
            {
                "id": "pts",
                "source": "file",
                "path": str(data_file),
                "data_type": "vector",
            }
        ],
        [_buffer_step("pts")],
        [{"type": "geojson", "source": "buffered"}],
        registry=registry,
    )
    points = run_scenario(scenario, registry=registry).store["pts"]

    assert points.crs.to_epsg() == 32633
    # ETRS89/UTM33 und WGS84/UTM33 liegen im Zentimeterbereich beieinander
    assert abs(points.geometry.iloc[0].x - 318000) < 1
    assert list(points["z"]) == [101.5, 99.0]


def test_fa40_file_format_plugin_must_match_data_type(plugin_dir, tmp_path):
    """Typfehler-Fall: ein Vektorformat als Raster deklariert wird vor der
    Ausführung abgelehnt (FA2)."""
    _write(plugin_dir, "xyz_format.py", FILE_FORMAT_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])
    with pytest.raises(ValidationError, match="Format 'xyz' liefert vector"):
        _scenario(
            [
                {
                    "id": "pts",
                    "source": "file",
                    "path": "x.xyz",
                    "format": "xyz",
                    "data_type": "raster",
                }
            ],
            [
                {
                    "id": "z",
                    "op": "zonal_stats",
                    "inputs": {"zones": "r", "values": "pts"},
                }
            ],
            [{"type": "geojson", "source": "z"}],
            registry=registry,
        )


def test_fa40_table_format_plugin_reuses_column_and_geometry_mapping(
    plugin_dir, tmp_path
):
    """Happy Path Tabellenformat: das Plugin liest nur den DataFrame, das
    Spalten-/Geometrie-Mapping des Kerns (FA14) gilt unverändert."""
    _write(plugin_dir, "psv_format.py", TABLE_FORMAT_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])
    table = tmp_path / "stationen.psv"
    table.write_text("name|lon|lat\nA|12.35|51.33\nB|12.36|51.34\n", encoding="utf-8")

    scenario = _scenario(
        [
            {
                "id": "st",
                "source": "table",
                "path": str(table),
                "geometry": {"x_column": "lon", "y_column": "lat"},
                "columns": {"name": {}},
            }
        ],
        [_buffer_step("st")],
        [{"type": "geojson", "source": "buffered"}],
        registry=registry,
    )
    stations = run_scenario(scenario, registry=registry).store["st"]
    assert list(stations["name"]) == ["A", "B"]
    assert stations.crs.to_epsg() == 32633


def test_fa40_output_plugin_writes_file_with_validated_options(plugin_dir, tmp_path):
    _write(plugin_dir, "count_output.py", OUTPUT_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])
    scenario = _scenario(
        [{"id": "r", "source": "region"}],
        [_buffer_step("r")],
        [{"type": "count_txt", "source": "buffered", "label": "Flächen"}],
        registry=registry,
    )
    result = run_scenario(scenario, registry=registry)

    outcome = write_outputs(scenario, result, tmp_path / "out", registry=registry)
    assert [p.suffix for p in outcome.written] == [".txt"]
    assert outcome.written[0].read_text(encoding="utf-8") == "Flächen: 1"


def test_fa40_output_plugin_rejects_unsupported_data_type_before_the_run(plugin_dir):
    """Randfall: ein Graph-Ergebnis an ein reines Vektorformat wird schon bei der
    Prüfung der Konfiguration abgelehnt (FA2) - nicht erst im Lauf übersprungen."""
    _write(plugin_dir, "count_output.py", OUTPUT_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    with pytest.raises(
        ValidationError, match="schreibt nur Vektor.*liefert aber Graph"
    ) as caught:
        _scenario(
            [
                {
                    "id": "lines",
                    "source": "file",
                    "path": str(fixtures / "power_lines.geojson"),
                    "data_type": "vector",
                },
                {
                    "id": "subs",
                    "source": "file",
                    "path": str(fixtures / "substations.geojson"),
                    "data_type": "vector",
                },
            ],
            [
                {
                    "id": "net",
                    "op": "build_network",
                    "inputs": {"lines": "lines", "nodes": "subs"},
                }
            ],
            [{"type": "count_txt", "source": "net"}],
            registry=registry,
        )
    assert [error["loc"] for error in caught.value.errors()] == [
        ("output", 0, "source")
    ]


def test_fa40_output_plugin_options_model_must_forbid_unknown_fields(plugin_dir):
    """Ein options_model, das unbekannte Felder durchließe, würde Tippfehler
    in der YAML verschlucken - das Plugin wird abgelehnt und gemeldet."""
    _write(
        plugin_dir,
        "lax_output.py",
        OUTPUT_PLUGIN.replace('    model_config = ConfigDict(extra="forbid")\n\n', ""),
    )
    with pytest.warns(PluginLoadWarning, match="extra='forbid'"):
        registry = discover(plugin_paths=[plugin_dir])
    assert "count_txt" not in registry.names("output")


def test_fa40_operation_plugin_from_directory(plugin_dir):
    _write(plugin_dir, "centroid_op.py", OPERATION_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])
    scenario = _scenario(
        [{"id": "r", "source": "region"}],
        [
            _buffer_step("r"),
            {
                "id": "center",
                "op": "centroid_points",
                "inputs": {"features": "buffered"},
            },
        ],
        [{"type": "geojson", "source": "center"}],
        registry=registry,
    )
    center = run_scenario(scenario, registry=registry).store["center"]
    assert center.geometry.iloc[0].geom_type == "Point"


# ---------------------------------------------------------------------------
# Fehlerfälle: defekte Plugins verschwinden nicht still
# ---------------------------------------------------------------------------


def _names(registry) -> dict[str, list[str]]:
    return {
        kind: registry.names(kind)
        for kind in (
            "source",
            "file_format",
            "table_format",
            "transform",
            "operation",
            "output",
        )
    }


def test_fa40_empty_plugin_dir_changes_nothing(plugin_dir):
    """Randfall: leerer Plugin-Ordner."""
    assert _names(discover(plugin_paths=[plugin_dir])) == _names(default_registry())


def test_fa40_broken_plugin_is_skipped_and_named_in_errors(plugin_dir):
    """Vorbedingung: eine Plugin-Datei lässt sich nicht importieren.
    Nachbedingung: der Rest bleibt nutzbar, die Datei erscheint als
    Warnung, in Registry.failed und in "Unbekannte ..."-Meldungen."""
    broken = _write(plugin_dir, "kaputt.py", "import gibt_es_nicht\n")
    with pytest.warns(PluginLoadWarning, match="kaputt.py"):
        registry = discover(plugin_paths=[plugin_dir])

    assert str(broken) in registry.failed
    assert any(e["op"] == "buffer" for e in catalog.operation_catalog(registry))
    with pytest.raises(ValidationError, match="nicht geladene Plugins: .*kaputt.py"):
        _scenario(
            [{"id": "r", "source": "region"}],
            [{"id": "x", "op": "aus_kaputtem_plugin", "inputs": {}}],
            [{"type": "geojson", "source": "x"}],
            registry=registry,
        )


def test_fa40_plugin_cannot_replace_core_operation(plugin_dir):
    """Ein Plugin mit dem Namen einer Kernoperation wird abgelehnt; der
    Vertrag der Kernoperation bleibt unverändert."""
    core_buffer = default_registry().operation("buffer").step_model
    fake = _write(
        plugin_dir,
        "fake_buffer.py",
        OPERATION_PLUGIN.replace("centroid_points", "buffer"),
    )
    with pytest.warns(
        PluginLoadWarning, match="Operation 'buffer' ist doppelt registriert"
    ):
        registry = discover(plugin_paths=[plugin_dir])
    assert registry.operation("buffer").step_model is core_buffer
    assert str(fake) in registry.failed


def test_fa40_plugin_cannot_replace_core_source(plugin_dir):
    _write(plugin_dir, "fake_osm.py", SOURCE_PLUGIN.replace('"grid_points"', '"osm"'))
    with pytest.warns(PluginLoadWarning, match="Quellart 'osm' ist doppelt"):
        registry = discover(plugin_paths=[plugin_dir])
    assert registry.source("osm").origin == "geofact.builtin.sources.osm"


WRONG_SIGNATURE_PLUGIN = '''
"""Lesefunktion mit falscher Signatur: ctx fehlt."""
from geofact.plugin_api import DataType, register_file_format


@register_file_format("falsch", extensions=("falsch",), data_type=DataType.VECTOR)
def lies_irgendwas(path, layer):
    raise AssertionError("darf nie aufgerufen werden")
'''


def test_fa40_plugin_with_wrong_signature_is_rejected_at_registration(plugin_dir):
    """Vorbedingung: die registrierte Funktion erfüllt die Schnittstelle
    ihrer Art (hier FileReader(path, layer, ctx)). Nachbedingung: sonst wird
    das Plugin beim Laden abgelehnt und mit Schnittstelle gemeldet, statt
    erst im Lauf zu scheitern."""
    _write(plugin_dir, "falsch.py", WRONG_SIGNATURE_PLUGIN)
    with pytest.warns(PluginLoadWarning, match="FileReader\\(path, layer, ctx\\)"):
        registry = discover(plugin_paths=[plugin_dir])
    assert "falsch" not in registry.names("file_format")


FREE_NAME_PLUGIN = """
from geofact.plugin_api import register_transform


@register_transform("frei_benannt")
def irgendein_name(layer, data, ctx, faktor=1):
    return data
"""


def test_fa40_function_name_is_free_only_signature_counts(plugin_dir):
    """Der Name der Funktion ist nicht Teil des Vertrags: eine beliebig
    benannte Funktion mit passender Signatur (auch mit Zusatzparametern mit
    Default) wird registriert und unter dem registrierten Namen gefunden."""
    _write(plugin_dir, "frei.py", FREE_NAME_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])
    assert registry.transform("frei_benannt").apply.__name__ == "irgendein_name"


def test_fa40_own_transform_callable_must_match_transform_interface():
    """Randfall: eine direkt mitgebrachte Transformation (statt Name) wird
    ebenfalls gegen die Schnittstelle Transform geprüft."""
    with pytest.raises(PluginError, match="Transform\\(layer, data, ctx\\)"):
        register_file_format(
            "ohne_ctx",
            extensions=("oc",),
            data_type=DataType.VECTOR,
            transform=lambda layer, data: data,
        )


def test_fa40_operation_contract_without_run_function_is_removed(plugin_dir):
    """Ein Vertrag ohne Ausführung würde die Prüfung bestehen und erst im
    Lauf scheitern - das Plugin wird deshalb abgelehnt und gemeldet."""
    contract_only = OPERATION_PLUGIN.split("@register_operation")[0]
    _write(plugin_dir, "nur_vertrag.py", contract_only)
    with pytest.warns(PluginLoadWarning, match="keine Ausführungsfunktion"):
        registry = discover(plugin_paths=[plugin_dir])
    assert "centroid_points" not in registry.names("operation")


def test_fa40_extension_catalog_names_origin(plugin_dir):
    _write(plugin_dir, "count_output.py", OUTPUT_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])
    outputs = {e["name"]: e for e in catalog.extension_catalog(registry)["outputs"]}
    assert outputs["map"]["origin"] == "geofact.builtin.outputs.map"
    assert outputs["count_txt"]["origin"].endswith("count_output.py")


# ---------------------------------------------------------------------------
# Ende-zu-Ende: frischer Prozess, nur GEOFACT_PLUGIN_PATH gesetzt
# ---------------------------------------------------------------------------


def test_fa40_cli_discovers_plugins_from_environment(tmp_path):
    """NFA4-Nachweis im Kleinen: dieselbe Konfiguration ist ohne Plugin-
    Ordner ungültig und mit ihm gültig - der Unterschied ist allein eine
    abgelegte Datei, kein geänderter Code."""
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    _write(plugins, "grid_points.py", SOURCE_PLUGIN)
    config = tmp_path / "szenario.yaml"
    config.write_text(
        textwrap.dedent(f"""
        scenario: {{name: Plugin, region: "{BBOX_REGION}"}}
        layers:
          - {{id: pts, source: grid_points, count: 2}}
        steps:
          - {{id: buffered, op: buffer, inputs: {{geometry: pts}}, params: {{radius_km: 0.1}}}}
        output:
          - {{type: geojson, source: buffered}}
    """),
        encoding="utf-8",
    )

    env = {k: v for k, v in os.environ.items() if k != PLUGIN_PATH_ENV}
    without = subprocess.run(
        [sys.executable, "-m", "geofact.cli", "validate", str(config)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert without.returncode == 1
    assert "Unbekannte Quellart 'grid_points'" in without.stderr

    env[PLUGIN_PATH_ENV] = str(plugins)
    with_plugin = subprocess.run(
        [sys.executable, "-m", "geofact.cli", "validate", str(config)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert with_plugin.returncode == 0, with_plugin.stderr

    listing = subprocess.run(
        [sys.executable, "-m", "geofact.cli", "plugins"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert listing.returncode == 0, listing.stderr
    assert "grid_points" in listing.stdout
    assert "grid_points.py" in listing.stdout


def test_fa40_missing_plugin_path_warns_but_core_stays_usable(tmp_path):
    env = dict(os.environ, **{PLUGIN_PATH_ENV: str(tmp_path / "gibt_es_nicht")})
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import warnings; warnings.simplefilter('always');"
            "from geofact import api; api.load_extensions().operation('buffer')",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "gibt_es_nicht" in result.stderr and "existiert nicht" in result.stderr


def test_fa40_region_plugin_data_passes_validation_like_core_data(plugin_dir):
    """Die Validierung (FA6) bleibt im Kern und gilt auch für Plugin-Daten."""
    _write(plugin_dir, "grid_points.py", SOURCE_PLUGIN)
    registry = discover(plugin_paths=[plugin_dir])
    scenario = _scenario(
        [
            {
                "id": "pts",
                "source": "grid_points",
                "count": 3,
                "validation": {
                    "constraints": {"n": {"min": 1}},
                    "on_violation": "drop",
                },
            }
        ],
        [_buffer_step("pts")],
        [{"type": "geojson", "source": "buffered"}],
        registry=registry,
    )
    points = run_scenario(scenario, registry=registry).store["pts"]
    assert list(points["n"]) == [1, 2]
    assert isinstance(points, gpd.GeoDataFrame)
