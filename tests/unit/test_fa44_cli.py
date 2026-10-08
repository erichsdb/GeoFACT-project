"""Implements: FA44 (CLI auf dem Anwendungsfall), FA9 (CLI run), FA2 (CLI validate), FA40 (CLI plugins).

Contract-Tests der Kommandozeile `geofact validate|run|plugins`: run schreibt
die deklarierten Ausgaben, Warnungen und übersprungene Ausgaben gehen nach
stderr, Exit-Codes (0 ok / 1 Fehler / 2 Ausgabe übersprungen, 0 mit
--allow-skipped), nie ein Traceback für Konfigurationsfehler.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

from geofact import cli

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _config() -> dict:
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
        "output": [
            {"type": "geojson", "source": "einzug", "path": "einzug.geojson"},
            {"type": "csv", "source": "einzug", "path": "einzug.csv"},
        ],
    }


def _write(directory: Path, config: dict, name: str = "szenario.yaml") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return target


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.chdir(tmp_path)


def _geofact(monkeypatch, capsys, *argv: object) -> tuple[int, str, str]:
    """Der echte Einstieg (cli.main) im Prozess: (Exit-Code, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["geofact", *map(str, argv)])
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    else:
        code = 0
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# =====================================================================
# run schreibt die deklarierten Ausgaben
# =====================================================================


def test_fa44_cli_run_writes_the_declared_files_and_lists_them(
    tmp_path, monkeypatch, capsys
):
    config = _write(tmp_path / "konfig", _config())
    out = tmp_path / "ergebnis"
    code, stdout, stderr = _geofact(monkeypatch, capsys, "run", config, "--out", out)

    assert code == 0, stderr
    assert (out / "einzug.geojson").stat().st_size > 0
    assert (out / "einzug.csv").stat().st_size > 0
    assert "einzug" in stdout  # Ausführungsreihenfolge
    assert "einzug.geojson" in stdout and "einzug.csv" in stdout  # geschriebene Dateien
    assert stderr == ""


def test_fa44_cli_run_relative_layer_paths_are_relative_to_the_yaml_not_the_cwd(
    tmp_path, monkeypatch, capsys
):
    configuration = _config()
    configuration["layers"][0]["path"] = "umspannwerke.geojson"
    directory = tmp_path / "konfig"
    config = _write(directory, configuration)
    shutil.copy(
        FIXTURES_DIR / "substations.geojson", directory / "umspannwerke.geojson"
    )
    elsewhere = tmp_path / "anderswo"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    code, _, stderr = _geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )
    assert code == 0, stderr
    assert (tmp_path / "out" / "einzug.geojson").is_file()


def test_fa44_cli_run_prints_layer_warnings_to_stderr(tmp_path, monkeypatch, capsys):
    configuration = _config()
    configuration["layers"][0]["validation"] = {
        "required_fields": ["gibt_es_nicht"],
        "on_violation": "warn",
    }
    config = _write(tmp_path / "konfig", configuration)
    code, stdout, stderr = _geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )

    assert code == 0
    assert "Warnung (Layer 'umspannwerke')" in stderr
    assert "gibt_es_nicht" in stderr
    assert "Warnung" not in stdout


def test_fa44_cli_run_prints_step_warnings_to_stderr(tmp_path, monkeypatch, capsys):
    """Eine Warnung aus einer Operation (getrennte Netzkomponenten in build_network)."""
    configuration = _config()
    configuration["layers"].append(
        {
            "id": "leitungen",
            "source": "file",
            "data_type": "vector",
            "path": str(FIXTURES_DIR / "power_lines.geojson"),
        }
    )
    configuration["steps"] = [
        {
            "id": "netz",
            "op": "build_network",
            "inputs": {"lines": "leitungen", "nodes": "umspannwerke"},
            "params": {"tolerance_m": 100},
        }
    ]
    configuration["steps"].append(
        {"id": "knoten", "op": "graph_to_vector", "inputs": {"graph": "netz"}}
    )
    configuration["output"] = [
        {"type": "geojson", "source": "knoten", "path": "knoten.geojson"}
    ]
    config = _write(tmp_path / "konfig", configuration)
    code, _, stderr = _geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )
    assert code == 0
    assert "Warnung (Schritt 'netz')" in stderr


# =====================================================================
# Exit-Codes
# =====================================================================


def _config_with_a_skipped_output() -> dict:
    """Eine Ausgabe, die zur Laufzeit übersprungen wird: RDF verlangt ein CRS, eine
    Tabelle ohne Geometrie hat keins (FA13). Die Konfiguration selbst ist gültig -
    Datentyp und Format passen, erst das Ergebnis kann nicht geschrieben werden."""
    configuration = _config()
    configuration["layers"].append(
        {
            "id": "bevoelkerung",
            "source": "table",
            "delimiter": ";",
            "path": str(FIXTURES_DIR / "bip_mini.csv"),
        }
    )
    configuration["output"].append(
        {"type": "rdf", "source": "bevoelkerung", "path": "statistik.ttl"}
    )
    return configuration


