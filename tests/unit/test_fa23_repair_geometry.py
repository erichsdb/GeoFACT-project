"""Implements: FA23 (Geometrie-Validität reparieren).

Contract-Tests für src/geofact/builtin/operations/repair.py: Happy Path,
Typfehler-Fall, Randfall leerer Layer (Testregel für Operationen) plus
Schema-Validierung des RepairGeometryStep.
"""

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from geofact.builtin.operations import repair
from geofact.builtin.operations.repair import RepairGeometryStep


def empty_features() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")


def bowtie_polygon() -> Polygon:
    """Klassisches Bowtie/Self-Intersection - zwei Dreiecke, die sich in
    einem Punkt kreuzen. is_valid == False, aber laedt/parsed fehlerfrei."""
    return Polygon([(0, 0), (10, 10), (10, 0), (0, 10), (0, 0)])


# =====================================================================
# Happy Path
# =====================================================================


def test_fa23_bowtie_polygon_repaired():
    features = gpd.GeoDataFrame(
        {"id": ["p1", "p2"], "name": ["bowtie", "valid"]},
        geometry=[
            bowtie_polygon(),
            Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]),
        ],
        crs="EPSG:32633",
    )
    assert not features.geometry.iloc[0].is_valid

    with pytest.warns(repair.GeometryRepairedWarning, match="1 Objekt"):
        result = repair.run_repair_geometry({"features": features}, {})

    assert len(result) == 2
    assert result.geometry.is_valid.all()
    assert not result.geometry.is_empty.any()
    # Attribute und Reihenfolge bleiben erhalten.
    assert list(result["id"]) == ["p1", "p2"]
    assert list(result["name"]) == ["bowtie", "valid"]
    # die zweite (bereits gültige) Geometrie bleibt byte-identisch.
    assert result.geometry.iloc[1].equals_exact(
        Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]), tolerance=0
    )


def test_fa23_valid_layer_unchanged():
    features = gpd.GeoDataFrame(
        {"id": ["p1", "p2"]},
        geometry=[
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]),
        ],
        crs="EPSG:32633",
    )
    with warnings_none():
        result = repair.run_repair_geometry({"features": features}, {})
    assert len(result) == 2
    assert result.geometry.iloc[0].equals_exact(features.geometry.iloc[0], tolerance=0)
    assert result.geometry.iloc[1].equals_exact(features.geometry.iloc[1], tolerance=0)


# =====================================================================
# Randfall: leerer Layer
# =====================================================================


def test_fa23_empty_layer_passthrough():
    with warnings_none():
        result = repair.run_repair_geometry({"features": empty_features()}, {})
    assert result.empty
    assert result.crs == empty_features().crs


# =====================================================================
# Fehlende/leere Geometrien werden verworfen
# =====================================================================


def test_fa23_missing_and_empty_geometries_dropped_with_warning():
    features = gpd.GeoDataFrame(
        {"id": ["ok", "missing", "empty"]},
        geometry=[
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            None,
            Polygon(),
        ],
        crs="EPSG:32633",
    )
    with pytest.warns(repair.EmptyGeometryDroppedWarning, match="2 Objekt"):
        result = repair.run_repair_geometry({"features": features}, {})
    assert len(result) == 1
    assert list(result["id"]) == ["ok"]
    assert result.geometry.is_valid.all()
    assert not result.geometry.is_empty.any()


# =====================================================================
# Typfehler-Fall: Warnung meldet Anzahl reparierter/verworfener Objekte
# =====================================================================


def test_fa23_repair_warns_with_count():
    features = gpd.GeoDataFrame(
        {"id": ["b1", "b2", "ok", "missing"]},
        geometry=[
            bowtie_polygon(),
            bowtie_polygon(),
            Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]),
            None,
        ],
        crs="EPSG:32633",
    )
    with pytest.warns(UserWarning) as records:
        result = repair.run_repair_geometry({"features": features}, {})

    messages = [str(r.message) for r in records]
    assert any("2 Objekt" in m and "repariert" in m for m in messages)
    assert any("1 Objekt" in m and "verworfen" in m for m in messages)
    # 4 Eingabezeilen - 1 verworfene (missing) = 3 verbleibende Zeilen;
    # die Reparatur selbst verwirft keine Zeile.
    assert len(result) == 3
    assert result.geometry.is_valid.all()


def test_fa23_repair_can_change_geometry_type():
    """make_valid kann den Geometrietyp ändern (Bowtie-Polygon ->
    MultiPolygon) - dokumentiertes, erwartetes Verhalten (FA23)."""
    features = gpd.GeoDataFrame(
        {"id": ["b1"]}, geometry=[bowtie_polygon()], crs="EPSG:32633"
    )
    with pytest.warns(repair.GeometryRepairedWarning):
        result = repair.run_repair_geometry({"features": features}, {})
    assert len(result) == 1
    assert result.geometry.iloc[0].is_valid
    assert result.geometry.iloc[0].geom_type in (
        "MultiPolygon",
        "Polygon",
        "GeometryCollection",
    )


# =====================================================================
# CRS bleibt erhalten
# =====================================================================


def test_fa23_crs_preserved():
    features = gpd.GeoDataFrame(
        {"id": ["b1"]}, geometry=[bowtie_polygon()], crs="EPSG:25833"
    )
    with pytest.warns(repair.GeometryRepairedWarning):
        result = repair.run_repair_geometry({"features": features}, {})
    assert result.crs == "EPSG:25833"


# =====================================================================
# Schema-Validierung (RepairGeometryStep)
# =====================================================================


def test_fa23_schema_requires_features_port():
    with pytest.raises(ValueError, match="fehlende Eingänge"):
        RepairGeometryStep(id="r", op="repair_geometry", inputs={}, params={})


def test_fa23_schema_valid_minimal_step():
    step = RepairGeometryStep(
        id="r", op="repair_geometry", inputs={"features": "flaechennutzung"}, params={}
    )
    assert step.OUTPUT_TYPE.value == "vector"
    assert step.INPUT_PORTS == {"features": step.INPUT_PORTS["features"]}


def test_fa23_schema_rejects_unknown_input_port():
    with pytest.raises(ValueError, match="unbekannte Eingänge"):
        RepairGeometryStep(
            id="r",
            op="repair_geometry",
            inputs={"features": "flaechennutzung", "extra": "sonstiges"},
            params={},
        )


class warnings_none:
    """Kleiner Kontextmanager, der sicherstellt, dass KEINE Warnung
    ausgelöst wird (pytest.warns(None) ist in neueren pytest-Versionen
    entfernt) - analog test_fa18_homogenize_geometry.py::warnings_none."""

    def __enter__(self):
        import warnings

        self._catch = warnings.catch_warnings(record=True)
        self._records = self._catch.__enter__()
        warnings.simplefilter("always")
        return self._records

    def __exit__(self, exc_type, exc, tb):
        self._catch.__exit__(exc_type, exc, tb)
        assert not self._records, f"unerwartete Warnung(en): {self._records}"
        return False
