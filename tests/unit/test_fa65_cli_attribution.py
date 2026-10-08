"""Implements: FA65 (Lizenzen und Namensnennung in der CLI).

``geofact validate`` prints the licences of all output sources from
``plan.attribution`` without running; ``geofact run`` prints them plus the path
of ``ATTRIBUTION.txt``. Offline: file fixture, bbox region.
"""

from __future__ import annotations

import pytest
from _cli_doubles import base_config, geofact, write_config

from geofact import api


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.chdir(tmp_path)


def _licensed() -> dict:
    raw = base_config()
    raw["layers"][0]["license"] = {"name": "CC-BY-4.0", "attribution": "Stadt Beispiel"}
    return raw


def test_fa65_validate_cli_prints_the_attribution_without_running(
    tmp_path, monkeypatch, capsys
):
    def no_run(*args, **kwargs):  # validate must never execute
        raise AssertionError("validate darf nicht ausführen")

    monkeypatch.setattr(api, "run", no_run)
    config = write_config(tmp_path / "konfig", _licensed())
    code, stdout, stderr = geofact(monkeypatch, capsys, "validate", config)
    assert code == 0, stderr
    assert "Datenquellen/Lizenzen:" in stdout
    assert "Stadt Beispiel (CC-BY-4.0)" in stdout


def test_fa65_validate_cli_names_layers_without_licence(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path / "konfig", base_config())
    code, stdout, stderr = geofact(monkeypatch, capsys, "validate", config)
    assert code == 0, stderr
    assert "ohne Lizenzangabe: umspannwerke" in stdout


def test_fa65_run_cli_prints_attribution_file(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path / "konfig", _licensed())
    out = tmp_path / "out"
    code, stdout, stderr = geofact(monkeypatch, capsys, "run", config, "--out", out)
    assert code == 0, stderr
    assert (out / "ATTRIBUTION.txt").is_file()
    assert f"Namensnennung abgelegt in: {out / 'ATTRIBUTION.txt'}" in stdout
    assert "Stadt Beispiel (CC-BY-4.0)" in stdout


def test_fa65_run_cli_without_licence_writes_no_attribution_line(
    tmp_path, monkeypatch, capsys
):
    config = write_config(tmp_path / "konfig", base_config())
    code, stdout, stderr = geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )
    assert code == 0, stderr
    assert "Namensnennung abgelegt in" not in stdout
