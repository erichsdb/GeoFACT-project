"""Implements: FA12 (Attributauswahl der Web-Ansicht).

Contract-Tests für backend/geofact_web/presentation/view_columns.py: konfigurationsrelevante
Spalten bleiben erhalten, der Rest wird nach Belegungsdichte bis zum
Budget aufgefüllt, Verworfenes wird gemeldet. Zusätzlich: Contract-Tests
für Step.produced_fields() (core/contracts.py) - der Bugfix-Gegenstand
vom 26.08.2026: zonal_stats' Ergebnisspalte
'sum_value' wurde vom Spaltenbudget verworfen, weil sie nirgends als
Pflichtspalte deklariert war.
"""

from pathlib import Path

import geopandas as gpd
import numpy as np
from affine import Affine
from pyproj import CRS
from shapely.geometry import Point, Polygon

from geofact_web.presentation import view_columns
from geofact.core.scenario import Scenario
from geofact.core.types import RasterLayer
from geofact.builtin.operations import hexbin as density, hex_grid as hexgrid
from geofact.builtin.operations.zonal_stats import run_zonal_stats
from geofact.builtin.operations.aggregate import AggregateStep
from geofact.builtin.operations.aggregate import run_aggregate
from geofact.builtin.operations.hexbin import HexbinStep
from geofact.builtin.operations.hex_grid import HexGridStep
from geofact.builtin.operations.reachability import IsochroneStep, ShortestPathStep
from geofact.builtin.operations.classify import ClassifyStep
from geofact.builtin.operations.nearest_distance import NearestDistanceStep
from geofact.builtin.operations.ranking import RankingStep
from geofact.builtin.operations.zonal_stats import ZonalStatsStep

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _scenario(**overrides) -> Scenario:
    config = {
        "scenario": {"name": "Test", "region": "Dresden"},
        "layers": [
            {
                "id": "places",
                "source": "osm",
                "tags": {"place": "village"},
                "data_type": "vector",
            }
        ],
        "steps": [
            {
                "id": "nearest",
                "op": "nearest_distance",
                "inputs": {"from": "places", "to": "places"},
                "params": {},
            },
            {
                "id": "isolated",
                "op": "filter",
                "inputs": {"features": "nearest"},
                "params": {"condition": "place == 'village' and distance > 5000"},
            },
        ],
        "output": [{"type": "geojson", "source": "isolated"}],
    }
    config.update(overrides)
    return Scenario.model_validate(config)


def _wide_gdf(rows: int = 100) -> gpd.GeoDataFrame:
    """Breiter, dünn besetzter Layer (OSM-Realfall im Kleinen)."""
    data = {
        "name": [f"Ort {i}" for i in range(rows)],
        "place": ["village"] * rows,
        # distance ist konfigurationsrelevant, aber nur selten belegt.
        "distance": [1234.0 if i == 0 else None for i in range(rows)],
        "population": [i for i in range(rows)],
        # Sehr dünne Spalten - Kandidaten fürs Verwerfen.
        "name:fa": [None] * rows,
        "openGeoDB:layer": ["x" if i < 2 else None for i in range(rows)],
        "wikidata": [f"Q{i}" if i < 50 else None for i in range(rows)],
    }
    return gpd.GeoDataFrame(
        data,
        geometry=[Point(13.7 + i * 1e-4, 51.0) for i in range(rows)],
        crs="EPSG:4326",
    )


def test_fa12_required_columns_from_filter_condition():
    """Vorbedingung: Filterbedingung nennt Attribute.
    Nachbedingung: Attributnamen sind erkannt, String-Literale nicht."""
    names = view_columns.required_columns(_scenario())

    assert "place" in names
    assert "distance" in names
    # 'village' ist ein Wert, kein Spaltenname.
    assert "village" not in names
    # Operatoren/Schluesselwoerter sind keine Spalten.
    assert "and" not in names


