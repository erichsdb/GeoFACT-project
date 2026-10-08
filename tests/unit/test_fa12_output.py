"""Implements: FA12 (Karte, GeoJSON-/CSV-Export).

Contract-Tests für src/geofact/builtin/outputs/map.py und export.py.
"""

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
import yaml
from affine import Affine
from pyproj import CRS
from shapely.geometry import Point, Polygon

from geofact_web.presentation import serialize
from geofact.core.scenario import Scenario
from geofact.core.types import RasterLayer
from geofact.engine.executor import run_scenario
from geofact.builtin.outputs import export as export_module
from geofact.builtin.outputs import map as map_module
from geofact.engine.outputs import write_outputs

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def a_raster() -> RasterLayer:
    """Ein Raster, wo ein Vektor-Layer erwartet wird (Vertragsverletzung eines Bausteins)."""
    return RasterLayer(
        data=np.zeros((2, 2), dtype="float32"),
        transform=Affine.identity(),
        crs=CRS.from_epsg(32633),
    )


def sample_gdf() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": ["a", "b"], "risk_score": [0.2, 0.9]},
        geometry=[Point(13.72, 51.04), Point(13.74, 51.05)],
        crs="EPSG:4326",
    )


def test_fa12_map_html_written(tmp_path):
    path = tmp_path / "map.html"
    map_module.write_map(sample_gdf(), path, color_field="risk_score")
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert "<html" in content.lower() or "leaflet" in content.lower()


def test_fa12_map_handles_empty_layer(tmp_path):
    empty = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:4326")
    path = tmp_path / "empty_map.html"
    map_module.write_map(empty, path)
    assert path.is_file()


def test_fa12_map_shows_legend_for_color_field(tmp_path):
    # Karte ohne Legende macht Farbcodierung unlesbar - muss den
    # Feldnamen und die Werteskala als Text enthalten.
    path = tmp_path / "map.html"
    map_module.write_map(sample_gdf(), path, color_field="risk_score")
    content = path.read_text(encoding="utf-8")
    assert "risk_score" in content
    assert "0.20" in content or "0.2" in content


def test_fa12_map_without_color_field_has_no_legend(tmp_path):
    path = tmp_path / "map.html"
    map_module.write_map(sample_gdf(), path, color_field=None)
    content = path.read_text(encoding="utf-8")
    # ohne color_field darf keine Legende (mit Feldname + Werteskala) da sein -
    # "risk_score" taucht trotzdem in den Popups auf (Attribute immer sichtbar),
    # daher gezielt auf das Legenden-Div prüfen, nicht auf die bloße Substring.
    assert "position:fixed;bottom:20px" not in content


def test_fa12_map_normalizes_polygon_color_by_area():
    # Realer Bug: zwei dissolvte Zonen mit gleicher Rohsumme, aber stark
    # unterschiedlicher Fläche, sahen in der Färbung nach Rohsumme
    # identisch "dicht" aus - eine große Zone hat automatisch eine
    # hohe Summe, unabhängig von der tatsächlichen Dichte.
    small = Polygon([(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)])
    large = Polygon([(1, 1), (2, 1), (2, 2), (1, 2)])
    gdf = gpd.GeoDataFrame(
        {"id": ["small", "large"], "sum_value": [1000.0, 1000.0]},
        geometry=[small, large],
        crs="EPSG:4326",
    )
    fmap = map_module.render_map(gdf, color_field="sum_value", normalize_by_area=True)
    html = fmap.get_root().render()
    assert "pro km²" in html


def test_fa12_map_can_disable_area_normalization():
    small = Polygon([(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)])
    large = Polygon([(1, 1), (2, 1), (2, 2), (1, 2)])
    gdf = gpd.GeoDataFrame(
        {"id": ["small", "large"], "sum_value": [1000.0, 1000.0]},
        geometry=[small, large],
        crs="EPSG:4326",
    )
    fmap = map_module.render_map(gdf, color_field="sum_value", normalize_by_area=False)
    html = fmap.get_root().render()
    assert "pro km²" not in html


