"""Implements: FA66 (Nachbedingung 4: CLI schaltet die Freigabe ein, ausser bei --dump-intermediate oder --keep-intermediates).

The CLI passes ``release_intermediates=True`` to ``api.run`` by default and
False when ``--keep-intermediates`` or ``--dump-intermediate`` is given;
``api.run`` itself defaults to False. Offline: file fixture, bbox region.
"""

from __future__ import annotations

import inspect

import pytest
from _cli_doubles import base_config, geofact, write_config

from geofact import api


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def seen(monkeypatch) -> list[bool]:
    calls: list[bool] = []
    original = api.run

    def spy(document, **kwargs):
        calls.append(kwargs.get("release_intermediates"))
        return original(document, **kwargs)

    monkeypatch.setattr(api, "run", spy)
    return calls


def _chain() -> dict:
    raw = base_config()
    raw["steps"].append(
        {
            "id": "groß",
            "op": "buffer",
            "inputs": {"geometry": "einzug"},
            "params": {"radius_km": 1},
        }
    )
    raw["output"] = [{"type": "geojson", "source": "groß", "path": "gross.geojson"}]
    return raw


def test_fa66_api_run_defaults_to_keeping_intermediates():
    assert (
        inspect.signature(api.run).parameters["release_intermediates"].default is False
    )


def test_fa66_cli_run_releases_intermediates_by_default(
    tmp_path, monkeypatch, capsys, seen
):
    config = write_config(tmp_path / "konfig", _chain())
    out = tmp_path / "out"
    code, _, stderr = geofact(monkeypatch, capsys, "run", config, "--out", out)
    assert code == 0, stderr
    assert seen == [True]
    assert (out / "gross.geojson").is_file()


def test_fa66_cli_keep_intermediates_turns_release_off(
    tmp_path, monkeypatch, capsys, seen
):
    config = write_config(tmp_path / "konfig", _chain())
    code, _, stderr = geofact(
        monkeypatch,
        capsys,
        "run",
        config,
        "--out",
        tmp_path / "out",
        "--keep-intermediates",
    )
    assert code == 0, stderr
    assert seen == [False]


def test_fa66_cli_dump_intermediate_turns_release_off(
    tmp_path, monkeypatch, capsys, seen
):
    config = write_config(tmp_path / "konfig", _chain())
    dump = tmp_path / "zwischen"
    code, _, stderr = geofact(
        monkeypatch,
        capsys,
        "run",
        config,
        "--out",
        tmp_path / "out",
        "--dump-intermediate",
        dump,
    )
    assert code == 0, stderr
    assert seen == [False]
    assert (dump / "einzug.geojson").is_file()
