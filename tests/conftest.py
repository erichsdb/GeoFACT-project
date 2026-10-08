"""Pytest-Konfiguration: integration-Tests skippen sich selbst ohne DSN;
die Erweiterungen werden einmal je Sitzung explizit geladen (FA40)."""

import ipaddress
import os
import socket

import pytest


@pytest.fixture(scope="session", autouse=True)
def _load_extensions():
    """FA40: Es gibt keine versteckte Erkennung beim ersten Zugriff - die
    Standard-Registry wird hier einmal je Sitzung installiert, nur mit den
    Kernbausteinen (plugin_paths=[]): ein lokal gesetztes GEOFACT_PLUGIN_PATH
    darf die Tests nicht verändern. Tests mit Plugins bauen sich eine eigene
    Registry per ``discover(plugin_paths=[...])``."""
    from geofact import api

    return api.load_extensions(plugin_paths=[])


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if os.environ.get("GEOFACT_PG_DSN"):
        return
    skip_integration = pytest.mark.skip(reason="GEOFACT_PG_DSN nicht gesetzt")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


def _is_loopback(host: object) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _offline_unless_netcheck(request, monkeypatch):
    """FA45: der Standardlauf ist offline. Jede Verbindung ins Netz scheitert
    laut, statt den Test vom Netz abhängig zu machen - ein vergessener
    Live-Aufruf (Nominatim, Overpass, WFS) fällt sofort auf. Ausgenommen sind
    Tests mit der Marke ``netcheck`` (sie fragen echte Endpunkte ab und laufen
    nur mit ``-m netcheck``). Loopback bleibt offen (lokale Demo-Dienste); die
    PostGIS-Integrationstests sprechen über libpq, nicht über Python-Sockets."""
    if request.node.get_closest_marker("netcheck") is not None:
        return
    real_connect = socket.socket.connect

    def guarded_connect(self, address, *args, **kwargs):
        if isinstance(address, tuple) and not _is_loopback(address[0]):
            raise RuntimeError(
                f"Netzzugriff in einem Offline-Test: {address}. Quelle ersetzen "
                "(connector_override / region_fetcher / Fixture) oder den Test mit "
                "@pytest.mark.netcheck als Live-Test kennzeichnen."
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _web_data_in_tmp_path(tmp_path, monkeypatch):
    """FA44: Läufe der Web-Schicht schreiben ihre Laufverzeichnisse unter das
    Datenverzeichnis (``<data_dir>/runs``). Ohne Umlenkung landeten sie in
    ``.geofact_web/`` im Repo (dort liegen auch gespeicherte Szenarien) -
    jeder Test bekommt deshalb ein eigenes Datenverzeichnis. Tests, die das
    Verzeichnis selbst setzen, überschreiben diese Werte."""
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "web-data"))
    monkeypatch.setenv(
        "GEOFACT_WEB_UPLOADS_DIR", str(tmp_path / "web-data" / "uploads")
    )


@pytest.fixture(autouse=True)
def _no_web_auth_by_default(monkeypatch):
    """FA24: Web-Tests, die sich nicht explizit mit Auth befassen, gehen
    implizit davon aus, dass keine Anmeldung nötig ist (Zustand vor FA24).
    Eine lokal in .env gesetzte GEOFACT_WEB_USERS (z. B. für einen
    manuellen Login-Test während der Entwicklung) würde sonst über
    settings.py::load_dotenv in JEDEN Test durchsickern und bestehende
    Web-Tests mit unerwarteten 401ern brechen - so bleibt die Testsuite
    unabhängig vom lokalen Entwicklungszustand reproduzierbar. Tests, die
    Auth tatsächlich brauchen (test_fa24_auth.py), setzen die Variablen
    über ihre eigene two_users-Fixture explizit wieder."""
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    monkeypatch.delenv("GEOFACT_WEB_SECRET_KEY", raising=False)
    try:
        from geofact_web import settings as settings_module
    except ImportError:
        # Web-Extra nicht installiert (Kern-Tests laufen auch ohne) - dann
        # gibt es auch keinen Cache zu leeren. Die Fixture muss trotzdem
        # yielden: ein 'return' vor dem 'yield' ließe jeden Test mit
        # "fixture did not yield" scheitern.
        yield
        return
    settings_module.get_settings.cache_clear()
    yield
    settings_module.get_settings.cache_clear()