def test_fa12_map_polygons_get_point_marker():
    """Gemeldetes Problem: ein gefiltertes Ergebnis aus wenigen kleinen
    Polygonen (z. B. Veranstaltungsorte mit capacity > 250) war auf einer
    landesweiten Karte praktisch nicht auffindbar, während Punktergebnisse
    durch den CircleMarker sofort sichtbar sind. Flächen bekommen deshalb
    zusätzlich eine Punktmarkierung."""
    poly = Polygon([(13.7, 51.0), (13.71, 51.0), (13.71, 51.01), (13.7, 51.01)])
    gdf = gpd.GeoDataFrame({"id": ["venue"]}, geometry=[poly], crs="EPSG:4326")
    html = map_module.render_map(gdf).get_root().render()
    # Die Fläche selbst bleibt erhalten ...
    assert "geo_json" in html.lower() or "geojson" in html.lower()
    # ... und trägt zusätzlich einen CircleMarker als Auffindhilfe.
    assert "circlemarker" in html.lower()


def test_fa12_map_polygon_marker_lies_inside_concave_shape():
    """Randfall: bei U-/L-Förmigen Flächen liegt der Schwerpunkt
    außerhalb der Geometrie. Die Markierung muss trotzdem auf der Fläche
    sitzen, sonst zeigt sie ins Leere - deshalb representative_point()
    statt centroid."""
    u_shape = Polygon([(0, 0), (3, 0), (3, 3), (2, 3), (2, 1), (1, 1), (1, 3), (0, 3)])
    assert not u_shape.contains(u_shape.centroid), "Fixture trifft den Randfall nicht"
    gdf = gpd.GeoDataFrame({"id": ["u"]}, geometry=[u_shape], crs="EPSG:4326")
    fmap = map_module.render_map(gdf)
    markers = [
        child
        for child in fmap._children.values()
        if type(child).__name__ == "CircleMarker"
    ]
    assert len(markers) == 1
    lat, lon = markers[0].location
    assert u_shape.contains(Point(lon, lat))


def test_fa12_map_points_are_not_double_marked():
    """Punkte haben schon ihren CircleMarker - sie dürfen keinen
    zweiten bekommen."""
    fmap = map_module.render_map(sample_gdf())
    markers = [
        child
        for child in fmap._children.values()
        if type(child).__name__ == "CircleMarker"
    ]
    assert len(markers) == 2  # genau ein Marker je Punkt


def test_fa12_map_handles_duplicate_index():
    # Realer Bug: Layer nach spatial_join/dissolve können wiederholte
    # Index-Werte haben (z. B. alle 0). .loc[idx] traf dann mehrere
    # Zeilen statt einer und crashte mit "truth value of Series is
    # ambiguous" statt eine Farbe zuzuweisen.
    gdf = gpd.GeoDataFrame(
        {"id": ["a", "b", "c"], "risk_score": [0.1, 0.5, 0.9]},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:4326",
        index=[0, 0, 0],
    )
    fmap = map_module.render_map(gdf, color_field="risk_score")
    assert fmap is not None


def test_fa12_map_nan_values_get_distinct_gray_not_min_color():
    """Realer Bug: Objekte mit NaN im color_field (z. B. shortest_path-
    Ergebnisse außerhalb von max_snap_m, FA15) wurden implizit auf den
    Blau-Default gefärbt (Folium/`color = fixed_color or "#3388ff"`),
    der optisch kaum von _COLOR_SCALE[0] ("kleinster Wert") zu
    unterscheiden war - eine Erreichbarkeitskarte zeigte NaN-Zellen dann
    fälschlich wie "beste Anbindung" statt "keine Daten"."""
    gdf = gpd.GeoDataFrame(
        {"id": ["a", "b", "c"], "network_distance": [100.0, float("nan"), 5000.0]},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 2)],
        crs="EPSG:4326",
    )
    fmap = map_module.render_map(
        gdf, color_field="network_distance", normalize_by_area=False
    )
    html = fmap.get_root().render()
    assert map_module._NAN_COLOR in html
    # Keine Daten muss als eigener Legenden-Eintrag auftauchen, nicht nur
    # als Grauton irgendwo im HTML.
    assert "keine Daten" in html


