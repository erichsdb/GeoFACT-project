"""Implements: FA36 (API-Katalog).

Prüft die eingecheckten Katalogeinträge gegen die ECHTEN Endpunkte.

Warum als eigener, abgeschalteter Marker (`-m netcheck`) und nicht im
normalen `pytest`-Lauf: der Katalog ist eine kuratierte Liste fremder
Behördendienste. Ob die erreichbar sind, hängt vom Netz und vom
Betreiber ab, nicht vom Code - ein Fehlschlag hier ist keine Regression
in GeoFACT. Die Unit-Tests in test_fa36_api_catalog.py bleiben deshalb
offline und prüfen nur Struktur/Schema.

Gebraucht wird der Lauf trotzdem: zweimal hintereinander standen
Einträge im Katalog, die es so nicht (mehr) gab - der GeoSN-WFS
(durchgängig HTTP 403) und das Basis-DLM (falscher Dienstname
`wfs_basis-dlm` UND frei erfundene Feature-Typen `dlm:gew01_l` &
Co., die der echte Dienst nie kannte). Weil der Katalog den LLM-Prompt
grundiert, erzeugt jeder tote Eintrag aktiv fehlschlagende Szenarien
beim Nutzer. Vor jeder Katalogänderung also:

    uv run pytest -m netcheck

Kein Snapshot, kein Mock - das ist der Sinn der Sache.
"""

from __future__ import annotations

import json

import pytest
import requests

from geofact_web import api_catalog
from geofact.plugin_api import LoadContext
from geofact.support.http import USER_AGENT

pytestmark = pytest.mark.netcheck

TIMEOUT_S = 90
# Sachsen - deckt die Primärregion der Arbeit ab. Für Dienste mit
# anderem räumlichen Zuschnitt (NRW) steht der passende Ausschnitt in
# _BBOX_OVERRIDE.
_BBOX_SACHSEN = "50.1713271,11.8714837,51.6849651,15.0419309,urn:ogc:def:crs:EPSG::4326"
_BBOX_OVERRIDE = {
    "nrw_alkis": "50.90,6.90,50.96,7.00,urn:ogc:def:crs:EPSG::4326",
}


def _wfs_entries() -> list[dict]:
    return [e for e in api_catalog.api_entries() if e["protocol"] == "wfs"]


def _get(url: str, params: dict[str, str]) -> requests.Response:
    return requests.get(
        url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S
    )


@pytest.mark.parametrize("entry", _wfs_entries(), ids=lambda e: e["id"])
def test_fa36_live_wfs_capabilities_reachable(entry):
    """Vorbedingung: der Dienst existiert überhaupt und antwortet.

    Genau das hat bei GeoSN (403) und Basis-DLM (400
    ERROR_UNKNOWN_SERVICE) gefehlt.
    """
    response = _get(
        entry["base_url"],
        {"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"},
    )
    assert response.status_code == 200, (
        f"{entry['id']}: GetCapabilities auf {entry['base_url']} liefert "
        f"HTTP {response.status_code}. Erste 300 Zeichen: {response.text[:300]}"
    )
    assert "WFS_Capabilities" in response.text, (
        f"{entry['id']}: Antwort ist kein WFS-Capabilities-Dokument"
    )


@pytest.mark.parametrize("entry", _wfs_entries(), ids=lambda e: e["id"])
def test_fa36_live_wfs_typenames_exist(entry):
    """Nachbedingung: JEDER dokumentierte Feature-Typ existiert wirklich.

    Der Katalog ist LLM-Grounding - ein erfundener typename führt
    direkt zu einem fehlschlagenden Nutzer-Szenario.
    """
    response = _get(
        entry["base_url"],
        {"service": "WFS", "version": "2.0.0", "request": "GetCapabilities"},
    )
    response.raise_for_status()
    advertised = set()
    # Namespace-tolerant: die Dienste nutzen wfs 1.1/2.0 unterschiedlich.
    import xml.etree.ElementTree as ET

    root = ET.fromstring(response.content)
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "Name":
            text = (element.text or "").strip()
            if text:
                advertised.add(text)

    for endpoint in entry.get("endpoints") or []:
        assert endpoint["name"] in advertised, (
            f"{entry['id']}: typename '{endpoint['name']}' steht im Katalog, "
            f"wird vom Dienst aber nicht angeboten. Verfügbar (Auszug): "
            f"{sorted(n for n in advertised if ':' in n)[:12]}"
        )


@pytest.mark.parametrize("entry", _wfs_entries(), ids=lambda e: e["id"])
def test_fa36_live_wfs_getfeature_returns_data(entry):
    """Nachbedingung: der erste dokumentierte Typ liefert in der
    Zielregion auch tatsächlich Objekte - mit genau der Anfrage, die
    src/geofact/builtin/sources/wfs.py baut.

    Ein leeres Ergebnis ist hier ein Fehler: entweder stimmt die
    BBox-Achsenreihenfolge nicht, oder der Typ deckt die Region nicht ab
    - beides macht den Katalogeintrag für Szenarien wertlos.
    """
    endpoints = entry.get("endpoints") or []
    assert endpoints, f"{entry['id']} hat keine Endpunkte"
    typename = endpoints[0]["name"]
    bbox = _BBOX_OVERRIDE.get(entry["id"], _BBOX_SACHSEN)

    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": typename,
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "bbox": bbox,
        "count": "5",
        "startIndex": "0",
    }
    response = _get(entry["base_url"], params)

    if response.status_code != 200 or not response.text.lstrip().startswith("{"):
        # Kein JSON: zulässig, solange der Dienst GML kann - der
        # Konnektor fällt darauf zurück (FA16, z. B. nrw_alkis).
        gml_params = {k: v for k, v in params.items() if k != "outputFormat"}
        gml = _get(entry["base_url"], gml_params)
        assert gml.status_code == 200 and "FeatureCollection" in gml.text, (
            f"{entry['id']}/{typename}: weder JSON noch GML nutzbar "
            f"(JSON: HTTP {response.status_code}, GML: HTTP {gml.status_code}). "
            f"Antwort: {response.text[:300]}"
        )
        return

    payload = json.loads(response.text)
    features = payload.get("features", [])
    assert features, (
        f"{entry['id']}/{typename}: 0 Objekte in der Zielregion. "
        f"Verdacht auf falsche BBox-Achsenreihenfolge oder einen Typ, der "
        f"die Region nicht abdeckt (numberMatched={payload.get('numberMatched')})."
    )


