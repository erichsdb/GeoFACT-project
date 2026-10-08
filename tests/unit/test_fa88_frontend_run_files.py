"""Implements: FA88 (Ausgaben eines Laufs als Dateiliste, einzeln abrufbar) - Frontend-Teil.

Es gibt kein Frontend-Testwerkzeug im Repo (siehe test_fa09_frontend_events.py).
Die reinen Helfer in frontend/lib/runFiles.ts werden über fa88_run_files_driver.mjs
unter node ausgeführt; die Nachbedingungen an die Ansicht (Knopf „Ausgaben“ mit Dateiliste in Schritt 3,
kein Knopf mehr in der Kopfzeile, jeder Abruf über die Rückfrage aus FA54,
übersprungene Ausgaben sichtbar) sind Quelltextverträge. Nicht im Browser geprüft.

Der Backend-Teil steht in test_fa88_run_outputs.py.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
HELPERS = FRONTEND / "lib" / "runFiles.ts"
API = FRONTEND / "lib" / "api.ts"
TYPES = FRONTEND / "lib" / "types.ts"
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


def test_fa88_file_sizes_are_formatted_in_german(js) -> None:
    assert js["bytes"] == {
        "zero": "0 B",
        "small": "812 B",
        "edge": "999 B",
        "kilo": "1 kB",
        "kilo_fraction": "12,3 kB",
        "mega": "4,5 MB",
        "giga": "1,2 GB",
        "negative": "–",
        "nan": "–",
    }


def test_fa88_caption_names_format_and_source_only_when_the_backend_does(js) -> None:
    assert js["caption"] == {
        "output": "geojson aus catchment",
        "output_without_source": "geojson",
        "output_unmatched": None,  # nicht zuordenbar: nichts erfinden
        "companion": None,
    }


def test_fa88_outputs_and_companions_are_separated_in_backend_order(js) -> None:
    assert js["split"]["outputs"] == ["b.csv", "a.geojson"]
    assert js["split"]["companions"] == ["scenario.yaml", "ATTRIBUTION.txt"]
    assert js["split"]["none"] == {
        "outputs": [],
        "companions": [],
    }  # Randfall: noch keine Liste


def test_fa88_zip_is_offered_only_for_a_finished_run(js) -> None:
    assert js["zip"] == {
        "pending": False,
        "running": False,
        "done": True,
        "error": False,
        "cancelled": False,
    }


# --- API-Client ----------------------------------------------------------------


def test_fa88_api_client_lists_the_files_and_builds_a_link_per_file() -> None:
    source = _text(API)
    assert (
        "runOutputs: (runId: string) => request<RunOutputs>(`/api/runs/${runId}/outputs`)"
        in source
    )
    link = _between(source, "runFileUrl:", "runFileText:")
    # ein Link kann keinen Authorization-Header tragen: Token als ?token=, Name kodiert
    assert "withTokenParam(" in link and "encodeURIComponent(name)" in link
    assert "/outputs/" in link


def test_fa88_types_mirror_the_backend_answer() -> None:
    types = _text(TYPES)
    file_type = _between(types, "export interface RunFile {", "\n}\n")
    assert (
        'role: "output" | "companion";' in file_type
        and "size_bytes: number;" in file_type
    )
    outputs = _between(types, "export interface RunOutputs {", "\n}\n")
    assert "files: RunFile[];" in outputs and "skipped: SkippedOutput[];" in outputs
    skipped = _between(types, "export interface SkippedOutput {", "\n}\n")
    assert "index: number;" in skipped and "reason: string;" in skipped


# --- Ansicht: Dateiliste in Schritt 3 -------------------------------------------


def test_fa88_header_has_no_download_button_anymore() -> None:
    shell = _text(APP_SHELL)
    header = _between(shell, "<header", "</header>")
    assert "downloadUrl" not in header and "Ergebnisse" not in header
    assert "<Download" not in shell


def test_fa88_result_step_offers_the_files_of_a_finished_run() -> None:
    shell = _text(APP_SHELL)
    step = _between(
        shell, "function ResultStep(", "// --- Lauf-Attribution und Lauf-Warnungen"
    )
    assert '{status?.status === "done" && (' in step
    assert (
        "<RunFilesButton runId={runId} onDownload={onDownload} onGone={onRunGone}"
        in step
    )
    # in der Kopfzeile der Ergebnisansicht, in Ergebnis- und Graphansicht erreichbar
    toolbar = _between(step, 'data-slot="run-attribution"', "{/* Lauf-Fehler bleiben")
    assert "<RunFilesButton" in toolbar
    assert "onDownload={downloadFromRun}" in shell


def test_fa88_the_file_list_takes_no_room_from_the_map() -> None:
    """Die Liste öffnet als Dialog; in der Seite steht nur der Knopf (Nutzerwunsch 08.10.2026)."""
    step = _between(
        _text(APP_SHELL),
        "function ResultStep(",
        "// --- Lauf-Attribution und Lauf-Warnungen",
    )
    assert "<RunFiles " not in step and "<RunFilesView" not in step
    button = _between(
        _text(RUN_OUTPUTS), "export function RunFilesButton(", "function FileLink("
    )
    assert "<Dialog open={open} onOpenChange={setOpen}>" in button
    assert (
        "<RunFilesView runId={runId} files={files} onDownload={onDownload} bare />"
        in button
    )
    # der Knopf nennt die Zahl der Dateien und zeigt eine übersprungene Ausgabe schon vor dem Öffnen
    assert "const count = splitFiles(files.data).outputs.length;" in button
    assert (
        "const problem = skipped > 0 || !!files.error;" in button
        and "{problem && <TriangleAlert" in button
    )


def test_fa88_every_file_is_its_own_link_and_the_zip_stays() -> None:
    files = _between(
        _text(RUN_OUTPUTS), "function RunFilesView(", "// --- Liste der Läufe"
    )
    assert (
        "outputs.map((file) => (" in files
        and "<FileLink runId={runId} file={file}" in files
    )
    assert "companions.map((file) => (" in files
    assert "api.runFileUrl(runId, file.name)" in files
    assert "Alle als ZIP" in files and "api.downloadUrl(runId)" in files
    # das ZIP gibt es nur nach dem Abschluss (sonst 409)
    assert "const finished = data ? canDownloadZip(data.status) : false;" in files
    assert files.index("{finished && (") < files.index("Alle als ZIP")


def test_fa88_no_link_leaves_the_page_without_asking_the_backend() -> None:
    """Jeder href auf eine Datei des Laufs hat einen onClick, der über onDownload läuft (FA54)."""
    files = _text(RUN_OUTPUTS)
    hrefs = len(re.findall(r"href=\{(?:url|api\.(?:runFileUrl|downloadUrl)\()", files))
    clicks = len(re.findall(r"onDownload\(e, runId, ", files))
    assert hrefs == 3 and clicks == 3
    # der Handler selbst: test_fa54_download_asks_the_backend_before_leaving_the_page


def test_fa88_skipped_outputs_are_shown_with_their_reason() -> None:
    files = _between(
        _text(RUN_OUTPUTS), "function RunFilesView(", "// --- Liste der Läufe"
    )
    block = _between(files, 'data-slot="skipped-outputs"', "</ul>")
    assert (
        "Nicht geschrieben: {s.type}" in block
        and "{s.reason}" in block
        and "output[{s.index}]" in block
    )


def test_fa88_a_lost_run_is_reported_from_the_file_list() -> None:
    files = _between(
        _text(RUN_OUTPUTS), "function useRunOutputs(", "export function RunFiles("
    )
    # am HTTP-Status erkannt, nie am Text der Meldung (FA54)
    assert "const gone = isNotFoundError(e);" in files
    assert "if (gone) onGone?.(runId);" in files
    assert "RUN_GONE_MESSAGE" in files


def test_fa88_empty_and_unfinished_runs_say_so_instead_of_showing_nothing() -> None:
    files = _between(
        _text(RUN_OUTPUTS), "function RunFilesView(", "// --- Liste der Läufe"
    )
    assert "Dieser Lauf hat keine Ausgabe geschrieben." in files
    assert "Der Lauf ist nicht abgeschlossen – es gibt keine Ausgaben." in files