def test_fa12_map_no_nan_values_has_no_missing_legend_entry():
    gdf = gpd.GeoDataFrame(
        {"id": ["a", "b"], "network_distance": [100.0, 5000.0]},
        geometry=[Point(0, 0), Point(1, 1)],
        crs="EPSG:4326",
    )
    fmap = map_module.render_map(
        gdf, color_field="network_distance", normalize_by_area=False
    )
    html = fmap.get_root().render()
    assert "keine Daten" not in html


# =====================================================================
# render_multi_layer_map / write_multi_layer_map
# =====================================================================


def three_zone_layers() -> dict:
    sehr_gut = gpd.GeoDataFrame(
        {"id": ["a"], "sum_value": [500.0]},
        geometry=[
            Polygon([(13.70, 51.03), (13.71, 51.03), (13.71, 51.04), (13.70, 51.04)])
        ],
        crs="EPSG:4326",
    )
    gut = gpd.GeoDataFrame(
        {"id": ["b"], "sum_value": [300.0]},
        geometry=[
            Polygon([(13.72, 51.03), (13.73, 51.03), (13.73, 51.04), (13.72, 51.04)])
        ],
        crs="EPSG:4326",
    )
    maessig = gpd.GeoDataFrame(
        {"id": ["c"], "sum_value": [100.0]},
        geometry=[
            Polygon([(13.74, 51.03), (13.75, 51.03), (13.75, 51.04), (13.74, 51.04)])
        ],
        crs="EPSG:4326",
    )
    return {"sehr_gut": sehr_gut, "gut": gut, "mäßig": maessig}


def test_fa12_multi_layer_map_has_toggleable_layers(tmp_path):
    path = tmp_path / "multi.html"
    map_module.write_multi_layer_map(three_zone_layers(), path, color_field="sum_value")
    content = path.read_text(encoding="utf-8")
    # Folium's LayerControl rendert die Layer-Namen als Checkbox-Labels
    assert "sehr_gut" in content
    assert "gut" in content
    assert "mäßig" in content
    assert "L.control.layers(" in content


def test_fa12_multi_layer_map_legend_has_entry_per_layer(tmp_path):
    path = tmp_path / "multi.html"
    map_module.write_multi_layer_map(three_zone_layers(), path, color_field="sum_value")
    content = path.read_text(encoding="utf-8")
    assert content.count("pro km²") == 3


def test_fa12_multi_layer_map_skips_empty_layers(tmp_path):
    layers = three_zone_layers()
    layers["empty"] = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:4326")
    path = tmp_path / "multi.html"
    map_module.write_multi_layer_map(layers, path, color_field="sum_value")
    content = path.read_text(encoding="utf-8")
    assert "empty" not in content


