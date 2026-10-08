"""Tests für die Web-Schicht-Ergänzungen im Framework-Kern:

- progress.py + executor on_event-Hook (Live-DAG-Zustand)
- serialize.py (Vektor/Graph/Raster -> JSON für Per-Node-Karten)
- catalog.py (Operations-/Schema-Introspektion)
- engine/outputs.py (deklarierte Ausgaben ausführen -> Download)

Alle Szenarien lösen die Region über ein BBox-Literal auf (kein Netz).
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest

from geofact.engine import catalog
from geofact.engine import events as progress
from geofact_web.presentation import serialize
from geofact.core.scenario import Scenario
from geofact.core.store import LayerStore
from geofact.engine.executor import run_scenario
from geofact.engine.outputs import write_outputs
from geofact.plugin_api import LoadContext

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _network_scenario() -> Scenario:
    """Kleines Netz-Szenario: Vektor -> Graph -> Vektor, deckt alle drei
    Ergebnistypen (Vektor, Graph, Raster kommt separat) ab."""
    config = {
        "scenario": {"name": "Web-Test", "region": BBOX_REGION},
        "layers": [
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
            {
                "id": "power_lines",
                "source": "file",
                "path": str(FIXTURES_DIR / "power_lines.geojson"),
                "data_type": "vector",
            },
        ],
        "steps": [
            {
                "id": "grid",
                "op": "build_network",
                "inputs": {"lines": "power_lines", "nodes": "substations"},
                "params": {"tolerance_m": 500},
            },
            {"id": "scored", "op": "centrality", "inputs": {"graph": "grid"}},
            {"id": "nodes", "op": "graph_to_vector", "inputs": {"graph": "scored"}},
        ],
        "output": [
            {"type": "geojson", "source": "nodes", "path": "nodes.geojson"},
            {"type": "csv", "source": "nodes", "path": "nodes.csv"},
        ],
    }
    return Scenario(**config)


# ---------------------------------------------------------------------------
# progress / executor hook
# ---------------------------------------------------------------------------


def test_progress_events_emitted_in_order():
    events: list[progress.ProgressEvent] = []
    run_scenario(_network_scenario(), on_event=events.append)

    types = [e.type for e in events]
    assert types[:2] == [progress.REGION_READY, progress.PLAN]
    assert types[-1] == progress.RUN_DONE

    # Jeder ausgeführte Schritt meldet running gefolgt von done.
    for step_id in ("grid", "scored", "nodes"):
        running = [
            e for e in events if e.type == progress.STEP_RUNNING and e.step == step_id
        ]
        done = [e for e in events if e.type == progress.STEP_DONE and e.step == step_id]
        assert running and done, step_id
        assert done[0].duration_ms is not None and done[0].duration_ms >= 0

    plan = next(e for e in events if e.type == progress.PLAN)
    assert plan.detail["order"] == ["grid", "scored", "nodes"]
    assert set(plan.detail["layers"]) == {"substations", "power_lines"}


def test_progress_event_to_dict_drops_empty_fields():
    ev = progress.ProgressEvent(
        type=progress.STEP_DONE, step="x", op="buffer", output_type="vector"
    )
    d = ev.to_dict()
    assert d == {
        "type": "step_done",
        "step": "x",
        "op": "buffer",
        "output_type": "vector",
    }
    assert "error" not in d and "detail" not in d


def test_shared_store_exposes_completed_nodes_live():
    # Der Aufrufer hält den Store und kann fertige Knoten sofort lesen.
    store = LayerStore()
    seen: dict[str, bool] = {}

    def on_event(ev: progress.ProgressEvent) -> None:
        if ev.type == progress.STEP_DONE:
            seen[ev.step] = ev.step in store

    run_scenario(_network_scenario(), on_event=on_event, store=store)
    assert seen == {"grid": True, "scored": True, "nodes": True}


def test_step_error_event_on_failure():
    config = {
        "scenario": {"name": "Fehler", "region": BBOX_REGION},
        "layers": [
            {
                "id": "pts",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
            },
        ],
        # ranking mit unbekanntem Feld -> Laufzeitfehler im Schritt
        "steps": [
            {
                "id": "bad",
                "op": "ranking",
                "inputs": {"features": "pts"},
                "params": {"by": ["does_not_exist"], "weights": [1.0]},
            },
        ],
        "output": [{"type": "geojson", "source": "bad"}],
    }
    scenario = Scenario(**config)
    events: list[progress.ProgressEvent] = []
    raised = False
    try:
        run_scenario(scenario, on_event=events.append)
    except Exception:
        raised = True
    assert raised
    assert any(e.type == progress.STEP_ERROR and e.step == "bad" for e in events)
    assert any(e.type == progress.RUN_ERROR for e in events)


# ---------------------------------------------------------------------------
# serialize
# ---------------------------------------------------------------------------


def test_serialize_vector_geojson_is_json_safe():
    result = run_scenario(_network_scenario())
    nodes = result.store["nodes"]
    assert isinstance(nodes, gpd.GeoDataFrame)

    payload = serialize.serialize_result(nodes)
    assert payload["describe"]["kind"] == "vector"
    fc = payload["geojson"]
    assert fc["type"] == "FeatureCollection"
    # Muss durch einen strikten JSON-Parser gehen (kein NaN-Literal).
    reparsed = json.loads(json.dumps(fc, allow_nan=False))
    assert reparsed["features"]


def test_serialize_graph_nodelink_has_coordinates():
    result = run_scenario(_network_scenario())
    graph = result.store["scored"]
    payload = serialize.serialize_result(graph)
    assert payload["describe"]["kind"] == "graph"
    nl = payload["graph"]
    assert nl["nodes"]
    # Knoten tragen Geo-Koordinaten und die Zentralitätsmetrik.
    first = nl["nodes"][0]
    assert "lon" in first and "lat" in first
    assert "betweenness" in first
    json.dumps(nl, allow_nan=False)  # darf nicht werfen


def test_serialize_raster_describe_and_bounds():
    from geofact.builtin.sources import file as files_module
    from geofact.builtin.sources.file import FileLayer

    layer = FileLayer(
        id="pop",
        source="file",
        path=str(FIXTURES_DIR / "population.tif"),
        data_type="raster",
        format="tif",
    )
    raster = files_module.load(layer, LoadContext())
    desc = serialize.describe(raster)
    assert desc["kind"] == "raster"
    assert len(desc["shape"]) == 2
    assert desc["stats"] is not None
    assert desc["stats"]["max"] >= desc["stats"]["min"]
    assert desc["stats"]["valid_count"] > 0
    bounds = serialize.raster_bounds_wgs84(raster)
    assert bounds is not None and len(bounds) == 4
    minx, miny, maxx, maxy = bounds
    assert minx < maxx and miny < maxy


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------


def test_operation_catalog_lists_known_ops():
    cat = catalog.operation_catalog()
    ops = {e["op"] for e in cat}
    assert {"buffer", "spatial_join", "build_network", "centrality"}.issubset(ops)
    buffer = next(e for e in cat if e["op"] == "buffer")
    assert buffer["input_ports"] == {"geometry": "vector"}
    assert buffer["output_type"] == "vector"
    assert "radius_km" in buffer["required_params"]


def test_operation_catalog_includes_plugin():
    ops = {e["op"] for e in catalog.operation_catalog()}
    assert "power_grid_topology" in ops


def test_catalog_exposes_full_param_schema():
    cat = {e["op"]: e for e in catalog.operation_catalog()}
    buffer = cat["buffer"]["params"]
    assert buffer["radius_km"]["type"] == "number"
    assert buffer["radius_km"]["required"] is True
    # Enum-Parameter mit Auswahl.
    predicate = cat["spatial_join"]["params"]["predicate"]
    assert predicate["type"] == "enum"
    assert set(predicate["choices"]) == {"within", "intersects", "contains"}
    # Optionaler Parameter mit Default.
    assert cat["zonal_stats"]["params"]["stat"]["default"] == "sum"


def test_catalog_params_are_derived_from_the_params_models():
    """Es gibt nur EINE Beschreibung der Parameter: die Params-Klasse. Katalog,
    Editor und LLM leiten daraus ab - ``required_params`` ist genau die Menge der
    Felder ohne Default, und jedes Feld hat Typ und Pflicht-Kennzeichen."""
    from geofact.core.registry import default_registry

    entries = {entry["op"]: entry for entry in catalog.operation_catalog()}
    for plugin in default_registry().entries("operation"):
        fields = plugin.step_model.Params.model_fields
        entry = entries[plugin.name]
        assert set(entry["params"]) == set(fields), plugin.name
        assert entry["required_params"] == sorted(
            name for name, field in fields.items() if field.is_required()
        ), plugin.name
        for name, spec in entry["params"].items():
            assert spec["required"] == (name in entry["required_params"]), (
                plugin.name,
                name,
            )
            assert spec["type"] in {
                "number",
                "integer",
                "string",
                "boolean",
                "enum",
                "array",
                "object",
            }, (plugin.name, name, spec["type"])


def test_step_warning_events_emitted():
    # build_network mit unverbundenen Linien -> DisconnectedNetworkWarning.
    events: list[progress.ProgressEvent] = []
    run_scenario(_network_scenario(), on_event=events.append)
    warns = [e for e in events if e.type == progress.STEP_WARNING]
    assert warns, "erwartete mindestens eine step_warning (getrennte Komponenten)"
    assert warns[0].message
    assert warns[0].detail.get("category")


def test_scenario_json_schema_is_serializable():
    schema = catalog.scenario_json_schema()
    assert schema["title"] == "Scenario"
    json.dumps(schema)  # muss JSON-serialisierbar sein


def test_broken_plugin_import_warns_instead_of_vanishing_silently(tmp_path):
    """Vorbedingung: ein Plugin lässt sich nicht importieren (z. B.
    fehlende Abhängigkeit, Syntaxfehler). Nachbedingung: der Katalog
    darf nicht kippen (Plugins sind optional), aber der Ausfall muss
    sichtbar sein (Warnung) - vorher verschwand ein defektes Plugin
    kommentarlos (Regel 7). Seit FA40 baut discover() die Registry; der
    Katalog liest nur noch sie."""
    from geofact.core.errors import PluginLoadWarning
    from geofact.engine.discovery import discover

    (tmp_path / "kaputt.py").write_text(
        "raise ImportError('simulierter Plugin-Importfehler')\n", encoding="utf-8"
    )
    with pytest.warns(PluginLoadWarning, match="simulierter Plugin-Importfehler"):
        registry = discover(plugin_paths=[tmp_path])
    ops = {e["op"] for e in catalog.operation_catalog(registry)}
    assert "buffer" in ops
    assert "power_grid_topology" in ops
    assert any("kaputt.py" in origin for origin in registry.failed)


# ---------------------------------------------------------------------------
# engine/outputs
# ---------------------------------------------------------------------------


def test_write_outputs_creates_declared_files(tmp_path):
    scenario = _network_scenario()
    result = run_scenario(scenario)
    written = write_outputs(scenario, result, tmp_path).written
    names = {p.name for p in written}
    assert names == {"nodes.geojson", "nodes.csv"}
    for path in written:
        assert path.is_file() and path.stat().st_size > 0


def test_serialize_graph_attributes_do_not_overwrite_node_ids_coordinates_or_edge_ends():
    """Nachtreview 02.10. (Runde 2): Attribute wie ``source`` (OSM-Tag an
    Leitungen), ``id`` oder ``lat`` ersetzten id/lon/lat bzw. source/target -
    die Karte fand die Endknoten nicht mehr und ließ die Kante still weg."""
    import networkx as nx
    from shapely.geometry import Point

    from geofact.core.types import Graph

    g = nx.Graph()
    g.add_node("a", geometry=Point(12.0, 51.0), id=111, lat=7)
    g.add_node("b", geometry=Point(12.1, 51.1))
    g.add_edge("a", "b", source="survey", target="bing", voltage=110000)
    payload = serialize.graph_to_nodelink(Graph(g, "EPSG:4326"))

    node_a = next(n for n in payload["nodes"] if n["id"] == "a")
    assert node_a["lon"] == pytest.approx(12.0) and node_a["lat"] == pytest.approx(51.0)
    assert node_a["attr_id"] == 111 and node_a["attr_lat"] == 7
    [edge] = payload["edges"]
    assert {edge["source"], edge["target"]} == {"a", "b"}
    assert edge["attr_source"] == "survey" and edge["attr_target"] == "bing"
    assert edge["voltage"] == 110000
