"""Implements: FA55 (deklarierbares Arbeits-CRS: scenario.crs, densify_km, core/crs.py).

Contract-Tests der Vertragsseite: ``parse_crs_spec``/``require_projected_metric``
in ``core/crs.py`` und die Felder ``scenario.crs``/``scenario.densify_km``
(Default ``auto``, Ablehnung mit Position ``scenario -> crs``, verlustfreier
Round-Trip aller Beispiele)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pyproj import CRS

from geofact import api
from geofact.core.crs import (
    AUTO,
    CRS_PRESETS,
    crs_label,
    parse_crs_spec,
    require_projected_metric,
)
from geofact.core.errors import ConfigError
from geofact.core.scenario import Scenario

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
PLUGIN_DEMO_DIR = EXAMPLES_DIR / "plugin_demo"
ALL_EXAMPLES = sorted(EXAMPLES_DIR.rglob("*.yaml"))

MINIMAL = """
scenario:
  name: CRS
  region: "12.2,51.2,12.5,51.4"
  {extra}
layers:
  - id: a
    source: file
    path: data/a.geojson
    data_type: vector
steps:
  - id: p
    op: buffer
    inputs: {{geometry: a}}
    params: {{radius_km: 1}}
output:
  - type: geojson
    source: p
"""


def _doc(extra: str = ""):
    return api.load_scenario(MINIMAL.format(extra=extra))


# ---------------------------------------------------------------- parse_crs_spec


def test_fa55_auto_means_no_declared_crs():
    assert parse_crs_spec(AUTO) is None
    assert parse_crs_spec("  auto ") is None


def test_fa55_presets_resolve_to_equal_area_crs():
    assert dict(CRS_PRESETS) == {
        "equal_area": "EPSG:6933",
        "equal_area_europe": "EPSG:3035",
    }
    assert parse_crs_spec("equal_area").to_epsg() == 6933
    assert parse_crs_spec("equal_area_europe").to_epsg() == 3035
    with pytest.raises(TypeError):
        CRS_PRESETS["x"] = "EPSG:1"  # type: ignore[index]


def test_fa55_pyproj_spec_is_accepted():
    assert parse_crs_spec("EPSG:25833").to_epsg() == 25833


def test_fa55_unknown_crs_is_rejected_with_allowed_forms():
    with pytest.raises(ValueError) as exc:
        parse_crs_spec("EPSG:999999")
    message = str(exc.value)
    assert "unbekanntes CRS 'EPSG:999999'" in message
    assert "auto, equal_area, equal_area_europe" in message


def test_fa55_geographic_crs_is_rejected():
    with pytest.raises(ValueError, match="ist ein geographisches CRS"):
        require_projected_metric(CRS.from_epsg(4326), what="EPSG:4326")


def test_fa55_non_metric_crs_is_rejected():
    with pytest.raises(ValueError, match="hat die Einheit 'US survey foot'"):
        require_projected_metric(CRS.from_epsg(2227), what="EPSG:2227")


def test_fa55_projected_metric_crs_passes():
    require_projected_metric(CRS.from_epsg(25833), what="EPSG:25833")
    require_projected_metric(CRS.from_epsg(6933), what="equal_area")


def test_fa55_crs_label_names_code_and_name():
    assert crs_label(CRS.from_epsg(25833)) == "EPSG:25833 (ETRS89 / UTM zone 33N)"
    assert crs_label(None) == "ohne CRS"


# ---------------------------------------------------------------- scenario.crs


def test_fa55_scenario_crs_defaults_to_auto_in_the_dump():
    scenario = _doc().scenario
    assert scenario.scenario.crs == "auto"
    assert scenario.scenario.densify_km is None
    dumped = scenario.model_dump(mode="json")
    assert dumped["scenario"]["crs"] == "auto"
    assert Scenario(**dumped).model_dump(mode="json") == dumped


@pytest.mark.parametrize("spec", ["equal_area", "equal_area_europe", "EPSG:25833"])
def test_fa55_scenario_crs_accepts_presets_and_projected(spec):
    assert _doc(f"crs: {spec}").scenario.scenario.crs == spec


@pytest.mark.parametrize(
    ("spec", "fragment"),
    [
        ("EPSG:4326", "geographisches CRS"),
        ("EPSG:2227", "US survey foot"),
        ("EPSG:999999", "unbekanntes CRS"),
    ],
)
def test_fa55_scenario_crs_rejected_at_position(spec, fragment):
    with pytest.raises(ConfigError) as exc:
        _doc(f"crs: '{spec}'")
    locations = [location for location, _ in exc.value.issues]
    assert any(location.startswith("scenario -> crs") for location in locations), (
        exc.value.issues
    )
    assert fragment in str(exc.value)


def test_fa55_densify_km_must_be_positive():
    assert _doc("densify_km: 5").scenario.scenario.densify_km == 5.0
    with pytest.raises(ConfigError) as exc:
        _doc("densify_km: 0")
    assert any("densify_km" in location for location, _ in exc.value.issues)


def test_fa55_load_context_carries_densify_km_with_default():
    from geofact.core.contracts import LoadContext

    assert LoadContext().densify_km is None
    assert LoadContext(densify_km=2.5).densify_km == 2.5


# ---------------------------------------------------------------- Round-Trip


@pytest.fixture(scope="module")
def plugin_registry():
    # plugin demo and the domain plugin of scenario 1 (FA94)
    return api.discover(
        plugin_paths=[PLUGIN_DEMO_DIR / "plugins", EXAMPLES_DIR / "plugins"]
    )


@pytest.mark.parametrize(
    "path", ALL_EXAMPLES, ids=lambda p: p.relative_to(EXAMPLES_DIR).as_posix()
)
def test_fa55_round_trip_of_all_examples_keeps_crs_auto(path, plugin_registry):
    needs_plugins = (
        PLUGIN_DEMO_DIR in path.parents or path.stem == "sachsen_netz_resilienz"
    )
    registry = plugin_registry if needs_plugins else None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    # the 22 examples up to 01.10.2026 (26 since 02.10.2026) declare no working CRS (default auto); later
    # showcases may declare one explicitly (W4-04: EPSG:25832), which must round-trip as is
    declared = raw["scenario"].get("crs", "auto")
    doc = api.load_scenario(path, registry=registry)
    dumped = doc.scenario.model_dump(mode="json")
    assert dumped["scenario"]["crs"] == declared
    rebuilt = api.load_scenario(dumped, registry=registry).scenario
    assert rebuilt.model_dump(mode="json") == dumped
