"""Implements: FA55 (Nachbedingung 2: Arbeits-CRS im Lauf sichtbar - CLI-Zeilen), FA56 (Regionswarnungen auf stderr).

``geofact validate`` names the declared working CRS; ``geofact run`` prints the
``region_ready`` line ``Region ... -> Arbeits-CRS EPSG:... (auto|deklariert)``
and every region warning on stderr. Offline: file fixture, bbox region.
"""

from __future__ import annotations

import pytest
from _cli_doubles import base_config, geofact, write_config


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.chdir(tmp_path)


def test_fa55_cli_validate_prints_auto_working_crs(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path / "konfig", base_config())
    code, stdout, stderr = geofact(monkeypatch, capsys, "validate", config)
    assert code == 0, stderr
    assert "Arbeits-CRS: auto" in stdout


def test_fa55_cli_validate_prints_declared_working_crs(tmp_path, monkeypatch, capsys):
    raw = base_config()
    raw["scenario"]["crs"] = "EPSG:25833"
    config = write_config(tmp_path / "konfig", raw)
    code, stdout, stderr = geofact(monkeypatch, capsys, "validate", config)
    assert code == 0, stderr
    assert "Arbeits-CRS: EPSG:25833 (deklariert)" in stdout


def test_fa55_cli_run_prints_the_region_line_with_auto_crs(
    tmp_path, monkeypatch, capsys
):
    config = write_config(tmp_path / "konfig", base_config())
    code, stdout, stderr = geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )
    assert code == 0, stderr
    [line] = [line for line in stdout.splitlines() if line.startswith("Region ")]
    assert "13.6" in line and "km2" in line
    assert line.endswith("-> Arbeits-CRS EPSG:32633 (auto)")


def test_fa55_cli_run_prints_the_region_line_with_declared_crs(
    tmp_path, monkeypatch, capsys
):
    raw = base_config()
    raw["scenario"]["crs"] = "equal_area_europe"
    config = write_config(tmp_path / "konfig", raw)
    code, stdout, stderr = geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )
    assert code == 0, stderr
    assert "-> Arbeits-CRS EPSG:3035 (deklariert)" in stdout


def test_fa56_cli_run_prints_region_warnings_to_stderr(tmp_path, monkeypatch, capsys):
    raw = base_config()
    raw["scenario"]["region"] = (
        "5.0,47.0,15.0,55.0"  # wider than one UTM zone under auto
    )
    config = write_config(tmp_path / "konfig", raw)
    code, _, stderr = geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )
    assert code == 0, stderr
    assert "Regionswarnung:" in stderr
