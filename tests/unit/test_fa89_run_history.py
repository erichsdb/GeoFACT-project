"""Implements: FA89 (Läufe des Nutzers auflisten und einen früheren Lauf öffnen) - Backend-Teil.

Contract-Tests von ``GET /api/runs``: nur eigene Läufe, neuester zuerst, genau
die Läufe, die das Backend noch hält, mit Zeitpunkten und Zahl der Ausgaben.
Randfall: leere Liste. Typfehler-Fall: fremder Nutzer, fehlendes Token.

Der Frontend-Teil steht in test_fa89_frontend_run_history.py.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from geofact import api  # noqa: E402
from geofact_web import auth as auth_module  # noqa: E402
from geofact_web import scenario_service  # noqa: E402
from geofact_web import settings as settings_module  # noqa: E402
from geofact_web.main import create_app  # noqa: E402
from geofact_web.routers import runs as runs_router  # noqa: E402
from geofact_web.run_manager import RunManager  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _config(name: str = "Verlauf") -> dict:
    return {
        "scenario": {"name": name, "region": BBOX_REGION},
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
        "output": [
            {"type": "geojson", "source": "catchment", "path": "catchment.geojson"},
            {"type": "csv", "source": "catchment", "path": "catchment.csv"},
        ],
    }


@pytest.fixture
def fresh_manager(tmp_path, monkeypatch) -> RunManager:
    """Ein eigener Manager je Test: die Liste soll nicht von Läufen anderer
    Tests abhängen (der Manager des Moduls ist prozessweit)."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("GEOFACT_WEB_DATA_DIR", str(tmp_path / "web"))
    monkeypatch.delenv("GEOFACT_WEB_USERS", raising=False)
    settings_module.get_settings.cache_clear()
    own = RunManager(max_retained_runs=3)
    monkeypatch.setattr(runs_router, "manager", own)
    try:
        yield own
    finally:
        settings_module.get_settings.cache_clear()


@pytest.fixture
def client(fresh_manager) -> TestClient:
    return TestClient(create_app())


def _run(client: TestClient, headers: dict | None = None, **body) -> str:
    created = client.post("/api/runs", json=body, headers=headers)
    assert created.status_code == 200, created.text
    run_id = created.json()["run_id"]
    deadline = time.monotonic() + 30
    while True:
        status = client.get(f"/api/runs/{run_id}", headers=headers).json()["status"]
        if status in ("done", "error", "cancelled"):
            return run_id
        assert time.monotonic() < deadline, "web run did not finish"
        time.sleep(0.05)


def _wait_finished(
    client: TestClient, run_id: str, headers: dict | None = None
) -> dict:
    """finished_at wird nach dem letzten Ereignis gesetzt - kurz darauf warten."""
    deadline = time.monotonic() + 10
    while True:
        entry = next(
            r
            for r in client.get("/api/runs", headers=headers).json()["runs"]
            if r["run_id"] == run_id
        )
        if entry["finished_at"] is not None:
            return entry
        assert time.monotonic() < deadline, "run has no finished_at"
        time.sleep(0.05)


# =====================================================================
# Liste
# =====================================================================


def test_fa89_without_runs_the_list_is_empty_and_names_the_limit(client):
    body = client.get("/api/runs").json()
    assert body == {"runs": [], "max_retained": 3}


def test_fa89_a_finished_run_is_listed_with_name_times_and_counts(client):
    before = datetime.now(timezone.utc)
    run_id = _run(client, config=_config("Mein Szenario"))
    entry = _wait_finished(client, run_id)

    assert entry["status"] == "done" and entry["scenario_name"] == "Mein Szenario"
    assert (entry["step_count"], entry["output_count"], entry["skipped_count"]) == (
        1,
        2,
        0,
    )
    assert entry["parameters"] is None
    created = datetime.fromisoformat(entry["created_at"])
    finished = datetime.fromisoformat(entry["finished_at"])
    assert (
        created.tzinfo is not None
        and before.replace(microsecond=0) <= created <= finished
    )


def test_fa89_newest_run_comes_first(client):
    first = _run(client, config=_config("eins"))
    second = _run(client, config=_config("zwei"))
    listed = client.get("/api/runs").json()["runs"]
    assert [(r["run_id"], r["scenario_name"]) for r in listed] == [
        (second, "zwei"),
        (first, "eins"),
    ]