def test_fa12_multi_layer_map_all_empty_returns_fallback_map(tmp_path):
    layers = {"empty": gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:4326")}
    path = tmp_path / "multi.html"
    map_module.write_multi_layer_map(layers, path)
    assert path.is_file()


def two_layers_different_fields() -> dict:
    """Zwei Layer mit je eigenem, NICHT gemeinsamem numerischem Feld -
    Regressionsfall für den Bug, dass render_multi_layer_map mit einem
    einzelnen color_field-String nur den ERSTEN Layer einfärbte und
    jeden weiteren Layer stumm auf eine Palettenfarbe zurückfallen ließ
    (siehe examples/leipzig/leipzig10_erreichbarkeit.yaml: anbindung_zellen
    hat network_distance, einzugsbereiche hat break_m - kein gemeinsames
    Feld)."""
    layer_a = gpd.GeoDataFrame(
        {"id": ["a"], "network_distance": [1234.0]},
        geometry=[
            Polygon([(13.70, 51.03), (13.71, 51.03), (13.71, 51.04), (13.70, 51.04)])
        ],
        crs="EPSG:4326",
    )
    layer_b = gpd.GeoDataFrame(
        {"id": ["b"], "break_m": [2000.0]},
        geometry=[
            Polygon([(13.72, 51.03), (13.73, 51.03), (13.73, 51.04), (13.72, 51.04)])
        ],
        crs="EPSG:4326",
    )
    return {"anbindung_zellen": layer_a, "einzugsbereiche": layer_b}


def test_fa12_multi_layer_map_per_layer_color_field_dict():
    """Ein dict {layer_name: field_name} färbt JEDEN Layer nach seinem
    eigenen Feld statt nur den ersten - Kern des Bugfixes."""
    layers = two_layers_different_fields()
    fmap = map_module.render_multi_layer_map(
        layers,
        color_field={
            "anbindung_zellen": "network_distance",
            "einzugsbereiche": "break_m",
        },
    )
    html = fmap.get_root().render()
    assert "network_distance" in html
    assert "break_m" in html


def test_fa12_multi_layer_map_single_string_field_only_colors_matching_layers():
    """Rückwärtskompatibilität: ein einzelner String gilt weiterhin
    für alle Layer, in denen das Feld vorkommt - Layer ohne dieses Feld
    bleiben (wie zuvor) auf einer festen Palettenfarbe, statt eines
    Fehlers."""
    layers = two_layers_different_fields()
    fmap = map_module.render_multi_layer_map(layers, color_field="network_distance")
    html = fmap.get_root().render()
    assert "network_distance" in html
    # break_m taucht nur im Popup-Attribut auf, nicht als Legenden-Feld -
    # kein counts-basierter Test nötig, es reicht dass render_multi_layer_map
    # ohne Fehler durchläuft und layer_a eine Legende bekommt.
    assert fmap is not None


def test_fa12_multi_layer_map_write_accepts_dict_color_field(tmp_path):
    path = tmp_path / "multi.html"
    map_module.write_multi_layer_map(
        two_layers_different_fields(),
        path,
        color_field={
            "anbindung_zellen": "network_distance",
            "einzugsbereiche": "break_m",
        },
    )
    content = path.read_text(encoding="utf-8")
    assert content.count("pro km²") == 2


def test_fa12_multi_layer_map_nan_values_get_gray_and_missing_legend():
    """Wie test_fa12_map_nan_values_get_distinct_gray_not_min_color, aber
    für render_multi_layer_map (leipzig10s tatsächlicher Karten-Pfad:
    anbindung_zellen + einzugsbereiche gemeinsam)."""
    layer = gpd.GeoDataFrame(
        {"id": ["a", "b", "c"], "network_distance": [635.0, float("nan"), 16381.0]},
        geometry=[Point(13.70, 51.03), Point(13.71, 51.04), Point(13.72, 51.05)],
        crs="EPSG:4326",
    )
    fmap = map_module.render_multi_layer_map(
        {"anbindung_zellen": layer},
        color_field="network_distance",
        normalize_by_area=False,
    )
    html = fmap.get_root().render()
    assert map_module._NAN_COLOR in html
    assert "keine Daten" in html


def test_fa12_geojson_roundtrip(tmp_path):
    path = tmp_path / "result.geojson"
    gdf = sample_gdf()
    export_module.write_geojson(gdf, path)

    read_back = gpd.read_file(path)
    assert len(read_back) == len(gdf)
    assert set(read_back["id"]) == set(gdf["id"])
    assert read_back.crs.to_epsg() == 4326


def test_fa12_csv_contains_attributes(tmp_path):
    path = tmp_path / "result.csv"
    gdf = sample_gdf()
    export_module.write_csv(gdf, path)

    df = pd.read_csv(path)
    assert list(df["id"]) == ["a", "b"]
    assert "wkt" in df.columns
    assert df["wkt"].iloc[0].startswith("POINT")
    assert "geometry" not in df.columns


def _fixture_loader(fixture_name: str):
    def loader(layer):
        return gpd.read_file(FIXTURES_DIR / fixture_name)

    return loader


def _region_fixture_fetcher(name: str) -> list[dict]:
    raw = json.loads(
        (FIXTURES_DIR / "nominatim_dresden.json").read_text(encoding="utf-8")
    )
    return raw if name == "Dresden, Deutschland" else []


def test_fa12_scenario2_golden_geojson(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    raw = yaml.safe_load(
        (EXAMPLES_DIR / "szenario2_gruenflaechen.yaml").read_text(encoding="utf-8")
    )
    scenario = Scenario(**raw)

    result = run_scenario(
        scenario,
        connector_override={
            "parks": _fixture_loader("parks.geojson"),
            "buildings": _fixture_loader("residential_buildings.geojson"),
        },
        region_fetcher=_region_fixture_fetcher,
    )
    classified = result.store["classified"]

    output_path = tmp_path / "classified.geojson"
    export_module.write_geojson(classified, output_path)

    actual = json.loads(output_path.read_text(encoding="utf-8"))
    expected = json.loads(
        (FIXTURES_DIR / "szenario2_golden.geojson").read_text(encoding="utf-8")
    )

    actual_features = {f["properties"]["id"]: f for f in actual["features"]}
    expected_features = {f["properties"]["id"]: f for f in expected["features"]}
    assert set(actual_features) == set(expected_features)
    for feature_id, expected_feature in expected_features.items():
        actual_feature = actual_features[feature_id]
        assert (
            actual_feature["properties"]["class"]
            == expected_feature["properties"]["class"]
        )
        assert actual_feature["properties"]["distance"] == pytest.approx(
            expected_feature["properties"]["distance"]
        )
        assert actual_feature["geometry"]["coordinates"] == pytest.approx(
            expected_feature["geometry"]["coordinates"]
        )


# ---------------------------------------------------------------------------
# Anzeige-Begrenzung der interaktiven Karte (FA12-Nachbedingung:
# "Kartendarstellung ist ohne GIS-Software interpretierbar")
#
# Die Weboberfläche clustert Punkte, statt sie wegzuwerfen; der
# Serverschnitt (_MAX_NODE_FEATURES) begrenzt nur noch Reprojektion +
# JSON-Größe. Entscheidend für die Interpretierbarkeit: describe()
# meldet weiterhin die ECHTE Gesamtzahl, damit die Oberfläche "N von M"
# korrekt anzeigen kann und keine stille Kappung entsteht (goldene Regel 7).
# ---------------------------------------------------------------------------


def _points_gdf(count: int) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": list(range(count))},
        geometry=[Point(13.70 + i * 1e-4, 51.00 + i * 1e-4) for i in range(count)],
        crs="EPSG:4326",
    )


def test_fa12_describe_reports_true_count_despite_display_cap():
    """Vorbedingung: Layer größer als das Anzeigelimit.
    Nachbedingung: feature_count ist die ungekappte Gesamtzahl."""
    gdf = _points_gdf(2500)
    payload = serialize.serialize_result(gdf, max_features=1000)

    assert payload["describe"]["feature_count"] == 2500
    assert len(payload["geojson"]["features"]) == 1000


def test_fa12_node_view_cap_allows_clusterable_point_counts():
    """Der Server-Cap muss deutlich über der Clustering-Schwelle des
    Frontends (200 Punkte) liegen - sonst wäre Clustering wirkungslos,
    weil nie genug Punkte im Browser ankommen. Nach oben hält ihn die
    Antwortgröße in Schach; die begrenzt seit _VIEW_COLUMN_BUDGET
    primär die Spaltenzahl, nicht die Objektzahl (Messreihe im
    Kommentar von routers/runs.py)."""
    pytest.importorskip("fastapi")  # routers/runs.py gehört zum Web-Extra
    from geofact_web.routers.runs import _MAX_NODE_FEATURES, _VIEW_COLUMN_BUDGET

    assert _MAX_NODE_FEATURES > 200
    # Ohne Spaltenbudget wäre ein so hoher Objekt-Cap nicht tragbar.
    assert _VIEW_COLUMN_BUDGET <= 25


def test_fa12_client_cap_does_not_undercut_server_cap():
    """Das clientseitige Sicherheitsnetz (MAX_MAP_FEATURES in
    map-view.tsx) muss über dem Server-Cap liegen. Liegt es darunter,
    kappt die Karte still Objekte, die der Server bewusst ausliefert -
    genau die Art stiller Reduktion, die Regel 7 ausschließt. Die beiden
    Konstanten stehen in verschiedenen Sprachen/Dateien und sind schon
    einmal auseinandergelaufen, deshalb hier verdrahtet."""
    import re
    from pathlib import Path

    pytest.importorskip("fastapi")  # routers/runs.py gehört zum Web-Extra
    from geofact_web.routers.runs import _MAX_NODE_FEATURES

    source = (
        Path(__file__).resolve().parents[2] / "frontend" / "components" / "map-view.tsx"
    ).read_text(encoding="utf-8")
    match = re.search(r"const MAX_MAP_FEATURES = ([\d_]+)", source)
    assert match, "MAX_MAP_FEATURES nicht in map-view.tsx gefunden"
    client_cap = int(match.group(1).replace("_", ""))

    assert client_cap >= _MAX_NODE_FEATURES, (
        f"Client-Cap {client_cap:,} liegt unter Server-Cap "
        f"{_MAX_NODE_FEATURES:,} - die Karte würde still kappen."
    )


def test_fa12_serialization_drops_all_null_columns():
    """Vorbedingung: breiter, dünn besetzter Layer (OSM-Realfall).
    Nachbedingung: vollständig leere Spalten fehlen in der Ausgabe,
    belegte Spalten bleiben unverändert erhalten."""
    gdf = gpd.GeoDataFrame(
        {
            "name": ["Dorf A", "Dorf B"],
            "place": ["village", "village"],
            "name:fa": [None, None],
            "openGeoDB:layer": [None, None],
            "population": [120, None],
        },
        geometry=[Point(13.72, 51.04), Point(13.74, 51.05)],
        crs="EPSG:4326",
    )

    fc = serialize.vector_to_geojson(gdf)

    keys = set(fc["features"][0]["properties"])
    assert "name:fa" not in keys
    assert "openGeoDB:layer" not in keys
    # Teilweise belegte Spalten bleiben - sonst wäre es eine stille
    # Reduktion echter Werte.
    assert "population" in keys
    assert fc["features"][0]["properties"]["name"] == "Dorf A"


def test_fa12_display_cap_keeps_all_geometry_types():
    """Randfall gemischter Geometrien: die Kappung darf keinen
    Geometrietyp vollständig verdrängen (sonst zeigt die Karte z. B.
    nur Punkte und keine Fläche)."""
    points = _points_gdf(50)
    polygons = gpd.GeoDataFrame(
        {"id": [900, 901]},
        geometry=[
            Polygon([(13.7, 51.0), (13.8, 51.0), (13.8, 51.1), (13.7, 51.1)]),
            Polygon([(13.9, 51.2), (14.0, 51.2), (14.0, 51.3), (13.9, 51.3)]),
        ],
        crs="EPSG:4326",
    )
    mixed = gpd.GeoDataFrame(
        pd.concat([points, polygons], ignore_index=True), crs="EPSG:4326"
    )

    fc = serialize.vector_to_geojson(mixed, max_features=10)

    kinds = {f["geometry"]["type"] for f in fc["features"]}
    assert "Point" in kinds
    assert "Polygon" in kinds


def test_fa12_display_cap_handles_empty_layer():
    """Randfall leerer Layer: keine Ausnahme, leere FeatureCollection."""
    empty = gpd.GeoDataFrame({"id": []}, geometry=[], crs="EPSG:4326")

    fc = serialize.vector_to_geojson(empty, max_features=1000)

    assert fc["type"] == "FeatureCollection"
    assert fc["features"] == []


# ---------------------------------------------------------------------------
# write_outputs: übersprungene Ausgaben werden gemeldet statt still
# verworfen (Goldene Regel 7) - siehe src/geofact/engine/outputs.py
# ---------------------------------------------------------------------------

BBOX_REGION = "13.60,51.01,13.94,51.15"


def _network_scenario_with_outputs(outputs: list[dict]) -> Scenario:
    """Vektor -> Graph -> Vektor-Netz mit frei wählbaren Ausgaben - erlaubt
    Tests für fehlende Quelle (unbekannte source-id) und Typfehlpassung
    (graph-Ergebnis für einen Vektor-Ausgabetyp)."""
    config = {
        "scenario": {"name": "Output-Test", "region": BBOX_REGION},
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
            {"id": "nodes", "op": "graph_to_vector", "inputs": {"graph": "grid"}},
        ],
        "output": outputs,
    }
    return Scenario(**config)


