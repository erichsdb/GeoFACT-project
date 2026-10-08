"""Implements: FA8 (räumliche Operationen).

Contract-Tests für die Vektor-Operationen src/geofact/builtin/operations/{buffer,
nearest_distance,spatial_join,overlay,clip,dissolve,zonal_stats,classify,filter,ranking}.py. Je Operation:
Happy Path, Typfehler-Fall (falscher Werttyp/Parameter), Randfall
leerer Layer (Testregel für Operationen).
"""

import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from affine import Affine
from pyproj import CRS
from shapely.geometry import LineString, Point, Polygon

from geofact.core.types import RasterLayer
from geofact.builtin.operations import to_points as normalize
from geofact.builtin.operations.buffer import run_buffer
from geofact.builtin.operations.classify import run_classify
from geofact.builtin.operations.clip import run_clip
from geofact.builtin.operations.dissolve import run_dissolve
from geofact.builtin.operations.filter import run_filter
from geofact.builtin.operations.nearest_distance import run_nearest_distance
from geofact.builtin.operations.overlay import run_overlay
from geofact.builtin.operations.ranking import run_ranking
from geofact.builtin.operations.spatial_join import run_spatial_join
from geofact.builtin.operations.zonal_stats import run_zonal_stats

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def substations() -> gpd.GeoDataFrame:
    return gpd.read_file(FIXTURES_DIR / "substations.geojson")


def empty_points() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:4326")


# =====================================================================
# buffer
# =====================================================================


def test_fa08_buffer_radius_km():
    gdf = substations()
    result = run_buffer({"geometry": gdf}, {"radius_km": 1})
    assert (result.geometry.area > 0).all()
    assert len(result) == len(gdf)


def test_fa08_buffer_zero_radius_area_zero():
    gdf = substations()
    result = run_buffer({"geometry": gdf}, {"radius_km": 0})
    assert (result.geometry.area == 0).all()


def test_fa08_buffer_empty_layer():
    gdf = empty_points()
    result = run_buffer({"geometry": gdf}, {"radius_km": 1})
    assert len(result) == 0


# =====================================================================
# nearest_distance
# =====================================================================


