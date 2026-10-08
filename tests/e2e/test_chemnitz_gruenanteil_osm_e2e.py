"""Implements: FA68 (area, calculate_field), FA69 (select_columns), FA70 (area_share), FA8 (clip, dissolve), FA3 (OSM geometry/columns).

End-to-end test of examples/chemnitz_gruenanteil_osm.yaml, offline: the two OSM layers are
replaced by the synthetic stand-ins of tests/chemnitz_gruen_scenes.py, the region by the
Nominatim fixture of Chemnitz. The run goes through the delivery entry point
(geofact.api.load_scenario + geofact.api.run) and writes the declared outputs.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import box

from geofact import api

from chemnitz_gruen_scenes import (
    EMPTY_DISTRICTS,
    districts,
    green_areas,
    gruen_overrides,
)

pytestmark = pytest.mark.e2e

EXAMPLE = (
    Path(__file__).resolve().parents[2] / "examples" / "chemnitz_gruenanteil_osm.yaml"
)
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _region_fetcher(name: str) -> list[dict]:
    assert name == "Chemnitz, Deutschland"
    return json.loads(
        (FIXTURES_DIR / "nominatim_chemnitz.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    out_dir = tmp_path_factory.mktemp("chemnitz_gruen")
    document = api.load_scenario(EXAMPLE)
    with pytest.MonkeyPatch.context() as patch:
        # a region cached by a real run must not replace the fixture region
        patch.setenv("GEOFACT_SNAPSHOT_DIR", str(out_dir / "snapshots"))
        return api.run(
            document,
            out_dir=out_dir / "out",
            connector_override=gruen_overrides(),
            region_fetcher=_region_fetcher,
        )


def test_fa03_example_osm_layers_declare_polygons_and_column_whitelist():
    scenario = api.load_scenario(EXAMPLE).scenario
    layers = {layer.id: layer for layer in scenario.layers}
    assert layers["gruen"].geometry_kinds() == ("polygon",)
    assert layers["gruen"].columns == ["name", "leisure", "landuse", "natural"]
    assert layers["stadtteile"].geometry_kinds() == ("polygon",)


def test_fa70_example_shares_are_between_0_and_1(report):
    shares = report.result.store["kontrolle"]
    assert len(shares) == 9
    assert shares["gruenanteil"].between(0.0, 1.0).all()
    by_id = shares.set_index("id")
    for district in EMPTY_DISTRICTS:
        assert by_id.loc[district, "gruenanteil"] == 0.0
        assert by_id.loc[district, "gruen_m2"] == 0.0
    assert (by_id.drop(index=list(EMPTY_DISTRICTS))["gruenanteil"] > 0).all()


def test_fa70_example_overlapping_grass_inside_forest_is_counted_once(report):
    """The unnamed grass lies completely inside the Kuechwald: the covered area of z2 and
    z5 together equals the forest plus the Schlossteich, not forest + grass."""
    store = report.result.store
    crs = store["kontrolle"].crs
    by_id = store["kontrolle"].set_index("id")
    zones = districts().to_crs(crs).set_index("id")
    green = green_areas().to_crs(crs)
    forest = green[green["name"] == "Küchwald"].geometry.iloc[0]
    teich = green[green["name"] == "Schlossteich"].geometry.iloc[0]
    expected = (
        forest.intersection(zones.loc["z2"].geometry).area
        + forest.intersection(zones.loc["z5"].geometry).area
        + teich.area
    )
    covered = by_id.loc["z2", "gruen_m2"] + by_id.loc["z5", "gruen_m2"]
    assert covered == pytest.approx(expected, rel=1e-9)


def test_fa68_control_field_equals_area_share(report):
    shares = report.result.store["kontrolle"]
    assert {"area_m2", "anteil_kontrolle"} <= set(shares.columns)
    np.testing.assert_allclose(
        shares["anteil_kontrolle"], shares["gruenanteil"], atol=1e-3
    )


def test_fa68_top_parks_have_area_column(report):
    ranking = report.result.store["rangliste"]
    assert sorted(ranking.columns.drop(ranking.geometry.name)) == [
        "area_ha",
        "landuse",
        "leisure",
        "name",
        "natural",
        "rang",
    ]
    assert list(ranking["rang"]) == list(range(1, len(ranking) + 1))
    areas = list(ranking["area_ha"])
    assert areas == sorted(areas, reverse=True)
    assert all(value > 0 for value in areas)
    # the hectares are the planar area in the working CRS (both Zeisigwald pieces together)
    zeisig = green_areas().to_crs(ranking.crs)
    zeisig_ha = zeisig[zeisig["name"] == "Zeisigwald"].geometry.area.sum() / 1e4
    assert ranking.set_index("name").loc["Zeisigwald", "area_ha"] == pytest.approx(
        zeisig_ha, abs=0.01
    )


def test_fa08_ranking_counts_only_the_part_inside_the_city(report):
    """Nachtreview 02.10. (Runde 2): der OSM-Lader behält ganze Flächen, die die
    Region berühren; die Rangliste mass die Struth samt ihrem Teil im Umland."""
    ranking = report.result.store["rangliste"].set_index("name")
    city = districts().to_crs(ranking.crs).union_all()
    green = green_areas().to_crs(ranking.crs)
    struth = green[green["name"] == "Struth"].geometry.iloc[0]
    assert ranking.loc["Struth", "area_ha"] == pytest.approx(
        struth.intersection(city).area / 1e4, abs=0.01
    )
    assert (
        ranking.loc["Struth", "area_ha"] < 0.6 * struth.area / 1e4
    )  # half lies outside


def test_fa08_ranking_merges_all_pieces_of_a_name(report):
    """Nachtreview 02.10. (Runde 2): deduplicate (by name, largest_geometry) ließ
    getrennte Stücke desselben Waldes still weg. Jetzt: eine Zeile je Name, eine
    doppelt kartierte Fläche zählt einmal, getrennte Stücke zählen alle; Flächen
    ohne Namen fehlen in der Rangliste und werden gemeldet."""
    store = report.result.store
    ranking = store["rangliste"]
    assert ranking["name"].is_unique and ranking["name"].notna().all()
    assert set(ranking["name"]) == {
        "Stadtpark",
        "Küchwald",
        "Zeisigwald",
        "Struth",
        "Schlossteich",
    }
    by_name = ranking.set_index("name")
    largest = box(12.86, 50.81, 12.90, 50.83)  # the small Stadtpark way lies inside it
    expected_ha = (
        gpd.GeoSeries([largest], crs="EPSG:4326").to_crs(ranking.crs).area.iloc[0] / 1e4
    )
    assert by_name.loc["Stadtpark", "area_ha"] == pytest.approx(expected_ha, abs=0.01)
    messages = [warning.message for warning in report.result.warnings]
    assert any("ohne Wert für 'name'" in message for message in messages)


def test_fa65_example_outputs_are_written_with_osm_attribution(report):
    written = {path.name for path in report.outputs.written}
    assert written == {
        "gruenanteil_stadtteile.html",
        "gruenanteil_stadtteile.geojson",
        "gruenanteil_stadtteile.csv",
        "groesste_gruenflaechen.csv",
    }
    assert report.attribution_file is not None
    assert "OpenStreetMap" in report.attribution_file.read_text(encoding="utf-8")
