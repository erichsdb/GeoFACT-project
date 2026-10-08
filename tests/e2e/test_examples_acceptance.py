"""Implements: FA44 (run a scenario as one use case), FA1/FA2 (validate), FA4 (path rule), FA9 (executor).

Acceptance net over ALL reference scenarios in examples/**/*.yaml. It is the
safety net of the restructuring (architecture B): every change must leave
these tests green. No xfail mark is left - everything the net was written to
demand is fulfilled.

What is checked
1. Every example validates through the CLI (`geofact validate`); the plugin
   demo with its plugin directory (GEOFACT_PLUGIN_PATH, the real mechanism).
2. Every example runs end to end OFFLINE (connector overrides + fixture region
   fetcher, the pattern of the existing e2e tests): only the required layers
   are loaded, every required step produces a result, every declared output
   is written (non-empty file) or explicitly reported as skipped - never lost.
3. Every example is classified: either it has an offline setup here or it is
   listed in LIVE_ONLY with a reason. An example added later without a
   setup fails `test_fa44_every_example_is_classified` (no silent gaps).

Also asserted for every example (formerly xfail until the matching stage landed):
- Round trip Scenario(**scenario.model_dump(mode="json")) is lossless (B step 7:
  SerializeAsAny, extra="forbid").
- `geofact run CONFIG [--out DIR]` writes the declared outputs (default
  ./output/<yaml-stem>/), and one path rule for CLI and web - relative layer
  paths resolve against the directory of the YAML file (B step 6, FA44; szenario1,
  szenario3 and leipzig13 were migrated from repo-relative paths; the web passes
  the origin of a loaded example on).

`_execute()` is the single adapter to the entry point under test
(geofact.api.load_scenario + geofact.api.run).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

import geopandas as gpd
import numpy as np
import pytest
import yaml
from rasterio.transform import from_origin
from affine import Affine
from shapely.geometry import Polygon, box, mapping
from pyproj import CRS

from geofact import api, cli
from geofact.core.registry import set_default_registry
from geofact.engine.discovery import discover
from geofact.api import (
    PLUGIN_PATH_ENV,
    ExecutionResult,
    LayerStore,
    RasterLayer,
    Scenario,
)

from chemnitz_gruen_scenes import gruen_overrides
from nachthimmel_scenes import nachthimmel_overrides
import gruenste_grossstadt_doubles
from stac_scenes import CITY_BOX, aligned_ring, expected_bands

# Reuses the plugin-demo fixtures (local demo service + plugin directory) so
# the plugin handling of the tests is adapted in exactly one place.
from .test_plugin_demo_e2e import demo_plugins, demo_registry, demo_service  # noqa: F401

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
PLUGIN_DEMO_DIR = EXAMPLES_DIR / "plugin_demo"
PLUGIN_DEMO_ID = "plugin_demo/szenario_plugins"
# Scenario 1 of the thesis needs the domain plugin examples/plugins/power_grid_osm.py (FA94).
GRID_EXAMPLE_ID = "sachsen_netz_resilienz"

pytestmark = pytest.mark.e2e

ALL_EXAMPLES = sorted(EXAMPLES_DIR.rglob("*.yaml"))
GRID_PLUGINS = EXAMPLES_DIR / "plugins"
# example id -> plugin directory it needs on GEOFACT_PLUGIN_PATH
PLUGIN_DIRS = {
    PLUGIN_DEMO_ID: PLUGIN_DEMO_DIR / "plugins",
    GRID_EXAMPLE_ID: GRID_PLUGINS,
}
# example id -> fixture that installs its plugins as the default registry
PLUGIN_FIXTURES = {PLUGIN_DEMO_ID: "demo_plugins", GRID_EXAMPLE_ID: "grid_plugins"}


@pytest.fixture
def grid_registry():
    """Fresh registry of the core blocks plus the domain plugin of scenario 1."""
    return discover(plugin_paths=[GRID_PLUGINS])


@pytest.fixture
def grid_plugins(grid_registry):
    """Installs that registry as the default for code without a registry parameter."""
    previous = set_default_registry(grid_registry)
    yield grid_registry
    set_default_registry(previous)


_LOOPBACK = ("127.0.0.1", "localhost", "::1")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """The acceptance runs are offline by contract: any connection that leaves
    the machine (Nominatim, Overpass, WFS, ...) fails the test loudly instead
    of making it depend on the network. Loopback stays open for the local demo
    service of the plugin example."""
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] not in _LOOPBACK:
            raise RuntimeError(
                f"network access in an offline acceptance test: {address}"
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def example_id(path: Path) -> str:
    return path.relative_to(EXAMPLES_DIR).with_suffix("").as_posix()


def example_path(example: str) -> Path:
    return (EXAMPLES_DIR / example).with_suffix(".yaml")


# =====================================================================
# Offline test data (deterministic, tiny, no network)
# =====================================================================

# Inner extents well inside the regions of the examples.
LEIPZIG_INNER = (12.30, 51.28, 12.46, 51.38)  # inside the Nominatim fixture of Leipzig
LEIPZIG_BBOX_INNER = (
    12.25,
    51.26,
    12.52,
    51.43,
)  # inside bbox 12.2292,51.2378,12.5467,51.4501
LEIPZIG_BBOX_COVER = (12.20, 51.20, 12.58, 51.48)  # covers that whole bbox
WEST_HALF = (12.30, 51.28, 12.38, 51.38)
BERLIN_BOX = (13.30, 52.45, 13.50, 52.57)  # stands in for the city boundary


def _zones(
    bounds: tuple[float, float, float, float], nx: int, ny: int
) -> gpd.GeoDataFrame:
    """nx x ny rectangular zones ('districts') covering the bounds."""
    minx, miny, maxx, maxy = bounds
    dx, dy = (maxx - minx) / nx, (maxy - miny) / ny
    geoms, names = [], []
    for i in range(nx):
        for j in range(ny):
            geoms.append(
                box(
                    minx + i * dx,
                    miny + j * dy,
                    minx + (i + 1) * dx,
                    miny + (j + 1) * dy,
                )
            )
            names.append(f"Ortsteil {i * ny + j + 1}")
    return gpd.GeoDataFrame(
        {"id": [f"z{k}" for k in range(len(geoms))], "name": names},
        geometry=geoms,
        crs="EPSG:4326",
    )


def _points(
    bounds: tuple[float, float, float, float], count: int, **tags: list[str]
) -> gpd.GeoDataFrame:
    """`count` deterministic points spread over the bounds; each tag column
    cycles through its values."""
    minx, miny, maxx, maxy = bounds
    width, height = maxx - minx, maxy - miny
    xs = [minx + (0.05 + 0.9 * ((k * 0.6180339887) % 1)) * width for k in range(count)]
    ys = [
        miny + (0.05 + 0.9 * ((k * 0.4142135623 + 0.25) % 1)) * height
        for k in range(count)
    ]
    data = {"id": [f"n{k}" for k in range(count)]}
    for column, values in tags.items():
        data[column] = [values[k % len(values)] for k in range(count)]
    return gpd.GeoDataFrame(data, geometry=gpd.points_from_xy(xs, ys), crs="EPSG:4326")


def _population_raster(bounds: tuple[float, float, float, float]) -> RasterLayer:
    """Count-like raster; the bounds must cover the whole region, because
    zonal_stats cannot handle a zone that lies entirely outside the raster."""
    minx, miny, maxx, maxy = bounds
    rows = cols = 80
    transform = from_origin(minx, maxy, (maxx - minx) / cols, (maxy - miny) / rows)
    data = (np.arange(rows * cols, dtype="float32").reshape(rows, cols) % 50) + 1
    return RasterLayer(
        data=data, transform=transform, crs=CRS.from_epsg(4326), path="synthetic"
    )


def _sentinel2_scene(layer) -> RasterLayer:
    """Stand-in for the STAC layer: red/nir/scl of a synthetic scene over the Chemnitz box in UTM 33
    with the cloud cells already set to nodata (what `mask_clouds: true` does)."""
    left, bottom, right, top = CITY_BOX
    rows, cols = int((top - bottom) / 100), int((right - left) / 100)
    transform = Affine(100, 0, left, 0, -100, top)
    bands = expected_bands(transform, rows, cols)
    data = np.stack([bands["red"], bands["nir"], bands["scl"]])
    data[:, np.isin(bands["scl"], [3, 8, 9, 10])] = 0
    return RasterLayer(
        data=data,
        transform=transform,
        crs=CRS.from_epsg(32633),
        path="synthetic",
        nodata=0,
        band_names=["red", "nir", "scl"],
    )


def _staedte_zones() -> gpd.GeoDataFrame:
    """Stand-in for the three city boundaries of the Staedte example: thirds of the grid-aligned
    box of the synthetic scene (tests/stac_scenes.py), so every 'city' covers whole cells."""
    left, bottom, right, top = CITY_BOX
    width = (right - left) / 3
    boxes = [(left + i * width, bottom, left + (i + 1) * width, top) for i in range(3)]
    return gpd.GeoDataFrame(
        {"name": ["Leipzig", "Dresden", "Chemnitz"]},
        geometry=[Polygon(aligned_ring(box_utm)) for box_utm in boxes],
        crs="EPSG:4326",
    )


def _berlin_green(layer) -> gpd.GeoDataFrame:
    """OSM stand-in of the Berlin example: two green areas above the 2 ha limit (one
    reaching across the city boundary) and one below it, which the filter drops."""
    return gpd.GeoDataFrame(
        {"leisure": ["park", "park", "park"]},
        geometry=[
            box(13.34, 52.48, 13.36, 52.50),
            box(13.49, 52.52, 13.52, 52.54),
            box(13.42, 52.47, 13.4205, 52.4705),
        ],
        crs="EPSG:4326",
    )


def _fixture_vector(name: str) -> Callable[[object], gpd.GeoDataFrame]:
    def loader(layer):
        return gpd.read_file(FIXTURES_DIR / name)

    return loader


def _frame(
    make: Callable[[], gpd.GeoDataFrame],
) -> Callable[[object], gpd.GeoDataFrame]:
    def loader(layer):
        return make()

    return loader


def _raster(make: Callable[[], RasterLayer]) -> Callable[[object], RasterLayer]:
    def loader(layer):
        return make()

    return loader


def _fixture_population(layer) -> RasterLayer:
    import rasterio

    with rasterio.open(FIXTURES_DIR / "population.tif") as src:
        return RasterLayer(
            data=src.read(1), transform=src.transform, crs=src.crs, path=str(src.name)
        )


def _bundeslaender(layer) -> gpd.GeoDataFrame:
    """Stand-in for the BKG WFS: 16 states with the join key 'gen' (same
    construction as tests/e2e/test_szenario4_e2e.py). Like the real WFS,
    Baden-Württemberg comes as three areas (land plus two small water parts)."""
    names = [
        "Nordrhein-Westfalen",
        "Bayern",
        "Baden-Württemberg",
        "Niedersachsen",
        "Hessen",
        "Rheinland-Pfalz",
        "Berlin",
        "Sachsen",
        "Schleswig-Holstein",
        "Hamburg",
        "Brandenburg",
        "Sachsen-Anhalt",
        "Thüringen",
        "Mecklenburg-Vorpommern",
        "Saarland",
        "Bremen",
    ]
    geoms = [box(6.0 + i * 0.4, 50.0, 6.3 + i * 0.4, 50.3) for i in range(len(names))]
    extra = [box(6.8, 50.4, 6.85, 50.45), box(6.9, 50.4, 6.95, 50.45)]
    return gpd.GeoDataFrame(
        {"gen": names + ["Baden-Württemberg"] * 2},
        geometry=geoms + extra,
        crs="EPSG:4326",
    )


def _bundeslaender_nuts(layer) -> gpd.GeoDataFrame:
    """Stand-in for the BKG WFS with the join key 'nuts' (NUTS 1, three characters):
    16 states, Baden-Württemberg (DE1) again as three areas."""
    codes = [f"DE{c}" for c in "123456789ABCDEFG"]
    geoms = [box(6.0 + i * 0.4, 50.0, 6.3 + i * 0.4, 50.3) for i in range(len(codes))]
    extra = [box(6.8, 50.4, 6.85, 50.45), box(6.9, 50.4, 6.95, 50.45)]
    return gpd.GeoDataFrame(
        {"nuts": codes + ["DE1"] * 2}, geometry=geoms + extra, crs="EPSG:4326"
    )


def _kreise_nuts(layer) -> gpd.GeoDataFrame:
    """Stand-in for the BKG WFS of the districts: the 13 Saxon districts (sn_l 14,
    both 'Leipzig' included, Dresden with a second small area) plus two districts
    of neighbouring states that lie in the bounding box of the region."""
    rows = [
        ("Chemnitz", "DED41"),
        ("Erzgebirgskreis", "DED42"),
        ("Mittelsachsen", "DED43"),
        ("Vogtlandkreis", "DED44"),
        ("Zwickau", "DED45"),
        ("Dresden", "DED21"),
        ("Bautzen", "DED2C"),
        ("Görlitz", "DED2D"),
        ("Meißen", "DED2E"),
        ("Sächsische Schweiz-Osterzgebirge", "DED2F"),
        ("Leipzig", "DED51"),
        ("Leipzig", "DED52"),
        ("Nordsachsen", "DED53"),
    ]
    frame = gpd.GeoDataFrame(
        {
            "gen": [name for name, _ in rows]
            + ["Dresden", "Altenburger Land", "Elbe-Elster"],
            "nuts": [nuts for _, nuts in rows] + ["DED21", "DEG0M", "DE407"],
            "sn_l": ["14"] * (len(rows) + 1) + ["16", "12"],
        },
        geometry=[
            box(12.0 + i * 0.15, 50.5, 12.1 + i * 0.15, 50.6) for i in range(len(rows))
        ]
        + [
            box(12.0, 50.7, 12.02, 50.72),
            box(12.2, 50.9, 12.3, 51.0),
            box(13.3, 51.5, 13.4, 51.6),
        ],
        crs="EPSG:4326",
    )
    return frame


def _europa_overrides() -> dict[str, Callable]:
    """Stand-ins for the PyPSA-Eur tables and GHS-POP of examples/europa (FA76/FA77):
    seven substations in central Germany, nine buses (two stations with two voltage
    levels, one HVDC pair), lines, transformers, an HVDC link and two converters -
    together one connected network. Columns come as strings, like the table
    connector reads them."""
    stations = {  # station tag -> (lon, lat)
        "S1": (9.0, 50.0),
        "S2": (10.0, 50.2),
        "S3": (11.0, 50.0),
        "S4": (10.5, 49.5),
        "S5": (9.0, 51.0),
        "S6": (11.0, 51.0),
        "S7": (12.0, 51.2),
    }
    buses = [  # bus_id, station, voltage, dc
        ("b1-380", "S1", "380", "f"),
        ("b1-220", "S1", "220", "f"),
        ("b2-380", "S2", "380", "f"),
        ("b3-380", "S3", "380", "f"),
        ("b3-220", "S3", "220", "f"),
        ("b4-220", "S4", "220", "f"),
        ("b5-dc", "S5", "320", "t"),
        ("b6-380", "S6", "380", "f"),
        ("b7-dc", "S7", "320", "t"),
    ]

    def point(bus_id: str):
        return next(stations[s] for b, s, _, _ in buses if b == bus_id)

    def edges(pairs, with_length: bool) -> Callable[[object], gpd.GeoDataFrame]:
        def loader(layer):
            from shapely.geometry import LineString

            data = {"bus0": [a for a, _ in pairs], "bus1": [b for _, b in pairs]}
            if with_length:
                data["length"] = [str(50_000 + 1000 * k) for k in range(len(pairs))]
            return gpd.GeoDataFrame(
                data,
                geometry=[LineString([point(a), point(b)]) for a, b in pairs],
                crs="EPSG:4326",
            )

        return loader

    def bus_frame(layer) -> gpd.GeoDataFrame:
        return gpd.GeoDataFrame(
            {
                "bus_id": [b for b, *_ in buses],
                "tags": [f"relation/{s}" for _, s, _, _ in buses],
                "voltage": [v for _, _, v, _ in buses],
                "dc": [d for *_, d in buses],
                "country": ["DE"] * len(buses),
            },
            geometry=gpd.points_from_xy(*zip(*(stations[s] for _, s, _, _ in buses))),
            crs="EPSG:4326",
        )

    return {
        "sammelschienen": bus_frame,
        "leitungen": edges(
            [
                ("b1-380", "b2-380"),
                ("b2-380", "b3-380"),
                ("b3-220", "b4-220"),
                ("b1-220", "b4-220"),
                ("b2-380", "b6-380"),
            ],
            with_length=True,
        ),
        "transformatoren": edges(
            [("b1-380", "b1-220"), ("b3-380", "b3-220")], with_length=False
        ),
        "hgue_verbindungen": edges([("b5-dc", "b7-dc")], with_length=True),
        "umrichter": edges(
            [("b5-dc", "b1-380"), ("b7-dc", "b6-380")], with_length=False
        ),
        "bevoelkerung": _raster(lambda: _population_raster((7.5, 48.5, 13.5, 52.5))),
    }


def region_fetcher(name: str) -> list[dict]:
    """Nominatim stand-in for all named regions used by the examples."""
    if name == "Dresden, Deutschland":
        return json.loads(
            (FIXTURES_DIR / "nominatim_dresden.json").read_text(encoding="utf-8")
        )
    if name == "Leipzig, Deutschland":
        return json.loads(
            (FIXTURES_DIR / "nominatim_leipzig.json").read_text(encoding="utf-8")
        )
    if name == "Chemnitz, Deutschland":
        return json.loads(
            (FIXTURES_DIR / "nominatim_chemnitz.json").read_text(encoding="utf-8")
        )
    if name == "Sachsen, Deutschland":
        geometry = gpd.read_file(FIXTURES_DIR / "sachsen_region.geojson").geometry.iloc[
            0
        ]
        return [{"geojson": mapping(geometry)}]
    if name == "Berlin, Deutschland":
        return [{"geojson": mapping(box(*BERLIN_BOX))}]
    return []


def sentinel2_region_fetcher(name: str) -> list[dict]:
    """Region stand-in of the Sentinel-2 example: Chemnitz as a rectangle on the grid of the
    synthetic scene (tests/stac_scenes.py), so the city-wide share covers whole cells. The other
    Chemnitz example (routing) uses the Nominatim fixture of `region_fetcher`."""
    if name == "Chemnitz, Deutschland":
        return [{"geojson": mapping(Polygon(aligned_ring(CITY_BOX)))}]
    return region_fetcher(name)


# example id -> region stand-in, where the shared `region_fetcher` does not fit
REGION_FETCHERS: dict[str, Callable[[str], list[dict]]] = {
    "chemnitz_gruenflaechenanteil_sentinel2": sentinel2_region_fetcher,
    "chemnitz_datenquellen_lizenzen": sentinel2_region_fetcher,  # W4-02
    # W4-04: the 80 cities and Germany as small rectangles
    "deutschland_gruenste_grossstadt_osm": gruenste_grossstadt_doubles.region_fetcher,
}


def _places(*places: tuple[str, float, float]) -> Callable[[object], gpd.GeoDataFrame]:
    """Stand-in for the Nominatim geocoder: named points (name, lon, lat) in WGS84."""

    def loader(layer):
        return gpd.GeoDataFrame(
            {
                "name": [name for name, _, _ in places],
                "display_name": [name for name, _, _ in places],
            },
            geometry=gpd.points_from_xy(
                [lon for _, lon, _ in places], [lat for _, _, lat in places]
            ),
            crs="EPSG:4326",
        )

    return loader


# example id -> connector overrides (layer id -> loader). Layers that are not
# listed load for real (local files, region layers), e.g. the BIP table.
OFFLINE_SETUPS: dict[str, Callable[[], dict[str, Callable]]] = {
    "szenario2_gruenflaechen": lambda: {
        "parks": _fixture_vector("parks.geojson"),
        "buildings": _fixture_vector("residential_buildings.geojson"),
    },
    "szenario3_gruenflaechen_bevoelkerung": lambda: {
        "parks": _fixture_vector("parks.geojson"),
        "population": _fixture_population,
    },
    "szenario4_bip_bundeslaender": lambda: {"bundeslaender": _bundeslaender},
    # Scenario 2 of the thesis: one OSM layer per definition of green (module instances)
    "berlin_gruenflaechen_erreichbarkeit": lambda: {
        "def[parks_wald].gruen": _berlin_green,
        "def[mit_friedhof_kleingarten].gruen": _berlin_green,
        "population": _raster(lambda: _population_raster((13.2, 52.4, 13.6, 52.6))),
    },
    "deutschland_einkommen_erwerbslosigkeit": lambda: {
        "bundeslaender": _bundeslaender_nuts
    },
    "sachsen_bip_je_einwohner_kreise": lambda: {"kreise": _kreise_nuts},
    "chemnitz_gruenflaechenanteil_sentinel2": lambda: {
        "stadtteile": _frame(lambda: _zones((12.75, 50.76, 13.03, 50.88), 3, 3)),
        "sentinel2": _sentinel2_scene,
    },
    # FA46: region is a bbox literal (no geocoder); the synthetic scene stands in for the
    # mosaic of four tiles at 20 m, three grid-aligned rectangles for the OSM city boundaries
    "sachsen_staedte_gruenanteil_sentinel2": lambda: {
        "staedte": _frame(_staedte_zones),
        "sentinel2": _sentinel2_scene,
    },
    "leipzig/leipzig1_aerzte_kategorien": lambda: {
        "aerzte": _frame(
            lambda: _points(
                LEIPZIG_INNER,
                9,
                **{"healthcare:speciality": ["general", "dentistry", "cardiology"]},
            )
        ),
        "ortsteile": _frame(lambda: _zones(LEIPZIG_INNER, 2, 2)),
    },
    "leipzig/leipzig2_apotheken_luecken": lambda: {
        "apotheken": _frame(lambda: _points(WEST_HALF, 4)),  # east zones stay empty
        "ortsteile": _frame(lambda: _zones(LEIPZIG_INNER, 2, 2)),
    },
    "leipzig/leipzig3_supermarkt_dichte": lambda: {
        "supermaerkte": _frame(
            lambda: _points(LEIPZIG_INNER, 14, shop=["supermarket", "convenience"])
        ),
    },
    "leipzig/leipzig4_oepnv_abdeckung": lambda: {
        "haltestellen": _frame(lambda: _points(LEIPZIG_INNER, 6, highway=["bus_stop"])),
        "wohnflaechen": _frame(lambda: _zones(LEIPZIG_INNER, 1, 2)),
    },
    "leipzig/leipzig5_gruen_erreichbarkeit": lambda: {
        "gruenflaechen": _frame(lambda: _zones((12.32, 51.30, 12.36, 51.34), 2, 1)),
        "wohnflaechen": _frame(lambda: _zones(LEIPZIG_INNER, 1, 2)),
    },
    "leipzig/leipzig6_spielplaetze": lambda: {
        "spielplaetze": _fixture_vector("leipzig_playgrounds_osm.geojson"),
        "ortsteile": _fixture_vector("leipzig_ortsteile_polygon.geojson"),
    },
    "leipzig/leipzig7_ladesaeulen_dichte": lambda: {
        "ladesaeulen": _frame(
            lambda: _points(LEIPZIG_INNER, 10, amenity=["charging_station"])
        ),
    },
    "leipzig/leipzig8_feuerwehr_luecken": lambda: {
        "feuerwachen": _frame(lambda: _points(WEST_HALF, 3)),
        "ortsteile": _frame(lambda: _zones(LEIPZIG_INNER, 2, 2)),
    },
    "leipzig/leipzig9_kliniknaehe": lambda: {
        "ortsteile": _frame(lambda: _zones(LEIPZIG_INNER, 2, 2)),
        "kliniken": _frame(lambda: _points(LEIPZIG_INNER, 2, amenity=["hospital"])),
    },
    "leipzig/leipzig10_erreichbarkeit": lambda: {
        "tramgleise": _fixture_vector("leipzig_tram_lines.geojson"),
        # Stand-ins as in tests/e2e/test_leipzig10_e2e.py: only the pipeline
        # wiring counts, not geometric realism.
        "sbahn_gleise": _fixture_vector("leipzig_tram_lines.geojson"),
        "bus_liniennetz": _fixture_vector("leipzig_tram_lines.geojson"),
        "bahnhoefe": _fixture_vector("leipzig_hbf.geojson"),
    },
    "leipzig/leipzig11_opendata_vergleich": lambda: {
        "baeume_amtlich": _fixture_vector("wfs_getfeature_response.json"),
        "baeume_osm": _fixture_vector("leipzig_playgrounds_osm.geojson"),
        "ortsteile": _fixture_vector("leipzig_ortsteile_polygon.geojson"),
    },
    "leipzig/leipzig12_baeume_ortsteile_amtlich": lambda: {
        "baeume_amtlich": _frame(lambda: _points(LEIPZIG_BBOX_INNER, 12)),
        "ortsteile_amtlich": _frame(lambda: _zones(LEIPZIG_BBOX_INNER, 2, 2)),
    },
    "leipzig/leipzig13_busnetz_dichte": lambda: {
        "bus_liniennetz": _fixture_vector("leipzig_tram_lines.geojson"),
        "bevoelkerung": _raster(lambda: _population_raster(LEIPZIG_BBOX_COVER)),
    },
    # FA50-FA53: street grid between the main station and the TU (with a roundabout
    # polygon, tests/fixtures/make_street_grid_fixture.py) and the two geocoded places.
    "chemnitz_route_hbf_tu": lambda: {
        "strassen": _fixture_vector("chemnitz_street_grid.geojson"),
        "start": _places(("Chemnitz Hauptbahnhof", 12.9306, 50.8396)),
        "ziel": _places(("Reichenhainer Straße 70, Chemnitz", 12.9295, 50.8158)),
    },
    # FA12: night-sky example; synthetic viewpoints, parking lots, night-light raster and
    # elevation plane (tests/nachthimmel_scenes.py) stand in for OSM and the two git-ignored
    # raster downloads.
    "sachsen_nachthimmel": nachthimmel_overrides,
    # W4-01 (FA68-FA70): 3 x 3 districts and eight green areas with one duplicate name and
    # an overlap (tests/chemnitz_gruen_scenes.py) stand in for the two OSM layers
    "chemnitz_gruenanteil_osm": gruen_overrides,
    # W4-02 (FA65): licence showcase; the two inline point layers load for real
    "chemnitz_datenquellen_lizenzen": lambda: {
        "stadtteile": _frame(lambda: _zones((12.75, 50.76, 13.03, 50.88), 3, 3)),
        "sentinel2": _sentinel2_scene,
    },
    # W4-03: FA55-FA58 showcase; local files in three CRS and a bbox region - nothing to override
    "crs_showcase/drei_crs_eine_stadt": lambda: {},
    # W4-04/FA75: module instance over 80 cities; two or three green polygons per city in its own region,
    # the boundaries (source: region) load through the region double
    "deutschland_gruenste_grossstadt_osm": gruenste_grossstadt_doubles.overrides,
    # FA76/FA77: PyPSA-Eur network and GHS-POP (git-ignored, ~6.6 GB) replaced by a
    # synthetic network of seven substations and a small count raster
    "europa/europa_netz_resilienz": _europa_overrides,
}

# Examples that cannot run offline, with the reason. Empty today: all of them run
# offline. Anything added later must go here or into OFFLINE_SETUPS.
LIVE_ONLY: dict[str, str] = {}

# Result fingerprint per example and output source on the synthetic data above:
# (row count or (lo, hi) range, sorted attribute columns without geometry).
# The restructuring must not change results (UMBAU_SPEC section 1.5); a change
# here needs an explicit, documented reason in the stage that causes it. The
# two hex-grid examples get a +-2 % row range (cells on the region border
# depend on floating point geometry).
EXPECTED_RESULTS: dict[str, dict[str, tuple[int | tuple[int, int], list[str]]]] = {
    "szenario2_gruenflaechen": {"classified": (4, ["class", "distance", "id"])},
    "szenario3_gruenflaechen_bevoelkerung": {
        "pop_sehr_gut": (2, ["id", "sum_value"]),
        "pop_gut": (2, ["id", "sum_value"]),
        "pop_maessig": (1, ["id", "sum_value"]),
    },
    "berlin_gruenflaechen_erreichbarkeit": {
        "vergleich": (
            2,
            [
                "anteil_300",
                "anteil_500",
                "definition",
                "einwohner",
                "name",
                "sum_ew_300",
                "sum_ew_300_500",
            ],
        ),
        "def[parks_wald].gross": (2, ["area_ha", "id"]),
        "pop_city": (1, ["einwohner", "name"]),
    },
    "szenario4_bip_bundeslaender": {
        "laender": (16, ["gen"]),
        "top5": (5, ["bip_mio_eur", "gen", "rang"]),
    },
    "deutschland_einkommen_erwerbslosigkeit": {
        "rangfolge": (
            16,
            [
                "einkommen_eur_je_einwohner",
                "erwerbslosenquote_prozent",
                "nuts",
                "wirtschaftslage",
            ],
        )
    },
    "sachsen_bip_je_einwohner_kreise": {
        "mit_bip": (13, ["bip_eur_je_einwohner", "bip_mio_eur", "gen", "nuts", "sn_l"]),
        "top3": (
            3,
            ["bip_eur_je_einwohner", "bip_mio_eur", "gen", "nuts", "rang", "sn_l"],
        ),
    },
    "chemnitz_gruenflaechenanteil_sentinel2": {
        "anteil_stadtteile": (9, ["gruenanteil", "id", "name"]),
        "anteil_stadt": (1, ["gruenanteil", "name"]),
    },
    "sachsen_staedte_gruenanteil_sentinel2": {"anteil": (3, ["gruenanteil", "name"])},
    "leipzig/leipzig1_aerzte_kategorien": {
        "aerzte_je_ortsteil": (
            4,
            [
                "count",
                "count_cardiology",
                "count_dentistry",
                "count_general",
                "id",
                "name",
            ],
        )
    },
    "leipzig/leipzig2_apotheken_luecken": {
        "ohne_apotheke": (2, ["count", "id", "name"])
    },
    "leipzig/leipzig3_supermarkt_dichte": {"dichte": (14, ["count"])},
    "leipzig/leipzig4_oepnv_abdeckung": {"unversorgt": (2, ["id", "name"])},
    "leipzig/leipzig5_gruen_erreichbarkeit": {"unversorgt": (2, ["id", "name"])},
    "leipzig/leipzig6_spielplaetze": {"stufen": (1, ["class", "count", "id"])},
    "leipzig/leipzig7_ladesaeulen_dichte": {"dichte": (10, ["count"])},
    "leipzig/leipzig8_feuerwehr_luecken": {"ohne_wache": (2, ["count", "id", "name"])},
    "leipzig/leipzig9_kliniknaehe": {
        "stufen": (4, ["class", "distance", "id", "name"])
    },
    "leipzig/leipzig10_erreichbarkeit": {
        "anbindung_zellen": (
            (342, 356),
            [
                "index_right",
                "network_distance",
                "q_left",
                "q_right",
                "r_left",
                "r_right",
            ],
        ),
        "einzugsbereiche": (3, ["break_m"]),
    },
    "leipzig/leipzig11_opendata_vergleich": {
        "amtlich_je_ortsteil": (1, ["count", "id"]),
        "osm_je_ortsteil": (1, ["count", "id"]),
    },
    "leipzig/leipzig12_baeume_ortsteile_amtlich": {
        "baeume_je_ortsteil": (4, ["count", "id", "name"])
    },
    "leipzig/leipzig13_busnetz_dichte": {
        "dichteklassen": ((590, 614), ["class", "count", "q", "r"]),
        "einwohner_je_zelle": ((590, 614), ["class", "count", "q", "r", "sum_value"]),
    },
    "chemnitz_route_hbf_tu": {
        "route": (
            1,
            [
                "destination",
                "destination_snap_m",
                "detour_factor",
                "length_m",
                "origin",
                "origin_snap_m",
                "straight_line_m",
                "travel_time_min",
            ],
        )
    },
    "sachsen_nachthimmel": {
        "beste": (
            10,
            ["distance", "id", "max_value", "mean_value", "name", "rang", "tourism"],
        )
    },
    # W4-01
    "chemnitz_gruenanteil_osm": {
        "kontrolle": (
            9,
            ["anteil_kontrolle", "area_m2", "gruen_m2", "gruenanteil", "id", "name"],
        ),
        "rangliste": (5, ["area_ha", "landuse", "leisure", "name", "natural", "rang"]),
    },
    # W4-02
    "chemnitz_datenquellen_lizenzen": {
        "anteil_stadtteile": (9, ["gruenanteil", "id", "name"]),
        "punkte_im_stadtteil": (
            8,
            ["gruenanteil", "id", "index_right", "name_left", "name_right"],
        ),
    },
    # W4-03
    "crs_showcase/drei_crs_eine_stadt": {"je_stadtteil": (9, ["count", "id", "name"])},
    # W4-04
    "deutschland_gruenste_grossstadt_osm": {
        "alle_staedte": (80, ["gruen_m2", "gruenanteil", "name", "stadt_id"]),
        "rangliste": (10, ["gruen_m2", "gruenanteil", "name", "rang", "stadt_id"]),
    },
    "europa/europa_netz_resilienz": {
        "ergebnis": (
            7,
            [
                "betweenness",
                "country",
                "einwohner_20km",
                "einwohner_log",
                "id",
                "is_articulation_point",
                "rang",
                "risiko",
                "tags",
                "voltage",
            ],
        )
    },
}


# Fingerprint of scenario 1 of the thesis, which runs with its own registry (FA94).
GRID_EXPECTED: dict[str, tuple[int | tuple[int, int], list[str]]] = {
    "critical": (
        3,
        [
            "betweenness",
            "component",
            "id",
            "is_articulation_point",
            "kind",
            "rang",
            "risk_score",
            "sum_value",
            "voltage",
            "voltage_max",
        ],
    )
}

# =====================================================================
# The adapter to the entry point under test
# =====================================================================


def _execute(
    example: Path, overrides: dict[str, Callable], out_dir: Path, registry=None
) -> tuple[Scenario, ExecutionResult, object]:
    """Run an example like the delivery code does and write its outputs
    (FA44: api.load_scenario + api.run, base_dir = the YAML directory).
    registry: a registry of its own (plugin demo); None = the session registry."""
    document = api.load_scenario(example, registry=registry)
    assert document.base_dir == example.resolve().parent
    report = api.run(
        document,
        out_dir=out_dir,
        connector_override=overrides,
        region_fetcher=REGION_FETCHERS.get(example_id(example), region_fetcher),
        registry=registry,
    )
    assert report.outputs is not None
    return document.scenario, report.result, report.outputs


def _assert_run_complete(
    scenario: Scenario,
    result: ExecutionResult,
    outcome,
    out_dir: Path,
    expected: dict[str, tuple[int | tuple[int, int], list[str]]] | None = None,
) -> None:
    required = scenario.required_sources()

    # Lazy loading by design: exactly the required layers, never an unused one.
    assert set(result.loaded_layers) == set(required["layers"])
    assert not set(result.loaded_layers) & set(required["unused_layers"])

    # Every required step ran and left a result.
    assert set(result.order) == set(required["steps"])
    assert all(step_id in result.store for step_id in result.order)

    # Outputs: nothing vanishes - every declared output is written or reported skipped.
    declared = len(scenario.output)
    assert len(outcome.written) + len(outcome.skipped) == declared
    assert outcome.skipped == [], f"outputs skipped: {outcome.skipped}"
    for path in outcome.written:
        assert path.is_file() and path.stat().st_size > 0, (
            f"empty or missing output {path}"
        )
        assert out_dir in path.parents
    written_names = {path.name for path in outcome.written}
    declared_names = {Path(spec.path).name for spec in scenario.output if spec.path}
    assert declared_names <= written_names

    # The results behind the outputs are not empty ...
    for spec in scenario.output:
        produced = result.store[spec.source]
        size = (
            produced.data.size if isinstance(produced, RasterLayer) else len(produced)
        )
        assert size > 0, f"output source '{spec.source}' is empty"
    # ... and (where fingerprinted) unchanged in size and attributes.
    for source, (rows, columns) in (expected or {}).items():
        frame = result.store[source]
        count = len(frame)
        low, high = rows if isinstance(rows, tuple) else (rows, rows)
        assert low <= count <= high, f"'{source}': {count} rows, expected {rows}"
        assert sorted(frame.columns.drop(frame.geometry.name)) == columns, (
            f"'{source}' columns changed"
        )


# =====================================================================
# 1. Every example validates
# =====================================================================


def _cli(monkeypatch, *argv: object) -> int:
    """Run the real CLI entry point in-process and return its exit code."""
    monkeypatch.setattr(sys, "argv", ["geofact", *map(str, argv)])
    try:
        cli.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return 0


@pytest.mark.parametrize(
    "path", ALL_EXAMPLES, ids=[example_id(p) for p in ALL_EXAMPLES]
)
def test_fa01_example_validates(path, monkeypatch, capsys):
    if example_id(path) in PLUGIN_DIRS:
        # The plugin directory is discovered once per process, so this example
        # is validated in a fresh process with the real GEOFACT_PLUGIN_PATH.
        env = {**os.environ, PLUGIN_PATH_ENV: str(PLUGIN_DIRS[example_id(path)])}
        done = subprocess.run(
            [sys.executable, "-m", "geofact.cli", "validate", str(path)],
            capture_output=True,
            text=True,
            env=env,
        )
        assert done.returncode == 0, done.stderr
        return
    exit_code = _cli(monkeypatch, "validate", path)
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err


def test_fa44_offline_guard_blocks_external_connections():
    """The guard itself works: a connection to a non-loopback address (TEST-NET-1,
    never routed) is refused before any packet leaves."""
    with pytest.raises(RuntimeError, match="offline acceptance test"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)


def test_fa44_examples_are_found():
    """Guard against an empty parametrisation (wrong examples path)."""
    assert len(ALL_EXAMPLES) >= 18


def test_fa44_every_offline_example_has_a_result_fingerprint():
    assert set(EXPECTED_RESULTS) == set(OFFLINE_SETUPS)


def test_fa44_every_example_is_classified():
    """No example without an offline setup or a LIVE_ONLY reason."""
    known = set(OFFLINE_SETUPS) | set(LIVE_ONLY) | set(PLUGIN_DIRS)
    found = {example_id(p) for p in ALL_EXAMPLES}
    assert found == known, (
        f"examples without setup: {sorted(found - known)}; "
        f"setups without example: {sorted(known - found)}"
    )


# =====================================================================
# 2. Every example runs offline and its outputs are accounted for
# =====================================================================


@pytest.mark.parametrize("example", sorted(OFFLINE_SETUPS))
def test_fa44_example_runs_offline_and_accounts_for_every_output(
    example, tmp_path, monkeypatch
):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    out_dir = tmp_path / "out"
    scenario, result, outcome = _execute(
        example_path(example), OFFLINE_SETUPS[example](), out_dir
    )
    _assert_run_complete(scenario, result, outcome, out_dir, EXPECTED_RESULTS[example])


def test_fa44_plugin_demo_runs_offline_and_accounts_for_every_output(
    demo_registry,  # noqa: F811 - fixtures imported above
    demo_service,  # noqa: F811
    tmp_path,
):
    out_dir = tmp_path / "out"
    scenario, result, outcome = _execute(
        example_path(PLUGIN_DEMO_ID), {}, out_dir, registry=demo_registry
    )
    _assert_run_complete(scenario, result, outcome, out_dir)
    assert sorted(p.suffix for p in outcome.written) == [".geojson", ".html", ".kml"]


def test_fa94_grid_example_runs_offline_with_its_domain_plugin(
    grid_registry, tmp_path, monkeypatch
):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    out_dir = tmp_path / "out"
    overrides = {
        "substations": _fixture_vector("substations.geojson"),
        # the fixture lines carry no voltage; the plugin needs one per line
        "power_lines": lambda layer: gpd.read_file(
            FIXTURES_DIR / "power_lines.geojson"
        ).assign(voltage="110000"),
        "population": _fixture_population,
    }
    scenario, result, outcome = _execute(
        example_path(GRID_EXAMPLE_ID), overrides, out_dir, registry=grid_registry
    )
    _assert_run_complete(scenario, result, outcome, out_dir, GRID_EXPECTED)
    assert sorted(p.suffix for p in outcome.written) == [".geojson", ".html", ".ttl"]
    assert set(result.store["critical"]["kind"]) == {"substation"}


# =====================================================================
# 3. The CLI writes the declared outputs; one path rule for CLI and web
# =====================================================================


def _write_cli_scenario(directory: Path) -> Path:
    """A self-contained scenario (file layer from a fixture, three outputs)
    that needs no connector override, so the real CLI can run it."""
    config = {
        "scenario": {"name": "CLI outputs", "region": "13.60,51.01,13.94,51.15"},
        "layers": [
            {
                "id": "umspannwerke",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "substations.geojson"),
            }
        ],
        "steps": [
            {
                "id": "einzug",
                "op": "buffer",
                "inputs": {"geometry": "umspannwerke"},
                "params": {"radius_km": 1},
            }
        ],
        "output": [
            {"type": "geojson", "source": "einzug", "path": "einzug.geojson"},
            {"type": "csv", "source": "einzug", "path": "einzug.csv"},
            {"type": "map", "source": "einzug"},
        ],
    }
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "szenario_cli.yaml"
    target.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return target


def _assert_cli_outputs_written(out_dir: Path) -> None:
    for name in ("einzug.geojson", "einzug.csv"):
        target = out_dir / name
        assert target.is_file() and target.stat().st_size > 0, (
            f"{name} was not written to {out_dir}"
        )
    assert list(out_dir.glob("*.html")), f"no map written to {out_dir}"


def test_fa44_cli_run_writes_declared_outputs_to_out_dir(tmp_path, monkeypatch):
    config = _write_cli_scenario(tmp_path / "config")
    out_dir = tmp_path / "ergebnis"
    exit_code = _cli(monkeypatch, "run", config, "--out", out_dir)
    assert exit_code == 0
    _assert_cli_outputs_written(out_dir)


def test_fa44_cli_run_default_out_dir_is_output_yaml_stem_below_cwd(
    tmp_path, monkeypatch
):
    config = _write_cli_scenario(tmp_path / "config")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    exit_code = _cli(monkeypatch, "run", config)
    assert exit_code == 0
    _assert_cli_outputs_written(work / "output" / "szenario_cli")


# --- one path rule: relative layer paths resolve against the YAML directory ---

GITIGNORED_DATA_DIR = (EXAMPLES_DIR / "data").resolve()


def _relative_layer_paths(path: Path) -> list[tuple[str, str]]:
    """(layer id, declared path) of all file/table layers with a relative,
    local path."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    declared = []
    # FA75: layers of modules count as well; the top-level list may be omitted
    module_layers = [
        layer
        for module in (raw.get("modules") or {}).values()
        for layer in module.get("layers", [])
    ]
    for layer in [*(raw.get("layers") or []), *module_layers]:
        value = layer.get("path")
        if (
            layer.get("source") in ("file", "table")
            and value
            and "://" not in value
            and "${" not in value
            and not Path(value).is_absolute()
        ):
            declared.append((layer["id"], value))
    return declared