def test_fa08_nearest_distance_happy_path():
    from_gdf = gpd.GeoDataFrame({"id": ["a"]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    to_gdf = gpd.GeoDataFrame(
        {"id": ["b", "c"]}, geometry=[Point(1, 0), Point(5, 0)], crs="EPSG:4326"
    )
    result = run_nearest_distance({"from": from_gdf, "to": to_gdf}, {})
    assert result["distance"].iloc[0] == pytest.approx(1.0)


def test_fa08_nearest_distance_empty_to_yields_nan():
    from_gdf = gpd.GeoDataFrame({"id": ["a"]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    result = run_nearest_distance({"from": from_gdf, "to": empty_points()}, {})
    assert result["distance"].isna().all()


def test_fa08_nearest_distance_empty_from():
    result = run_nearest_distance({"from": empty_points(), "to": substations()}, {})
    assert len(result) == 0


def test_fa08_nearest_distance_excludes_self_match_same_layer():
    """Nachbedingung: bei identischem from/to-Layer (z. B. nächster ANDERER
    Ort im selben Dorf-Layer) darf jede Geometrie nicht sich selbst als
    nächsten Punkt finden (sonst distance=0 für alle Objekte, Bugfix)."""
    villages = gpd.GeoDataFrame(
        {"id": ["a", "b", "c"]},
        geometry=[Point(0, 0), Point(1, 0), Point(5, 0)],
        crs="EPSG:4326",
    )
    result = run_nearest_distance({"from": villages, "to": villages}, {})
    assert list(result["distance"]) == pytest.approx([1.0, 1.0, 4.0])


def test_fa08_nearest_distance_excludes_duplicate_geometry_across_layers():
    """Selbstausschluss basiert auf Geometrie-Gleichheit, nicht auf
    Layer-Identität - ein Duplikat in einem separaten to-Layer wird
    ebenfalls ausgeschlossen."""
    from_gdf = gpd.GeoDataFrame({"id": ["a"]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    to_gdf = gpd.GeoDataFrame(
        {"id": ["dup", "b"]}, geometry=[Point(0, 0), Point(3, 0)], crs="EPSG:4326"
    )
    result = run_nearest_distance({"from": from_gdf, "to": to_gdf}, {})
    assert result["distance"].iloc[0] == pytest.approx(3.0)


def test_fa08_nearest_distance_single_feature_no_other_target_yields_nan():
    """Einzelnes Objekt in from/to identisch, keine Alternative vorhanden
    -> kein sinnvoller 'nächster ANDERER' Punkt, also NaN statt 0."""
    solo = gpd.GeoDataFrame({"id": ["a"]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    result = run_nearest_distance({"from": solo, "to": solo}, {})
    assert result["distance"].isna().all()


def test_fa08_nearest_distance_tie_preserves_row_count_and_order():
    """sjoin_nearest liefert bei Gleichstand (mehrere gleich weit entfernte
    Treffer) mehrere Zeilen pro from-Objekt - die Nachbedingung 'ein
    Ergebnis pro Eingabeobjekt, gleiche Reihenfolge wie from' muss trotzdem
    gelten (sonst würden nachfolgende Schritte durch zusaetzliche/
    verschobene Zeilen falsch rechnen)."""
    villages = gpd.GeoDataFrame(
        {"id": ["a", "b", "c"]},
        geometry=[Point(0, 0), Point(1, 0), Point(-1, 0)],  # b und c gleich weit von a
        crs="EPSG:4326",
    )
    result = run_nearest_distance({"from": villages, "to": villages}, {})
    assert list(result["id"]) == ["a", "b", "c"]
    assert len(result) == len(villages)
    assert list(result["distance"]) == pytest.approx([1.0, 1.0, 1.0])


# =====================================================================
# spatial_join
# =====================================================================


def test_fa08_spatial_join_within():
    left = gpd.GeoDataFrame({"id": ["p1"]}, geometry=[Point(0.5, 0.5)], crs="EPSG:4326")
    right = gpd.GeoDataFrame(
        {"zone": ["z1"]},
        geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])],
        crs="EPSG:4326",
    )
    result = run_spatial_join({"left": left, "right": right}, {"predicate": "within"})
    assert result["zone"].iloc[0] == "z1"


def test_fa08_spatial_join_no_match_yields_nan_attribute():
    left = gpd.GeoDataFrame({"id": ["p1"]}, geometry=[Point(10, 10)], crs="EPSG:4326")
    right = gpd.GeoDataFrame(
        {"zone": ["z1"]},
        geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])],
        crs="EPSG:4326",
    )
    result = run_spatial_join({"left": left, "right": right}, {"predicate": "within"})
    assert result["zone"].isna().iloc[0]


def test_fa08_spatial_join_empty_left():
    right = gpd.GeoDataFrame(
        {"zone": ["z1"]},
        geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])],
        crs="EPSG:4326",
    )
    result = run_spatial_join(
        {"left": empty_points(), "right": right}, {"predicate": "within"}
    )
    assert len(result) == 0


# =====================================================================
# overlay
# =====================================================================


def test_fa08_overlay_intersection():
    left = gpd.GeoDataFrame(
        {"id": ["a"]},
        geometry=[Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])],
        crs="EPSG:4326",
    )
    right = gpd.GeoDataFrame(
        {"id": ["b"]},
        geometry=[Polygon([(1, 1), (3, 1), (3, 3), (1, 3)])],
        crs="EPSG:4326",
    )
    result = run_overlay({"left": left, "right": right}, {"method": "intersection"})
    assert len(result) == 1
    assert result.geometry.iloc[0].area == pytest.approx(1.0)


