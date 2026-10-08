"""Implements: FA36 (kuratierter API-Katalog).

Contract-Tests für backend/geofact_web/api_catalog.py, den /api/apis-Endpunkt
und die Einbindung des Katalogs in das LLM-Grounding. Kein Netzzugriff -
der Katalog ist bewusst eine eingecheckte, offline lesbare Datei.

Motivierender Fall: eine API ist von außen undurchdringlich. Ohne
aufbereiteten Katalog muss man Feature-Types und Attributnamen aus
Portalseiten und GetCapabilities-XML zusammensuchen, und das LLM erfindet
plausibel klingende, nicht existierende Endpunkte.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from geofact_web import api_catalog


@pytest.fixture(autouse=True)
def _clean_catalog_cache(monkeypatch):
    """Katalog ist gecacht (Datei ändert sich zur Laufzeit nicht) - für
    Tests, die eine eigene Datei einschieben, muss der Cache weg."""
    monkeypatch.delenv("GEOFACT_API_CATALOG", raising=False)
    api_catalog.reset_cache()
    yield
    api_catalog.reset_cache()


def write_catalog(tmp_path, entries: list[dict]) -> str:
    path = tmp_path / "api_catalog.yaml"
    path.write_text(
        yaml.safe_dump({"apis": entries}, allow_unicode=True), encoding="utf-8"
    )
    return str(path)


# =====================================================================
# Der eingecheckte Katalog selbst (Referenz-Szenarien müssen valide
# bleiben - hier: der Katalog bleibt lesbar und vollständig)
# =====================================================================


def test_fa36_shipped_catalog_loads():
    entries = api_catalog.api_entries()
    assert len(entries) >= 5


def test_fa36_shipped_catalog_entries_have_required_fields():
    for entry in api_catalog.api_entries():
        assert entry["id"]
        assert entry["label"]
        assert entry["protocol"]
        assert entry["base_url"].startswith("http")


def test_fa36_shipped_catalog_ids_are_unique():
    ids = [entry["id"] for entry in api_catalog.api_entries()]
    assert len(ids) == len(set(ids))


def test_fa36_shipped_catalog_has_authority_sources():
    """Anforderung: vor allem behördliche Daten (ALKIS/ATKIS,
    Geoportale der Länder, amtliche Statistik)."""
    ids = {entry["id"] for entry in api_catalog.api_entries()}
    assert "bkg_vg250" in ids  # amtliche Verwaltungsgrenzen
    assert "bkg_dlm250" in ids  # ATKIS (Basis-DLM 1:250.000)
    assert "nrw_alkis" in ids  # ALKIS / Landes-Geoportal
    assert "destatis_genesis" in ids  # amtliche Statistik (nicht-räumlich)
    # sachsen_geosn_dop stand hier bis 09/2026. Der GeoSN-WFS antwortet
    # inzwischen durchgängig mit HTTP 403 und wurde aus dem Katalog
    # entfernt; sächsische Verwaltungsgrenzen liefert bkg_vg250 mit.


def test_fa36_shipped_catalog_protocols_are_supported():
    """Jeder Eintrag muss von einem bestehenden Konnektor bedienbar sein
    oder als Tabellenquelle gekennzeichnet sein - der Katalog führt
    keine Quelle auf, die der Kern nicht beziehen kann."""
    for entry in api_catalog.api_entries():
        assert entry["protocol"] in api_catalog.SUPPORTED_PROTOCOLS, entry["id"]


def test_fa36_endpoints_are_documented_for_geodata_apis():
    """Kern der Anforderung: man soll SEHEN, welche Endpunkte definiert
    sind und wie sie beschrieben werden."""
    for entry in api_catalog.api_entries():
        endpoints = entry.get("endpoints") or []
        assert endpoints, f"{entry['id']} hat keine dokumentierten Endpunkte"
        for endpoint in endpoints:
            assert endpoint.get("name")
            assert endpoint.get("title"), (
                f"{entry['id']}/{endpoint.get('name')} ohne Titel"
            )


def test_fa36_example_layer_fragments_are_valid_layers():
    """Nachbedingung: ein example_layer ist einbaufertig - es validiert
    als Layer im Kern-Schema (FA1), nicht nur als YAML."""
    from geofact.core.scenario import Scenario

    for entry in api_catalog.api_entries():
        template = entry.get("example_layer")
        if not template:
            continue
        scenario = Scenario(
            **{
                "scenario": {"name": entry["id"], "region": "12.0,50.0,13.0,51.0"},
                "layers": [template],
                "steps": [
                    # ein Rasterfragment (esa_worldcover) braucht einen Raster-Schritt
                    {
                        "id": "gefiltert",
                        "op": "raster_classify",
                        "inputs": {"raster": template["id"]},
                        "params": {"breaks": [50], "values": [0, 1]},
                    }
                    if template.get("data_type") == "raster"
                    else {
                        "id": "gefiltert",
                        "op": "filter",
                        "inputs": {"features": template["id"]},
                        "params": {"condition": "1 == 1"},
                    },
                ],
                "output": [{"type": "map", "source": "gefiltert"}],
            }
        )
        assert scenario.execution_order() == ["gefiltert"], entry["id"]


def test_fa36_spec_urls_are_documented_where_machine_readable():
    """Wo es eine maschinenlesbare Spezifikation gibt, ist sie verlinkt
    und ihr Typ benannt (WFS-Capabilities, CKAN-API, OpenAPI)."""
    for entry in api_catalog.api_entries():
        if entry.get("spec_url"):
            assert entry.get("spec_type"), entry["id"]


# =====================================================================
# Lookup und Fehlerfälle
# =====================================================================


def test_fa36_find_api_and_endpoints():
    entry = api_catalog.find_api("bkg_vg250")
    assert entry is not None
    names = [e["name"] for e in api_catalog.endpoints("bkg_vg250")]
    assert "vg250:vg250_lan" in names


def test_fa36_find_unknown_api_returns_none():
    assert api_catalog.find_api("gibt_es_nicht") is None


def test_fa36_endpoints_of_unknown_api_is_an_error():
    """Goldene Regel 7: keine stille leere Liste für eine unbekannte API."""
    with pytest.raises(api_catalog.ApiCatalogError, match="Unbekannte API"):
        api_catalog.endpoints("gibt_es_nicht")


def test_fa36_missing_catalog_file_is_an_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setenv("GEOFACT_API_CATALOG", str(tmp_path / "fehlt.yaml"))
    api_catalog.reset_cache()
    with pytest.raises(api_catalog.ApiCatalogError, match="nicht gefunden"):
        api_catalog.api_entries()


def test_fa36_invalid_yaml_is_an_explicit_error(monkeypatch, tmp_path):
    path = tmp_path / "api_catalog.yaml"
    path.write_text("apis: [unclosed", encoding="utf-8")
    monkeypatch.setenv("GEOFACT_API_CATALOG", str(path))
    api_catalog.reset_cache()
    with pytest.raises(api_catalog.ApiCatalogError, match="kein gültiges YAML"):
        api_catalog.api_entries()


def test_fa36_missing_required_field_is_an_explicit_error(monkeypatch, tmp_path):
    path = write_catalog(tmp_path, [{"id": "x", "label": "X", "protocol": "wfs"}])
    monkeypatch.setenv("GEOFACT_API_CATALOG", path)
    api_catalog.reset_cache()
    with pytest.raises(api_catalog.ApiCatalogError, match="base_url"):
        api_catalog.api_entries()


def test_fa36_duplicate_ids_are_an_explicit_error(monkeypatch, tmp_path):
    entry = {
        "id": "x",
        "label": "X",
        "protocol": "wfs",
        "base_url": "https://e.example",
    }
    path = write_catalog(tmp_path, [entry, dict(entry)])
    monkeypatch.setenv("GEOFACT_API_CATALOG", path)
    api_catalog.reset_cache()
    with pytest.raises(api_catalog.ApiCatalogError, match="doppelte id"):
        api_catalog.api_entries()


def test_fa36_empty_api_list_is_an_explicit_error(monkeypatch, tmp_path):
    path = write_catalog(tmp_path, [])
    monkeypatch.setenv("GEOFACT_API_CATALOG", path)
    api_catalog.reset_cache()
    with pytest.raises(api_catalog.ApiCatalogError, match="nicht-leere Liste"):
        api_catalog.api_entries()


# =====================================================================
# LLM-Grounding: das Modell sieht Endpunkte und Attribute
# =====================================================================


def test_fa36_prompt_text_names_endpoints_and_attributes():
    text = api_catalog.prompt_text(["bkg_vg250"])
    assert "vg250:vg250_lan" in text
    assert "gen" in text  # Join-Schlüssel-Attribut
    assert "https://sgx.geodatenzentrum.de/wfs_vg250" in text


def test_fa36_prompt_text_explains_that_rest_is_not_a_layer_source():
    """Wichtig gegen Halluzination: 'rest' ist kein Layer-Quelltyp - das
    Modell soll solche Daten als table einbinden und joinen."""
    text = api_catalog.prompt_text(["destatis_genesis"])
    assert "source: table" in text
    assert "attribute_join" in text


def test_fa36_prompt_text_warns_about_credentials():
    """Keine Secrets in der Szenario-YAML (Lizenz-/Sichtbarkeitsregel)."""
    text = api_catalog.prompt_text(["destatis_genesis"])
    assert "Umgebungsvariable" in text


def test_fa36_prompt_text_selected_only():
    text = api_catalog.prompt_text(["bkg_vg250"])
    assert "bkg_vg250" in text
    assert "leipzig_opendata" not in text


def test_fa36_prompt_text_all_entries_when_unselected():
    text = api_catalog.prompt_text()
    assert "bkg_vg250" in text
    assert "leipzig_opendata" in text


def test_fa36_prompt_text_is_empty_without_catalog(monkeypatch, tmp_path):
    """Das Grounding ist eine Verbesserung, kein Muss - ein fehlender
    Katalog darf die Generierung nicht blockieren."""
    monkeypatch.setenv("GEOFACT_API_CATALOG", str(tmp_path / "fehlt.yaml"))
    api_catalog.reset_cache()
    assert api_catalog.prompt_text() == ""


def test_fa36_layer_templates_of_selected_apis():
    templates = api_catalog.layer_templates(["bkg_vg250"])
    assert len(templates) == 1
    assert templates[0]["source"] == "wfs"
    assert templates[0]["typename"] == "vg250:vg250_lan"


def test_fa36_layer_templates_skips_entries_without_example():
    templates = api_catalog.layer_templates(["govdata", "gibt_es_nicht"])
    assert templates == []


def test_fa36_system_prompt_contains_api_catalog():
    """Nachbedingung: angelegte APIs sind für das LLM automatisch
    sichtbar - ohne dass der Nutzer sie im Prompt beschreibt."""
    pytest.importorskip("fastapi")
    from geofact_web.llm import build_system_prompt

    prompt = build_system_prompt([], [])
    assert "vg250:vg250_lan" in prompt
    assert "attribute_join" in prompt
    assert "top_n" in prompt


def test_fa36_custom_catalog_entry_reaches_the_prompt(monkeypatch, tmp_path):
    """Eine im Katalog ergänzte API erscheint ohne Codeänderung im
    Prompt (Open-Closed für Datenquellen)."""
    pytest.importorskip("fastapi")
    from geofact_web.llm import build_system_prompt

    path = write_catalog(
        tmp_path,
        [
            {
                "id": "eigene_api",
                "label": "Eigene Testschnittstelle",
                "protocol": "wfs",
                "base_url": "https://eigene.example/wfs",
                "endpoints": [
                    {
                        "name": "test:schicht",
                        "title": "Testschicht",
                        "attributes": ["name", "wert"],
                    }
                ],
            }
        ],
    )
    monkeypatch.setenv("GEOFACT_API_CATALOG", path)
    api_catalog.reset_cache()

    prompt = build_system_prompt([], [])
    assert "eigene_api" in prompt
    assert "test:schicht" in prompt


def test_fa36_preselected_sources_are_offered_not_commanded():
    """Gemeldetes Verhalten: bei einer vorausgewählten, für die Frage
    irrelevanten Quelle (Bevölkerungsraster bei einer reinen
    Stationskarte) hat das Modell sehr lange damit gerungen, wie es die
    Quelle unterbringt, statt sie wegzulassen - der Prompt sagte
    "baue sie als layers ein". Die Vorauswahl kommt aus der Oberfläche
    und ist ein Vorschlag, keine Anforderung des Nutzers."""
    pytest.importorskip("fastapi")
    from geofact_web.llm import build_system_prompt

    template = {
        "id": "ghs_pop",
        "source": "file",
        "path": "x.tif",
        "data_type": "raster",
    }
    prompt = build_system_prompt([template], [])

    assert "baue sie als layers ein" not in prompt
    assert "VORSCHLAG, keine Pflicht" in prompt
    # Das Template selbst muss weiterhin im Prompt stehen - es soll ja
    # benutzbar sein, nur eben nicht zwingend.
    assert "ghs_pop" in prompt


def test_fa36_prompt_documents_table_layer_fields():
    """Das Modell musste die Felder eines table-Layers aus einem
    unverwandten Beispiel rekonstruieren und hat lange über
    Trennzeichen/Encoding spekuliert. Die Felder stehen jetzt im
    Prompt."""
    pytest.importorskip("fastapi")
    from geofact_web.llm import build_system_prompt

    prompt = build_system_prompt([], [])
    for field in ("delimiter", "encoding", "decimal", "wkt_column", "x_column"):
        assert field in prompt, field
    # Fixed-Width braucht format: fwf mit expliziten Grenzen - sonst
    # konstruiert das Modell Regex-Trenner, die an der Datei scheitern.
    assert "FESTER SPALTENBREITE" in prompt
    assert "colspecs" in prompt
    # URLs sind erlaubt (FA38) - der Prompt hat das früher fälschlich
    # zugesichert, bevor der Konnektor es konnte.
    assert "http(s)-URL" in prompt


def test_fa36_prompt_mentions_region_source():
    """Der Quelltyp 'region' stand nur in der Quelltyp-Liste, ohne Hinweis
    wozu er gut ist - das Modell hat ihn nie in Betracht gezogen."""
    pytest.importorskip("fastapi")
    from geofact_web.llm import build_system_prompt

    assert "source: region" in build_system_prompt([], [])


# =====================================================================
# HTTP-Endpunkte
# =====================================================================


def test_fa36_api_endpoint_lists_catalog(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from geofact_web import settings as settings_module
    from geofact_web.main import create_app

    monkeypatch.setenv("GEOFACT_WEB_USERS", "")
    settings_module.get_settings.cache_clear()
    try:
        with TestClient(create_app()) as http:
            response = http.get("/api/apis")
            assert response.status_code == 200
            payload = response.json()
            entry = next(a for a in payload["apis"] if a["id"] == "bkg_vg250")
            assert entry["protocol"] == "wfs"
            assert entry["license"]
            assert entry["spec_url"]
            names = [e["name"] for e in entry["endpoints"]]
            assert "vg250:vg250_lan" in names
            landes_endpoint = next(
                e for e in entry["endpoints"] if e["name"] == "vg250:vg250_lan"
            )
            assert "gen" in landes_endpoint["attributes"]
            # Attribut-Erklärungen sind für Menschen da (FA36).
            assert landes_endpoint["attribute_notes"]
    finally:
        settings_module.get_settings.cache_clear()


def test_fa36_spec_endpoint_404_for_unknown_api(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from geofact_web import settings as settings_module
    from geofact_web.main import create_app

    monkeypatch.setenv("GEOFACT_WEB_USERS", "")
    settings_module.get_settings.cache_clear()
    try:
        with TestClient(create_app()) as http:
            assert http.get("/api/apis/gibt_es_nicht/spec").status_code == 404
    finally:
        settings_module.get_settings.cache_clear()


def test_fa36_spec_endpoint_404_when_no_spec_url(monkeypatch, tmp_path):
    """Eine API ohne maschinenlesbare Spezifikation meldet das explizit
    und nennt die Dokumentation - kein leerer Erfolg (Regel 7)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from geofact_web import settings as settings_module
    from geofact_web.main import create_app

    path = write_catalog(
        tmp_path,
        [
            {
                "id": "ohne_spec",
                "label": "Ohne Spezifikation",
                "protocol": "wfs",
                "base_url": "https://ohne.example/wfs",
                "documentation_url": "https://ohne.example/doku",
                "endpoints": [{"name": "x", "title": "X"}],
            }
        ],
    )
    monkeypatch.setenv("GEOFACT_API_CATALOG", path)
    monkeypatch.setenv("GEOFACT_WEB_USERS", "")
    api_catalog.reset_cache()
    settings_module.get_settings.cache_clear()
    try:
        with TestClient(create_app()) as http:
            response = http.get("/api/apis/ohne_spec/spec")
            assert response.status_code == 404
            assert "ohne.example/doku" in response.json()["detail"]
    finally:
        settings_module.get_settings.cache_clear()


