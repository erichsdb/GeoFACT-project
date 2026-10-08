"""Implements: FA59 (Parameter), FA60 (collect), FA75 (Modul + Instanz + Selektor), FA62 (when), FA64 (Region je Layer), FA70 (area_share), FA35 (top_n-Rangliste) - example examples/deutschland_gruenste_grossstadt_osm.yaml.

End-to-end contract tests of the showcase "greenest big city of Germany": the module
``stadt_gruen`` is instantiated once per city of the parameter ``cities``, every city loads its
boundary and its green areas in its OWN region, ``collect`` merges the per-city shares and
``top_n`` ranks them; the map is written only with ``with_map``. Offline: the region fetcher
and the OSM connector are replaced by the doubles of tests/gruenste_grossstadt_doubles.py.
"""

from __future__ import annotations

import socket

import pytest
import yaml

from geofact import api
from geofact.core.expansion import InstanceInfo, expand

import gruenste_grossstadt_doubles as doubles

pytestmark = pytest.mark.e2e

EXAMPLE = doubles.EXAMPLE
THREE = [
    {"id": "leipzig", "region": "Leipzig, Deutschland"},
    {"id": "dresden", "region": "Dresden, Deutschland"},
    {"id": "chemnitz", "region": "Chemnitz, Deutschland"},
]


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    """No network (Nominatim/PostGIS would be the real thing) and an empty snapshot
    directory, so no cached real region answers leak into the run."""
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] not in (
            "127.0.0.1",
            "localhost",
            "::1",
        ):
            raise RuntimeError(f"network access in an offline test: {address}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))


def _raw() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _run(overrides: dict | None, out_dir):
    document = api.load_scenario(EXAMPLE, parameters=overrides)
    cities = (
        tuple((c["id"], c["region"]) for c in (overrides or {}).get("cities", []))
        or None
    )
    report = api.run(
        document,
        out_dir=out_dir,
        connector_override=doubles.overrides(cities),
        region_fetcher=doubles.region_fetcher,
    )
    return document.scenario, report


def test_fa75_example_expands_to_one_pipeline_per_city():
    cities = doubles.default_cities()
    assert (
        len(cities) == 80
    )  # Destatis 31.12.2024: 80 cities with >= 100 000 inhabitants
    report = expand(_raw())
    layers = [layer["id"] for layer in report.expanded["layers"]]
    steps = {step["id"]: step for step in report.expanded["steps"]}
    ids = [city_id for city_id, _ in cities]
    assert layers == [
        f"stadt[{city}].{kind}" for city in ids for kind in ("grenze", "gruen")
    ]
    assert len(layers) + len(steps) == 242
    assert [s for s in steps if s.endswith(".anteil")] == [
        f"stadt[{city}].anteil" for city in ids
    ]
    assert steps["stadt[berlin].anteil"]["inputs"] == {
        "zones": "stadt[berlin].grenze",
        "features": "stadt[berlin].gruen",
    }
    collect = steps["alle_staedte"]
    assert collect["op"] == "collect"
    assert collect["inputs"] == {city: f"stadt[{city}].anteil" for city in ids}
    assert collect["params"] == {"source_field": "stadt_id"}
    assert steps["rangliste"]["params"]["n"] == 10  # ${top} keeps its type
    assert report.instances == (InstanceInfo("stadt", "stadt_gruen", tuple(ids), ()),)
    assert report.node_origins["stadt[berlin].anteil"].location() == (
        "instances",
        "stadt[berlin]",
        "steps",
        "anteil",
    )
    # the expanded form is a plain, valid scenario
    assert api.validate(report.expanded, base_dir=EXAMPLE.parent).valid


def test_fa59_example_override_with_three_cities_runs_offline(tmp_path):
    scenario, report = _run({"cities": THREE}, tmp_path / "out")
    assert len(scenario.layers) == 6
    frame = report.result.store["alle_staedte"]
    assert sorted(frame["stadt_id"]) == ["chemnitz", "dresden", "leipzig"]
    assert frame["gruenanteil"].between(0, 1).all()
    ranking = report.result.store["rangliste"]
    assert len(ranking) == 3  # top: 10 > three cities - all of them, ranked
    written = {path.name for path in report.outputs.written}
    assert {
        "rangliste_gruenste_grossstaedte.csv",
        "gruenanteil_grossstaedte.csv",
        "rangliste_gruenste_grossstaedte.geojson",
        "rangliste_gruenste_grossstaedte.html",
    } <= written


def test_fa62_map_output_is_absent_unless_with_map(tmp_path):
    with_map = expand(_raw())
    assert [o["type"] for o in with_map.expanded["output"]] == [
        "csv",
        "csv",
        "geojson",
        "map",
    ]
    without = expand(_raw(), {"with_map": False, "cities": THREE})
    assert [o["type"] for o in without.expanded["output"]] == ["csv", "csv", "geojson"]
    assert [location for location, _ in without.dropped] == [("output", "3")]

    _, report = _run({"with_map": False, "cities": THREE}, tmp_path / "out")
    assert not [path for path in report.outputs.written if path.suffix == ".html"]


def test_fa64_each_city_layer_loads_in_its_own_region(tmp_path):
    _, report = _run({"cities": THREE}, tmp_path / "out")
    provenance = report.result.provenance
    for city in THREE:
        minx, miny, maxx, maxy = doubles.city_box(city["region"])
        boundary = provenance[f"stadt[{city['id']}].grenze"].raw_bounds
        assert boundary == pytest.approx(
            (minx, miny, maxx, maxy)
        )  # source: region -> ITS region
        green = (
            report.result.store[f"stadt[{city['id']}].gruen"].to_crs(4326).total_bounds
        )
        assert (
            green[1] >= miny - 1e-9 and green[3] <= maxy + 1e-9
        )  # the city's own green areas
        name = (
            report.result.store["alle_staedte"]
            .set_index("stadt_id")
            .loc[city["id"], "name"]
        )
        assert name == city["region"]


def test_fa35_ranking_is_descending_by_gruenanteil(tmp_path):
    _, report = _run(None, tmp_path / "out")
    everything = report.result.store["alle_staedte"]
    assert len(everything) == 80
    ranking = report.result.store["rangliste"]
    assert list(ranking["rang"]) == list(range(1, 11))
    shares = list(ranking["gruenanteil"])
    assert shares == sorted(shares, reverse=True)
    expected = sorted(range(80), key=doubles.share_of, reverse=True)[:10]
    ids = [city_id for city_id, _ in doubles.default_cities()]
    assert list(ranking["stadt_id"]) == [ids[k] for k in expected]
    for k in expected:
        assert ranking.set_index("stadt_id").loc[
            ids[k], "gruenanteil"
        ] == pytest.approx(doubles.share_of(k), abs=0.002)
