"""Implements: FA75 (Nachbedingung 6: Herkunft strukturiert, Web-Graph gruppiert je Instanz, Farbe je Modul).

Quelltextverträge für das Frontend (kein Frontend-Testwerkzeug im Repo,
siehe test_fa54_frontend_lost_run.py): ``NodeOriginOut`` spiegelt
``NodeOrigin.to_dict`` (module, instance, key, inner_id), der Graph klappt
Instanzen mit mehreren Schlüsseln zu einem Knoten ein (``@inst:``), legt
aufgeklappte Instanzen als dagre-compound-Gruppen an, färbt je Modul und
kodiert Knoten-ids mit ``[``/``]`` in URLs; der Inspektor zeigt Modul,
Instanz, Knoten im Modul und Position.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TYPES = FRONTEND / "lib" / "types.ts"
API = FRONTEND / "lib" / "api.ts"
RUN_GRAPH = FRONTEND / "components" / "run-graph.tsx"
INSPECTOR = FRONTEND / "components" / "node-inspector.tsx"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _block(source: str, start_marker: str) -> str:
    begin = source.index(start_marker)
    return source[begin : source.index("\n}\n", begin)]


def test_fa75_frontend_types_mirror_node_origin():
    types = _read(TYPES)
    origin = _block(types, "export interface NodeOriginOut")
    for field in (
        "section",
        "index",
        "module",
        "instance",
        "key",
        "inner_id",
        "location",
    ):
        assert re.search(rf"\b{field}\??:", origin), f"NodeOriginOut ohne Feld {field}"
    assert "trail" not in origin and "source_index" not in origin
    expansion = _block(types, "export interface ExpansionInfo")
    assert re.search(r"instances\?: InstanceOut\[\]", expansion)
    assert "foreach_count" not in expansion and "template_count" not in expansion
    instance = _block(types, "export interface InstanceOut")
    for field in ("id", "module", "keys", "dropped_keys"):
        assert re.search(rf"\b{field}:", instance), field


def test_fa75_frontend_graph_collapses_instances_and_colors_per_module():
    graph = _read(RUN_GRAPH)
    assert '"@inst:"' in graph, "eingeklappte Instanz als ein Knoten fehlt"
    assert "compound" in graph and "setParent" in graph, (
        "dagre compound für Gruppen fehlt"
    )
    assert "parentId" in graph and 'extent: "parent"' in graph
    assert "MODULE_COLORS" in graph, "Farbe je Modul fehlt"
    assert (
        "trailColor" not in graph
        and "TRAIL_COLORS" not in graph
        and ".trail" not in graph
    )
    assert "Einklappen" in graph, "Gruppe lässt sich nicht wieder einklappen"
    assert "setExpanded" in graph and "expanded" in graph
    # Kanten zu einer eingeklappten Instanz werden je (Quelle, Ziel) zusammengefasst
    assert "merged" in graph
    # Rückfall, wenn das compound-Layout scheitert (Spec: Risiken)
    assert "catch" in graph


def test_fa75_frontend_node_urls_encode_bracket_ids():
    api = _read(API)
    assert "nodes/${encodeURIComponent(nodeId)}" in api
    assert "nodes/${nodeId}" not in api


def test_fa75_frontend_inspector_shows_module_instance_and_inner_id():
    inspector = _read(INSPECTOR)
    for label in ("Modul", "Instanz", "Knoten im Modul", "Position"):
        assert f">{label}</dt>" in inspector, label
    assert "origin.trail" not in inspector
