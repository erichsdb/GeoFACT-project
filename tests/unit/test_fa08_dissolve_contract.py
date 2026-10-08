"""Implements: FA8 (dissolve: Vertrag und Verhalten stimmen überein).

Regression zur LLM-Vorstudie (03.10.2026): der Katalogtext von dissolve
versprach ohne 'by' "eine einzige Geometrie", die Operation liefert aber eine
Zeile je zusammenhängender Teilfläche. Ein erzeugtes Szenario erwartete eine
Zeile und scheiterte an einem attribute_join. Entschieden: das Verhalten
bleibt (Szenario 3 und die Leipzig-Beispiele weisen Werte je Teilfläche
aus), der Vertragstext wird korrigiert. Diese Tests binden beides aneinander.
"""

from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from geofact import api
from geofact.builtin.operations.dissolve import DissolveStep, run_dissolve


def _squares(
    *origins: tuple[float, float], groups: list[str] | None = None
) -> gpd.GeoDataFrame:
    data = {"name": [f"f{i}" for i in range(len(origins))]}
    if groups is not None:
        data["group"] = groups
    return gpd.GeoDataFrame(
        data,
        geometry=[
            Polygon([(x, y), (x + 2, y), (x + 2, y + 2), (x, y + 2)])
            for x, y in origins
        ],
        crs="EPSG:32633",
    )


def _catalog_entry() -> dict:
    api.load_extensions()
    catalog = api.extension_catalog()
    operations = catalog["operations"]
    if isinstance(operations, dict):
        return operations["dissolve"]
    return next(op for op in operations if op.get("name") == "dissolve")


def test_fa08_dissolve_without_by_gives_one_row_per_connected_part():
    """Verhalten: zwei überlappende Quadrate verschmelzen, ein entferntes
    bleibt eine eigene Zeile - zwei Zeilen, nicht eine."""
    features = _squares((0, 0), (1, 1), (100, 100))
    result = run_dissolve({"features": features}, {})
    assert len(result) == 2
    assert list(result["id"]) == [0, 1]
    assert set(result.geometry.geom_type) == {"Polygon"}
    assert result.geometry.area.sum() == pytest.approx(7.0 + 4.0)
    # Teilflächen überlappen sich nicht
    assert result.geometry.iloc[0].intersection(result.geometry.iloc[1]).area == 0


def test_fa08_dissolve_with_by_gives_one_row_per_group_even_when_disjoint():
    features = _squares((0, 0), (100, 100), (200, 200), groups=["a", "a", "b"])
    result = run_dissolve({"features": features}, {"by": "group"})
    assert sorted(result["group"]) == ["a", "b"]
    assert result.set_index("group").geometry["a"].geom_type == "MultiPolygon"


def test_fa08_dissolve_contract_text_describes_one_row_per_part():
    """Vertrag: der Text, den Katalog und LLM-Prompt zeigen, beschreibt
    das Verhalten oben - keine 'einzige Geometrie' mehr."""
    doc = " ".join((DissolveStep.__doc__ or "").split())
    assert "EINE ZEILE JE ZUSAMMENHAENGENDER TEILFLAECHE" in doc
    assert "einzigen" not in doc
    by_description = DissolveStep.Params.model_fields["by"].description or ""
    assert "Teilfläche" in by_description

    entry = _catalog_entry()
    shown = " ".join(str(value) for value in entry.values())
    assert "TEILFLAECHE" in shown
    assert "zu einer einzigen" not in shown


def test_fa08_dissolve_empty_layer_stays_empty():
    features = _squares((0, 0)).iloc[0:0]
    assert len(run_dissolve({"features": features}, {})) == 0
