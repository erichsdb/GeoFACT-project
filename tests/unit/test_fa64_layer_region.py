"""Implements: FA64 (Region je Layer).

Contract-Tests für engine/loading.py: ein Layer mit ``region`` sieht beim
Laden seine eigene Region (Konnektor, ``source: region``,
``spatial_check.within: scenario_region``), das Arbeits-CRS bleibt das des
Szenarios, eine nicht auflösbare Region ist ein LayerLoadError."""

from __future__ import annotations

import pytest

from _loading_helpers import (
    DRESDEN_BBOX,
    FAR_AWAY_BBOX,
    FIXTURES_DIR,
    context,
    fixture_fetcher,
    layer_from,
)
from geofact.core.errors import LayerLoadError
from geofact.core.registry import default_registry
from geofact.engine.loading import layer_loading_detail, load_layer

LEIPZIG_BBOX = "12.30,51.30,12.45,51.38"


def _bounds_wgs84(gdf):
    return tuple(gdf.to_crs(4326).total_bounds)


def test_fa64_layer_without_region_sees_the_scenario_region():
    layer = layer_from({"id": "gebiet", "source": "region"})

    data = load_layer(layer, context(DRESDEN_BBOX), default_registry())

    assert layer.region is None
    assert _bounds_wgs84(data) == pytest.approx((13.60, 51.01, 13.94, 51.15), abs=1e-6)


def test_fa64_region_source_with_own_region_returns_that_area():
    layer = layer_from({"id": "gebiet", "source": "region", "region": LEIPZIG_BBOX})

    data = load_layer(layer, context(DRESDEN_BBOX), default_registry())

    assert _bounds_wgs84(data) == pytest.approx((12.30, 51.30, 12.45, 51.38), abs=1e-6)


def test_fa64_working_crs_stays_the_scenario_crs():
    # Region in UTM-Zone 32, Szenario in Zone 33: ein Arbeits-CRS je Lauf.
    layer = layer_from({"id": "gebiet", "source": "region", "region": FAR_AWAY_BBOX})
    ctx = context(DRESDEN_BBOX)

    data = load_layer(layer, ctx, default_registry())

    assert data.crs.to_epsg() == 32633 == ctx.target_crs.to_epsg()


def test_fa64_layer_region_name_is_resolved_with_the_fetcher(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    layer = layer_from({"id": "gebiet", "source": "region", "region": "Leipzig"})

    data = load_layer(
        layer,
        context(DRESDEN_BBOX),
        default_registry(),
        region_fetcher=fixture_fetcher("leipzig"),
    )

    min_lon, min_lat, max_lon, max_lat = _bounds_wgs84(data)
    assert 12.0 < min_lon < max_lon < 12.6 and 51.1 < min_lat < max_lat < 51.5


def test_fa64_unresolvable_layer_region_is_a_layer_load_error(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    layer = layer_from({"id": "gebiet", "source": "region", "region": "Nirgendwo"})

    with pytest.raises(
        LayerLoadError, match="Region 'Nirgendwo' konnte nicht aufgelöst werden"
    ):
        load_layer(
            layer,
            context(DRESDEN_BBOX),
            default_registry(),
            region_fetcher=lambda _name: [],
        )


def test_fa64_spatial_check_scenario_region_means_the_layer_region():
    validation = {
        "spatial_check": {"within": "scenario_region"},
        "on_violation": "abort",
    }
    substations = {
        "id": "umspannwerke",
        "source": "file",
        "data_type": "vector",
        "path": str(FIXTURES_DIR / "substations.geojson"),
        "validation": validation,
    }
    with_own_region = layer_from({**substations, "region": DRESDEN_BBOX})

    # Szenario-Region weit weg; die eigene Region des Layers deckt die Daten.
    data = load_layer(with_own_region, context(FAR_AWAY_BBOX), default_registry())
    assert len(data) > 0

    with pytest.raises(LayerLoadError):
        load_layer(layer_from(substations), context(FAR_AWAY_BBOX), default_registry())


def test_fa64_layer_loading_detail_names_the_region():
    assert layer_loading_detail(layer_from({"id": "a", "source": "region"})) == {}
    assert layer_loading_detail(
        layer_from({"id": "a", "source": "region", "region": LEIPZIG_BBOX})
    ) == {"region": LEIPZIG_BBOX}


def test_fa07_refresh_re_resolves_the_layer_region(tmp_path, monkeypatch):
    """Nachtreview 02.10. (Runde 2): ``ctx.refresh`` gilt auch für die Region
    eines Layers (FA64), nicht nur für die Snapshots der Konnektoren."""
    from dataclasses import replace

    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path))
    calls = []
    inner = fixture_fetcher("leipzig")

    def fetcher(name):
        calls.append(name)
        return inner(name)

    layer = layer_from({"id": "gebiet", "source": "region", "region": "Leipzig"})
    load_layer(layer, context(DRESDEN_BBOX), default_registry(), region_fetcher=fetcher)
    load_layer(layer, context(DRESDEN_BBOX), default_registry(), region_fetcher=fetcher)
    assert calls == ["Leipzig"]
    load_layer(
        layer,
        replace(context(DRESDEN_BBOX), refresh=True),
        default_registry(),
        region_fetcher=fetcher,
    )
    assert calls == ["Leipzig", "Leipzig"]