def test_fa89_a_run_that_is_not_finished_has_no_finished_at_and_no_outputs(
    client, fresh_manager
):
    document, _ = scenario_service.validate_config(None, _config("wartet"))
    state = fresh_manager.create(document, refresh_snapshots=False)

    entry = client.get("/api/runs").json()["runs"][0]
    assert entry["run_id"] == state.run_id and entry["status"] == "pending"
    assert entry["finished_at"] is None and entry["output_count"] == 0


def test_fa89_skipped_outputs_are_counted(client):
    config = _config()
    config["layers"].append(
        {
            "id": "bevoelkerung",
            "source": "table",
            "delimiter": ";",
            "path": (FIXTURES_DIR / "bip_mini.csv").as_posix(),
        }
    )
    config["output"].append(
        {"type": "rdf", "source": "bevoelkerung", "path": "statistik.ttl"}
    )
    with pytest.warns(UserWarning, match="nicht alle deklarierten Ausgaben"):
        run_id = _run(client, config=config)
    entry = _wait_finished(client, run_id)
    assert (entry["output_count"], entry["skipped_count"]) == (2, 1)


def test_fa89_parameter_overrides_travel_with_the_entry(client, fresh_manager):
    document, _ = scenario_service.validate_config(None, _config())
    state = fresh_manager.create(document, refresh_snapshots=False)
    state.parameters = {"radius": 2}
    assert client.get("/api/runs").json()["runs"][0]["parameters"] == {"radius": 2}


# =====================================================================
# Genau die Läufe, die das Backend noch hält
# =====================================================================


def test_fa89_an_evicted_run_is_gone_from_the_list_and_from_the_status_endpoint(
    fresh_manager,
):
    manager = RunManager(max_retained_runs=1)
    document = api.load_scenario(yaml.safe_load(yaml.safe_dump(_config())))
    first = manager.create(document, refresh_snapshots=False)
    manager._run(first)
    second = manager.create(document, refresh_snapshots=False)
    manager._run(second)

    assert [state.run_id for state in manager.list_for_owner("anonymous")] == [
        second.run_id
    ]
    assert manager.get(first.run_id) is None
    second.remove_run_dir()


def test_fa89_every_listed_run_answers_the_status_endpoint(client):
    for name in ("a", "b", "c", "d"):  # eine mehr, als der Manager hält
        _run(client, config=_config(name))
    time.sleep(0.2)  # die Verdrängung läuft nach dem letzten Ereignis
    listed = client.get("/api/runs").json()["runs"]
    assert [r["scenario_name"] for r in listed] == ["d", "c", "b"]
    assert all(
        client.get(f"/api/runs/{r['run_id']}").status_code == 200 for r in listed
    )


def test_fa89_finished_at_is_set_for_failed_and_cancelled_runs_too(fresh_manager):
    document = api.load_scenario(_config())
    state = fresh_manager.create(document, refresh_snapshots=False)
    state.cancel.cancel()
    fresh_manager._run(state)
    assert state.status == "cancelled"
    assert state.finished_at is not None and state.finished_at >= state.created_at


# =====================================================================
# Anmeldung (FA24)
# =====================================================================


@pytest.fixture
def two_users(monkeypatch, fresh_manager):
    users = ",".join(
        f"{name}:{auth_module.hash_password(name + '-geheim')}"
        for name in ("alice", "bob")
    )
    monkeypatch.setenv("GEOFACT_WEB_USERS", users)
    settings_module.get_settings.cache_clear()


def _headers(client: TestClient, name: str) -> dict:
    response = client.post(
        "/api/auth/login", json={"username": name, "password": name + "-geheim"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_fa89_the_list_holds_only_the_runs_of_the_user(client, two_users):
    alice, bob = _headers(client, "alice"), _headers(client, "bob")
    own = _run(client, headers=alice, config=_config("von alice"))
    other = _run(client, headers=bob, config=_config("von bob"))

    assert [
        r["run_id"] for r in client.get("/api/runs", headers=alice).json()["runs"]
    ] == [own]
    assert [
        r["run_id"] for r in client.get("/api/runs", headers=bob).json()["runs"]
    ] == [other]


def test_fa89_the_list_needs_a_token_once_users_are_configured(client, two_users):
    assert client.get("/api/runs").status_code == 401
    token = _headers(client, "alice")["Authorization"].removeprefix("Bearer ")
    assert client.get(f"/api/runs?token={token}").status_code == 401
