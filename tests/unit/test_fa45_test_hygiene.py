"""Implements: FA45 (Standardlauf offline; Live-Tests nur auf Anforderung).

Contract-Tests für die Test-Konfiguration: ``pytest`` ohne Argumente läuft
offline (pyproject.toml ``addopts``, Marke ``netcheck``, Netzsperre in
tests/conftest.py); Live-Tests laufen nur mit ``pytest -m netcheck``.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pytest_options() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["tool"]["pytest"]["ini_options"]


def test_fa45_default_run_excludes_live_network_tests():
    assert "not netcheck" in _pytest_options()["addopts"]


def test_fa45_test_markers_are_registered():
    markers = {entry.split(":", 1)[0] for entry in _pytest_options()["markers"]}
    assert {"netcheck", "integration", "e2e"} <= markers


def test_fa45_readme_documents_how_to_run_live_and_integration_tests():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "pytest -m netcheck" in readme
    assert "pytest -m integration" in readme
    assert "GEOFACT_PG_DSN" in readme


def test_fa45_live_tests_are_deselected_by_default():
    """Ein Lauf ohne -m sammelt keinen der Live-Tests ein (die Marke trifft
    wirklich: kein Tippfehler, der sie stillschweigend wieder aktiviert)."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "tests/integration",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert "deselected" in result.stdout, result.stdout[-400:]
    assert "catalog_endpoints_live" not in result.stdout
    assert "backends_equivalent" not in result.stdout


def test_fa45_default_tests_cannot_reach_the_network():
    """Die Netzsperre aus conftest.py: eine Verbindung ins Netz scheitert laut
    und nennt den Ausweg."""
    with pytest.raises(RuntimeError, match="Offline-Test"):
        socket.create_connection(
            ("192.0.2.1", 80), timeout=1
        )  # TEST-NET-1, nie geroutet


def test_fa45_default_tests_may_use_loopback():
    """Randfall: lokale Demo-Dienste (127.0.0.1) bleiben erreichbar."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=2):
            pass
