"""Implements: FA12 (Web-Demo), FA65/FA67 (Ergebnisansicht), FA72 (Link), FA73 (Lizenzpanel), FA75 (Graph) - Fixes aus dem Browsercheck vom 03.10.2026.

Es gibt kein Frontend-Testwerkzeug im Repo (siehe test_fa54_frontend_lost_run.py).
Diese Vertragstests lesen den Quelltext und halten die im Browser gefundenen
und behobenen Fehler fest: Tab-Overlay über den Karten-Controls (B1), Kanten
des Plans vor dem Lauf (B6), Einpassen nach Auf-/Zuklappen (B7), Lauf gilt
ab dem Klick und bei "pending" als laufend (B10), Hinweis auf nicht
enthaltene Parameter im Link (Q13), begrenzte Editor-/Listenhoehe (R12),
Vorschau folgt dem Suchfilter (R17), einheitliche Zahlenformate (R3).
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
COMPONENTS = FRONTEND / "components"


def _read(name: str) -> str:
    path = FRONTEND / name if "/" in name else COMPONENTS / name
    return path.read_text(encoding="utf-8")


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i : src.index(end, i)]


# --- B1 / R2: Ergebnisansicht ------------------------------------------------


def test_fa67_table_and_stats_overlay_sits_above_the_map_controls():
    inspector = _read("node-inspector.tsx")
    overlay = re.search(r'<div data-slot="tab-overlay" className="([^"]+)"', inspector)
    assert overlay, "Overlay für Tabelle/Statistik fehlt"
    classes = overlay.group(1).split()
    # MapLibre-Controls und Attribution haben z-index 2 - ohne eigenen
    # Stapelkontext über ihnen scheinen sie durch Tabelle und Statistik.
    assert "absolute" in classes and "inset-0" in classes and "bg-background" in classes
    assert any(re.fullmatch(r"z-(1\d|[2-9]\d)", c) for c in classes), classes


def test_fa65_map_attribution_separates_basemap_and_data():
    map_view = _read("map-view.tsx")
    assert 'attribution: "Karte: © OpenStreetMap contributors"' in map_view
    body = _between(map_view, "function attributionText(", "\n}\n")
    assert "`Daten: ${" in body


# --- B6 / B7 / R8 / R9: Ausführungsgraph ------------------------------------


def test_fa75_preview_graph_takes_edges_from_the_planned_steps():
    types = _read("lib/types.ts")
    assert re.search(r"planned_steps\?: PlannedStepOut\[\] \| null", types)
    planned = _between(types, "export interface PlannedStepOut", "\n}\n")
    for field in ("id: string", "op: string", "inputs: Record<string, string>"):
        assert field in planned
    shell = _read("app-shell.tsx")
    static = _between(
        shell, "const staticStatus = useMemo", "}, [configYaml, validation]);"
    )
    loop = _between(
        static, "for (const planned of validation.planned_steps ?? [])", "\n    }\n"
    )
    assert "inputs: planned.inputs" in loop and "op: planned.op" in loop
    # die Planschritte überschreiben die wörtlichen YAML-Eingänge (Selektor)
    assert static.index("validation.planned_steps") > static.index(
        "for (const raw of stepList)"
    )


def test_fa75_expand_and_collapse_refit_the_view():
    graph = _read("run-graph.tsx")
    toggle = _between(graph, "const toggleInstance = ", "\n  };")
    assert "fitAfterLayout.current = true" in toggle
    assert "toggleInstance(node.id.slice(INSTANCE_PREFIX.length), true)" in graph
    assert "toggleInstance(group.id, false)" in graph
    # erst einpassen, wenn die neuen Knoten vermessen sind
    assert "useNodesInitialized()" in graph
    fit = _between(
        graph, "if (!fitAfterLayout.current || !nodesInitialized) return;", "}, [nodes"
    )
    assert "fitView({ padding: FIT_PADDING" in fit
    assert "fitViewOptions={{ padding: FIT_PADDING }}" in graph


def test_fa75_legend_and_crs_badge_do_not_cover_the_first_group():
    graph = _read("run-graph.tsx")
    legend = _between(graph, "function Legend()", "\n}\n")
    badge = _between(graph, "function WorkingCrsBadge(", "\n}\n")
    for block in (legend, badge):
        assert "right-3" in block and "left-3" not in block


def test_fa75_collapsed_subtitle_starts_with_the_counts():
    graph = _read("run-graph.tsx")
    assert (
        "sub: `${group.stepCount} Schritte · ${group.layerCount} Layer · ${group.module}`"
        in graph
    )


# --- B10: Lauf-Knopf ---------------------------------------------------------


def test_fa12_run_counts_as_running_while_starting_and_pending():
    shell = _read("app-shell.tsx")
    helper = _between(shell, "function runInProgress(", "\n}\n")
    assert (
        'status?.status === "pending"' in helper
        and 'status?.status === "running"' in helper
    )
    assert "starting ||" in helper
    assert "const isRunning = runInProgress(status, starting);" in shell
    start = _between(shell, "const startRun = async () => {", "\n  };")
    assert start.index("if (startingRef.current) return;") < start.index(
        "api.createRun("
    )
    # erst der erste SSE-Status (oder ein Fehler) beendet das Anlegen - nicht
    # schon die Antwort von POST /api/runs (sonst kurz wieder "Ausführen")
    assert "finishStarting();" in start[start.index("catch (e)") :]
    assert "finally" not in start
    finish = _between(shell, "const finishStarting = () => {", "\n  };")
    assert "startingRef.current = false;" in finish and "setStarting(false);" in finish
    subscribe = _between(
        shell, "const subscribe = (id: string) => {", 'es.addEventListener("progress"'
    )
    assert subscribe.index("setStatus(parsed);") < subscribe.index("finishStarting();")
    onerror = _between(shell, "es.onerror = () => {", "void api.runIsGone(id)")
    assert "finishStarting();" in onerror
    cancel = _between(shell, "const cancelRun = async () => {", "\n  };")
    assert "startingRef.current" in cancel


# --- Q13: Link und Parameter -------------------------------------------------


def test_fa72_copied_link_says_that_changed_parameters_are_not_included():
    shell = _read("app-shell.tsx")
    copy = _between(shell, "const copyShareLink", "const handleInsertStep")
    assert "Object.keys(effectiveParams)" in copy
    assert "notify.info(" in copy and "Standardwert" in copy
    assert 'notify.success("Link kopiert.")' in copy
    assert "linkOmitsParameters={Object.keys(effectiveParams).length > 0}" in shell
    assert "geänderte Parameter sind nicht enthalten" in shell


# --- R12: Höhenbegrenzung ---------------------------------------------------


def test_fa12_yaml_editor_and_list_parameters_have_a_height_limit():
    editor = _read("config-editor.tsx")
    assert 'maxHeight = "70vh"' in editor and "maxHeight={maxHeight}" in editor
    panel = _read("parameter-panel.tsx")
    textarea = _between(panel, "<Textarea", "/>")
    assert "max-h-40" in textarea and "overflow-y-auto" in textarea


# --- R15 / R21: Neuladen, Live-Vorschau --------------------------------------


def test_fa12_reload_on_step_two_without_a_run_stays_on_step_two():
    shell = _read("app-shell.tsx")
    guard = _between(shell, "    if (!runId) {", "      return;\n    }")
    assert 'if (step === "result") setStep("prompt");' in guard


def test_fa12_live_preview_is_only_shown_while_generating():
    prompt = _read("prompt-panel.tsx")
    assert "{busy && (" in prompt
    assert 'phase === "done" && streamedYaml' not in prompt


# --- R17 / R19: Szenarioauswahl ----------------------------------------------


def test_fa12_picker_selection_follows_the_search_filter():
    picker = _read("scenario-picker.tsx")
    handler = _between(picker, "const onQueryChange = ", "\n  };")
    assert "filterScenarios(scenarios, next)" in handler
    assert "setActive(visible[0] ?? null)" in handler
    assert "onChange={(e) => onQueryChange(e.target.value)}" in picker
    # dieselbe Filterregel für Liste und Auswahl
    assert "useMemo(() => filterScenarios(scenarios, query)" in picker
    assert "line-clamp-2" in picker and "minmax(0,18rem)" in picker


# --- R3: Zahlenformate -------------------------------------------------------


def test_fa12_numbers_use_one_german_format():
    fmt = _read("lib/format.ts")
    assert 'const LOCALE = "de-DE";' in fmt
    for name in ("formatDecimal", "formatCount", "formatCell"):
        assert f"export function {name}(" in fmt
    for component in (
        "table-view.tsx",
        "stats-view.tsx",
        "chart-view.tsx",
        "node-inspector.tsx",
        "map-view.tsx",
    ):
        source = _read(component)
        assert "toFixed(" not in source, component
        assert "toLocaleString(" not in source, component


# --- R26 / Q25: Lizenzpanel --------------------------------------------------


def test_fa73_identical_verdicts_are_grouped_and_dalicc_conflicts_explained():
    panel = _read("license-check.tsx")
    group = _between(panel, "function groupChecks(", "\n}\n")
    assert (
        "check.source" in group and "check.status" in group and "check.message" in group
    )
    assert "groupChecks(result.checks).map(" in panel
    assert 'result.checks.some((c) => c.status === "conflicts")' in panel
    assert "stammt von DALICC" in panel and "CC0 neben ODbL" in panel