def test_fa08_overlay_unknown_method_raises_at_schema_level():
    from pydantic import ValidationError

    from geofact.builtin.operations.overlay import OverlayStep

    with pytest.raises(ValidationError, match="method"):
        OverlayStep(
            id="ov", inputs={"left": "a", "right": "b"}, params={"method": "xor"}
        )


def test_fa08_overlay_empty_right():
    left = gpd.GeoDataFrame(
        {"id": ["a"]},
        geometry=[Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])],
        crs="EPSG:4326",
    )
    right = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:4326")
    result = run_overlay({"left": left, "right": right}, {"method": "intersection"})
    assert len(result) == 0


def test_fa08_overlay_mixed_left_layer_homogenized_not_crash():
    # Realer Bug (Szenario "Supermarkt-Wüste"): der linke Layer
    # (chemnitz_stadtteile aus OSM) mischt Flächen-Relationen mit
    # vereinzelten Punkt-/Linien-Objekten. GeoPandas' overlay() brach
    # zuvor mit "df1 contains mixed geometry types" ab. Jetzt wird der
    # Layer auf seine dominante Dimension (Fläche) reduziert.
    left = gpd.GeoDataFrame(
        {"id": ["district", "stray_point", "stray_line"]},
        geometry=[
            Polygon([(0, 0), (4, 0), (4, 4), (0, 4)]),
            Point(1, 1),
            LineString([(0, 0), (1, 1)]),
        ],
        crs="EPSG:32633",
    )
    right = gpd.GeoDataFrame(
        {"id": ["buf"]},
        geometry=[Polygon([(2, 2), (3, 2), (3, 3), (2, 3)])],
        crs="EPSG:32633",
    )
    with pytest.warns(UserWarning, match="gemischte Geometrietypen"):
        result = run_overlay({"left": left, "right": right}, {"method": "difference"})
    # nur das Flächen-Objekt überlebt die Homogenisierung
    assert set(result.geometry.geom_type) <= {"Polygon", "MultiPolygon"}
    assert len(result) == 1


def test_fa08_overlay_homogeneous_left_no_warning():
    left = gpd.GeoDataFrame(
        {"id": ["a"]},
        geometry=[Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])],
        crs="EPSG:32633",
    )
    right = gpd.GeoDataFrame(
        {"id": ["b"]},
        geometry=[Polygon([(1, 1), (3, 1), (3, 3), (1, 3)])],
        crs="EPSG:32633",
    )
    with warnings.catch_warnings():
        warnings.simplefilter(
            "error"
        )  # jede Warnung würde den Test fehlschlagen lassen
        run_overlay({"left": left, "right": right}, {"method": "intersection"})


def test_fa08_spatial_join_homogenizes_mixed_layer():
    left = gpd.GeoDataFrame(
        {"id": ["zone", "noise"]},
        geometry=[Polygon([(0, 0), (4, 0), (4, 4), (0, 4)]), Point(1, 1)],
        crs="EPSG:32633",
    )
    right = gpd.GeoDataFrame({"tag": ["x"]}, geometry=[Point(2, 2)], crs="EPSG:32633")
    with pytest.warns(UserWarning, match="gemischte Geometrietypen"):
        result = run_spatial_join(
            {"left": left, "right": right}, {"predicate": "contains"}
        )
    assert len(result) == 1
    assert result["id"].iloc[0] == "zone"


# =====================================================================
# clip
# =====================================================================


def test_fa08_clip_happy_path():
    features = gpd.GeoDataFrame(
        {"id": ["a", "b"]}, geometry=[Point(0.5, 0.5), Point(5, 5)], crs="EPSG:4326"
    )
    mask = gpd.GeoDataFrame(
        {"id": ["m"]},
        geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])],
        crs="EPSG:4326",
    )
    result = run_clip({"features": features, "mask": mask}, {})
    assert len(result) == 1
    assert result["id"].iloc[0] == "a"


