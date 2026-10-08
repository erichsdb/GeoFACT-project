"""Implements: FA11 (Plugin-Schnittstelle).

Contract-Tests für src/geofact/builtin/operations/power_grid_topology.py.
"""

from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from pydantic import ValidationError

from geofact.core.scenario import Scenario

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def base_scenario(**overrides) -> dict:
    config = {
        "scenario": {"name": "Plugin-Test", "region": "13.60,51.01,13.94,51.15"},
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
                "op": "power_grid_topology",
                "inputs": {"substations": "substations", "power_lines": "power_lines"},
                "params": {"min_voltage": 100000},
            },
            {
                "id": "scored",
                "op": "centrality",
                "inputs": {"graph": "grid"},
            },
            {
                "id": "as_vector",
                "op": "graph_to_vector",
                "inputs": {"graph": "scored"},
            },
        ],
        "output": [{"type": "map", "source": "as_vector"}],
    }
    config.update(overrides)
    return config


def test_fa11_plugin_registered_and_callable_from_yaml():
    scenario = Scenario(**base_scenario())
    assert scenario.execution_order() == ["grid", "scored", "as_vector"]


def test_fa11_plugin_contract_type_checked():
    # power_grid_topology liefert GRAPH, nicht VECTOR - direkte
    # Weiterverwendung als Vektor-Eingang muss FA2 (Typprüfung) verletzen
    config = base_scenario(
        steps=[
            {
                "id": "grid",
                "op": "power_grid_topology",
                "inputs": {"substations": "substations", "power_lines": "power_lines"},
                "params": {"min_voltage": 100000},
            },
            {
                "id": "gefiltert",
                "op": "filter",
                "inputs": {"features": "grid"},
                "params": {"condition": "x > 1"},
            },
        ],
        output=[{"type": "map", "source": "gefiltert"}],
    )
    with pytest.raises(ValidationError, match="erwartet"):
        Scenario(**config)


def test_fa11_plugin_missing_min_voltage_raises():
    config = base_scenario(
        steps=[
            {
                "id": "grid",
                "op": "power_grid_topology",
                "inputs": {"substations": "substations", "power_lines": "power_lines"},
            },
        ],
        output=[{"type": "map", "source": "grid"}],
    )
    with pytest.raises(ValidationError) as caught:
        Scenario(**config)
    (error,) = caught.value.errors()
    assert error["type"] == "missing"
    assert error["loc"] == ("steps", 0, "params", "min_voltage")


def test_fa11_plugin_filters_by_voltage_and_builds_graph():
    from geofact.builtin.operations.power_grid_topology import run_power_grid_topology

    substations = gpd.read_file(FIXTURES_DIR / "substations.geojson")
    power_lines = gpd.read_file(FIXTURES_DIR / "power_lines.geojson")

    result = run_power_grid_topology(
        {"substations": substations, "power_lines": power_lines},
        {"min_voltage": 100000},
    )
    # s4 (5000V) und s5 (kein voltage -> NaN >= threshold ist False) fallen raus
    assert result.nx_graph.number_of_nodes() == 3
    assert "s4" not in result.nx_graph.nodes
    assert "s5" not in result.nx_graph.nodes


# =====================================================================
# Reale OSM-Spannungswerte (LLM-Vorstudie 03.10.2026)
# =====================================================================


def _run_with_voltages(voltages: list) -> tuple:
    """power_grid_topology auf der Fixture mit ersetzten voltage-Texten;
    liefert (Graph, VoltageParseWarnings)."""
    import warnings

    from geofact.builtin.operations.power_grid_topology import (
        VoltageParseWarning,
        run_power_grid_topology,
    )

    substations = gpd.read_file(FIXTURES_DIR / "substations.geojson").to_crs(
        "EPSG:32633"
    )
    power_lines = gpd.read_file(FIXTURES_DIR / "power_lines.geojson").to_crs(
        "EPSG:32633"
    )
    substations["voltage"] = pd.Series(voltages, dtype="object")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = run_power_grid_topology(
            {"substations": substations, "power_lines": power_lines},
            {"min_voltage": 100000},
        )
    return graph, [w for w in caught if issubclass(w.category, VoltageParseWarning)]


def test_fa11_voltage_text_values_like_low_do_not_crash():
    """Regression: 'low' brach mit 'could not convert string to float' ab
    (P09 der Vorstudie). Jetzt: Umspannwerk fällt mit Warnung heraus."""
    graph, caught = _run_with_voltages(["110000", "low", "380000", "5000", "220000"])
    assert set(graph.nx_graph.nodes) == {"s1", "s3", "s5"}
    (warning,) = caught
    message = str(warning.message)
    assert "1 mit nicht numerischer Spannung (z. B. 'low')" in message
    assert "diese 1 von 5 werden nicht einbezogen" in message


def test_fa11_voltage_multi_value_counts_with_its_highest_level():
    """'110000;20000' ist ein 110/20-kV-Umspannwerk: es gehört zum
    110-kV-Netz und bleibt bei min_voltage 100000 drin; '5000;10000' nicht."""
    graph, caught = _run_with_voltages(
        ["110000;20000", "380000;110000", "220000", "5000;10000", None]
    )
    assert set(graph.nx_graph.nodes) == {"s1", "s2", "s3"}
    (warning,) = caught
    message = str(warning.message)
    assert "3 mit mehreren Spannungen" in message
    assert "'110000;20000'" in message
    assert "1 ohne Spannungsangabe" in message


def test_fa11_parse_voltage_categories():
    from geofact.builtin.operations.power_grid_topology import parse_voltage

    raw = pd.Series(
        [
            "110000",
            " 380000 ; 220000 ",
            "medium",
            "110000;unknown",
            "",
            None,
            20000.0,
            float("nan"),
        ],
        dtype="object",
    )
    parse = parse_voltage(raw)
    values = parse.values
    assert values[[0, 1, 3, 6]].tolist() == [110000.0, 380000.0, 110000.0, 20000.0]
    assert values[[2, 4, 5, 7]].isna().all()
    assert parse.multi == 2
    assert parse.unreadable == 1
    assert parse.unreadable_examples == ("'medium'",)
    assert parse.missing == 3


def test_fa11_numeric_voltage_without_gaps_gives_no_warning():
    graph, caught = _run_with_voltages([110000, 220000, 380000, 5000, 1000])
    assert set(graph.nx_graph.nodes) == {"s1", "s2", "s3"}
    assert caught == []


def test_fa11_voltage_all_unreadable_gives_an_empty_selection_with_a_warning():
    """Randfall: keine einzige lesbare Spannung -> kein Umspannwerk, aber eine
    Warnung mit der Ursache statt eines leeren Ergebnisses ohne Hinweis."""
    graph, caught = _run_with_voltages(["low", "high", "medium", "low", "yes"])
    assert not any(
        node in graph.nx_graph.nodes for node in ("s1", "s2", "s3", "s4", "s5")
    )
    (warning,) = caught
    assert "diese 5 von 5 werden nicht einbezogen" in str(warning.message)
