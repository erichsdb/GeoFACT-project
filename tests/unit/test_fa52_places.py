"""Implements: FA52 (Orte als Datenquelle: source geocode, source points).

Contract-Tests für src/geofact/builtin/sources/geocode.py, points.py und
_places.py: Happy Path, Konfigurationsfehler, Treffer außerhalb der Region,
Snapshot, Nominatim-Anfrage (Parameter, Drosselung), Harmonisierung ins
Arbeits-CRS. Offline: die Nominatim-Anfrage ist durch eine Funktion ersetzt.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from geofact import api
from geofact.builtin.sources import geocode as geocode_module
from geofact.builtin.sources import points as points_module
from geofact.builtin.sources.geocode import GeocodeLayer
from geofact.builtin.sources.points import PointsLayer
from geofact.core.contracts import LoadContext
from geofact.engine import region as region_module

CHEMNITZ_BBOX = "12.7275,50.7414,13.0540,50.9039"
HBF = {
    "lon": "12.9305892",
    "lat": "50.8395789",
    "display_name": "Chemnitz Hauptbahnhof, Zentrum, Chemnitz",
}
TU = {
    "lon": "12.9294896",
    "lat": "50.8157942",
    "display_name": "Reichenhainer Straße 70, Chemnitz",
}
ANSWERS = {"Chemnitz Hauptbahnhof": HBF, "Reichenhainer Straße 70, Chemnitz": TU}


@pytest.fixture
def context() -> LoadContext:
    return LoadContext(region=region_module.resolve_region(CHEMNITZ_BBOX))


@pytest.fixture(autouse=True)
def snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))


@pytest.fixture
def nominatim(monkeypatch) -> list[tuple]:
    """Ersetzt die Nominatim-Anfrage; liefert die Liste der Aufrufe."""
    calls: list[tuple] = []

    def fake_search(url, query, countrycodes):
        calls.append((url, query, countrycodes))
        hit = ANSWERS.get(query)
        return [hit] if hit else []

    monkeypatch.setattr(geocode_module, "_search", fake_search)
    return calls


def geocode_layer(**fields) -> GeocodeLayer:
    return GeocodeLayer(id="start", **fields)


# =====================================================================
# geocode: Happy Path
# =====================================================================


def test_fa52_geocode_resolves_a_query_to_one_point(nominatim, context):
    places = geocode_module.load(geocode_layer(query="Chemnitz Hauptbahnhof"), context)
    assert list(places.columns) == ["name", "display_name", "geometry"]
    assert places.crs == "EPSG:4326"
    assert places["name"].tolist() == ["Chemnitz Hauptbahnhof"]
    assert places["display_name"].iloc[0] == HBF["display_name"]
    point = places.geometry.iloc[0]
    assert (point.x, point.y) == pytest.approx((12.9305892, 50.8395789))


def test_fa52_geocode_resolves_several_queries_in_order(nominatim, context):
    layer = geocode_layer(
        queries=["Reichenhainer Straße 70, Chemnitz", "Chemnitz Hauptbahnhof"]
    )
    places = geocode_module.load(layer, context)
    assert places["name"].tolist() == [
        "Reichenhainer Straße 70, Chemnitz",
        "Chemnitz Hauptbahnhof",
    ]
    assert [call[1] for call in nominatim] == places["name"].tolist()


def test_fa52_geocode_passes_country_codes_and_the_endpoint(
    nominatim, context, monkeypatch
):
    monkeypatch.setenv("GEOFACT_NOMINATIM_URL", "http://localhost:8080/search")
    geocode_module.load(
        geocode_layer(query="Chemnitz Hauptbahnhof", countrycodes="DE, at"), context
    )
    assert nominatim == [
        ("http://localhost:8080/search", "Chemnitz Hauptbahnhof", ["de", "at"])
    ]


# =====================================================================
# geocode: Fehlerfälle
# =====================================================================


def test_fa52_geocode_without_a_hit_names_the_query(nominatim, context):
    with pytest.raises(ValueError, match="keinen Treffer für 'Gibt es nicht'"):
        geocode_module.load(geocode_layer(query="Gibt es nicht"), context)


def test_fa52_geocode_hit_outside_the_region_is_an_error_naming_place_and_coordinates(
    monkeypatch, context
):
    monkeypatch.setattr(
        geocode_module,
        "_search",
        lambda url, query, countrycodes: [
            {"lon": "13.7373", "lat": "51.0504", "display_name": "Dresden"}
        ],
    )
    with pytest.raises(
        ValueError, match=r"'Dresden Hbf' \(lon 13\.73730, lat 51\.05040\)"
    ):
        geocode_module.load(geocode_layer(query="Dresden Hbf"), context)


def test_fa52_geocode_hit_just_outside_the_bbox_is_accepted_within_the_margin(
    monkeypatch, context
):
    # 0.005 Grad (ca. 0.55 km) östlich der BBox, Rand 1 km
    monkeypatch.setattr(
        geocode_module,
        "_search",
        lambda url, query, countrycodes: [
            {"lon": "13.0590", "lat": "50.8200", "display_name": "Rand"}
        ],
    )
    assert len(geocode_module.load(geocode_layer(query="Rand"), context)) == 1
    with pytest.raises(ValueError, match="außerhalb der Szenario-Region"):
        geocode_module.load(geocode_layer(query="Rand", margin_km=0.1), context)


def test_fa52_geocode_response_without_coordinates_is_an_error(monkeypatch, context):
    monkeypatch.setattr(
        geocode_module, "_search", lambda *a: [{"display_name": "ohne Punkt"}]
    )
    with pytest.raises(ValueError, match="keine verwertbaren Koordinaten"):
        geocode_module.load(geocode_layer(query="x"), context)


def test_fa52_geocode_needs_exactly_one_of_query_and_queries():
    with pytest.raises(ValidationError, match="genau eines von 'query' und 'queries'"):
        geocode_layer()
    with pytest.raises(ValidationError, match="genau eines von 'query' und 'queries'"):
        geocode_layer(query="a", queries=["b"])


def test_fa52_geocode_rejects_blank_queries_and_empty_query_lists():
    with pytest.raises(ValidationError, match="nicht leer"):
        geocode_layer(query="   ")
    with pytest.raises(ValidationError):
        geocode_layer(queries=[])


def test_fa52_geocode_rejects_bad_country_codes_and_unknown_fields():
    with pytest.raises(ValidationError, match="Ländercodes"):
        geocode_layer(query="a", countrycodes="deutschland")
    with pytest.raises(ValidationError, match="qeury"):
        GeocodeLayer(id="start", qeury="a")


def test_fa52_geocode_without_a_region_in_the_context_skips_the_region_check(
    monkeypatch,
):
    monkeypatch.setattr(
        geocode_module,
        "_search",
        lambda *a: [{"lon": "13.7373", "lat": "51.0504", "display_name": "Dresden"}],
    )
    places = geocode_module.load(geocode_layer(query="Dresden Hbf"), LoadContext())
    assert len(places) == 1


# =====================================================================
# geocode: Snapshot (FA7)
# =====================================================================


def test_fa52_geocode_second_load_reads_the_snapshot_instead_of_asking_again(
    nominatim, context
):
    layer = geocode_layer(query="Chemnitz Hauptbahnhof")
    first = geocode_module.load(layer, context)
    second = geocode_module.load(layer, context)
    assert len(nominatim) == 1
    assert second["name"].tolist() == first["name"].tolist()
    assert second["display_name"].tolist() == first["display_name"].tolist()
    assert second.geometry.iloc[0].equals_exact(first.geometry.iloc[0], 1e-9)
    assert second.crs == "EPSG:4326"


def test_fa52_geocode_refresh_asks_again(nominatim, context):
    layer = geocode_layer(query="Chemnitz Hauptbahnhof")
    geocode_module.load(layer, context)
    geocode_module.load(layer, LoadContext(region=context.region, refresh=True))
    assert len(nominatim) == 2


def test_fa52_geocode_snapshot_is_checked_against_the_region_again(nominatim, context):
    """Der Snapshot hängt nicht an der Region; ein anderes Szenario mit anderer Region
    bekommt denselben Treffer, prüft ihn aber neu."""
    layer = geocode_layer(query="Chemnitz Hauptbahnhof")
    geocode_module.load(layer, context)
    other_region = LoadContext(
        region=region_module.resolve_region("13.60,51.01,13.94,51.15")
    )
    with pytest.raises(ValueError, match="außerhalb der Szenario-Region"):
        geocode_module.load(layer, other_region)
    assert len(nominatim) == 1


def test_fa52_geocode_snapshot_key_depends_on_the_query(nominatim, context):
    geocode_module.load(geocode_layer(query="Chemnitz Hauptbahnhof"), context)
    geocode_module.load(
        geocode_layer(query="Reichenhainer Straße 70, Chemnitz"), context
    )
    assert len(nominatim) == 2


# =====================================================================
# geocode: die Anfrage selbst (Nominatim-Richtlinie)
# =====================================================================


def test_fa52_geocode_search_sends_limit_format_and_country_codes_through_the_http_helper(
    monkeypatch,
):
    seen: dict = {}

    def fake_get_json(url, *, params=None, context, max_retries=3):
        seen.update(url=url, params=params, context=context)
        return [HBF]

    monkeypatch.setattr(geocode_module.http, "get_json", fake_get_json)
    monkeypatch.setattr(geocode_module, "_throttle", lambda: None)
    result = geocode_module._search("http://n/search", "Chemnitz Hauptbahnhof", ["de"])
    assert result == [HBF]
    assert seen["url"] == "http://n/search"
    assert seen["params"] == {
        "q": "Chemnitz Hauptbahnhof",
        "format": "jsonv2",
        "limit": 1,
        "countrycodes": "de",
    }
    assert "Chemnitz Hauptbahnhof" in seen["context"]


def test_fa52_geocode_throttle_waits_before_the_first_and_between_requests(monkeypatch):
    clock = {"now": 100.0}
    sleeps: list[float] = []

    def fake_sleep(seconds):
        sleeps.append(round(seconds, 3))
        clock["now"] += seconds

    monkeypatch.setattr(geocode_module, "_last_request_at", None)
    monkeypatch.setattr(geocode_module.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(geocode_module.time, "sleep", fake_sleep)

    geocode_module._throttle()  # erste Anfrage: volles Intervall
    clock["now"] += 0.2
    geocode_module._throttle()  # 0.2 s später: Rest des Intervalls
    clock["now"] += 5
    geocode_module._throttle()  # lange her: keine Wartezeit
    assert sleeps == [
        geocode_module.MIN_INTERVAL_S,
        round(geocode_module.MIN_INTERVAL_S - 0.2, 3),
    ]


# =====================================================================
# points
# =====================================================================


def points_layer(**fields) -> PointsLayer:
    fields.setdefault(
        "features",
        [
            {"name": "Start", "lon": 12.9306, "lat": 50.8396},
            {"name": "Ziel", "lon": 12.9295, "lat": 50.8158},
        ],
    )
    return PointsLayer(id="orte", **fields)


def test_fa52_points_become_a_point_layer_with_names(context):
    places = points_module.load(points_layer(), context)
    assert list(places.columns) == ["name", "geometry"]
    assert places.crs == "EPSG:4326"
    assert places["name"].tolist() == ["Start", "Ziel"]
    assert (places.geometry.iloc[1].x, places.geometry.iloc[1].y) == pytest.approx(
        (12.9295, 50.8158)
    )


def test_fa52_points_need_at_least_one_feature():
    with pytest.raises(ValidationError, match="features"):
        PointsLayer(id="orte", features=[])


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "x", "lon": 200, "lat": 50},
        {"name": "x", "lon": 12, "lat": 95},
        {"name": "  ", "lon": 12, "lat": 50},
        {"name": "x", "lon": 12},
        {"name": "x", "lon": 12, "lat": 50, "hoehe": 3},
    ],
)
def test_fa52_points_reject_invalid_features(bad):
    with pytest.raises(ValidationError):
        PointsLayer(id="orte", features=[bad])


def test_fa52_points_outside_the_region_are_an_error(context):
    layer = points_layer(features=[{"name": "Dresden", "lon": 13.7373, "lat": 51.0504}])
    with pytest.raises(ValueError, match="außerhalb der Szenario-Region.*'Dresden'"):
        points_module.load(layer, context)


# =====================================================================
# Im Lauf: Harmonisierung ins Arbeits-CRS (FA5)
# =====================================================================


def _run(layers: list[dict], monkeypatch) -> api.RunReport:
    config = {
        "scenario": {"name": "FA52", "region": CHEMNITZ_BBOX},
        "layers": layers,
        "steps": [
            {
                "id": "umkreis",
                "op": "buffer",
                "inputs": {"geometry": layers[0]["id"]},
                "params": {"radius_km": 0.1},
            }
        ],
        "output": [{"type": "geojson", "source": "umkreis", "path": "umkreis.geojson"}],
    }
    return api.run(api.load_scenario(config))


def test_fa52_points_layer_is_reprojected_to_the_working_crs_in_a_run(monkeypatch):
    report = _run(
        [
            {
                "id": "orte",
                "source": "points",
                "features": [{"name": "Start", "lon": 12.9306, "lat": 50.8396}],
            }
        ],
        monkeypatch,
    )
    assert report.result.store["orte"].crs == "EPSG:32633"
    assert report.result.store["umkreis"].area.iloc[0] == pytest.approx(
        31_415.9, rel=0.01
    )


def test_fa52_geocode_layer_is_reprojected_to_the_working_crs_in_a_run(
    nominatim, monkeypatch
):
    report = _run(
        [{"id": "start", "source": "geocode", "query": "Chemnitz Hauptbahnhof"}],
        monkeypatch,
    )
    store = report.result.store
    assert store["start"].crs == "EPSG:32633"
    assert store["start"]["name"].tolist() == ["Chemnitz Hauptbahnhof"]
    assert len(nominatim) == 1


def test_fa52_unknown_field_in_a_scenario_layer_is_located():
    config = {
        "scenario": {"name": "FA52", "region": CHEMNITZ_BBOX},
        "layers": [{"id": "start", "source": "geocode", "qeury": "x"}],
        "steps": [],
        "output": [],
    }
    with pytest.raises(api.ConfigError) as caught:
        api.load_scenario(config)
    assert any("layers -> 0" in location for location, _ in caught.value.issues)


def test_fa52_both_sources_are_registered_and_listed_in_the_catalog():
    names = {entry["name"] for entry in api.extension_catalog()["sources"]}
    assert {"geocode", "points"} <= names