def test_fa12_required_columns_from_classify_and_color_field():
    scenario = _scenario(
        steps=[
            {
                "id": "klassiert",
                "op": "classify",
                "inputs": {"features": "places"},
                "params": {
                    "field": "risk_score",
                    "breaks": [1, 2],
                    "labels": ["a", "b", "c"],
                },
            }
        ],
        output=[{"type": "map", "source": "klassiert", "color_field": "population"}],
    )

    names = view_columns.required_columns(scenario)

    assert "risk_score" in names
    assert "population" in names


def test_fa12_pinned_columns_survive_sparse_occupancy():
    """Kernvertrag: eine konfigurationsrelevante Spalte darf NICHT wegen
    dünner Belegung verschwinden - sonst fällt genau das Attribut weg,
    um das es in der Analyse geht (goldene Regel 7)."""
    gdf = _wide_gdf()

    keep, dropped = view_columns.select_columns(
        gdf, pinned={"place", "distance"}, budget=3
    )

    assert "distance" in keep  # nur 1 von 100 Zeilen belegt
    assert "place" in keep
    assert dropped > 0


def test_fa12_budget_filled_by_density():
    """Nach den Pflichtspalten wird nach Belegungsdichte aufgefüllt."""
    gdf = _wide_gdf()

    keep, _ = view_columns.select_columns(gdf, pinned=set(), budget=3)

    # name/place/population sind voll belegt, wikidata nur zur Hälfte,
    # name:fa gar nicht.
    assert "name:fa" not in keep
    assert len([c for c in keep if c != "geometry"]) == 3


def test_fa12_never_fills_with_all_null_columns():
    """Randfall: Budget größer als die Zahl belegter Spalten - leere
    Spalten werden trotzdem nicht aufgefüllt."""
    gdf = _wide_gdf()

    keep, _ = view_columns.select_columns(gdf, pinned=set(), budget=50)

    assert "name:fa" not in keep


def test_fa12_geometry_always_present():
    gdf = _wide_gdf()

    keep, _ = view_columns.select_columns(gdf, pinned=set(), budget=1)

    assert "geometry" in keep


def test_fa12_select_columns_handles_attribute_free_layer():
    """Randfall: Layer ohne Attributspalten."""
    gdf = gpd.GeoDataFrame(geometry=[Point(13.7, 51.0)], crs="EPSG:4326")

    keep, dropped = view_columns.select_columns(gdf, pinned={"egal"}, budget=10)

    assert keep == ["geometry"]
    assert dropped == 0


def test_fa12_serialize_reports_dropped_columns():
    """Die Reduktion muss sichtbar sein, nicht still passieren."""
    from geofact_web.presentation import serialize

    gdf = _wide_gdf()
    payload = serialize.serialize_result(gdf, pinned_columns={"place"}, column_budget=2)

    assert payload["dropped_columns"] > 0
    props = payload["geojson"]["features"][0]["properties"]
    assert "place" in props
    # describe() meldet weiterhin ALLE Spalten des echten Layers.
    assert len(payload["describe"]["columns"]) > len(props)


# =====================================================================
# Step.produced_fields() Contract (Bugfix 26.08.2026, siehe Modul-
# Docstring): erzeugte Ergebnisspalten werden über diesen Contract
# gepinnt, unabhängig von ihrer Belegungsdichte.
# =====================================================================