def test_fa08_clip_empty_features():
    mask = gpd.GeoDataFrame(
        {"id": ["m"]},
        geometry=[Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])],
        crs="EPSG:4326",
    )
    result = run_clip({"features": empty_points(), "mask": mask}, {})
    assert len(result) == 0


# =====================================================================
# dissolve
# =====================================================================


def test_fa08_dissolve_overlapping_polygons_merge_into_one():
    features = gpd.GeoDataFrame(
        {"id": ["a", "b"]},
        geometry=[
            Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
            Polygon([(1, 1), (3, 1), (3, 3), (1, 3)]),
        ],
        crs="EPSG:32633",
    )
    result = run_dissolve({"features": features}, {})
    assert len(result) == 1
    # Fläche der Vereinigung < Summe der Einzelflächen (Überlappung entfernt)
    assert result.geometry.iloc[0].area == pytest.approx(7.0)


def test_fa08_dissolve_disjoint_polygons_stay_separate_parts():
    features = gpd.GeoDataFrame(
        {"id": ["a", "b"]},
        geometry=[
            Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
            Polygon([(10, 10), (11, 10), (11, 11), (10, 11)]),
        ],
        crs="EPSG:32633",
    )
    result = run_dissolve({"features": features}, {})
    assert len(result) == 2
    assert result.geometry.area.sum() == pytest.approx(2.0)


def test_fa08_dissolve_by_group():
    features = gpd.GeoDataFrame(
        {"id": ["a", "b", "c"], "group": ["x", "x", "y"]},
        geometry=[
            Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
            Polygon([(1, 1), (3, 1), (3, 3), (1, 3)]),
            Polygon([(10, 10), (11, 10), (11, 11), (10, 11)]),
        ],
        crs="EPSG:32633",
    )
    result = run_dissolve({"features": features}, {"by": "group"})
    assert len(result) == 2
    assert set(result["group"]) == {"x", "y"}


def test_fa08_dissolve_empty_layer():
    result = run_dissolve({"features": empty_points()}, {})
    assert len(result) == 0


def test_fa08_dissolve_mixed_geometry_types_keeps_all_parts():
    # Realer Bug: OSM-Parks sind meist Polygone, aber vereinzelt als
    # Punkt gemappt (z. B. "Albertpark" in Dresden). union_all() über
    # gemischte Typen erzeugt eine GeometryCollection - ohne Zerlegung
    # ging der Punkt-Park in einer nicht weiterverarbeitbaren einzigen
    # Zeile verloren, wodurch sein Einzugsgebiet fehlte.
    features = gpd.GeoDataFrame(
        {"id": ["polygon_park", "point_park"]},
        geometry=[
            Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
            Point(100, 100),
        ],
        crs="EPSG:32633",
    )
    result = run_dissolve({"features": features}, {})
    geom_types = set(result.geometry.geom_type)
    assert geom_types == {"Polygon", "Point"}
    assert len(result) == 2

    # jede Teilgeometrie muss einzeln bufferbar bleiben
    buffered = run_buffer({"geometry": result}, {"radius_km": 0.3})
    assert (buffered.geometry.area > 0).all()


# =====================================================================
# to_points (Geometrie-Normalisierung)
# =====================================================================


def test_fa08_to_points_mixed_layer_becomes_homogeneous_points():
    # Realer Bug (Szenario "Supermarkt-Wüste"): shop=supermarket kommt
    # aus OSM mal als Node (Point), mal als Gebäudefläche (Polygon).
    # to_points normalisiert das, damit overlay/spatial_join nicht mit
    # "mixed geometry types" abbricht.
    features = gpd.GeoDataFrame(
        {"name": ["node_shop", "building_shop"]},
        geometry=[
            Point(10, 10),
            Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]),
        ],
        crs="EPSG:32633",
    )
    result = normalize.run_to_points({"features": features}, {})
    assert set(result.geometry.geom_type) == {"Point"}
    assert len(result) == len(features)
    assert list(result["name"]) == ["node_shop", "building_shop"]
    # Punkt bleibt unverändert; Flächen-Ersatzpunkt liegt in der Fläche
    assert result.geometry.iloc[0].equals(Point(10, 10))
    assert Polygon([(0, 0), (2, 0), (2, 2), (0, 2)]).contains(result.geometry.iloc[1])


