"""Implements: FA1, FA2 (CLI-Anteil), FA9 (CLI-run-Anteil).

Tests für `geofact validate|run <config>`.
"""

from pathlib import Path

import pytest
import yaml

from geofact.cli import build_parser

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"


def run_validate(config_path: Path) -> int:
    parser = build_parser()
    args = parser.parse_args(["validate", str(config_path)])
    return args.func(args)


@pytest.mark.parametrize(
    "example_file",
    ["sachsen_nachthimmel.yaml", "szenario2_gruenflaechen.yaml"],
)
def test_fa01_cli_validate_valid_example_exits_zero(example_file, capsys):
    exit_code = run_validate(EXAMPLES_DIR / example_file)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Ausführungsreihenfolge" in captured.out


def test_fa02_cli_validate_broken_config_exits_one_with_position(capsys):
    exit_code = run_validate(FIXTURES_DIR / "szenario_kaputt.yaml")
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "catchment" in captured.err
    assert "unbekannte Quelle" in captured.err


def test_fa01_cli_validate_missing_file_exits_one(capsys):
    exit_code = run_validate(FIXTURES_DIR / "does_not_exist.yaml")
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "nicht gefunden" in captured.err


def test_fa09_cli_run_missing_file_exits_one(capsys):
    parser = build_parser()
    args = parser.parse_args(["run", str(FIXTURES_DIR / "does_not_exist.yaml")])
    exit_code = args.func(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "nicht gefunden" in captured.err


def test_fa09_cli_run_broken_config_exits_one(capsys):
    parser = build_parser()
    args = parser.parse_args(["run", str(FIXTURES_DIR / "szenario_kaputt.yaml")])
    exit_code = args.func(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "unbekannte Quelle" in captured.err


def test_fa09_cli_run_file_scenario_exits_zero(tmp_path, capsys):
    config = {
        "scenario": {"name": "CLI-Run-Test", "region": "13.60,51.01,13.94,51.15"},
        "layers": [
            {
                "id": "substations",
                "source": "file",
                "path": str(FIXTURES_DIR / "substations.geojson"),
                "data_type": "vector",
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
        "output": [{"type": "map", "source": "catchment"}],
    }
    config_path = tmp_path / "szenario.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(["run", str(config_path)])
    exit_code = args.func(args)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "catchment" in captured.out