def _resolves_like_cli(path: Path, declared: str) -> bool:
    """CLI rule (FA4): relative to the YAML directory. The big downloads live
    in the git-ignored examples/data/ and may be absent, so for them the
    location alone is checked."""
    resolved = (path.parent / declared).resolve()
    return resolved.exists() or resolved.is_relative_to(GITIGNORED_DATA_DIR)


# Examples that were written repo-relative ('examples/data/...') and are now
# YAML-relative (B step 6): the rule must keep holding for exactly these.
_MIGRATED_EXAMPLES = {
    "szenario3_gruenflaechen_bevoelkerung",
    "leipzig/leipzig13_busnetz_dichte",
}

_PATH_RULE_EXAMPLES = [p for p in ALL_EXAMPLES if _relative_layer_paths(p)]


@pytest.mark.parametrize(
    "path", _PATH_RULE_EXAMPLES, ids=[example_id(p) for p in _PATH_RULE_EXAMPLES]
)
def test_fa44_relative_layer_paths_are_yaml_relative(path):
    bad = [
        (layer_id, declared)
        for layer_id, declared in _relative_layer_paths(path)
        if not _resolves_like_cli(path, declared)
    ]
    assert not bad, (
        f"{example_id(path)}: paths do not resolve against the YAML directory: {bad}"
    )