def test_fa08_to_points_keeps_all_objects_as_points_for_overlay():
    # to_points erhält JEDES Objekt (Punkt je Objekt), im Gegensatz zur
    # Homogenisierung in overlay/sjoin, die die nicht-dominante Dimension
    # verwirft. Für punktbasierte Verschnitte ist das der richtige Weg.
    shops = gpd.GeoDataFrame(
        {"id": ["a", "b"]},
        geometry=[Point(1, 1), Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])],
        crs="EPSG:32633",
    )
    zone = gpd.GeoDataFrame(
        {"zone": ["z"]},
        geometry=[Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])],
        crs="EPSG:32633",
    )
    points = normalize.run_to_points({"features": shops}, {})
    joined = run_overlay({"left": points, "right": zone}, {"method": "intersection"})
    assert len(joined) == 2


def test_fa08_to_points_idempotent_on_point_layer():
    features = gpd.GeoDataFrame({"id": ["p"]}, geometry=[Point(3, 4)], crs="EPSG:32633")
    result = normalize.run_to_points({"features": features}, {})
    assert set(result.geometry.geom_type) == {"Point"}
    assert result.geometry.iloc[0].equals(Point(3, 4))


def test_fa08_to_points_empty_layer():
    result = normalize.run_to_points({"features": empty_points()}, {})
    assert len(result) == 0


# =====================================================================
# zonal_stats
# =====================================================================


def population_raster() -> RasterLayer:
    return RasterLayer(
        data=np.full((10, 10), 100, dtype="int32"),
        transform=Affine(0.008, 0, 13.70, 0, -0.004, 51.07),
        crs=CRS.from_epsg(4326),
    )


def test_fa08_zonal_stats_sum_known_raster():
    zone = gpd.GeoDataFrame(
        {"id": ["z1"]},
        geometry=[
            Polygon([(13.70, 51.03), (13.78, 51.03), (13.78, 51.07), (13.70, 51.07)])
        ],
        crs="EPSG:4326",
    )
    result = run_zonal_stats(
        {"zones": zone, "values": population_raster()}, {"stat": "sum"}
    )
    assert result["sum_value"].iloc[0] == pytest.approx(10000)


def test_fa08_zonal_stats_unknown_stat_raises():
    zone = gpd.GeoDataFrame(
        {"id": ["z1"]},
        geometry=[
            Polygon([(13.70, 51.03), (13.78, 51.03), (13.78, 51.07), (13.70, 51.07)])
        ],
        crs="EPSG:4326",
    )
    with pytest.raises(KeyError):
        run_zonal_stats(
            {"zones": zone, "values": population_raster()}, {"stat": "median"}
        )


def test_fa08_zonal_stats_empty_zones():
    result = run_zonal_stats(
        {"zones": empty_points(), "values": population_raster()}, {"stat": "sum"}
    )
    assert len(result) == 0


def varying_raster() -> RasterLayer:
    """Rasterwerte, die absichtlich keine runde Mean/Sum ergeben (FA8:
    decimals-Rundungs-Contract)."""
    data = np.arange(100, dtype="float64").reshape(10, 10) + 1  # 1..100, Mean=50.5
    return RasterLayer(
        data=data,
        transform=Affine(0.008, 0, 13.70, 0, -0.004, 51.07),
        crs=CRS.from_epsg(4326),
    )


