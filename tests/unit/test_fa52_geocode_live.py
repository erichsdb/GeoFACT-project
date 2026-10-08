"""Implements: FA52 (Orte als Datenquelle, Gegenprobe gegen das echte Nominatim).

Live-Test (Marke ``netcheck``, nur mit ``pytest -m netcheck``): löst die beiden Orte des
Beispiels `examples/chemnitz_route_hbf_tu.yaml` über das öffentliche Nominatim auf und
prüft, dass sie nahe den bekannten Koordinaten liegen (Stand 01.10.2026: Hauptbahnhof
50.83958 / 12.93059, Reichenhainer Straße 70 50.81579 / 12.92949). Der Test hält die
Nutzungsrichtlinie ein (eigener User-Agent, 1 Anfrage/s über den Konnektor).
"""

from __future__ import annotations

import pytest

from geofact.builtin.sources import geocode as geocode_module
from geofact.builtin.sources.geocode import GeocodeLayer
from geofact.core.contracts import LoadContext
from geofact.engine import region as region_module

pytestmark = pytest.mark.netcheck


def test_fa52_live_nominatim_resolves_the_example_places(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    context = LoadContext(
        region=region_module.resolve_region("12.7275,50.7414,13.0540,50.9039")
    )
    layer = GeocodeLayer(
        id="orte",
        queries=["Chemnitz Hauptbahnhof", "Reichenhainer Straße 70, Chemnitz"],
        countrycodes="de",
    )
    places = geocode_module.load(layer, context).set_index("name")
    hbf = places.loc["Chemnitz Hauptbahnhof"].geometry
    tu = places.loc["Reichenhainer Straße 70, Chemnitz"].geometry
    assert (hbf.x, hbf.y) == pytest.approx((12.93059, 50.83958), abs=0.002)
    assert (tu.x, tu.y) == pytest.approx((12.92949, 50.81579), abs=0.002)
    assert "Chemnitz" in places.loc["Chemnitz Hauptbahnhof", "display_name"]