def _leipzig_haushalte_scenario() -> Scenario:
    """Mirrors the saved web scenario "Einwohner außerhalb 1km Schule Leipzig"
    (Pipeline-Form, ohne echte OSM-/Rasterdaten): osm-Layer + file-Raster
    -> buffer -> dissolve -> overlay(difference) -> zonal_stats -> csv-
    Output ohne color_field. Der ursprüngliche Bug: zonal_stats' 'sum_
    value' war keine deklarierte Pflichtspalte und fiel beim Spalten-
    budget-Tiebreak (Reihenfolge im Layer) als zuletzt angehängte Spalte
    weg."""
    config = {
        "scenario": {
            "name": "Einwohner außerhalb 1km Schule Leipzig",
            "region": "Leipzig",
        },
        "layers": [
            {
                "id": "leipzig_boundary",
                "source": "osm",
                "data_type": "vector",
                "tags": {
                    "boundary": "administrative",
                    "admin_level": "6",
                    "name": "Leipzig",
                },
            },
            {
                "id": "schools",
                "source": "osm",
                "data_type": "vector",
                "tags": {"amenity": "school"},
            },
            {
                "id": "population",
                "source": "file",
                "data_type": "raster",
                "format": "tif",
                "path": "examples/data/population.tif",
            },
        ],
        "steps": [
            {
                "id": "buffer_schools",
                "op": "buffer",
                "inputs": {"geometry": "schools"},
                "params": {"radius_km": 1},
            },
            {
                "id": "dissolve_schools",
                "op": "dissolve",
                "inputs": {"features": "buffer_schools"},
                "params": {},
            },
            {
                "id": "outside_schools",
                "op": "overlay",
                "inputs": {"left": "leipzig_boundary", "right": "dissolve_schools"},
                "params": {"method": "difference"},
            },
            {
                "id": "pop_outside",
                "op": "zonal_stats",
                "inputs": {"zones": "outside_schools", "values": "population"},
                "params": {"stat": "sum", "decimals": 0},
            },
        ],
        "output": [{"type": "csv", "source": "pop_outside"}],
    }
    return Scenario.model_validate(config)


def _osm_boundary_row_with_sum_value() -> gpd.GeoDataFrame:
    """1 Zeile, 30 voll belegte OSM-Grenz-Tag-Spalten, 'sum_value' als
    LETZTE Spalte angehängt (wie run_zonal_stats es tut) - genau die
    Form, in der der Bug live beobachtet wurde."""
    data = {f"osm_tag_{i}": [f"wert_{i}"] for i in range(30)}
    data["sum_value"] = [60966.0]
    return gpd.GeoDataFrame(
        data, geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])], crs="EPSG:4326"
    )


def test_fa12_view_columns_pins_zonal_stats_result():
    scenario = _leipzig_haushalte_scenario()
    gdf = _osm_boundary_row_with_sum_value()

    keep, dropped = view_columns.select_columns(
        gdf, pinned=view_columns.required_columns(scenario), budget=10
    )

    assert "sum_value" in keep
    # 31 Attributspalten, budget=10: sum_value gepinnt + 9 weitere voll
    # belegte Spalten füllen das Budget auf -> 21 verworfen.
    assert dropped == 21


# ---------------------------------------------------------------------
# produced_fields() gegen die tatsächliche Operation verifiziert
# ---------------------------------------------------------------------


def test_fa12_produced_fields_zonal_stats_matches_operation_output():
    zone = gpd.GeoDataFrame(
        {"id": ["z1"]},
        geometry=[
            Polygon([(13.70, 51.03), (13.78, 51.03), (13.78, 51.07), (13.70, 51.07)])
        ],
        crs="EPSG:4326",
    )
    raster = RasterLayer(
        data=np.full((10, 10), 100, dtype="int32"),
        transform=Affine(0.008, 0, 13.70, 0, -0.004, 51.07),
        crs=CRS.from_epsg(4326),
    )
    step = ZonalStatsStep(
        id="s",
        op="zonal_stats",
        inputs={"zones": "zones", "values": "values"},
        params={"stat": "sum"},
    )
    result = run_zonal_stats({"zones": zone, "values": raster}, step.params)

    added = set(result.columns) - set(zone.columns)
    assert step.produced_fields() <= added


