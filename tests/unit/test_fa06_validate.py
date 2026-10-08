"""Implements: FA6 (Layer-Validator).

Contract-Tests für src/geofact/engine/validate.py.
"""

import warnings
from pathlib import Path

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point

from geofact.core.contracts import LayerValidation
from geofact.core.scenario import Scenario
from geofact.engine import validate as validate_module
from geofact.engine.executor import run_scenario
from geofact.engine.validate import ValidationAbortError

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def substations_gdf() -> gpd.GeoDataFrame:
    return gpd.read_file(FIXTURES_DIR / "substations.geojson")


def test_fa06_required_field():
    gdf = substations_gdf()  # s5 hat kein voltage (None/NaN)
    rules = LayerValidation(required_fields=["voltage"], on_violation="warn")
    result = validate_module.validate("substations", gdf, rules)
    assert any(v.rule == "required_fields" for v in result.violations)


def test_fa06_field_type():
    gdf = gpd.GeoDataFrame(
        {"id": [1, 2], "voltage": [110000, "not_a_number"]},
        geometry=[Point(13.7, 51.0), Point(13.71, 51.01)],
        crs="EPSG:4326",
    )
    rules = LayerValidation(field_types={"voltage": "integer"}, on_violation="warn")
    result = validate_module.validate("substations", gdf, rules)
    assert any(v.rule == "field_types" for v in result.violations)


def test_fa06_field_type_accepts_numeric_looking_string():
    # OSM liefert Attribute grundsätzlich als String - "110000" ist für
    # field_types=integer gültig, "not_a_number" nicht.
    gdf = gpd.GeoDataFrame(
        {"id": [1], "voltage": ["110000"]},
        geometry=[Point(13.7, 51.0)],
        crs="EPSG:4326",
    )
    rules = LayerValidation(field_types={"voltage": "integer"}, on_violation="abort")
    result = validate_module.validate("substations", gdf, rules)  # darf nicht werfen
    assert result.violations == []


def test_fa06_field_type_accepts_float_with_integer_value():
    # geopandas liest GeoJSON/GPKG-Zahlenfelder als float (110000.0),
    # auch wenn field_types=integer deklariert ist - das darf nicht als
    # Verstoß gewertet werden (realer Bug: substations.geojson wurde
    # dadurch komplett verworfen, siehe szenario1-E2E-Test).
    gdf = gpd.GeoDataFrame(
        {"id": [1], "voltage": [110000.0]},
        geometry=[Point(13.7, 51.0)],
        crs="EPSG:4326",
    )
    rules = LayerValidation(field_types={"voltage": "integer"}, on_violation="abort")
    result = validate_module.validate("substations", gdf, rules)  # darf nicht werfen
    assert result.violations == []


def test_fa06_field_type_rejects_non_integer_float():
    gdf = gpd.GeoDataFrame(
        {"id": [1], "voltage": [110000.5]},
        geometry=[Point(13.7, 51.0)],
        crs="EPSG:4326",
    )
    rules = LayerValidation(field_types={"voltage": "integer"}, on_violation="warn")
    result = validate_module.validate("substations", gdf, rules)
    assert any(v.rule == "field_types" for v in result.violations)


def test_fa06_value_range():
    gdf = substations_gdf()  # s4: voltage=5000 < min 10000
    rules = LayerValidation(
        constraints={"voltage": {"min": 10000}}, on_violation="warn"
    )
    result = validate_module.validate("substations", gdf, rules)
    assert any(v.rule == "constraints" for v in result.violations)


def test_fa06_precision_min_decimals():
    gdf = gpd.GeoDataFrame(
        {"id": ["l1", "l2"]},
        geometry=[Point(13.725123, 51.041456), Point(13.735, 51.046)],
        crs="EPSG:4326",
    )
    raw_strings = {"longitude": ["13.725123", "13.735"]}
    rules = LayerValidation(
        precision={"longitude": {"min_decimals": 4}}, on_violation="warn"
    )
    result = validate_module.validate(
        "ladesaeulen", gdf, rules, raw_strings=raw_strings
    )
    assert len(result.violations) == 1
    assert result.violations[0].index == 1  # l2, abgeschnitten auf 3 Nachkommastellen