def test_fa44_cli_run_exits_with_two_when_a_declared_output_is_skipped(
    tmp_path, monkeypatch, capsys
):
    config = _write(tmp_path / "konfig", _config_with_a_skipped_output())
    out = tmp_path / "out"
    code, stdout, stderr = _geofact(monkeypatch, capsys, "run", config, "--out", out)

    assert code == 2
    assert "Ausgabe übersprungen" in stderr and "bevoelkerung" in stderr
    assert (out / "einzug.geojson").is_file()  # die anderen Ausgaben sind geschrieben
    assert not (out / "statistik.ttl").exists()


def test_fa44_cli_run_allow_skipped_turns_the_exit_code_to_zero(
    tmp_path, monkeypatch, capsys
):
    config = _write(tmp_path / "konfig", _config_with_a_skipped_output())
    code, _, stderr = _geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out", "--allow-skipped"
    )
    assert code == 0
    assert "Ausgabe übersprungen" in stderr  # weiterhin gemeldet


def test_fa44_cli_run_config_error_exits_one_with_position_and_no_traceback(
    tmp_path, monkeypatch, capsys
):
    configuration = _config()
    configuration["steps"][0]["params"]["radius_km"] = "5km"
    config = _write(tmp_path / "konfig", configuration)
    code, stdout, stderr = _geofact(monkeypatch, capsys, "run", config)

    assert code == 1
    assert "Konfiguration ungültig" in stderr
    assert (
        "[steps -> 0 -> params -> radius_km] muss eine Zahl sein (erhalten: '5km')"
        in stderr
    )
    assert "Traceback" not in stderr
    assert not (tmp_path / "output").exists()  # nichts geschrieben


def test_fa44_cli_run_output_directory_that_cannot_be_created_exits_one_without_traceback(
    tmp_path, monkeypatch, capsys
):
    """Ein Dateisystemfehler beim Schreiben (hier: --out ist eine bestehende
    Datei) ist eine Meldung mit Exit-Code 1, kein Traceback."""
    config = _write(tmp_path / "konfig", _config())
    blocker = tmp_path / "ergebnis"
    blocker.write_text("ich bin eine Datei", encoding="utf-8")
    code, stdout, stderr = _geofact(
        monkeypatch, capsys, "run", config, "--out", blocker
    )

    assert code == 1
    assert "Lauf fehlgeschlagen" in stderr and "(vollständig) geschrieben" in stderr
    assert str(blocker) in stderr  # nennt den Pfad
    assert "Traceback" not in stderr and "OSError" not in stderr
    assert "Ausgeführt" not in stdout  # kein falscher Erfolg
    assert blocker.read_text(encoding="utf-8") == "ich bin eine Datei"


def test_fa44_cli_run_permission_error_while_writing_names_the_path_and_the_remedy(
    tmp_path, monkeypatch, capsys
):
    from geofact.engine import run as run_module

    out = tmp_path / "gesperrt"

    def refuse(scenario, result, out_dir, registry=None):
        raise PermissionError(13, "Zugriff verweigert", str(out_dir))

    monkeypatch.setattr(run_module, "write_outputs", refuse)
    config = _write(tmp_path / "konfig", _config())
    code, _, stderr = _geofact(monkeypatch, capsys, "run", config, "--out", out)

    assert code == 1
    assert "Zugriff verweigert" in stderr and str(out) in stderr
    assert "Schreibrechte prüfen" in stderr and "--out" in stderr
    assert "Traceback" not in stderr


def test_fa44_cli_run_unwritable_intermediate_directory_exits_one_naming_it(
    tmp_path, monkeypatch, capsys
):
    config = _write(tmp_path / "konfig", _config())
    blocker = tmp_path / "zwischen"
    blocker.write_text("Datei", encoding="utf-8")
    code, _, stderr = _geofact(
        monkeypatch,
        capsys,
        "run",
        config,
        "--out",
        tmp_path / "out",
        "--dump-intermediate",
        blocker,
    )
    assert code == 1
    assert "Lauf fehlgeschlagen" in stderr and str(blocker) in stderr
    assert "Traceback" not in stderr


