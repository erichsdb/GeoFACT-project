"""Implements: FA65 (Lizenzen und Namensnennung, Web-Frontend-Teil), FA67 (Attribution im Kopf der Ergebnisansicht).

Quelltextverträge (kein Frontend-Testwerkzeug im Repo, siehe
test_fa54_frontend_lost_run.py): Die Typen spiegeln ``AttributionOut``; die
Kopfzeile der Ergebnisansicht füllt den Platzhalter ``run-attribution``;
der Inspector nennt die Datenquellen des Knotens mit Links und den Layern ohne
Lizenzangabe; die Karte führt die Lizenzen in ihrer Attribution.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TYPES = FRONTEND / "lib" / "types.ts"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
INSPECTOR = FRONTEND / "components" / "node-inspector.tsx"
MAP_VIEW = FRONTEND / "components" / "map-view.tsx"


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


def test_fa65_frontend_types_mirror_the_attribution():
    types = _read(TYPES)
    lic = _block(types, "export interface LicenseOut")
    assert "name: string" in lic and "attribution" in lic and "url" in lic
    attr = _block(types, "export interface AttributionOut")
    assert "licenses: LicenseOut[]" in attr and "undeclared: string[]" in attr
    assert re.search(
        r"attribution\?: AttributionOut \| null",
        _block(types, "export interface RunStatus"),
    )
    assert re.search(
        r"attribution\?: AttributionOut \| null",
        _block(types, "export interface NodeResult"),
    )
    assert re.search(
        r"attribution\?: AttributionOut \| null",
        _block(types, "export interface ValidateResponse"),
    )


def test_fa65_frontend_result_header_fills_the_run_attribution_slot():
    body = _block(_read(APP_SHELL), "function ResultStep(")
    slot = body[body.index('<div data-slot="run-attribution"') :]
    opening = slot[: slot.index(">") + 1]
    assert not opening.endswith("/>"), "der Platzhalter ist noch leer"
    assert "<RunAttribution" in slot[: slot.index("</div>")]


def test_fa65_frontend_run_attribution_names_licences_links_and_undeclared_layers():
    shell = _read(APP_SHELL)
    comp = _block(shell, "function RunAttribution(")
    assert "Datenquellen" in comp
    assert "href=" in comp and "lic.url" in comp
    assert "ohne Lizenzangabe" in comp


def test_fa65_frontend_live_view_takes_the_attribution_from_plan_and_run_done():
    source = _apply_event_source()
    assert '"plan"' in source and '"run_done"' in source
    assert "attribution" in source[source.index('"run_done"') :][:400]


def test_fa65_frontend_inspector_footer_lists_the_sources_of_the_node():
    inspector = _read(INSPECTOR)
    assert "Datenquellen:" in inspector
    assert "result?.attribution" in inspector or "result.attribution" in inspector
    assert "ohne Lizenzangabe" in inspector
    assert 'target="_blank"' in inspector and 'rel="noreferrer"' in inspector


def test_fa65_frontend_map_carries_the_licences_in_its_attribution():
    map_view = _read(MAP_VIEW)
    assert "attributionText" in map_view
    # MapLibre-Gegenstück zu Leaflets addAttribution: eigenes
    # Attribution-Control mit der Lizenzzeile als customAttribution, das beim
    # Knotenwechsel ersetzt wird (neben dem Kachel-Nachweis der Basiskarte).
    assert "attributionControl: false" in map_view
    assert map_view.count("customAttribution: attributionText(") >= 2
    assert "map.removeControl(old)" in map_view
