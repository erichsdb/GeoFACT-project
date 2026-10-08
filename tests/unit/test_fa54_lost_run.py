"""Implements: FA54 (verlorenen Lauf der Web-Demo erkennen und erklären) - Backend-Teil.

Läufe liegen im Arbeitsspeicher genau eines Backend-Prozesses. Das Backend
- räumt beim Start nur VERALTETE Laufverzeichnisse ab (nie die frischen eines
  anderen, gleichzeitig laufenden Prozesses und nie die eines eigenen Laufs),
- erklärt einem unbekannten Lauf in der 404-Meldung den Grund (für fremde und
  unbekannte Läufe dieselbe Antwort, FA24),
- beobachtet bei GEOFACT_WEB_RELOAD nur Kern und Backend,
- warnt beim Start, wenn auf dem Port schon ein anderer Prozess lauscht.

Der Frontend-Teil steht in test_fa54_frontend_lost_run.py.
"""

from __future__ import annotations

import os
import socket
import time
import warnings
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact import api  # noqa: E402
from geofact_web import main as main_module  # noqa: E402
from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.run_manager import RunManager, manager  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

SCENARIO = {
    "scenario": {"name": "FA54", "region": "13.60,51.01,13.94,51.15"},
    "layers": [
        {
            "id": "substations",
            "source": "file",
            "data_type": "vector",
            "path": (FIXTURES_DIR / "substations.geojson").as_posix(),
        },
    ],
    "steps": [
        {
            "id": "catchment",
            "op": "buffer",
            "inputs": {"geometry": "substations"},
            "params": {"radius_km": 1},
        },
    ],
    "output": [{"type": "geojson", "source": "catchment", "path": "catchment.geojson"}],
}


def _document() -> api.ScenarioDocument:
    return api.load_scenario(yaml.safe_load(yaml.safe_dump(SCENARIO)))


def _age(path: Path, hours: float) -> None:
    """Datiert die letzte Änderung des Eintrags um `hours` Stunden zurück."""
    past = time.time() - hours * 3600
    os.utime(path, (past, past))


@pytest.fixture
def runs_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "daten"))
    monkeypatch.delenv("GEOFACT_WEB_RUN_DIR_MAX_AGE_HOURS", raising=False)
    settings_module.get_settings.cache_clear()
    return settings_module.get_settings().runs_dir


@pytest.fixture
def client(runs_dir) -> TestClient:
    return TestClient(create_app())


# =====================================================================
# a) Aufräumen beim Start: nur veraltete Reste
# =====================================================================


def test_fa54_a_fresh_run_dir_of_another_process_survives_the_purge(runs_dir):
    foreign = runs_dir / "geofact_run_fremd_abc"
    foreign.mkdir(parents=True)
    (foreign / "catchment.geojson").write_text("{}", encoding="utf-8")
    loose = runs_dir / "lose_datei.tmp"
    loose.write_text("x", encoding="utf-8")

    removed = RunManager().purge_orphaned_run_dirs()

    assert removed == []
    assert foreign.is_dir() and (foreign / "catchment.geojson").exists()
    assert loose.exists()


def test_fa54_an_old_run_dir_is_purged_and_a_young_one_is_not(runs_dir):
    old = runs_dir / "geofact_run_alt_abc"
    young = runs_dir / "geofact_run_jung_abc"
    for entry in (old, young):
        entry.mkdir(parents=True)
        (entry / "ergebnis.csv").write_text("a;b", encoding="utf-8")
    _age(old, hours=7)  # Standardgrenze: 6 Stunden
    _age(young, hours=5)

    removed = RunManager().purge_orphaned_run_dirs()

    assert removed == [old]
    assert not old.exists() and young.exists()


def test_fa54_run_dirs_of_this_process_survive_even_when_they_are_old(runs_dir):
    manager_ = RunManager()
    state = manager_.create(_document(), refresh_snapshots=False)
    manager_._run(state)
    assert (
        state.status == "done" and state.run_dir is not None and state.run_dir.is_dir()
    )
    _age(state.run_dir, hours=48)

    assert manager_.purge_orphaned_run_dirs() == []
    assert state.run_dir.is_dir()  # gehört zu einem gehaltenen Lauf
    state.remove_run_dir()