def test_fa44_cli_validate_config_error_exits_one_with_german_positions(
    tmp_path, monkeypatch, capsys
):
    configuration = _config()
    del configuration["output"]
    configuration["layers"].append(
        {"id": "strassen", "source": "osm", "data_type": "vector"}
    )
    config = _write(tmp_path / "konfig", configuration)
    code, _, stderr = _geofact(monkeypatch, capsys, "validate", config)

    assert code == 1
    assert "[output] Pflichtfeld fehlt" in stderr
    assert "[layers -> 1 -> tags] Pflichtfeld fehlt" in stderr
    assert "Traceback" not in stderr


def test_fa44_cli_run_layer_failure_exits_one_naming_the_layer(
    tmp_path, monkeypatch, capsys
):
    # Eine fehlende Datei fängt seit W2-02 der Vorab-Check (FA44, ConfigError vor
    # dem Laden); ein Ladefehler entsteht hier durch eine vorhandene, unlesbare Datei.
    configuration = _config()
    broken = tmp_path / "kaputt.geojson"
    broken.write_text("kein GeoJSON", encoding="utf-8")
    configuration["layers"][0]["path"] = str(broken)
    config = _write(tmp_path / "konfig", configuration)
    code, _, stderr = _geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )

    assert code == 1
    assert "Lauf fehlgeschlagen" in stderr and "umspannwerke" in stderr
    assert "Traceback" not in stderr


def test_fa44_cli_run_step_failure_exits_one_naming_step_and_operation(
    tmp_path, monkeypatch, capsys
):
    configuration = _config()
    configuration["steps"].append(
        {
            "id": "kaputt",
            "op": "filter",
            "inputs": {"features": "einzug"},
            "params": {"condition": "spalte_gibt_es_nicht > 1"},
        }
    )
    configuration["output"] = [
        {"type": "geojson", "source": "kaputt", "path": "kaputt.geojson"}
    ]
    config = _write(tmp_path / "konfig", configuration)
    code, _, stderr = _geofact(
        monkeypatch, capsys, "run", config, "--out", tmp_path / "out"
    )

    assert code == 1
    assert "Schritt 'kaputt' (filter" in stderr


@pytest.mark.parametrize("command", ["validate", "run"])
def test_fa44_cli_missing_file_exits_one(command, tmp_path, monkeypatch, capsys):
    code, _, stderr = _geofact(
        monkeypatch, capsys, command, tmp_path / "gibt_es_nicht.yaml"
    )
    assert code == 1 and "nicht gefunden" in stderr


# =====================================================================
# Zwischenergebnisse, Standard-Ausgabeverzeichnis, plugins
# =====================================================================


def test_fa44_cli_run_dump_intermediate_writes_vector_steps(
    tmp_path, monkeypatch, capsys
):
    config = _write(tmp_path / "konfig", _config())
    dump = tmp_path / "zwischen"
    code, stdout, _ = _geofact(
        monkeypatch,
        capsys,
        "run",
        config,
        "--out",
        tmp_path / "out",
        "--dump-intermediate",
        dump,
    )
    assert code == 0
    assert (dump / "einzug.geojson").stat().st_size > 0
    assert str(dump) in stdout


def test_fa44_cli_run_default_out_dir_is_output_below_cwd_named_after_the_yaml(
    tmp_path, monkeypatch, capsys
):
    config = _write(tmp_path / "konfig", _config(), name="mein_szenario.yaml")
    work = tmp_path / "arbeit"
    work.mkdir()
    monkeypatch.chdir(work)
    code, _, stderr = _geofact(monkeypatch, capsys, "run", config)
    assert code == 0, stderr
    assert (work / "output" / "mein_szenario" / "einzug.geojson").is_file()


def test_fa44_cli_plugins_lists_the_extension_points(monkeypatch, capsys):
    code, stdout, _ = _geofact(monkeypatch, capsys, "plugins")
    assert code == 0
    assert "Operationen" in stdout and "buffer" in stdout and "Ausgabeformate" in stdout


def test_fa44_cli_run_unwritable_dump_directory_is_reported_before_the_run(
    tmp_path, monkeypatch, capsys
):
    """Nachtreview 02.10. (Runde 2): vorher lief die Analyse, die Ausgaben
    wurden geschrieben, und die Meldung behauptete, die Ausgaben in --out
    seien nicht geschrieben."""
    config = _write(tmp_path / "konfig", _config())
    blocker = tmp_path / "zwischen"
    blocker.write_text("Datei", encoding="utf-8")
    code, stdout, stderr = _geofact(
        monkeypatch,
        capsys,
        "run",
        config,
        "--out",
        tmp_path / "out",
        "--dump-intermediate",
        blocker,
    )
    assert code == 1
    assert "--dump-intermediate" in stderr and str(blocker) in stderr
    assert "Ausgabeverzeichnis" not in stderr
    assert not (tmp_path / "out").exists()
    assert "Ausgeführt" not in stdout