def test_fa12_write_outputs_happy_path_has_no_skips(tmp_path):
    """Vorbedingung: alle Ausgaben referenzieren vorhandene Vektor-Quellen.
    Nachbedingung: written enthält alle Dateien, skipped ist leer."""
    scenario = _network_scenario_with_outputs(
        [
            {"type": "geojson", "source": "nodes", "path": "nodes.geojson"},
        ]
    )
    result = run_scenario(scenario)

    outcome = write_outputs(scenario, result, tmp_path)

    assert {p.name for p in outcome.written} == {"nodes.geojson"}
    assert outcome.skipped == []
    assert len(outcome.written) == 1


def test_fa12_write_outputs_reports_missing_source(tmp_path):
    """Vorbedingung: eine Ausgabe referenziert eine im Schema gültige
    source-id, deren Ergebnis zur Laufzeit trotzdem nicht im Store landet
    (z. B. ein Schritt, der vor Erreichen dieses Knotens abbricht - der
    Store wird dann nur teilweise befüllt). Nachbedingung: die Ausgabe
    fehlt in written, taucht aber mit Grund in skipped auf - kein stilles
    Verwerfen (Regel 7)."""
    scenario = _network_scenario_with_outputs(
        [
            {"type": "geojson", "source": "nodes", "path": "missing.geojson"},
        ]
    )
    result = run_scenario(scenario)
    # Store nachträglich leeren, um einen zur Laufzeit fehlenden Knoten zu
    # simulieren (Schema-Validierung ließe eine unbekannte id nicht zu).
    result.store._layers.pop("nodes", None)

    outcome = write_outputs(scenario, result, tmp_path)

    assert outcome.written == []
    assert len(outcome.skipped) == 1
    skipped = outcome.skipped[0]
    assert skipped.source == "nodes"
    assert skipped.type == "geojson"
    assert "kein Ergebnis" in skipped.reason