def test_fa54_a_second_backend_on_the_same_data_dir_keeps_the_runs_of_the_first(
    runs_dir,
):
    """Der beobachtete Fehler: ein zweiter Prozess räumte beim Start die
    Laufverzeichnisse des ersten ab, dessen Läufe noch gehalten wurden."""
    first = RunManager()
    state = first.create(_document(), refresh_snapshots=False)
    first._run(state)
    assert state.run_dir is not None and any(state.run_dir.iterdir())
    written = sorted(p.name for p in state.run_dir.iterdir())

    second = RunManager()  # zweiter Prozess: kennt den Lauf des ersten nicht
    assert second.purge_orphaned_run_dirs() == []

    assert sorted(p.name for p in state.run_dir.iterdir()) == written
    state.remove_run_dir()


def test_fa54_the_age_limit_is_configurable_and_validated(runs_dir, monkeypatch):
    assert RunManager().run_dir_max_age_hours == 6.0  # Standard
    monkeypatch.setenv("GEOFACT_WEB_RUN_DIR_MAX_AGE_HOURS", "0.5")
    assert RunManager().run_dir_max_age_hours == 0.5
    assert (
        RunManager(run_dir_max_age_hours=2).run_dir_max_age_hours == 2
    )  # Konstruktor schlägt Umgebung

    stale = runs_dir / "geofact_run_halbe_stunde"
    stale.mkdir(parents=True)
    _age(stale, hours=1)  # älter als 0,5 h
    assert RunManager().purge_orphaned_run_dirs() == [stale]


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "nan", "inf"])
def test_fa54_an_invalid_age_limit_is_an_error_not_a_silent_default(
    runs_dir, monkeypatch, raw
):
    monkeypatch.setenv("GEOFACT_WEB_RUN_DIR_MAX_AGE_HOURS", raw)
    with pytest.raises(ValueError, match="GEOFACT_WEB_RUN_DIR_MAX_AGE_HOURS"):
        RunManager().purge_orphaned_run_dirs()


def test_fa54_a_missing_runs_dir_is_an_empty_purge(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "noch-nichts"))
    settings_module.get_settings.cache_clear()
    settings_module.get_settings().runs_dir.rmdir()  # ensure_dirs hat es angelegt
    assert RunManager().purge_orphaned_run_dirs() == []


def test_fa54_backend_startup_keeps_a_fresh_foreign_run_dir_and_purges_an_old_one(
    runs_dir,
):
    fresh = runs_dir / "geofact_run_frisch_abc"
    old = runs_dir / "geofact_run_alt_abc"
    for entry in (fresh, old):
        entry.mkdir(parents=True)
        (entry / "ergebnis.csv").write_text("a;b", encoding="utf-8")
    _age(old, hours=24)

    with TestClient(create_app()) as started:  # Lifespan läuft beim Eintritt
        assert started.get("/api/health").status_code == 200
    assert fresh.exists() and not old.exists()


# =====================================================================
# c) 404 für einen unbekannten Lauf: dieselbe Antwort, hilfreiche Meldung
# =====================================================================

RUN_ENDPOINTS = (
    ("get", "/api/runs/{id}"),
    ("post", "/api/runs/{id}/cancel"),
    ("get", "/api/runs/{id}/events"),
    ("get", "/api/runs/{id}/nodes/catchment"),
    ("get", "/api/runs/{id}/nodes/catchment/raster.png"),
    ("get", "/api/runs/{id}/download"),
)


@pytest.mark.parametrize(("method", "path"), RUN_ENDPOINTS)
def test_fa54_an_unknown_run_is_a_404_that_explains_why(client, method, path):
    response = getattr(client, method)(path.format(id="51b37dafc8b1"))

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "'51b37dafc8b1'" in detail and "nicht gefunden" in detail
    assert "Arbeitsspeicher" in detail and "erneut ausführen" in detail