def test_fa08_zonal_stats_default_decimals_rounds_to_whole_number():
    zone = gpd.GeoDataFrame(
        {"id": ["z1"]},
        geometry=[
            Polygon([(13.70, 51.03), (13.78, 51.03), (13.78, 51.07), (13.70, 51.07)])
        ],
        crs="EPSG:4326",
    )
    result = run_zonal_stats(
        {"zones": zone, "values": varying_raster()}, {"stat": "mean"}
    )
    value = result["mean_value"].iloc[0]
    assert value == float(round(value))


def test_fa08_zonal_stats_decimals_param_overrides_default():
    zone = gpd.GeoDataFrame(
        {"id": ["z1"]},
        geometry=[
            Polygon([(13.70, 51.03), (13.78, 51.03), (13.78, 51.07), (13.70, 51.07)])
        ],
        crs="EPSG:4326",
    )
    result = run_zonal_stats(
        {"zones": zone, "values": varying_raster()}, {"stat": "mean", "decimals": 2}
    )
    value = result["mean_value"].iloc[0]
    assert round(value, 2) == pytest.approx(value)
    assert value != float(round(value))  # tatsächlich mehr Präzision als decimals=0


def nodata_raster() -> RasterLayer:
    """Wie population_raster(), aber die rechte Hälfte (Spalten 5-9) ist
    mit dem Nodata-Sentinel -200 belegt (analog tests/fixtures/
    population_nodata.tif) - für den Nodata-Ausschluss-Test."""
    data = np.full((10, 10), 100.0)
    data[:, 5:] = -200.0
    return RasterLayer(
        data=data,
        transform=Affine(0.008, 0, 13.70, 0, -0.004, 51.07),
        crs=CRS.from_epsg(4326),
        nodata=-200.0,
    )


def test_fa08_zonal_stats_excludes_nodata_cells_from_sum():
    """Regression/Bugfix-Test: eine Zone, die Nodata-Sentinelzellen
    überdeckt, darf diese nicht in die Summe einrechnen - sonst
    verfälscht der Sentinelwert (-200) das Ergebnis (stiller Fehler).
    Bekannte gültige Summe: linke Hälfte (5x10 Zellen a 100) = 5000."""
    zone = gpd.GeoDataFrame(
        {"id": ["z1"]},
        geometry=[
            Polygon([(13.70, 51.03), (13.78, 51.03), (13.78, 51.07), (13.70, 51.07)])
        ],
        crs="EPSG:4326",
    )
    result = run_zonal_stats(
        {"zones": zone, "values": nodata_raster()}, {"stat": "sum"}
    )
    assert result["sum_value"].iloc[0] == pytest.approx(5000)


def test_fa08_zonal_stats_excludes_nodata_cells_from_mean():
    """Ohne Ausschluss würde der Mittelwert stark von -200 nach unten
    verzerrt (Mittel über 100er- und -200er-Zellen); mit Ausschluss bleibt
    er exakt beim echten Messwert 100."""
    zone = gpd.GeoDataFrame(
        {"id": ["z1"]},
        geometry=[
            Polygon([(13.70, 51.03), (13.78, 51.03), (13.78, 51.07), (13.70, 51.07)])
        ],
        crs="EPSG:4326",
    )
    result = run_zonal_stats(
        {"zones": zone, "values": nodata_raster()}, {"stat": "mean"}
    )
    assert result["mean_value"].iloc[0] == pytest.approx(100)


def test_fa08_zonal_stats_zone_covering_only_nodata_yields_nan():
    """Eine Zone, die ausschließlich Nodata-Zellen überdeckt, hat keine
    gültigen Pixel - analog zum bestehenden leere-Zone-NaN-Fall."""
    zone = gpd.GeoDataFrame(
        {"id": ["z1"]},
        geometry=[
            Polygon([(13.74, 51.03), (13.78, 51.03), (13.78, 51.07), (13.74, 51.07)])
        ],
        crs="EPSG:4326",
    )
    result = run_zonal_stats(
        {"zones": zone, "values": nodata_raster()}, {"stat": "sum"}
    )
    assert np.isnan(result["sum_value"].iloc[0])