def test_fa36_spec_endpoint_reports_unreachable_endpoint(monkeypatch, tmp_path):
    """Ein nicht erreichbarer Fremd-Endpunkt liefert 502 mit Ursache und
    Dokumentations-Hinweis statt eines stillen Fehlers (FA16-Haltung)."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from geofact.support import http as http_module
    from geofact_web import settings as settings_module
    from geofact_web.main import create_app

    path = write_catalog(
        tmp_path,
        [
            {
                "id": "kaputt",
                "label": "Nicht erreichbar",
                "protocol": "wfs",
                "base_url": "https://kaputt.example/wfs",
                "spec_url": "https://kaputt.example/wfs?REQUEST=GetCapabilities",
                "spec_type": "wfs_capabilities",
                "documentation_url": "https://kaputt.example/doku",
                "endpoints": [{"name": "x", "title": "X"}],
            }
        ],
    )
    monkeypatch.setenv("GEOFACT_API_CATALOG", path)
    monkeypatch.setenv("GEOFACT_WEB_USERS", "")
    api_catalog.reset_cache()
    settings_module.get_settings.cache_clear()

    def boom(*args, **kwargs):
        raise RuntimeError("Endpunkt nicht erreichbar nach 3 Versuchen")

    monkeypatch.setattr(http_module, "download_limited", boom)
    try:
        with TestClient(create_app()) as http:
            response = http.get("/api/apis/kaputt/spec")
            assert response.status_code == 502
            detail = response.json()["detail"]
            assert "nicht erreichbar" in detail
            assert "kaputt.example/doku" in detail
    finally:
        settings_module.get_settings.cache_clear()


def test_fa36_spec_endpoint_returns_content(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from geofact.support import http as http_module
    from geofact_web import settings as settings_module
    from geofact_web.main import create_app

    path = write_catalog(
        tmp_path,
        [
            {
                "id": "mit_spec",
                "label": "Mit Spezifikation",
                "protocol": "wfs",
                "base_url": "https://mit.example/wfs",
                "spec_url": "https://mit.example/wfs?REQUEST=GetCapabilities",
                "spec_type": "wfs_capabilities",
                "endpoints": [{"name": "x", "title": "X"}],
            }
        ],
    )
    monkeypatch.setenv("GEOFACT_API_CATALOG", path)
    monkeypatch.setenv("GEOFACT_WEB_USERS", "")
    api_catalog.reset_cache()
    settings_module.get_settings.cache_clear()

    capabilities = textwrap.dedent("""\
        <?xml version="1.0" encoding="UTF-8"?>
        <wfs:WFS_Capabilities version="2.0.0">
          <FeatureTypeList><FeatureType><Name>x</Name></FeatureType></FeatureTypeList>
        </wfs:WFS_Capabilities>
        """).encode("utf-8")
    monkeypatch.setattr(http_module, "download_limited", lambda *a, **k: capabilities)
    try:
        with TestClient(create_app()) as http:
            response = http.get("/api/apis/mit_spec/spec")
            assert response.status_code == 200
            payload = response.json()
            assert payload["spec_type"] == "wfs_capabilities"
            assert "WFS_Capabilities" in payload["content"]
            assert payload["truncated"] is False
    finally:
        settings_module.get_settings.cache_clear()


# =====================================================================
# Paketdaten: der Katalog liegt im Paket geofact_web (nicht in docs/) und wird
# mit importlib.resources gelesen - funktioniert damit auch im Wheel und im
# Docker-Image (zuvor fehlte docs/api_catalog.yaml dort: /api/apis lieferte 500)
# =====================================================================

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


def test_fa36_default_catalog_is_package_data_read_with_importlib_resources():
    from importlib import resources

    resource = resources.files("geofact_web").joinpath("data", "api_catalog.yaml")
    assert resource.is_file()
    assert api_catalog.catalog_path() == api_catalog.PACKAGED
    assert not (REPO_ROOT / "docs" / "api_catalog.yaml").exists()  # keine zweite Kopie


def test_fa36_default_catalog_does_not_depend_on_the_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    api_catalog.reset_cache()
    assert len(api_catalog.api_entries()) >= 5


def test_fa36_missing_package_data_is_an_explicit_error(monkeypatch, tmp_path):
    """Ein Paket ohne seine Datendatei meldet das ausdrücklich (Regel 7)."""
    monkeypatch.setattr(api_catalog.resources, "files", lambda package: tmp_path)
    api_catalog.reset_cache()
    with pytest.raises(api_catalog.ApiCatalogError, match="Paketdaten"):
        api_catalog.api_entries()


def test_fa36_catalog_ships_with_the_wheel_and_the_docker_image():
    """pyproject nimmt backend/geofact_web samt Datendatei ins Wheel auf,
    Dockerfile.backend kopiert backend/ komplett und .dockerignore lässt
    die Datei durch."""
    import tomllib

    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert "backend/geofact_web" in wheel["packages"]
    assert "backend/geofact_web/data/*.yaml" in wheel["artifacts"]

    dockerfile = (REPO_ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
    assert "COPY backend ./backend" in dockerfile
    ignored = [
        line.strip()
        for line in (REPO_ROOT / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    ]
    blocking = [
        line
        for line in ignored
        if line.startswith("backend")
        or line.rstrip("/") in ("data", "**/data", "*.yaml", "**/*.yaml")
    ]
    assert not blocking, f".dockerignore schließt die Katalogdatei aus: {blocking}"


def test_fa36_apis_endpoint_serves_the_packaged_catalog_without_configuration():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from geofact_web.main import create_app

    response = TestClient(create_app()).get("/api/apis")
    assert response.status_code == 200
    assert {"bkg_vg250", "destatis_genesis"} <= {
        entry["id"] for entry in response.json()["apis"]
    }


# =====================================================================
# LLM-Vorstudie 03.10.2026: Attributangaben gegen den echten Dienst
# =====================================================================

# Attribute je Feature-Typ laut DescribeFeatureType der BKG-Dienste
# (abgefragt am 03.10.2026; die Live-Prüfung aller Einträge steht in
# tests/integration/test_fa36_catalog_endpoints_live.py, -m netcheck).
_VG250_ATTRIBUTES = {
    "objid",
    "beginn",
    "ade",
    "gf",
    "bsg",
    "lkz",
    "ars",
    "ags",
    "sdv_ars",
    "gen",
    "bez",
    "ibz",
    "bem",
    "nbd",
    "sn_l",
    "sn_r",
    "sn_k",
    "sn_v1",
    "sn_v2",
    "sn_g",
    "fk_s3",
    "nuts",
    "ars_0",
    "ags_0",
    "wsk",
    "dlm_id",
}
_VG250_EW_ATTRIBUTES = _VG250_ATTRIBUTES | {"ewz", "kfl"}


def test_fa36_vg250_lists_no_attribute_the_service_lacks():
    """Regression: der Katalog nannte 'ewz' (und 'kfl') am Dienst wfs_vg250.
    Der liefert beide nicht; alle drei P10-Läufe der Vorstudie brachen an
    top_n by ewz ab."""
    for endpoint in api_catalog.endpoints("bkg_vg250"):
        extra = set(endpoint.get("attributes") or []) - _VG250_ATTRIBUTES
        assert not extra, f"{endpoint['name']}: {sorted(extra)} gibt es im Dienst nicht"
    text = api_catalog.prompt_text(["bkg_vg250"])
    assert "bkg_vg250_ew" in text  # Verweis auf den Dienst mit Einwohnerzahl


def test_fa36_population_ewz_is_offered_by_the_vg250_ew_service():
    entry = api_catalog.find_api("bkg_vg250_ew")
    assert entry is not None
    assert entry["base_url"] == "https://sgx.geodatenzentrum.de/wfs_vg250-ew"
    endpoints = api_catalog.endpoints("bkg_vg250_ew")
    assert {e["name"] for e in endpoints} >= {
        "vg250-ew:vg250_gem",
        "vg250-ew:vg250_krs",
    }
    for endpoint in endpoints:
        assert endpoint["name"].startswith("vg250-ew:")
        attributes = set(endpoint["attributes"])
        assert {"ewz", "kfl"} <= attributes <= _VG250_EW_ATTRIBUTES
    assert entry["example_layer"]["url"] == entry["base_url"]