@pytest.fixture
def register_run():
    """Legt einen Lauf im Prozess-Singleton des Routers ab und entfernt ihn danach.
    Der Zustand entsteht in einem eigenen RunManager: das Limit gleichzeitiger
    Läufe des Singletons (andere Tests lassen pending-Läufe zurück) greift nicht."""
    added: list[str] = []

    def register(owner: str = "anonymous"):
        state = RunManager().create(_document(), refresh_snapshots=False, owner=owner)
        with manager._lock:
            manager._runs[state.run_id] = state
        added.append(state.run_id)
        return state

    yield register
    with manager._lock:
        for run_id in added:
            manager._runs.pop(run_id, None)


def test_fa54_a_foreign_run_gets_the_same_answer_as_an_unknown_one(
    client, register_run
):
    """FA24 bleibt: fremder und unbekannter Lauf sind nicht zu unterscheiden."""
    state = register_run(owner="alice")

    foreign = client.get(f"/api/runs/{state.run_id}")  # Anfrage als "anonymous"
    unknown = client.get("/api/runs/000000000000")

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json()["detail"].replace(state.run_id, "<id>") == (
        unknown.json()["detail"].replace("000000000000", "<id>")
    )


def test_fa54_a_node_without_result_in_a_known_run_keeps_its_own_404(
    client, register_run
):
    """Zwei 404 mit verschiedener Bedeutung: der Knoten-Endpunkt eines BEKANNTEN
    Laufs ohne Ergebnis meldet weiter den Knoten, nicht den Lauf."""
    state = register_run()

    response = client.get(f"/api/runs/{state.run_id}/nodes/catchment")

    assert response.status_code == 404
    assert "Knoten 'catchment'" in response.json()["detail"]
    assert "Arbeitsspeicher" not in response.json()["detail"]


# =====================================================================
# d) Auto-Reload beobachtet nur Kern und Backend
# =====================================================================


@pytest.fixture
def uvicorn_calls(monkeypatch) -> list[dict]:
    """Fängt uvicorn.run ab (kein echter Server) und schaltet die Portprobe aus."""
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda app, **kwargs: calls.append({"app": app, **kwargs})
    )
    monkeypatch.setattr(main_module, "warn_if_port_in_use", lambda host, port: None)
    return calls


def test_fa54_reload_watches_only_the_core_and_the_backend(uvicorn_calls, monkeypatch):
    monkeypatch.setenv("GEOFACT_WEB_RELOAD", "1")

    main_module.run()

    (call,) = uvicorn_calls
    assert call["reload"] is True
    repo_root = settings_module.REPO_ROOT
    assert [Path(d) for d in call["reload_dirs"]] == [
        repo_root / "src" / "geofact",
        repo_root / "backend" / "geofact_web",
    ]
    for directory in map(Path, call["reload_dirs"]):
        assert directory.is_absolute() and directory.is_dir()
        # nichts aus tests/, examples/ oder docs/ darf einen Neustart auslösen
        assert not any(
            part in ("tests", "examples", "docs")
            for part in directory.relative_to(repo_root).parts
        )


def test_fa54_without_reload_no_directories_are_watched(uvicorn_calls, monkeypatch):
    monkeypatch.delenv("GEOFACT_WEB_RELOAD", raising=False)

    main_module.run()

    (call,) = uvicorn_calls
    assert call["reload"] is False and call["reload_dirs"] is None


# =====================================================================
# e) Warnung bei bereits belegtem Port
# =====================================================================


def test_fa54_a_listener_on_the_port_is_reported_at_startup():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.warns(
            RuntimeWarning, match=f"Port {port} lauscht bereits"
        ) as caught:
            main_module.warn_if_port_in_use("0.0.0.0", port)
    assert "GEOFACT_WEB_PORT" in str(caught[0].message)


def test_fa54_a_free_port_is_not_reported():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # danach wieder frei
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        main_module.warn_if_port_in_use("127.0.0.1", port)