# =====================================================================
# classify
# =====================================================================


def test_fa08_classify_breaks_and_labels():
    gdf = gpd.GeoDataFrame(
        {"distance": [100, 400, 800, 2000]},
        geometry=[Point(0, 0)] * 4,
        crs="EPSG:4326",
    )
    result = run_classify(
        {"features": gdf},
        {
            "field": "distance",
            "breaks": [300, 500, 1000],
            "labels": ["sehr gut", "gut", "mäßig", "schlecht"],
        },
    )
    assert list(result["class"]) == ["sehr gut", "gut", "mäßig", "schlecht"]


def test_fa08_classify_missing_field_raises():
    gdf = gpd.GeoDataFrame({"other": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    with pytest.raises(KeyError):
        run_classify(
            {"features": gdf},
            {"field": "distance", "breaks": [1], "labels": ["a", "b"]},
        )


def test_fa08_classify_empty_layer():
    result = run_classify(
        {"features": empty_points()},
        {"field": "id", "breaks": [1], "labels": ["a", "b"]},
    )
    assert len(result) == 0


# =====================================================================
# filter
# =====================================================================


def test_fa08_filter_on_missing_exclude():
    gdf = substations()  # s5 hat kein voltage
    result = run_filter(
        {"features": gdf}, {"condition": "voltage > 1000", "on_missing": "exclude"}
    )
    assert "s5" not in result["id"].values


def test_fa08_filter_on_missing_include():
    gdf = substations()
    result = run_filter(
        {"features": gdf}, {"condition": "voltage > 1000000", "on_missing": "include"}
    )
    assert (
        "s5" in result["id"].values
    )  # fällt durch, obwohl condition sonst nichts matcht


def test_fa08_filter_on_missing_error():
    gdf = substations()
    with pytest.raises(ValueError, match="on_missing=error"):
        run_filter(
            {"features": gdf}, {"condition": "voltage > 1000", "on_missing": "error"}
        )


def test_fa08_filter_empty_layer():
    result = run_filter({"features": empty_points()}, {"condition": "id == 'x'"})
    assert len(result) == 0


def test_fa08_filter_scalar_condition_true():
    # condition referenziert keine Spalte (z. B. "1 == 1") -> eval() liefert
    # einen einzelnen bool statt einer Maske je Zeile.
    gdf = substations()
    result = run_filter({"features": gdf}, {"condition": "1 == 1"})
    assert len(result) == len(gdf)


def test_fa08_filter_scalar_condition_false():
    gdf = substations()
    result = run_filter({"features": gdf}, {"condition": "1 == 2"})
    assert len(result) == 0


# =====================================================================
# ranking
# =====================================================================


def test_fa08_ranking_weighted_score():
    gdf = gpd.GeoDataFrame(
        {"a": [0, 10], "b": [10, 0]},
        geometry=[Point(0, 0), Point(1, 1)],
        crs="EPSG:4326",
    )
    result = run_ranking(
        {"features": gdf},
        {"by": ["a", "b"], "weights": [0.5, 0.5], "output_field": "score"},
    )
    assert result["score"].iloc[0] == pytest.approx(0.5)
    assert result["score"].iloc[1] == pytest.approx(0.5)


def test_fa08_ranking_constant_field_yields_zero_contribution():
    gdf = gpd.GeoDataFrame(
        {"a": [5, 5]}, geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:4326"
    )
    result = run_ranking(
        {"features": gdf}, {"by": ["a"], "weights": [1.0], "output_field": "score"}
    )
    assert (result["score"] == 0).all()


def test_fa08_ranking_empty_layer():
    gdf = gpd.GeoDataFrame({"a": []}, geometry=[], crs="EPSG:4326")
    result = run_ranking(
        {"features": gdf}, {"by": ["a"], "weights": [1.0], "output_field": "score"}
    )
    assert len(result) == 0
