"""Implements: FA54 (verlorenen Lauf der Web-Demo erkennen und erklären) - Frontend-Teil.

Es gibt kein Frontend-Testwerkzeug im Repo (siehe test_fa09_frontend_events.py).
Diese Vertragstests lesen deshalb den Quelltext der beteiligten Stellen und
prüfen, dass jeder Weg zu einem Lauf (Status, Knoten-Ergebnis, Rasterbild,
Ereignisstrom, Download) ein 404 für den AKTUELLEN Lauf erkennt - am HTTP-Status,
nie am Text der Meldung - und dann erklärt statt den rohen Fehler zu zeigen:
der Lauf wird aus State und localStorage entfernt, ein Hinweis nennt den Grund
und bietet "Erneut ausführen" an. Andere Fehler bleiben, wie sie waren.

Der Backend-Teil steht in test_fa54_lost_run.py.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
API = FRONTEND / "lib" / "api.ts"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
NODE_INSPECTOR = FRONTEND / "components" / "node-inspector.tsx"
MAP_VIEW = FRONTEND / "components" / "map-view.tsx"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    """Quelltext ab `start` bis (ausschließlich) zum ersten `end` danach."""
    begin = text.index(start)
    return text[begin : text.index(end, begin + len(start))]


# =====================================================================
# API-Client: typisierter Fehler, Erkennung am Status
# =====================================================================


def test_fa54_api_client_raises_a_typed_error_with_the_http_status():
    source = _text(API)
    assert re.search(r"class ApiError extends Error\b", source)
    assert re.search(r"readonly status: number", source)
    # beide Stellen, die eine fehlgeschlagene Antwort in einen Fehler wandeln
    # (request und der SSE-Generierungsstrom), tragen den Status
    assert (
        len(
            re.findall(
                r"throw new ApiError\(await extractError\(res\), res\.status\)", source
            )
        )
        == 2
    )
    assert "throw new Error(await extractError" not in source


def test_fa54_not_found_is_detected_by_status_never_by_message_text():
    source = _text(API)
    detector = _between(source, "export function isNotFoundError", "\n}\n")
    assert "instanceof ApiError" in detector and ".status === 404" in detector
    assert "message" not in detector
    # kein Quelltext der Oberfläche wertet den Backend-Text "nicht gefunden" aus
    offenders = [
        path.relative_to(FRONTEND).as_posix()
        for path in [
            *(FRONTEND / "components").rglob("*.tsx"),
            *(FRONTEND / "lib").rglob("*.ts"),
        ]
        if "nicht gefunden" in _text(path)
    ]
    assert offenders == [], offenders


def test_fa54_a_lost_run_is_only_reported_for_a_404_of_the_status_probe():
    source = _text(API)
    probe = _between(source, "runIsGone: async", "\n  },\n")
    assert "/api/runs/${runId}" in probe  # die Statusabfrage des Laufs
    # Netzfehler und 5xx beweisen nichts (das Backend kann gerade neu starten): nicht verloren
    assert re.search(
        r"try \{.*?return false;.*?\} catch \(e\) \{\s*return isNotFoundError\(e\);",
        probe,
        re.S,
    )


def test_fa54_message_names_the_cause_and_the_remedy():
    source = _text(API)
    message = _between(source, "export const RUN_GONE_MESSAGE", ";\n")
    for part in (
        "nicht mehr bekannt",
        "Neustart",
        "andere Backend-Instanz",
        "erneut ausführen",
    ):
        assert part in message, part


# =====================================================================
# Knoten-Ergebnis und Rasterbild
# =====================================================================


def test_fa54_a_node_404_is_resolved_through_the_run_status_probe():
    source = _text(NODE_INSPECTOR)
    handler = _between(source, ".catch(async (e) =>", ".finally(")
    # zwei 404 mit verschiedener Bedeutung: Knoten ohne Ergebnis / Lauf unbekannt
    assert "isNotFoundError(e)" in handler and "api.runIsGone(runId)" in handler
    assert "RUN_GONE_MESSAGE" in handler and "onRunGoneRef.current?.(runId)" in handler
    # jeder andere Fehler bleibt wie er war
    assert "setError(e instanceof Error ? e.message : String(e))" in handler
    # nach der Rückfrage zählt nur noch, was zum aktuellen Abruf gehört
    assert handler.index("await api.runIsGone") < handler.rindex(
        "if (cancelled) return;"
    )


def test_fa54_a_failing_raster_image_probes_the_run_once():
    map_view = _text(MAP_VIEW)
    assert "onRasterError?: () => void" in map_view
    handler = _between(map_view, 'map.on("error"', "});")
    assert (
        'console.error("[MapView] maplibre error:"' in handler
    )  # bisheriges Log bleibt
    assert (
        "sourceId === RASTER_SOURCE_ID" in handler
        and "onRasterErrorRef.current?.()" in handler
    )
    assert 'const RASTER_SOURCE_ID = "raster-src"' in map_view
    assert "map.addSource(RASTER_SOURCE_ID" in map_view

    inspector = _text(NODE_INSPECTOR)
    assert "onRasterError={handleRasterError}" in inspector
    probe = _between(inspector, "const handleRasterError = useCallback", "[runId]);")
    assert "rasterProbedRef.current" in probe  # höchstens eine Probe je Abruf
    assert "api.runIsGone(runId)" in probe and "onRunGoneRef.current?.(runId)" in probe
    assert "rasterProbedRef.current = false;" in inspector  # neuer Abruf, neue Probe


# =====================================================================
# App-Hülle: Ereignisstrom, Download, Neuladen, Hinweis
# =====================================================================


def test_fa54_a_lost_run_is_dropped_from_state_and_local_storage():
    source = _text(APP_SHELL)
    handler = _between(source, "const handleRunGone = useCallback", "}, []);")
    # nur der AKTUELLE Lauf wird verworfen, ein zweiter Rückruf ist wirkungslos
    assert "if (runIdRef.current !== id) return;" in handler
    assert "runIdRef.current = null;" in handler
    for call in (
        "esRef.current?.close()",
        "setGoneRunId(id)",
        "setRunId(null)",
        "setStatus(null)",
        "setSelectedNode(null)",
    ):
        assert call in handler, call
    # die Ergebnisansicht braucht einen Lauf: zurück zur Ausführung
    assert re.search(
        r'setStep\(\(cur\) => \(cur === "result" \? "execution" : cur\)\)', handler
    )
    # runId = null entfernt den gemerkten Lauf aus dem localStorage
    assert re.search(
        r"else window\.localStorage\.removeItem\(RUN_ID_STORAGE_KEY\)", source
    )


def test_fa54_the_notice_offers_to_run_the_scenario_again():
    source = _text(APP_SHELL)
    notice = _between(source, "function RunGoneNotice", "// --- Step 1")
    assert (
        "RUN_GONE_MESSAGE" in notice
        and "Erneut ausführen" in notice
        and "onClick={onRerun}" in notice
    )
    assert "disabled={!canRerun}" in notice  # ungültige Konfiguration: keine Ausführung

    assert re.search(
        r'const rerunAfterLoss = \(\) => \{\s*setStep\("execution"\);\s*void startRun\(\);',
        source,
    )
    # der Hinweis verschwindet erst, wenn der neue Lauf angenommen ist (bei Fehler bleibt er)
    started = _between(source, "const startRun = async", "const subscribe")
    assert started.index("await api.createRun(configYaml)") < started.index(
        "setGoneRunId(null)"
    )
    assert "notify.error" in started
    assert "<RunGoneNotice" in source and 'goneRunId && step !== "prompt"' in source


def test_fa54_event_stream_failure_asks_the_backend_before_reporting_a_lost_connection():
    source = _text(APP_SHELL)
    handler = _between(source, "es.onerror = () => {", "\n    };\n  };")
    # EventSource nennt den HTTP-Status nicht: erst die Rückfrage, dann die Meldung
    assert "api.runIsGone(id)" in handler and "handleRunGone(id)" in handler
    assert (
        "Verbindung zum Server unterbrochen." in handler
    )  # anderer Fehler: wie bisher
    assert handler.index("handleRunGone(id)") < handler.index(
        "Verbindung zum Server unterbrochen."
    )
    assert "esRef.current !== es" in handler  # ein neuer Lauf bleibt unberührt


def test_fa54_download_asks_the_backend_before_leaving_the_page():
    source = _text(APP_SHELL)
    handler = _between(source, "const onDownloadClick = async", "const downloadFromRun")
    assert "e.preventDefault()" in handler
    assert "await api.runIsGone(id)" in handler and "handleRunGone(id)" in handler
    # seit FA88 gilt der Handler für das ZIP und jede einzelne Datei: die URL kommt vom Link
    assert "window.location.assign(url)" in handler
    assert handler.index("handleRunGone(id)") < handler.index("window.location.assign")
    # Zusatztasten (neuer Tab) bleiben beim Browser
    assert "e.metaKey || e.ctrlKey || e.shiftKey || e.altKey" in handler
    assert "void onDownloadClick(e, id, url)" in source
    # die Dateiliste ruft ihn für jeden Link auf (test_fa88_frontend_run_files.py)
    assert "onDownload={downloadFromRun}" in source


def test_fa54_cancel_of_an_unknown_run_is_a_lost_run():
    source = _text(APP_SHELL)
    cancel = _between(source, "const cancelRun = async", "const toggleSource")
    # der Abbruch-Endpunkt antwortet nur für einen unbekannten Lauf mit 404: keine Rückfrage nötig
    assert "isNotFoundError(e)" in cancel and "handleRunGone(runId)" in cancel
    assert "notify.error(" in cancel  # andere Fehler: wie bisher
    assert cancel.index("handleRunGone(runId)") < cancel.index("notify.error(")


def test_fa54_page_reload_with_a_forgotten_run_explains_instead_of_jumping_to_step_one():
    source = _text(APP_SHELL)
    recheck = _between(
        source,
        ".runStatus(runId)",
        "// eslint-disable-next-line react-hooks/exhaustive-deps",
    )
    assert "isNotFoundError(e)" in recheck and "handleRunGone(runId)" in recheck
    # jeder andere Fehler: wie bisher zurück auf Schritt 1
    assert re.search(
        r'setRunId\(null\);\s*setStatus\(null\);\s*setStep\("prompt"\);', recheck
    )


def test_fa54_the_inspector_reports_a_lost_run_to_the_app_shell():
    source = _text(APP_SHELL)
    assert "onRunGone={handleRunGone}" in source  # AppShell -> ResultStep
    assert "onRunGone={onRunGone}" in source  # ResultStep -> NodeInspector
    assert "onRunGone?: (runId: string) => void;" in _text(NODE_INSPECTOR)
