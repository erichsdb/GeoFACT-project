"""Implements: FA8 (Registry-Pattern), FA40 (Operationen als Art der Registry).

Contract-Tests für die Operationen in der Registry (core/registry.py):
Vertrag (Step-Klasse) und Ausführungsfunktion stehen in EINEM Eintrag. Die
Registry installiert die Sitzungs-Fixture in tests/conftest.py
(geofact.api.load_extensions()).
"""

import subprocess
import sys

import pytest

from geofact.core.registry import default_registry


def test_registry_buffer_is_registered():
    entry = default_registry().operation("buffer")
    assert entry.step_model.__name__ == "BufferStep"
    assert callable(entry.run)
    assert entry.origin == "geofact.builtin.operations.buffer"


def test_registry_unknown_operation_lists_available():
    with pytest.raises(ValueError, match="ranking"):
        default_registry().operation("does_not_exist")


def test_registry_all_fa8_operations_registered():
    expected = {
        "buffer",
        "nearest_distance",
        "spatial_join",
        "overlay",
        "clip",
        "zonal_stats",
        "classify",
        "filter",
        "ranking",
    }
    assert expected.issubset(set(default_registry().names("operation")))


# =====================================================================
# Registrierung ausgelagerter Operationen (FA34/FA35, NFA4, FA40)
# =====================================================================

_LOOKUP_IN_FRESH_PROCESS = """
import sys
{prelude}
from geofact import api
from geofact.core.scenario import resolve_step
api.load_extensions()
for op in sys.argv[1:]:
    try:
        resolve_step({{"id": "x", "op": op, "inputs": {{}}}})
    except Exception as exc:  # fehlende Ports/Parameter sind hier egal
        assert "Unbekannte Operation" not in str(exc), exc
print("ok")
"""

_WEB_VALIDATION_IN_FRESH_PROCESS = """
import sys
from geofact_web import scenario_service
for op in sys.argv[1:]:
    raw = {
        "scenario": {"name": "x", "region": "12.30,51.30,12.40,51.36"},
        "layers": [{"id": "r", "source": "region"}],
        "steps": [{"id": "s", "op": op, "inputs": {}}],
        "output": [{"type": "geojson", "source": "s"}],
    }
    _, response = scenario_service.validate_config(None, raw)
    for issue in response.issues:
        assert "Unbekannte Operation" not in issue.message, issue.message
print("ok")
"""


def _run_fresh(script: str, ops: tuple[str, ...]) -> None:
    result = subprocess.run(
        [sys.executable, "-c", script, *ops],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr


def test_extracted_operations_are_registered_for_cli_validation():
    """Regression: Operationen, die als EIGENE Datei vorliegen
    (Open-Closed, NFA4), müssen nach der expliziten Erkennung auf dem
    Validierungspfad bekannt sein - nicht nur im Executor (gefunden an
    attribute_join/top_n).

    Geprüft wird in einem frischen Prozess, in dem vorher kein
    Operationsmodul importiert wurde; allein ``api.load_extensions()``
    macht die Operationen bekannt (keine Importliste, keine versteckte
    Erkennung)."""
    _run_fresh(
        _LOOKUP_IN_FRESH_PROCESS.format(prelude="import geofact.cli"),
        ("attribute_join", "top_n", "power_grid_topology"),
    )


def test_extracted_operations_are_registered_for_web_validation():
    """Dasselbe für die Web-Validierung: eine korrekt generierte
    Konfiguration darf nicht als ungültig gelten (sonst bekommt das LLM
    eine unnötige Reparatur-Runde aufgedrängt). scenario_service lädt die
    Erweiterungen selbst (idempotent)."""
    pytest.importorskip("fastapi")
    _run_fresh(_WEB_VALIDATION_IN_FRESH_PROCESS, ("attribute_join", "top_n"))