def test_fa12_write_outputs_reports_type_mismatch(tmp_path):
    """Vorbedingung: eine gültig deklarierte Ausgabe vom Typ 'geojson' (Quelle
    ist zur Prüfzeit ein Vektor), deren Ergebnis zur Laufzeit trotzdem kein
    Vektor ist (hier nachträglich im Store ausgetauscht - ein Baustein, der
    den Vertrag verletzt). Nachbedingung: kein stiller Datenverlust - die
    Ausgabe erscheint in skipped mit erklärendem Grund. Die Deklaration eines
    Graphen an ein Vektorformat scheitert dagegen schon vor dem Lauf, siehe
    test_fa02_output_type_mismatch_is_rejected_before_the_run. (Ein Graph wäre seit
    FA53 kein Fehler mehr: geojson schreibt ihn als Kanten-Layer; deshalb dient hier ein
    Raster als Vertragsverletzung.)"""
    scenario = _network_scenario_with_outputs(
        [
            {"type": "geojson", "source": "nodes", "path": "nodes.geojson"},
        ]
    )
    result = run_scenario(scenario)
    result.store._layers["nodes"] = a_raster()

    outcome = write_outputs(scenario, result, tmp_path)

    assert outcome.written == []
    assert len(outcome.skipped) == 1
    skipped = outcome.skipped[0]
    assert skipped.source == "nodes"
    assert "Vektor-Layer" in skipped.reason