def test_fa12_produced_fields_aggregate_count_matches_operation_output():
    zones = gpd.GeoDataFrame(
        {"name": ["A"]},
        geometry=[Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
        crs="EPSG:32633",
    )
    features = gpd.GeoDataFrame(
        {"staff": [1.0]}, geometry=[Point(1, 1)], crs="EPSG:32633"
    )
    step = AggregateStep(
        id="s",
        op="aggregate",
        inputs={"features": "f", "zones": "z"},
        params={"statistic": "count"},
    )
    result = run_aggregate({"features": features, "zones": zones}, step.params)

    added = set(result.columns) - set(zones.columns)
    assert step.produced_fields() <= added


def test_fa12_produced_fields_aggregate_sum_matches_operation_output():
    zones = gpd.GeoDataFrame(
        {"name": ["A"]},
        geometry=[Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
        crs="EPSG:32633",
    )
    features = gpd.GeoDataFrame(
        {"staff": [2.0, 4.0]}, geometry=[Point(1, 1), Point(2, 2)], crs="EPSG:32633"
    )
    step = AggregateStep(
        id="s",
        op="aggregate",
        inputs={"features": "f", "zones": "z"},
        params={"statistic": "sum", "value_field": "staff"},
    )
    result = run_aggregate({"features": features, "zones": zones}, step.params)

    added = set(result.columns) - set(zones.columns)
    assert step.produced_fields() <= added


def test_fa12_produced_fields_hexbin_matches_operation_output():
    gdf = gpd.GeoDataFrame(
        {"id": range(5)},
        geometry=[Point(x, x) for x in range(5)],
        crs="EPSG:32633",
    )
    step = HexbinStep(
        id="s", op="hexbin", inputs={"features": "f"}, params={"cell_km": 1}
    )
    result = density.run_hexbin({"features": gdf}, step.params)

    added = set(result.columns) - set(gdf.columns)
    assert step.produced_fields() <= added


def test_fa12_produced_fields_hex_grid_matches_operation_output():
    region = gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[Polygon([(0, 0), (5000, 0), (5000, 5000), (0, 5000)])],
        crs="EPSG:32633",
    )
    step = HexGridStep(
        id="s", op="hex_grid", inputs={"region": "r"}, params={"cell_km": 1}
    )
    result = hexgrid.run_hex_grid({"region": region}, step.params)

    added = set(result.columns) - set(region.columns)
    assert step.produced_fields() <= added


def test_fa12_produced_fields_classify_name_assertion():
    step = ClassifyStep(
        id="s",
        op="classify",
        inputs={"features": "f"},
        params={"field": "x", "breaks": [1], "labels": ["a", "b"]},
    )
    assert step.produced_fields() == {"class"}


def test_fa12_produced_fields_nearest_distance_name_assertion():
    step = NearestDistanceStep(
        id="s", op="nearest_distance", inputs={"from": "a", "to": "b"}, params={}
    )
    assert step.produced_fields() == {"distance"}


def test_fa12_produced_fields_shortest_path_name_assertion():
    step = ShortestPathStep(
        id="s",
        op="shortest_path",
        inputs={"graph": "g", "origins": "o", "destinations": "d"},
        params={"max_snap_m": 100},
    )
    assert step.produced_fields() == {"network_distance"}


def test_fa12_produced_fields_isochrone_name_assertion():
    step = IsochroneStep(
        id="s",
        op="isochrone",
        inputs={"graph": "g", "origins": "o"},
        params={"breaks_m": [500], "max_snap_m": 100},
    )
    assert step.produced_fields() == {"break_m"}


# ---------------------------------------------------------------------
# Default-Implementierung: output_field aus params bzw. PARAMS-Default
# ---------------------------------------------------------------------


def test_fa12_produced_fields_default_output_field_from_params():
    """Ein Schritt mit explizitem params.output_field liefert genau
    diesen Namen (ranking: output_field ist optional mit Default 'score')."""
    step = RankingStep(
        id="s",
        op="ranking",
        inputs={"features": "f"},
        params={"by": ["a"], "weights": [1.0], "output_field": "prioritaet"},
    )
    assert step.produced_fields() == {"prioritaet"}


def test_fa12_produced_fields_default_output_field_from_params_spec_default():
    """Ohne explizites output_field greift der PARAMS-Default (ranking:
    'score')."""
    step = RankingStep(
        id="s",
        op="ranking",
        inputs={"features": "f"},
        params={"by": ["a"], "weights": [1.0]},
    )
    assert step.produced_fields() == {"score"}


def test_fa12_produced_fields_step_without_output_field_is_empty():
    """Schritte ohne neue Attributspalten (z. B. buffer) und ohne
    output_field-Contract behalten den leeren Default."""
    from geofact.builtin.operations.buffer import BufferStep

    step = BufferStep(
        id="s", op="buffer", inputs={"geometry": "f"}, params={"radius_km": 1}
    )
    assert step.produced_fields() == set()
