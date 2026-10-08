"""Implements: FA75 (Modul + Instanz + Selektor), FA60 (collect), FA64 (Region je Layer), FA70 (area_share) - live run of
examples/deutschland_gruenste_grossstadt_osm.yaml with three cities.

Integration test against the local PostGIS instance (Germany import) and Nominatim (one
request per city, cached under GEOFACT_SNAPSHOT_DIR). Skipped without GEOFACT_PG_DSN (see
tests/conftest.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from geofact import api

# netcheck: the region layers ask Nominatim (network) for every city not yet cached (FA45)
pytestmark = [pytest.mark.integration, pytest.mark.netcheck]

EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "deutschland_gruenste_grossstadt_osm.yaml"
)
THREE = [
    {"id": "chemnitz", "region": "Chemnitz, Deutschland"},
    {"id": "jena", "region": "Jena, Deutschland"},
    {"id": "trier", "region": "Trier, Deutschland"},
]


def test_fa75_gruenste_grossstadt_three_cities_live(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_OSM_BACKEND", "postgis")
    document = api.load_scenario(
        EXAMPLE, parameters={"cities": THREE, "with_map": False}
    )
    report = api.run(document, out_dir=tmp_path / "out")

    frame = report.result.store["alle_staedte"]
    assert sorted(frame["stadt_id"]) == ["chemnitz", "jena", "trier"]
    # mittlere Definition: real cities lie well between "parks only" and "all vegetation"
    assert frame["gruenanteil"].between(0.05, 0.8).all(), frame[
        ["stadt_id", "gruenanteil"]
    ]
    ranking = report.result.store["rangliste"]
    assert list(ranking["rang"]) == [1, 2, 3]
    assert list(ranking["gruenanteil"]) == sorted(ranking["gruenanteil"], reverse=True)
