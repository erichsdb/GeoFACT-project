"""Implements: FA34 (attribute_join), FA35 (top_n), FA37 (Tabelle ohne Geometrie), FA69 (deduplicate).

End-to-End-Test: Szenario 4 (die 5 größten Bundesländer nach BIP)
läuft komplett aus der eingecheckten YAML bis vor die Ausgabeschicht.
Der WFS-Fetch ist durch einen connector_override ersetzt (kein
Netzzugriff, deterministisch - Muster aus test_szenario2_e2e.py); die
BIP-Tabelle ist die echte, eingecheckte CSV.

Dieses Szenario ist der Nachweis für die vom Nutzer gestellte Frage
"Finde die 5 größten Bundesländer auf Basis ihres
Bruttosozialproduktes 2025": nicht-räumliche amtliche Statistik wird
über einen Gebietsnamen an Geometrien gehängt und dann begrenzt.
"""

from pathlib import Path

import geopandas as gpd
import pytest
import yaml
from shapely.geometry import Polygon

from geofact.core.scenario import Scenario
from geofact.engine.executor import run_scenario

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
SCENARIO_PATH = EXAMPLES_DIR / "szenario4_bip_bundeslaender.yaml"

# Reihenfolge wie in der eingecheckten CSV; die Geometrien selbst sind
# für dieses Szenario beliebig (es wird nicht räumlich verschnitten),
# müssen aber innerhalb der Szenario-Region liegen.
LAENDER = [
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

# Erwartete Rangfolge laut eingecheckter CSV (Destatis-Werte).
ERWARTETE_TOP5 = [
    "Nordrhein-Westfalen",
    "Bayern",
    "Baden-Württemberg",
    "Niedersachsen",
    "Hessen",
]


def _laender_layer(_layer) -> gpd.GeoDataFrame:
    """Stellvertreter für den BKG-WFS: 16 Flächen mit dem Attribut
    'gen' (geografischer Name) - genau das Attribut, das der
    attribute_join als Schlüssel nutzt."""
    polygons = [
        Polygon(
            [
                (6.0 + i * 0.4, 50.0),
                (6.3 + i * 0.4, 50.0),
                (6.3 + i * 0.4, 50.3),
                (6.0 + i * 0.4, 50.3),
            ]
        )
        for i in range(len(LAENDER))
    ]
    return gpd.GeoDataFrame({"gen": LAENDER}, geometry=polygons, crs="EPSG:4326")


def _laender_layer_mehrteilig(layer) -> gpd.GeoDataFrame:
    """Wie der echte WFS: Baden-Wuerttemberg kommt als drei Flächen (Land
    plus zwei kleine Gewässerteile)."""
    base = _laender_layer(layer)
    extra = gpd.GeoDataFrame(
        {"gen": ["Baden-Württemberg"] * 2},
        geometry=[
            Polygon([(6.8, 50.4), (6.85, 50.4), (6.85, 50.45), (6.8, 50.45)]),
            Polygon([(6.9, 50.4), (6.95, 50.4), (6.95, 50.45), (6.9, 50.45)]),
        ],
        crs="EPSG:4326",
    )
    return gpd.GeoDataFrame(
        gpd.pd.concat([base, extra], ignore_index=True),
        geometry="geometry",
        crs="EPSG:4326",
    )


@pytest.fixture
def scenario() -> Scenario:
    raw = yaml.safe_load(SCENARIO_PATH.read_text(encoding="utf-8"))
    return Scenario(**raw)


def _run(scenario: Scenario):
    return run_scenario(
        scenario,
        base_dir=EXAMPLES_DIR,
        connector_override={"bundeslaender": _laender_layer},
    )


def test_szenario4_executes_end_to_end(scenario):
    result = _run(scenario)
    assert result.order == ["laender", "mit_bip", "top5"]


def test_szenario4_returns_exactly_five_states(scenario):
    result = _run(scenario)
    assert len(result.store.get("top5")) == 5


def test_szenario4_ranking_matches_official_figures(scenario):
    """Nachbedingung der Nutzerfrage: die fünf größten nach BIP, in
    der richtigen Reihenfolge."""
    result = _run(scenario)
    top5 = result.store.get("top5")
    assert top5["gen"].tolist() == ERWARTETE_TOP5
    assert top5["rang"].tolist() == [1, 2, 3, 4, 5]


def test_szenario4_bip_values_are_numeric_and_descending(scenario):
    result = _run(scenario)
    values = result.store.get("top5")["bip_mio_eur"].tolist()
    assert all(isinstance(v, float) for v in values)
    assert values == sorted(values, reverse=True)


def test_szenario4_join_matched_every_state(scenario):
    """Die Umlaut-Normalisierung des Joins muss alle 16 Länder treffen -
    sonst fehlte z. B. Baden-Wuerttemberg im Ergebnis."""
    result = _run(scenario)
    joined = result.store.get("mit_bip")
    assert len(joined) == 16
    assert joined["bip_mio_eur"].notna().all()


def test_szenario4_geometry_survives_the_join(scenario):
    """Die Geometrie kommt aus dem linken Layer; die geometrielose
    Tabelle (FA37) darf sie nicht überschreiben."""
    result = _run(scenario)
    top5 = result.store.get("top5")
    assert top5.geometry.notna().all()
    assert set(top5.geometry.geom_type) == {"Polygon"}


def test_szenario4_statistics_table_loads_without_geometry(scenario):
    """FA37: die BIP-Tabelle hat keinen geometry-Block und wird trotzdem
    als Layer geladen (ohne CRS-Fehler)."""
    result = _run(scenario)
    bip = result.store.get("bip")
    assert len(bip) == 16
    assert bip.geometry.isna().all()


def test_fa69_szenario4_lists_each_state_once_when_the_wfs_returns_several_areas(
    scenario,
):
    """Regression: der WFS liefert manche Länder mehrfach; ohne deduplicate
    stand Baden-Wuerttemberg dreimal in den Top 5."""
    result = run_scenario(
        scenario,
        base_dir=EXAMPLES_DIR,
        connector_override={"bundeslaender": _laender_layer_mehrteilig},
    )
    top5 = result.store.get("top5")
    assert top5["gen"].tolist() == ERWARTETE_TOP5
    assert top5["gen"].is_unique
    # behalten wird die große Landfläche, nicht ein Gewässerteil
    bw = top5[top5["gen"] == "Baden-Württemberg"].geometry.iloc[0]
    assert bw.area >= 0.9 * top5.geometry.area.max()