def test_fa06_precision_without_raw_strings_raises():
    gdf = gpd.GeoDataFrame(
        {"id": ["l1"]}, geometry=[Point(13.725123, 51.041456)], crs="EPSG:4326"
    )
    rules = LayerValidation(
        precision={"longitude": {"min_decimals": 4}}, on_violation="warn"
    )
    with pytest.raises(ValueError, match="Roh-Strings"):
        validate_module.validate("ladesaeulen", gdf, rules)


def test_fa06_spatial_check_detects_swapped_lonlat():
    region = gpd.read_file(FIXTURES_DIR / "sachsen_region.geojson")
    # korrekter Punkt liegt in der Region, vertauschter (lat, lon) liegt weit außerhalb
    gdf = gpd.GeoDataFrame(
        {"id": ["ok", "swapped"]},
        geometry=[Point(13.72, 51.04), Point(51.04, 13.72)],
        crs="EPSG:4326",
    )
    rules = LayerValidation(
        spatial_check={"within": "scenario_region", "on_violation": "warn"},
        on_violation="warn",
    )
    result = validate_module.validate("substations", gdf, rules, region_geometry=region)
    assert len(result.violations) == 1
    assert result.violations[0].index == 1


def test_fa06_spatial_check_against_reference_layer():
    # Referenz-Layer: enges Polygon um s1 herum, s2 liegt außerhalb
    reference = gpd.GeoDataFrame(
        geometry=[Point(13.720, 51.040).buffer(0.01)], crs="EPSG:4326"
    )
    gdf = gpd.GeoDataFrame(
        {"id": ["s1", "s2"]},
        geometry=[Point(13.720, 51.040), Point(13.740, 51.045)],
        crs="EPSG:4326",
    )
    rules = LayerValidation(
        spatial_check={"within": "reference_layer", "on_violation": "warn"},
        on_violation="warn",
    )
    result = validate_module.validate(
        "substations", gdf, rules, region_geometry=reference
    )
    assert len(result.violations) == 1
    assert result.violations[0].index == 1


def test_fa06_on_violation_warn():
    gdf = substations_gdf()
    rules = LayerValidation(required_fields=["voltage"], on_violation="warn")
    result = validate_module.validate("substations", gdf, rules)
    assert len(result.gdf) == len(gdf)  # nichts entfernt
    assert result.violations


def test_fa06_on_violation_drop():
    gdf = substations_gdf()
    rules = LayerValidation(required_fields=["voltage"], on_violation="drop")
    result = validate_module.validate("substations", gdf, rules)
    assert len(result.gdf) == len(gdf) - 1  # s5 entfernt
    assert "s5" not in result.gdf["id"].values


def test_fa06_on_violation_abort():
    gdf = substations_gdf()
    rules = LayerValidation(required_fields=["voltage"], on_violation="abort")
    with pytest.raises(ValidationAbortError, match="substations"):
        validate_module.validate("substations", gdf, rules)


# =====================================================================
# Bericht: eine gesammelte Warnung je Regel, bei drop mit Anzahl
# =====================================================================


def numbers_gdf(values: list) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"id": [f"n{i}" for i in range(len(values))], "wert": values},
        geometry=[Point(13.72 + i * 0.001, 51.04) for i in range(len(values))],
        crs="EPSG:4326",
    )


def test_fa06_warn_reports_one_aggregated_warning_per_rule():
    gdf = numbers_gdf([1, 50, 60, None, 70, 80])
    rules = LayerValidation(
        required_fields=["wert"], constraints={"wert": {"max": 10}}, on_violation="warn"
    )
    with pytest.warns(validate_module.LayerValidationWarning) as recorded:
        result = validate_module.validate("messwerte", gdf, rules)
    texts = [str(w.message) for w in recorded]
    assert len(texts) == 2  # je Regel genau eine, nicht je Objekt
    constraints = next(t for t in texts if "'constraints'" in t)
    required = next(t for t in texts if "'required_fields'" in t)
    assert "Layer 'messwerte'" in constraints and "4 Objekt(e)" in constraints
    assert "behalten" in constraints and "on_violation: warn" in constraints
    assert "1 Objekt(e)" in required
    assert len(result.gdf) == len(gdf)  # warn entfernt nichts


