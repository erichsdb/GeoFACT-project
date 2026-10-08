"""Implements: FA93 (Abbruch eines Laufs gilt in der Web-Demo sofort).

Der Kern hält an der nächsten Layer- oder Schrittgrenze an (FA9); ein Baustein,
der gerade lädt oder rechnet, lässt sich nicht unterbrechen. Die Web-Demo meldet
den Abbruch deshalb sofort und verwirft, was der Arbeits-Thread danach noch
liefert. Hier steht ein blockierender Lauf für einen Baustein, der gerade lädt.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from geofact import api  # noqa: E402
from geofact.api import events as ev  # noqa: E402
from geofact_web import run_manager as run_manager_module  # noqa: E402
from geofact_web.run_manager import _SENTINEL, RunLimitExceeded, RunManager  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO / "tests" / "fixtures"
FRONTEND = REPO / "frontend"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
RUN_CANCEL = FRONTEND / "lib" / "runCancel.ts"
DRIVER = Path(__file__).resolve().parent / "fa93_run_cancel_driver.mjs"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _document() -> api.ScenarioDocument:
    return api.load_scenario(
        {
            "scenario": {"name": "sofort-abbrechen", "region": BBOX_REGION},
            "layers": [
                {
                    "id": "substations",
                    "source": "file",
                    "data_type": "vector",
                    "path": str(FIXTURES_DIR / "substations.geojson"),
                },
            ],
            "steps": [
                {
                    "id": "first",
                    "op": "buffer",
                    "inputs": {"geometry": "substations"},
                    "params": {"radius_km": 1},
                },
                {
                    "id": "slow",
                    "op": "buffer",
                    "inputs": {"geometry": "first"},
                    "params": {"radius_km": 0.2},
                },
            ],
            "output": [{"type": "geojson", "source": "slow", "path": "slow.geojson"}],
        }
    )


class _BlockingRun:
    """Stands in for `api.run`: the first step finishes, the second one hangs like a
    fetch until `release` is set. What it does afterwards is chosen per test."""

    def __init__(self, after: str) -> None:
        self.after = after  # "cancelled" | "done" | "error"
        self.blocked = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self, document, *, observer, cancel, **_):
        try:
            observer(ev.ProgressEvent(type=ev.PLAN))
            observer(ev.ProgressEvent(type=ev.STEP_RUNNING, step="first"))
            observer(ev.ProgressEvent(type=ev.STEP_DONE, step="first", duration_ms=1.0))
            observer(ev.ProgressEvent(type=ev.STEP_RUNNING, step="slow"))
            self.blocked.set()
            assert self.release.wait(timeout=30)
            observer(ev.ProgressEvent(type=ev.STEP_DONE, step="slow", duration_ms=2.0))
            if self.after == "cancelled":
                assert cancel.cancelled
                observer(ev.ProgressEvent(type=ev.RUN_CANCELLED))
                raise api.RunCancelled("Lauf abgebrochen")
            if self.after == "error":
                raise RuntimeError("Dienst nicht erreichbar")
            observer(ev.ProgressEvent(type=ev.RUN_DONE))
            raise AssertionError("a detached run must not need a report")
        finally:
            self.finished.set()


def _started(monkeypatch, after: str = "cancelled"):
    fake = _BlockingRun(after)
    monkeypatch.setattr(run_manager_module.api, "run", fake)
    manager = RunManager(max_retained_runs=5)
    state = manager.create(_document(), refresh_snapshots=False)
    manager.start(state)
    assert fake.blocked.wait(timeout=30)
    return manager, state, fake


def _wait_for_worker(state, fake) -> None:
    fake.release.set()
    assert fake.finished.wait(timeout=30)
    for _ in range(300):
        if not state.worker_active:
            return
        threading.Event().wait(0.01)
    raise AssertionError("worker did not finish")


def test_fa93_cancel_takes_effect_while_a_block_is_still_running(monkeypatch):
    manager, state, fake = _started(monkeypatch)
    stream = state.subscribe()
    assert state.status == "running"

    assert manager.cancel(state.run_id) is True

    # At once, although the block has not returned: status, steps, event, end of stream.
    assert not fake.release.is_set()
    status = state.to_status()
    assert status.status == "cancelled"
    assert status.steps["first"].status == "done"
    assert status.steps["slow"].status == "cancelled"
    assert state.finished_at is not None
    assert [e["type"] for e in state.events][-1] == ev.RUN_CANCELLED
    received = []
    while True:
        item = stream.get(timeout=5)
        if item is _SENTINEL:
            break
        received.append(item["type"])
    assert received[-1] == ev.RUN_CANCELLED

    _wait_for_worker(state, fake)


@pytest.mark.parametrize("after", ["cancelled", "done", "error"])
def test_fa93_nothing_the_worker_reports_later_changes_the_run(monkeypatch, after):
    manager, state, fake = _started(monkeypatch, after)
    manager.cancel(state.run_id)
    events_at_cancel = list(state.events)
    finished_at = state.finished_at

    _wait_for_worker(state, fake)

    assert state.status == "cancelled"
    assert state.error is None
    assert state.report is None and state.result is None
    assert state.to_status().steps["slow"].status == "cancelled"
    assert state.events == events_at_cancel
    assert [e["type"] for e in state.events].count(ev.RUN_CANCELLED) == 1
    assert ev.RUN_DONE not in [e["type"] for e in state.events]
    assert state.finished_at == finished_at


def test_fa93_cancelled_run_counts_against_the_limit_until_its_worker_ends(monkeypatch):
    manager, state, fake = _started(monkeypatch)
    monkeypatch.setattr(
        run_manager_module,
        "get_settings",
        lambda: type("S", (), {"max_concurrent_runs_per_user": 1})(),
    )
    manager.cancel(state.run_id)
    assert state.worker_active is True
    with pytest.raises(RunLimitExceeded):
        manager.create(_document(), refresh_snapshots=False)

    _wait_for_worker(state, fake)

    assert manager.create(_document(), refresh_snapshots=False).status == "pending"


def test_fa93_cancel_of_a_finished_or_unknown_run_changes_nothing(monkeypatch):
    manager, state, fake = _started(monkeypatch)
    assert manager.cancel(state.run_id) is True
    events = list(state.events)
    assert manager.cancel(state.run_id) is False
    assert manager.cancel("gibt-es-nicht") is False
    assert state.events == events
    _wait_for_worker(state, fake)


def test_fa93_cancel_before_the_first_event_ends_as_cancelled():
    manager = RunManager(max_retained_runs=5)
    state = manager.create(_document(), refresh_snapshots=False)
    assert manager.cancel(state.run_id) is True
    manager._run(state)  # the core stops at its first boundary (FA9)
    assert state.status == "cancelled"
    assert [e["type"] for e in state.events] == [ev.RUN_CANCELLED]
    assert all(step.status == "pending" for step in state.to_status().steps.values())


# --- Frontend ---------------------------------------------------------------


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(DRIVER), str(RUN_CANCEL)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    if proc.returncode != 0 and "ERR_UNKNOWN_FILE_EXTENSION" in proc.stderr:
        pytest.skip("node without TypeScript type stripping")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_fa93_frontend_marks_the_run_and_its_active_steps_cancelled(js):
    assert js["status"] == "cancelled"
    assert js["steps"] == {
        "a": "done",
        "b": "cancelled",
        "c": "cancelled",
        "d": "pending",
    }
    assert js["idempotent"] is True
    assert js["input_untouched"] is True
    assert js["other_fields_kept"] is True


def test_fa93_frontend_shows_the_cancel_before_the_request_returns():
    source = APP_SHELL.read_text(encoding="utf-8").replace("\r\n", "\n")
    begin = source.index("const cancelRun = async () => {")
    body = source[begin : source.index("\n  };\n", begin)]
    assert body.index("cancelRunStatus(prev)") < body.index(
        "await api.cancelRun(runId)"
    )
    # The event of the backend leads to the same state.
    assert 'if (type === "run_cancelled") return cancelRunStatus(status);' in source