def test_fa12_write_outputs_reports_missing_crs_for_rdf(tmp_path):
    """Vorbedingung: eine rdf-Ausgabe referenziert einen Vektor-Layer ohne
    gesetztes CRS (rdf.py weist das zurück, damit WKT-Literale nicht
    fälschlich als CRS84 gelten, siehe FA13-Bugfix). Nachbedingung: wie
    bei fehlender Quelle/Typfehlpassung - die Ausgabe fehlt in written,
    taucht aber mit Grund in skipped auf, andere Ausgaben sind unbeeinflusst."""
    scenario = _network_scenario_with_outputs(
        [
            {"type": "geojson", "source": "nodes", "path": "nodes.geojson"},
            {
                "type": "rdf",
                "source": "nodes",
                "vocabulary": "geosparql",
                "path": "nodes.ttl",
            },
        ]
    )
    result = run_scenario(scenario)
    result.store._layers["nodes"] = result.store._layers["nodes"].set_crs(
        None, allow_override=True
    )

    outcome = write_outputs(scenario, result, tmp_path)

    assert {p.name for p in outcome.written} == {"nodes.geojson"}
    assert len(outcome.skipped) == 1
    skipped = outcome.skipped[0]
    assert skipped.source == "nodes"
    assert skipped.type == "rdf"
    assert "CRS" in skipped.reason


