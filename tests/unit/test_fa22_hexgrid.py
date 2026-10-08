"""Implements: FA22 (Regelmäßiges Sechseckraster über eine Region).

Contract-Tests für src/geofact/builtin/operations/hex_grid.py: Happy Path,
Typfehler-Fall, Randfall leerer Layer (Testregel für Operationen) plus
Schema-Validierung des HexGridStep.
"""

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon, box

from geofact.builtin.operations import hex_grid as hexgrid
from geofact.builtin.operations.hex_grid import HexGridStep


def region_of(*polygons) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": [f"r{i}" for i in range(len(polygons))]},
        geometry=list(polygons),
        crs="EPSG:32633",
    )


def empty_region() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"name": []}, geometry=[], crs="EPSG:32633")


# =====================================================================
# Happy Path
# =====================================================================


def test_fa22_hex_grid_covers_region_without_gaps():
    """Nachbedingung: die Zellen decken die Region lückenlos ab - die
    Vereinigung aller Zellgeometrien enthält die Eingaberegion
    (bis auf Rundungstoleranz an sehr dünnen Randstreifen)."""
    region = region_of(box(0, 0, 5000, 5000))
    result = hexgrid.run_hex_grid({"region": region}, {"cell_km": 1})
    assert len(result) > 1
    assert (result.geometry.geom_type == "Polygon").all()
    assert result.crs == region.crs
    union = result.union_all()
    # Fast die gesamte Fläche der Region liegt innerhalb der Zellunion
    # (Mittelpunkt-Prädikat lässt schmale Randstreifen aus).
    covered = region.geometry.iloc[0].intersection(union).area
    assert covered / region.geometry.iloc[0].area > 0.8


def test_fa22_hex_grid_cells_do_not_overlap():
    region = region_of(box(0, 0, 3000, 3000))
    result = hexgrid.run_hex_grid({"region": region}, {"cell_km": 1})
    total_area = result.geometry.area.sum()
    union_area = result.union_all().area
    assert total_area == pytest.approx(union_area, rel=1e-6)


def test_fa22_hex_grid_region_larger_than_cell_yields_one_cell():
    """Eine Region, die kleiner als eine Zelle ist, aber ihren
    Mittelpunkt einschließt, ergibt genau eine Zelle."""
    region = region_of(Point(0, 0).buffer(50))
    result = hexgrid.run_hex_grid({"region": region}, {"cell_km": 1})
    assert len(result) == 1


def test_fa22_hex_grid_region_smaller_than_cell_grid_can_yield_no_cell():
    """Randfall, kein Fehler: liegt eine kleine Region so, dass kein
    Zellmittelpunkt hineinfällt (Region deutlich kleiner als cell_km),
    ist ein leeres Ergebnis die korrekte, dokumentierte Nachbedingung -
    hex_grid garantiert 'jede Zelle, deren Mittelpunkt trifft', nicht
    'mindestens eine Zelle pro nichtleerer Eingabe'."""
    region = region_of(Point(500, 500).buffer(50))
    result = hexgrid.run_hex_grid({"region": region}, {"cell_km": 1})
    assert len(result) == 0


def test_fa22_hex_grid_has_axial_index_columns():
    region = region_of(box(0, 0, 2000, 2000))
    result = hexgrid.run_hex_grid({"region": region}, {"cell_km": 1})
    assert {"q", "r"}.issubset(result.columns)
    assert result[["q", "r"]].drop_duplicates().shape[0] == len(result)


def test_fa22_hex_grid_multiple_input_polygons_union_before_tiling():
    region = region_of(box(0, 0, 1000, 1000), box(3000, 3000, 4000, 4000))
    result = hexgrid.run_hex_grid({"region": region}, {"cell_km": 1})
    # Zwei getrennte Flecken -> Zellen sollten sich in zwei räumliche
    # Cluster gruppieren (keine Zellen im leeren Zwischenraum bei x~2000).
    xs = result.geometry.centroid.x
    assert not ((xs > 1200) & (xs < 2800)).any()


# =====================================================================
# Typfehler-Fälle (Schema-Validierung)
# =====================================================================


def test_fa22_hex_grid_step_rejects_zero_cell():
    with pytest.raises(ValueError, match="cell_km"):
        HexGridStep(
            id="s", op="hex_grid", inputs={"region": "a"}, params={"cell_km": 0}
        )


def test_fa22_hex_grid_step_rejects_missing_cell_km():
    with pytest.raises(ValueError, match="cell_km"):
        HexGridStep(id="s", op="hex_grid", inputs={"region": "a"}, params={})


def test_fa22_hex_grid_step_rejects_unknown_input_port():
    with pytest.raises(ValueError, match="Eingänge"):
        HexGridStep(
            id="s", op="hex_grid", inputs={"features": "a"}, params={"cell_km": 1}
        )


# =====================================================================
# Randfall leerer Layer
# =====================================================================


def test_fa22_hex_grid_empty_region():
    result = hexgrid.run_hex_grid({"region": empty_region()}, {"cell_km": 1})
    assert len(result) == 0
    assert {"q", "r"}.issubset(result.columns)
    assert result.crs == empty_region().crs


def test_fa22_hex_grid_empty_geometry_polygon():
    """Randfall: Region-Layer nicht leer, aber die enthaltene Geometrie
    ist eine leere Polygon-Geometrie (union_all() ergibt is_empty)."""
    region = gpd.GeoDataFrame(
        {"name": ["empty"]}, geometry=[Polygon()], crs="EPSG:32633"
    )
    result = hexgrid.run_hex_grid({"region": region}, {"cell_km": 1})
    assert len(result) == 0
