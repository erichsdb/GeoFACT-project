"""Implements: FA58 (CRS-Provenienz je Layer, Web-Frontend-Teil), FA56 (Regionswarnungen in der Oberfläche).

Es gibt kein Frontend-Testwerkzeug im Repo (siehe test_fa54_frontend_lost_run.py).
Diese Vertragstests lesen den Quelltext und prüfen die Nachbedingung (6) von
FA58 auf der Seite der Oberfläche: die Typen spiegeln ``layer_provenance`` und
``region`` des Web-Status, die Live-Ansicht übernimmt sie aus den Ereignissen
``layer_loaded``/``region_ready``, Layer-Knoten zeigen ``Quell-CRS -> Arbeits-CRS``,
der Graph nennt das Arbeits-CRS, der Inspector hat einen Block "Herkunft", und
Warnungen werden nach ihrer Schwere (``info`` vs. ``warning``) unterschieden.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TYPES = FRONTEND / "lib" / "types.ts"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
RUN_GRAPH = FRONTEND / "components" / "run-graph.tsx"
INSPECTOR = FRONTEND / "components" / "node-inspector.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block(source: str, start_marker: str) -> str:
    begin = source.index(start_marker)
    return source[begin : source.index("\n}\n", begin)]


def _apply_event_source() -> str:
    text = _read(APP_SHELL)
    start = text.index("function applyEvent(")
    following = re.search(r"\n(?:export )?(?:async )?function \w+", text[start + 1 :])
    return text[start : start + 1 + following.start()] if following else text[start:]


def test_fa58_frontend_types_mirror_layer_provenance_and_region_info():
    types = _read(TYPES)
    prov = _block(types, "export interface LayerProvenanceOut")
    for field in (
        "source_crs",
        "source_crs_label",
        "crs_override",
        "target_crs",
        "target_crs_label",
        "raw_bounds",
        "bounds",
        "feature_count",
    ):
        assert f"{field}" in prov, f"LayerProvenanceOut ohne Feld {field}"
    region = _block(types, "export interface RegionInfoOut")
    for field in (
        "crs",
        "crs_label",
        "crs_mode",
        "bbox",
        "area_km2",
        "display_name",
        "warnings",
    ):
        assert field in region, f"RegionInfoOut ohne Feld {field}"
    status = _block(types, "export interface RunStatus")
    assert re.search(r"layer_provenance\?: Record<string, LayerProvenanceOut>", status)
    assert re.search(r"region\?: RegionInfoOut \| null", status)


def test_fa58_frontend_live_view_takes_provenance_and_region_from_events():
    source = _apply_event_source()
    loaded = source[source.index('"layer_loaded"') :]
    assert "provenance" in loaded[:800], (
        "layer_loaded übernimmt detail.provenance nicht"
    )
    assert "layer_provenance" in source
    region = source[source.index('"region_ready"') :]
    assert "region:" in region[:400], "region_ready setzt status.region nicht"
    assert '"region_warning"' in source


def test_fa58_frontend_layer_node_subtitle_shows_source_and_working_crs():
    graph = _read(RUN_GRAPH)
    assert "layer_provenance" in graph
    assert "→" in graph, "Untertitel 'Quell-CRS → Arbeits-CRS' fehlt"
    assert "erzwungen" in graph, "crs_override wird am Knoten nicht markiert"


def test_fa58_frontend_graph_header_names_the_working_crs_and_mode():
    graph = _read(RUN_GRAPH)
    assert "Arbeits-CRS" in graph
    assert "crs_mode" in graph


def test_fa58_frontend_inspector_has_a_provenance_block_with_region():
    inspector = _read(INSPECTOR)
    assert "Herkunft" in inspector
    assert "source_crs" in inspector and "target_crs" in inspector
    assert "display_name" in inspector or "area_km2" in inspector
    # der Block steht im Statistik-Tab
    stats = inspector[inspector.index('tab === "stats"') :]
    assert "ProvenanceBlock" in stats[:400]


def test_fa58_frontend_warnings_distinguish_info_from_warning():
    inspector = _read(INSPECTOR)
    assert re.search(r'severity === "info"', inspector), (
        "Hinweise (info) werden nicht getrennt"
    )
    assert "Hinweis" in inspector
    shell = _read(APP_SHELL)
    # die Live-Ansicht merkt sich die Schwere jeder Warnung (detail.severity)
    assert "severity" in _apply_event_source()
    assert "warning_details" in shell


def test_fa56_frontend_run_level_region_warnings_are_shown():
    shell = _read(APP_SHELL)
    result_step = _block(shell, "function ResultStep(")
    assert "RunWarnings" in result_step, (
        "Regionswarnungen des Laufs fehlen in der Ergebnisansicht"
    )
    assert "status.warnings" in shell or "status?.warnings" in shell