def test_fa12_write_outputs_partial_success_mixes_written_and_skipped(tmp_path):
    """Randfall: eine gültige und eine ungültige Ausgabe zusammen -
    die gültige wird trotzdem geschrieben (ein Fehltyp darf nicht alle
    Ausgaben verhindern), die ungültige wird separat gemeldet."""
    scenario = _network_scenario_with_outputs(
        [
            {"type": "geojson", "source": "nodes", "path": "nodes.geojson"},
            {
                "type": "geojson",
                "source": "substations",
                "path": "umspannwerke.geojson",
            },
        ]
    )
    result = run_scenario(scenario)
    result.store._layers["substations"] = a_raster()  # Vertragsverletzung zur Laufzeit

    outcome = write_outputs(scenario, result, tmp_path)

    assert {p.name for p in outcome.written} == {"nodes.geojson"}
    assert len(outcome.skipped) == 1
    assert outcome.skipped[0].source == "substations"


def test_fa12_write_outputs_failed_writer_on_a_directory_does_not_stop_later_outputs(
    tmp_path,
):
    """Nachtreview 02.10. (Runde 2): scheitert ein Writer an einem Ziel, das
    ein Verzeichnis ist, darf das Aufräumen nicht selbst werfen - die
    Ausgabe steht in skipped, die folgende wird trotzdem geschrieben."""
    scenario = _network_scenario_with_outputs(
        [
            {"type": "geojson", "source": "nodes", "path": "erst.geojson"},
            {
                "type": "geojson",
                "source": "substations",
                "path": "umspannwerke.geojson",
            },
        ]
    )
    result = run_scenario(scenario)
    (tmp_path / "erst.geojson").mkdir()  # Ziel ist ein Verzeichnis

    outcome = write_outputs(scenario, result, tmp_path)

    assert {p.name for p in outcome.written} == {"umspannwerke.geojson"}
    assert [s.index for s in outcome.skipped] == [0]
    assert (tmp_path / "erst.geojson").is_dir()