def test_fa36_live_example_layers_are_loadable():
    """Jedes example_layer im Katalog ist das, was der Nutzer als
    Startpunkt kopiert - es muss laden, nicht nur valide aussehen."""
    from geofact.core.scenario import Scenario

    failures = []
    for entry in api_catalog.api_entries():
        example = entry.get("example_layer")
        if not example or example.get("source") != "wfs":
            continue
        # Schema-Ebene: baut das überhaupt einen gültigen Layer? Das
        # Schema verlangt mindestens einen Schritt und ein Output,
        # deshalb ein minimaler Rahmen um den Layer herum.
        Scenario.model_validate(
            {
                "scenario": {"name": "netcheck", "region": "Sachsen, Deutschland"},
                "layers": [example],
                "steps": [
                    {
                        "id": "passthrough",
                        "op": "buffer",
                        "inputs": {"geometry": example["id"]},
                        "params": {"radius_km": 1},
                    }
                ],
                "output": [{"type": "geojson", "source": "passthrough"}],
            }
        )
        response = _get(
            example["url"],
            {
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": example["typename"],
                "outputFormat": "application/json",
                "srsName": "EPSG:4326",
                "bbox": _BBOX_SACHSEN,
                "count": "1",
                "startIndex": "0",
            },
        )
        if response.status_code != 200:
            failures.append(f"{entry['id']}: HTTP {response.status_code}")

    assert not failures, "example_layer nicht ladbar: " + "; ".join(failures)


def test_fa36_live_table_example_layers_load(tmp_path, monkeypatch):
    """FA38/FA39: ein example_layer mit source 'table' muss gegen die
    echte Datei laden - nicht nur schema-valide sein.

    Die DWD-Stationsliste hat feste Spaltenbreite; ein falscher
    Spaltenschnitt fällt sonst erst als unsinniger Attributwert auf,
    und genau daran ist eine früher vorgeschlagene Konfiguration
    gescheitert."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snap"))
    from geofact.builtin.sources.table import TableLayer
    from geofact.builtin.sources import table as tables

    checked = 0
    for entry in api_catalog.api_entries():
        template = entry.get("example_layer")
        if not template or template.get("source") != "table":
            continue
        # Nur Remote-Quellen: ein example_layer mit lokalem Pfad (z. B.
        # regionalstatistik mit 'bip_bundeslaender.csv') meint eine Datei
        # neben der Szenario-YAML, die hier gar nicht existieren soll.
        if not str(template.get("path", "")).startswith(("http://", "https://")):
            continue
        layer = TableLayer(**{k: v for k, v in template.items() if k != "source"})
        frame = tables.load(layer, LoadContext())
        assert len(frame) > 0, f"{entry['id']}: example_layer liefert keine Zeilen"
        if layer.geometry is not None:
            # Koordinaten müssen im richtigen Spaltenschnitt gelandet
            # sein - sonst sind es keine plausiblen Geokoordinaten.
            assert frame.geometry.notna().all(), f"{entry['id']}: leere Geometrien"
            bounds = frame.total_bounds
            assert -180 <= bounds[0] <= 180 and -90 <= bounds[1] <= 90, (
                f"{entry['id']}: Koordinaten außerhalb des gültigen Bereichs "
                f"{bounds} - vermutlich falscher Spaltenschnitt"
            )
        checked += 1
    assert checked > 0, "kein table-example_layer im Katalog gefunden"


def _described_attributes(base_url: str, typename: str) -> set[str]:
    """Attributnamen eines Feature-Typs laut DescribeFeatureType (XSD)."""
    import xml.etree.ElementTree as ET

    response = _get(
        base_url,
        {
            "service": "WFS",
            "version": "2.0.0",
            "request": "DescribeFeatureType",
            "typeNames": typename,
        },
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    return {
        element.attrib["name"]
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "element" and "name" in element.attrib
    }


@pytest.mark.parametrize("entry", _wfs_entries(), ids=lambda e: e["id"])
def test_fa36_live_wfs_attributes_exist(entry):
    """Nachbedingung: JEDES dokumentierte Attribut existiert am Feature-Typ.

    Anlass (LLM-Vorstudie 03.10.2026): bkg_vg250 nannte 'ewz', das nur der
    Dienst wfs_vg250-ew liefert; erzeugte Szenarien scheiterten daran."""
    missing = []
    for endpoint in entry.get("endpoints") or []:
        documented = set(endpoint.get("attributes") or [])
        if not documented:
            continue
        actual = _described_attributes(entry["base_url"], endpoint["name"])
        if not actual:
            continue  # Dienst ohne XSD-Beschreibung: nicht prüfbar
        absent = documented - actual
        if absent:
            missing.append(f"{endpoint['name']}: {sorted(absent)}")
    assert not missing, f"{entry['id']}: Attribute fehlen im Dienst: " + "; ".join(
        missing
    )
