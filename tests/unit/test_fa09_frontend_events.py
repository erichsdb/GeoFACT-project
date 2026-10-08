"""Implements: FA9 (beobachtbarer Lauf: Ereignisse live am Knoten), FA12 (Web-Frontend).

Die Live-Ansicht des Frontends (``applyEvent`` in frontend/components/app-shell.tsx)
bildet die Ereignisse des Laufs auf den Zustand der Knoten ab - wie das Backend
(``RunManager._apply``), damit Live-Ansicht und Status nach einem Neuladen
dasselbe zeigen. Es gibt kein Frontend-Testwerkzeug im Repo; dieser Vertragstest
liest den Quelltext der Funktion und prüft, dass jedes Ereignis, das das
Backend auf einen Knoten abbildet, auch dort vorkommt (``layer_warning`` und
``layer_error`` fehlten).
"""

from __future__ import annotations

import re
from pathlib import Path

from geofact.engine import events as ev

APP_SHELL = (
    Path(__file__).resolve().parents[2] / "frontend" / "components" / "app-shell.tsx"
)

# Ereignisse, die das Backend auf einen Knoten (Schritt) bzw. den Lauf abbildet
# (backend/geofact_web/run_manager.py::_apply).
NODE_EVENTS = (
    ev.LAYER_LOADING,
    ev.LAYER_LOADED,
    ev.LAYER_WARNING,
    ev.LAYER_ERROR,
    ev.STEP_RUNNING,
    ev.STEP_WARNING,
    ev.STEP_DONE,
    ev.STEP_ERROR,
    ev.RUN_ERROR,
    ev.RUN_CANCELLED,
)


def _apply_event_source() -> str:
    text = APP_SHELL.read_text(encoding="utf-8")
    start = text.index("function applyEvent(")
    # bis zur nächsten Funktion auf oberster Ebene bzw. zum Dateiende
    following = re.search(r"\n(?:export )?(?:async )?function \w+", text[start + 1 :])
    return text[start : start + 1 + following.start()] if following else text[start:]


def test_fa09_frontend_apply_event_handles_every_event_the_backend_applies():
    source = _apply_event_source()
    missing = [name for name in NODE_EVENTS if f'"{name}"' not in source]
    assert missing == [], f"applyEvent kennt diese Ereignisse nicht: {missing}"


def test_fa09_frontend_layer_events_land_on_the_step_like_in_the_backend():
    source = _apply_event_source()
    # layer_warning wie step_warning (Meldung an den Schritt), layer_error wie
    # step_error (Status error + Text) - dieselben Zweige, nicht ein eigener.
    assert re.search(r'type === "step_warning" \|\| type === "layer_warning"', source)
    assert re.search(r'type === "step_error" \|\| type === "layer_error"', source)


# FA55/FA56/FA58/FA65 (W3-03): Ereignisse, die den Lauf als Ganzes betreffen
# (Region und Arbeits-CRS, Regionswarnungen, Attribution im Plan und am Ende).
RUN_LEVEL_EVENTS = (ev.REGION_READY, ev.REGION_WARNING, ev.PLAN, ev.RUN_DONE)


def test_fa09_frontend_apply_event_handles_region_and_attribution_events():
    source = _apply_event_source()
    missing = [name for name in RUN_LEVEL_EVENTS if f'"{name}"' not in source]
    assert missing == [], f"applyEvent kennt diese Lauf-Ereignisse nicht: {missing}"


def test_fa09_frontend_warning_without_step_lands_at_run_level_like_in_the_backend():
    # run_manager._apply legt eine Layer-/Schrittwarnung ohne bekannten Schritt
    # (Layer nur für eine Ausgabe) auf Lauf-Ebene ab; die Live-Ansicht ebenso,
    # damit sie nach einem Neuladen nicht von RunStatus.warnings abweicht.
    source = _apply_event_source()
    no_step = source[source.index("if (!stepId || !status.steps[stepId])") :]
    branch = no_step[: no_step.index("const steps = ")]
    assert 'type === "layer_warning"' in branch
    assert "warnings: [...(status.warnings ?? []), warning]" in branch


def test_fa09_frontend_reload_during_a_run_subscribes_to_the_event_stream_again():
    """Nachtreview 02.10. (Runde 2): nach einem Neuladen während eines Laufs
    holte die Oberfläche den Status einmal und meldete sich nie wieder am
    SSE-Strom an - Anzeige blieb bei 'läuft…', kein Wechsel zum Ergebnis."""
    text = APP_SHELL.read_text(encoding="utf-8")
    start = text.index(".runStatus(runId)")
    reload_effect = text[start : text.index(".catch(", start)]
    assert 'if (s.status === "running" || s.status === "pending")' in reload_effect
    assert "subscribe(runId)" in reload_effect
    # das Effekt steht hinter der Deklaration von subscribe (kein Zugriff davor)
    assert text.index("const subscribe = (id: string)") < start
