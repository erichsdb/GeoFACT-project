"""Implements: FA44 (Vorab-Check der Layer-Pfade in der CLI).

A missing layer file makes ``geofact run`` exit 1 with the position
``layers -> i (id) -> path`` before the region is resolved or anything is
loaded; ``geofact validate`` does not check paths. Offline.
"""

from __future__ import annotations

import pytest
from _cli_doubles import base_config, geofact, write_config


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.chdir(tmp_path)


def _missing_file() -> dict:
    raw = base_config()
    raw["layers"][0]["path"] = "gibt_es_nicht.geojson"
    return raw


def test_fa44_cli_run_missing_layer_file_names_the_position(
    tmp_path, monkeypatch, capsys
):
    config = write_config(tmp_path / "konfig", _missing_file())
    code, stdout, stderr = geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )
    assert code == 1
    assert "Konfiguration ungültig" in stderr
    assert "[layers -> 0 (umspannwerke) -> path]" in stderr
    assert "gibt_es_nicht.geojson" in stderr
    assert "Traceback" not in stderr
    assert not any(line.startswith("Region ") for line in stdout.splitlines())


def test_fa44_cli_validate_does_not_check_layer_paths(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path / "konfig", _missing_file())
    code, _, stderr = geofact(monkeypatch, capsys, "validate", config)
    assert code == 0, stderr
