"""Implements: FA18 (Homogene Geometriedimension erzwingen).

Contract-Tests für src/geofact/builtin/operations/homogenize.py: Happy Path,
Typfehler-Fall, Randfall leerer Layer (Testregel für Operationen) plus
Schema-Validierung des HomogenizeGeometryStep.
"""

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from geofact.builtin.operations import homogenize
from geofact.builtin.operations.homogenize import HomogenizeGeometryStep


def empty_features() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:32633")


# =====================================================================
# Happy Path
# =====================================================================


def test_fa18_mixed_geometry_keeps_dominant_dimension():
    features = gpd.GeoDataFrame(
        {"id": ["poly1", "line1"]},
        geometry=[
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            LineString([(0, 0), (100, 0)]),
        ],
        crs="EPSG:32633",
    )
    with pytest.warns(UserWarning):
        result = homogenize.run_homogenize_geometry({"features": features}, {})
    assert len(result) == 1
    assert (result.geometry.geom_type == "Polygon").all()


def test_fa18_single_geometry_type_returned_unchanged_no_warning():
    features = gpd.GeoDataFrame(
        {"id": ["p1", "p2"]},
        geometry=[
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]),
        ],
        crs="EPSG:32633",
    )
    with warnings_none():
        result = homogenize.run_homogenize_geometry({"features": features}, {})
    assert len(result) == 2
    assert (result.geometry.geom_type == "Polygon").all()


def test_fa18_regression_ortsteile_boundary_fragments():
    """Regression für die Motivation aus FA18: ein OSM-Layer wie
    'ortsteile' (boundary=administrative) mit echten Polygon-Ortsteil-
    grenzen (mit 'name'-Attribut) und losen LineString-Grenzsegment-
    Fragmenten (ohne 'name', wie sie bei OSM-Relationsartefakten
    typischerweise auftreten). homogenize_geometry muss ausschließlich
    die benannten Polygone behalten."""
    features = gpd.GeoDataFrame(
        {
            "id": ["ot1", "ot2", "frag1", "frag2", "frag3"],
            "name": ["Zentrum", "Suedvorstadt", None, None, None],
        },
        geometry=[
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
            Polygon([(20, 0), (30, 0), (30, 10), (20, 10)]),
            LineString([(10, 0), (10, 10)]),
            LineString([(10, 10), (20, 10)]),
            LineString([(20, 0), (20, 10)]),
        ],
        crs="EPSG:32633",
    )
    with pytest.warns(UserWarning):
        result = homogenize.run_homogenize_geometry({"features": features}, {})
    assert len(result) == 2
    assert set(result["name"]) == {"Zentrum", "Suedvorstadt"}
    assert (result.geometry.geom_type == "Polygon").all()


# =====================================================================
# Randfall: leerer Layer
# =====================================================================


def test_fa18_empty_layer_returns_empty_unchanged_no_warning():
    with warnings_none():
        result = homogenize.run_homogenize_geometry({"features": empty_features()}, {})
    assert result.empty
    assert result.crs == empty_features().crs


# =====================================================================
# Typfehler-Fall (Punkt/Linie/Flaeche gemischt - Dreifach-Mix)
# =====================================================================


def test_fa18_three_way_mix_keeps_only_highest_dimension():
    features = gpd.GeoDataFrame(
        {"id": ["pt", "ln", "poly"]},
        geometry=[
            Point(0, 0),
            LineString([(0, 0), (100, 0)]),
            Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
        ],
        crs="EPSG:32633",
    )
    with pytest.warns(UserWarning):
        result = homogenize.run_homogenize_geometry({"features": features}, {})
    assert len(result) == 1
    assert result.geometry.iloc[0].geom_type == "Polygon"


# =====================================================================
# Schema-Validierung (HomogenizeGeometryStep)
# =====================================================================


def test_fa18_schema_requires_features_port():
    with pytest.raises(ValueError, match="fehlende Eingänge"):
        HomogenizeGeometryStep(id="h", op="homogenize_geometry", inputs={}, params={})


def test_fa18_schema_valid_minimal_step():
    step = HomogenizeGeometryStep(
        id="h", op="homogenize_geometry", inputs={"features": "ortsteile"}, params={}
    )
    assert step.OUTPUT_TYPE.value == "vector"
    assert step.INPUT_PORTS == {"features": step.INPUT_PORTS["features"]}


class warnings_none:
    """Kleiner Kontextmanager, der sicherstellt, dass KEINE Warnung
    ausgelöst wird (pytest.warns(None) ist in neueren pytest-Versionen
    entfernt) - analog test_fa17_network_nodes.py::_no_warning_context."""

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
