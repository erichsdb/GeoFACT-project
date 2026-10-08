"""Shared helpers for the CLI contract tests (W3-01): run ``cli.main`` in
process and write scenario dicts as YAML. Offline: file fixtures and a bbox
region, no network."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from geofact import cli

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def base_config() -> dict:
    return {
        "scenario": {"name": "CLI", "region": BBOX_REGION},
        "layers": [
            {
                "id": "umspannwerke",
                "source": "file",
                "data_type": "vector",
                "path": str(FIXTURES_DIR / "substations.geojson"),
            },
        ],
        "steps": [
            {
                "id": "einzug",
                "op": "buffer",
                "inputs": {"geometry": "umspannwerke"},
                "params": {"radius_km": 1},
            },
        ],
        "output": [{"type": "geojson", "source": "einzug", "path": "einzug.geojson"}],
    }


def write_config(directory: Path, config: dict, name: str = "szenario.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return target


def geofact(monkeypatch, capsys, *argv: object) -> tuple[int, str, str]:
    """The real entry point (cli.main) in process: (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["geofact", *map(str, argv)])
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err