def test_fa06_report_names_at_most_five_sample_indices():
    gdf = numbers_gdf([100] * 12)
    rules = LayerValidation(constraints={"wert": {"max": 10}}, on_violation="warn")
    with pytest.warns(
        validate_module.LayerValidationWarning, match="12 Objekt"
    ) as recorded:
        validate_module.validate("messwerte", gdf, rules)
    (warning,) = recorded
    sample = str(warning.message).split("(Index ")[1].split(";")[0]
    assert sample == "0, 1, 2, 3, 4, ..."


def test_fa06_drop_reports_how_many_objects_were_removed():
    gdf = numbers_gdf([1, 50, 60, 2])
    rules = LayerValidation(constraints={"wert": {"max": 10}}, on_violation="drop")
    with pytest.warns(validate_module.LayerValidationWarning) as recorded:
        result = validate_module.validate("messwerte", gdf, rules)
    texts = [str(w.message) for w in recorded]
    assert any("2 Objekt(e) verletzen" in t and "entfernt" in t for t in texts)
    assert any("2 von 4 Objekt(en) entfernt" in t for t in texts)
    assert list(result.gdf["id"]) == ["n0", "n3"]


def test_fa06_clean_layer_emits_no_warning():
    gdf = numbers_gdf([1, 2, 3])
    rules = LayerValidation(constraints={"wert": {"max": 10}}, on_violation="drop")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = validate_module.validate("messwerte", gdf, rules)
    assert len(result.gdf) == 3 and not result.violations


def test_fa06_validation_report_arrives_as_layer_warning_event():
    """Vom Konnektor bis zum Ereignisstrom: die gesammelte Meldung eines
    Layers erreicht den Beobachter als layer_warning mit Layer und Schritt."""
    scenario = Scenario(
        **{
            "scenario": {"name": "Bericht", "region": "13.60,51.01,13.94,51.15"},
            "layers": [
                {
                    "id": "messwerte",
                    "source": "file",
                    "path": "messwerte.geojson",
                    "data_type": "vector",
                    "validation": {
                        "constraints": {"wert": {"max": 10}},
                        "on_violation": "drop",
                    },
                }
            ],
            "steps": [
                {
                    "id": "puffer",
                    "op": "buffer",
                    "inputs": {"geometry": "messwerte"},
                    "params": {"radius_km": 0.1},
                }
            ],
            "output": [{"type": "geojson", "source": "puffer"}],
        }
    )
    events = []
    result = run_scenario(
        scenario,
        connector_override={"messwerte": lambda layer: numbers_gdf([1, 50, 2])},
        on_event=events.append,
    )
    assert list(result.store["messwerte"]["id"]) == ["n0", "n2"]
    texts = [(e.layer, e.step, e.message) for e in events if e.type == "layer_warning"]
    assert [t[:2] for t in texts] == [("messwerte", "puffer")] * 2
    assert any("1 von 3 Objekt(en) entfernt" in t[2] for t in texts)
    assert [w.layer for w in result.warnings] == ["messwerte", "messwerte"]


# =====================================================================
# spatial_check.on_violation: überschreibt die Policy des Layers
# =====================================================================


def _mixed_violations() -> gpd.GeoDataFrame:
    # n0 ok; n1 verletzt NUR den Wertebereich; n2 liegt außerhalb der
    # Region UND verletzt den Wertebereich
    return gpd.GeoDataFrame(
        {"id": ["n0", "n1", "n2"], "wert": [1, 50, 60]},
        geometry=[Point(13.72, 51.04), Point(13.73, 51.04), Point(51.04, 13.72)],
        crs="EPSG:4326",
    )


def _region() -> gpd.GeoDataFrame:
    return gpd.read_file(FIXTURES_DIR / "sachsen_region.geojson")


def test_fa06_spatial_check_on_violation_drop_overrides_layer_warn():
    rules = LayerValidation(
        constraints={"wert": {"max": 10}},
        spatial_check={"within": "scenario_region", "on_violation": "drop"},
        on_violation="warn",
    )
    with pytest.warns(validate_module.LayerValidationWarning):
        result = validate_module.validate(
            "l", _mixed_violations(), rules, region_geometry=_region()
        )
    # n2 (außerhalb) entfernt; n1 (nur Wertebereich, Layer-Policy warn) bleibt
    assert list(result.gdf["id"]) == ["n0", "n1"]