def test_fa44_path_rule_examples_are_found():
    """Guard: the parametrisation above must not be empty (szenario4, plugin demo, ...)."""
    assert len(_PATH_RULE_EXAMPLES) >= 5
    assert _MIGRATED_EXAMPLES <= {example_id(p) for p in _PATH_RULE_EXAMPLES}


@pytest.mark.parametrize(
    "example", sorted({example_id(p) for p in _PATH_RULE_EXAMPLES})
)
def test_fa44_web_resolves_relative_paths_like_the_cli(example, request, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from geofact_web import run_manager as run_manager_module
    from geofact_web.main import create_app

    if example in PLUGIN_FIXTURES:
        # validation needs the plugin registered
        request.getfixturevalue(PLUGIN_FIXTURES[example])

    seen: dict[str, object] = {}

    def fake_run(document, **kwargs):
        seen["base_dir"] = document.base_dir
        return api.RunReport(
            result=ExecutionResult(store=LayerStore(), order=[], loaded_layers=[])
        )

    monkeypatch.setattr(run_manager_module.api, "run", fake_run)

    client = TestClient(create_app())
    loaded = client.get(f"/api/scenarios/examples/{example}")
    assert loaded.status_code == 200, loaded.text
    created = client.post(
        "/api/runs", json={"config_yaml": loaded.json()["config_yaml"]}
    )
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    deadline = time.monotonic() + 30
    while client.get(f"/api/runs/{run_id}").json()["status"] not in (
        "done",
        "error",
        "cancelled",
    ):
        assert time.monotonic() < deadline, "web run did not finish"
        time.sleep(0.05)

    assert seen.get("base_dir") is not None, "the web run never reached the executor"
    assert Path(seen["base_dir"]).resolve() == example_path(example).parent.resolve()


# --- lossless scenario round trip ---


@pytest.mark.parametrize(
    "path", ALL_EXAMPLES, ids=[example_id(p) for p in ALL_EXAMPLES]
)
def test_fa02_scenario_round_trips_losslessly(path, request):
    if example_id(path) in PLUGIN_FIXTURES:
        request.getfixturevalue(PLUGIN_FIXTURES[example_id(path)])
    scenario = Scenario(**yaml.safe_load(path.read_text(encoding="utf-8")))

    dumped = scenario.model_dump(mode="json")
    # nothing may be lost on the way out ...
    assert dumped["layers"] == [
        layer.model_dump(mode="json") for layer in scenario.layers
    ]
    assert dumped["steps"] == [step.model_dump(mode="json") for step in scenario.steps]
    # ... and the dump loads again into the same scenario
    rebuilt = Scenario(**dumped)
    assert rebuilt.model_dump(mode="json") == dumped
    assert [layer.model_dump(mode="json") for layer in rebuilt.layers] == dumped[
        "layers"
    ]
