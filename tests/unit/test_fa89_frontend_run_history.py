"""Implements: FA89 (Läufe des Nutzers auflisten und einen früheren Lauf öffnen) - Frontend-Teil.

Die reinen Helfer in frontend/lib/runFiles.ts laufen über fa88_run_files_driver.mjs
unter node; die Nachbedingungen an die Ansicht (Liste in der Kopfzeile, „Ansehen“
ändert den Editor nicht, Übernahme der Konfiguration aus dem Hinweis, ein dem
Backend unbekannter Lauf wird gemeldet) sind Quelltextverträge. Nicht im Browser
geprüft.

Der Backend-Teil steht in test_fa89_run_history.py.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
HELPERS = FRONTEND / "lib" / "runFiles.ts"
API = FRONTEND / "lib" / "api.ts"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
RUN_OUTPUTS = FRONTEND / "components" / "run-outputs.tsx"
DRIVER = Path(__file__).resolve().parent / "fa88_run_files_driver.mjs"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _between(source: str, begin: str, end: str) -> str:
    start = source.index(begin)
    return source[start : source.index(end, start + len(begin))]


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(DRIVER), str(HELPERS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"node cannot run the TypeScript helpers: {proc.stderr[-300:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --- Helfer (unter node ausgeführt) -------------------------------------------


def test_fa89_every_status_has_a_german_label(js) -> None:
    assert js["status"] == {
        "pending": "wartet",
        "running": "läuft",
        "done": "fertig",
        "error": "fehlgeschlagen",
        "cancelled": "abgebrochen",
    }


def test_fa89_counts_name_steps_files_and_skipped_outputs(js) -> None:
    assert js["counts"] == {
        "singular": "1 Schritt · 1 Datei",
        "plural": "3 Schritte · 2 Dateien",
        "none": "0 Schritte · 0 Dateien",
        "skipped": "3 Schritte · 2 Dateien · 1 übersprungen",
    }


def test_fa89_time_is_relative_for_today_and_yesterday(js) -> None:
    assert js["time"]["today"] == "heute, 14:32"
    assert js["time"]["just_after_midnight"] == "heute, 00:05"
    assert js["time"]["yesterday"] == "gestern, 23:55"  # Kalendertag, nicht 24 Stunden
    assert js["time"]["older"] == "06.10.2026, 18:00"


def test_fa89_an_unreadable_time_is_called_unknown_not_guessed(js) -> None:
    assert (
        js["time"]["garbage"]
        == js["time"]["empty"]
        == js["time"]["null"]
        == "Zeitpunkt unbekannt"
    )


def test_fa89_the_note_names_the_limit_and_the_restart(js) -> None:
    note = js["note"]["with_limit"]
    assert (
        "letzten 20 abgeschlossenen Läufe" in note
        and "Arbeitsspeicher" in note
        and "Neustart" in note
    )
    # ohne bekannte Grenze keine erfundene Zahl
    assert "Neustart" in js["note"]["unknown"] and not any(
        ch.isdigit() for ch in js["note"]["unknown"]
    )


# --- Liste in der Kopfzeile -----------------------------------------------------


def test_fa89_api_client_lists_the_runs() -> None:
    assert 'listRuns: () => request<RunHistory>("/api/runs")' in _text(API)


def test_fa89_header_offers_the_runs() -> None:
    header = _between(_text(APP_SHELL), "<header", "</header>")
    assert (
        "<RunHistoryDialog currentRunId={runId} onOpenRun={openRun} onDownload={downloadFromRun} />"
        in header
    )


def test_fa89_the_list_is_loaded_when_the_dialog_opens_and_says_why_it_is_finite() -> (
    None
):
    dialog = _between(_text(RUN_OUTPUTS), "export function RunHistoryDialog(", "\n}\n")
    show = _between(dialog, "const show = () => {", "};")
    assert "setOpen(true);" in show and "reload();" in show
    assert "api\n      .listRuns()" in dialog
    assert "historyNote(state.data ? state.data.max_retained : null)" in dialog
    assert "Noch kein Lauf." in dialog  # Randfall: leere Liste


def test_fa89_each_run_shows_name_time_status_counts_and_its_files() -> None:
    dialog = _between(_text(RUN_OUTPUTS), "export function RunHistoryDialog(", "\n}\n")
    for part in (
        "{run.scenario_name}",
        "formatRunTime(run.created_at)",
        "runStatusLabel(run.status)",
        "runCounts(run)",
    ):
        assert part in dialog, part
    assert (
        "const current = run.run_id === currentRunId;" in dialog
        and "geöffnet" in dialog
    )
    # dieselbe Dateiliste wie in Schritt 3 (FA88), erst beim Aufklappen geladen
    assert (
        "{isOpen && (" in dialog
        and "<RunFiles" in dialog
        and "onGone={reload}" in dialog
    )


def test_fa89_a_run_that_could_not_be_opened_reloads_the_list() -> None:
    dialog = _between(_text(RUN_OUTPUTS), "export function RunHistoryDialog(", "\n}\n")
    view = _between(dialog, "const view = async (run: RunSummary) => {", "\n  };")
    assert (
        "if (await onOpenRun(run)) setOpen(false);" in view and "else reload();" in view
    )


# --- Einen früheren Lauf öffnen ---------------------------------------------------


def _open_run() -> str:
    return _between(
        _text(APP_SHELL),
        "const openRun = async (run: RunSummary)",
        "// FA89: die Konfiguration des gezeigten Laufs",
    )


def test_fa89_opening_a_run_loads_its_status_and_its_configuration() -> None:
    body = _open_run()
    assert (
        'Promise.all([api.runStatus(id), api.runFileText(id, "scenario.yaml")])' in body
    )
    assert "setRunId(id);" in body and "setStatus(s);" in body
    # die Basis des Laufs ist seine eigene Konfiguration: weicht der Editor ab, ist er "frühere Fassung" (FA86)
    assert "setRunBasis({ runId: id, yaml, params });" in body
    assert 'setStep("result");' in body


def test_fa89_opening_a_run_leaves_a_filled_editor_alone() -> None:
    body = _open_run()
    guarded = _between(body, "if (!configYamlRef.current.trim()) {", "}")
    assert (
        "applyConfigYaml(yaml);" in guarded and "setParamOverrides(params);" in guarded
    )
    # außerhalb dieser Bedingung wird der Editor nicht angefasst
    assert body.count("applyConfigYaml(") == 1 and body.count("setParamOverrides(") == 1


def test_fa89_a_run_that_is_still_running_is_followed_not_shown_as_a_result() -> None:
    body = _open_run()
    running = _between(
        body, 'if (s.status === "running" || s.status === "pending") {', "} else {"
    )
    assert "subscribe(id);" in running and 'setStep("execution");' in running


def test_fa89_an_unknown_run_is_reported_and_not_opened() -> None:
    body = _open_run()
    failure = _between(body, "} catch (e) {", "\n  };")
    assert (
        "notify.error(isNotFoundError(e) ? RUN_GONE_MESSAGE" in failure
        and "return false;" in failure
    )
    # während ein Lauf angelegt wird, wechselt die Ansicht nicht
    assert "if (startingRef.current) return false;" in body


def test_fa89_the_stale_notice_offers_the_configuration_of_the_run() -> None:
    shell = _text(APP_SHELL)
    adopt = _between(shell, "const adoptRunConfig = () => {", "\n  };")
    assert (
        "applyConfigYaml(runBasis.yaml);" in adopt
        and "setParamOverrides(runBasis.params);" in adopt
    )
    assert "onAdopt={adoptRunConfig}" in shell
    notice = _between(
        shell, "function StaleResultNotice(", "// --- Hinweis: Lauf nicht mehr bekannt"
    )
    assert (
        "Konfiguration dieses Laufs übernehmen" in notice
        and "onClick={onAdopt}" in notice
    )
    assert "Liste der Läufe" in notice


def test_fa89_a_lost_run_from_the_list_does_not_discard_the_open_one() -> None:
    shell = _text(APP_SHELL)
    handler = _between(shell, "const onDownloadClick = async", "const downloadFromRun")
    assert "if (runIdRef.current === id) handleRunGone(id);" in handler
    assert "else notify.error(RUN_GONE_MESSAGE);" in handler