def test_fa06_spatial_check_on_violation_warn_overrides_layer_drop():
    rules = LayerValidation(
        constraints={"wert": {"max": 10}},
        spatial_check={"within": "scenario_region", "on_violation": "warn"},
        on_violation="drop",
    )
    with pytest.warns(validate_module.LayerValidationWarning):
        result = validate_module.validate(
            "l", _mixed_violations(), rules, region_geometry=_region()
        )
    # Wertebereich folgt dem Layer (drop): n1 und n2 weg; die räumliche
    # Verletzung von n2 allein hätte n2 nur markiert
    assert list(result.gdf["id"]) == ["n0"]


def test_fa06_spatial_check_without_on_violation_inherits_the_layer_policy():
    gdf = _mixed_violations()
    rules = LayerValidation(
        spatial_check={"within": "scenario_region"}, on_violation="drop"
    )
    assert rules.spatial_check.on_violation is None
    with pytest.warns(validate_module.LayerValidationWarning):
        result = validate_module.validate("l", gdf, rules, region_geometry=_region())
    assert list(result.gdf["id"]) == ["n0", "n1"]


def test_fa06_spatial_check_on_violation_abort_overrides_layer_warn():
    rules = LayerValidation(
        spatial_check={"within": "scenario_region", "on_violation": "abort"},
        on_violation="warn",
    )
    with pytest.raises(ValidationAbortError, match="spatial_check"):
        validate_module.validate(
            "l", _mixed_violations(), rules, region_geometry=_region()
        )


# =====================================================================
# Regeln, die nie greifen könnten, werden bei der Konfigurationsprüfung abgelehnt
# =====================================================================


def _scenario_with_layer(layer: dict) -> Scenario:
    return Scenario(
        **{
            "scenario": {"name": "Regeln", "region": "13.60,51.01,13.94,51.15"},
            "layers": [layer],
            "steps": [
                {
                    "id": "puffer",
                    "op": "buffer",
                    "inputs": {"geometry": layer["id"]},
                    "params": {"radius_km": 1},
                }
            ]
            if layer["data_type"] == "vector"
            else [
                {
                    "id": "stat",
                    "op": "zonal_stats",
                    "inputs": {"zones": "zonen", "values": layer["id"]},
                    "params": {"stat": "sum"},
                }
            ],
            "output": [{"type": "geojson", "source": "puffer"}]
            if layer["data_type"] == "vector"
            else [{"type": "geojson", "source": "stat"}],
        }
    )


def test_fa06_precision_rule_is_rejected_at_validation():
    """Keine precision-Regel kann ausgeführt werden (kein Konnektor liefert die
    Roh-Strings) - sie würde erst zur Laufzeit mit einem ValueError scheitern."""
    with pytest.raises(ValidationError, match="precision.*nicht unterstützt"):
        _scenario_with_layer(
            {
                "id": "punkte",
                "source": "file",
                "path": "p.geojson",
                "data_type": "vector",
                "validation": {"precision": {"longitude": {"min_decimals": 4}}},
            }
        )


def test_fa06_validation_block_on_a_raster_layer_is_rejected():
    with pytest.raises(ValidationError, match="nur für Vektor-Layer"):
        Scenario(
            **{
                "scenario": {"name": "Raster", "region": "13.60,51.01,13.94,51.15"},
                "layers": [
                    {
                        "id": "zonen",
                        "source": "file",
                        "path": "z.geojson",
                        "data_type": "vector",
                    },
                    {
                        "id": "pop",
                        "source": "file",
                        "path": "pop.tif",
                        "data_type": "raster",
                        "format": "tif",
                        "validation": {"constraints": {"x": {"min": 0}}},
                    },
                ],
                "steps": [
                    {
                        "id": "stat",
                        "op": "zonal_stats",
                        "inputs": {"zones": "zonen", "values": "pop"},
                        "params": {"stat": "sum"},
                    }
                ],
                "output": [{"type": "geojson", "source": "stat"}],
            }
        )


def test_fa06_valid_vector_validation_is_still_accepted():
    scenario = _scenario_with_layer(
        {
            "id": "punkte",
            "source": "file",
            "path": "p.geojson",
            "data_type": "vector",
            "validation": {
                "constraints": {"x": {"min": 0}},
                "spatial_check": {"within": "scenario_region", "on_violation": "drop"},
            },
        }
    )
    assert scenario.layers[0].validation.spatial_check.on_violation == "drop"
