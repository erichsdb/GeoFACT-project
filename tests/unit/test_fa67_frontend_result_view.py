"""Implements: FA67 (Ergebnisansicht in voller Breite mit Vollbild-Graph) - Frontend.

Es gibt kein Frontend-Testwerkzeug im Repo (siehe test_fa54_frontend_lost_run.py).
Diese Vertragstests lesen deshalb den Quelltext von `ResultStep` und prüfen die
Nachbedingungen von FA67: genau eine Ansicht zur Zeit (Ergebnis ODER Graph),
Knotenklick im Vollbild-Graphen führt zum Ergebnis, "Endergebnis anzeigen" aus
jeder Ansicht, FA33-Alerts bleiben, ein neuer Lauf öffnet wieder `result`.
"""

from __future__ import annotations

import re
from pathlib import Path

APP_SHELL = (
    Path(__file__).resolve().parents[2] / "frontend" / "components" / "app-shell.tsx"
)


def _text() -> str:
    return APP_SHELL.read_text(encoding="utf-8")


def _result_step() -> str:
    source = _text()
    begin = source.index("function ResultStep(")
    return source[begin : source.index("\n}\n", begin)]


def test_fa67_view_state_is_result_or_graph_and_starts_on_result():
    body = _result_step()
    assert re.search(r'useState<"result" \| "graph">\("result"\)', body), (
        "view-State mit Anfangswert 'result' fehlt"
    )


def test_fa67_graph_and_result_are_never_shown_together():
    body = _result_step()
    # genau eine Verzweigung nach view: der Graph ODER der Inspector-Zweig
    assert re.search(r'view === "graph"[^\n]*\?', body)
    assert body.count("<RunGraph") == 1
    assert body.count("<NodeInspector") == 1
    # kein Spalten-Raster mehr, in dem beide die Breite teilen
    assert 'className="grid gap-4"' not in body
    assert "lg:grid-cols" not in body


def test_fa67_fullscreen_graph_has_the_specified_height():
    body = _result_step()
    assert "h-[calc(100vh-14rem)] min-h-[32rem]" in body


def test_fa67_node_click_in_the_fullscreen_graph_returns_to_the_result():
    body = _result_step()
    handler = body[body.index("const selectFromGraph") :]
    handler = handler[: handler.index("};")]
    assert "onSelectNode(id)" in handler
    assert 'setView("result")' in handler
    assert "onSelect={selectFromGraph}" in body


def test_fa67_final_result_button_leads_from_any_view_to_the_final_node():
    body = _result_step()
    assert "Endergebnis anzeigen" in body
    # sichtbar in der Graphansicht ODER bei einem anderen Knoten als dem Endknoten
    assert re.search(r'view === "graph" \|\| selectedNode !== finalNode', body)
    click = body[body.index("onClick={() => {", body.index("showFinal")) :]
    click = click[: click.index("}}")]
    assert 'setView("result")' in click and "onSelectNode(finalNode)" in click


def test_fa67_header_has_back_button_attribution_slot_and_view_toggle():
    body = _result_step()
    assert "Zurück zur Ausführung" in body
    assert '<div data-slot="run-attribution"' in body
    assert "Ausführungsgraph" in body and "Zurück zum Ergebnis" in body
    # Reihenfolge der Kopfzeile: Zurück, Attribution, Endergebnis, Umschalter
    order = [
        body.index("Zurück zur Ausführung"),
        body.index('data-slot="run-attribution"'),
        body.index("Endergebnis anzeigen"),
        body.index("Zurück zum Ergebnis"),
    ]
    assert order == sorted(order)


def test_fa67_fa33_alerts_stay_and_precede_the_view_body():
    body = _result_step()
    assert "Zwischenergebnisse bleiben verfügbar" in body
    assert "Lauf fehlgeschlagen" in body and "Dieser Schritt ist fehlgeschlagen" in body
    assert body.index("Zwischenergebnisse bleiben verfügbar") < body.index("<RunGraph")
    assert "onRunGone={onRunGone}" in body  # FA54 bleibt wörtlich


def test_fa67_a_new_run_remounts_the_result_step_on_the_result_view():
    source = _text()
    match = re.search(r"<ResultStep\s+key=\{[^}]*runId[^}]*\}", source)
    assert match, (
        "ResultStep braucht key={runId ...}, damit ein neuer Lauf 'result' öffnet"
    )
    # der alte Kommentarblock mit dem Nebeneinander/Untereinander-Text ist ersetzt
    assert "Untereinander statt nebeneinander" not in _result_step()
