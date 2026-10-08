"""Implements: FA9 (beobachtbarer Lauf: Ereignisse, Warnungen, Abbruch).

Contract-Tests für src/geofact/engine/events.py: Ereignis-Konstanten,
CancelToken und der EINE prozessweite Warnungs-Router, der Warnungen an den
Sammler des aktuellen Kontexts leitet (ContextVar) - ohne
``warnings.catch_warnings`` und damit sicher für parallele Läufe.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import threading
import warnings
from typing import Callable

from geofact.engine import events as ev


def _emitter(
    module_name: str, text: str, category: str = "UserWarning"
) -> Callable[[], None]:
    """Eine Funktion, die ``text`` aus einem Modul namens ``module_name``
    warnt (``warnings`` rechnet die Warnung dem Modul der aufrufenden Zeile
    zu): so lassen sich Bausteine (geofact.*, Plugins) und fremde Module
    nachstellen, ohne eine Datei anzulegen."""
    namespace: dict = {"__name__": module_name}
    source = f"import warnings\ndef emit():\n    warnings.warn({text!r}, {category})\n"
    exec(compile(source, f"<{module_name}>", "exec"), namespace)  # noqa: S102 - fester Testtext
    return namespace["emit"]


def test_fa09_new_event_types_are_stable_wire_strings():
    assert ev.LAYER_WARNING == "layer_warning"
    assert ev.LAYER_ERROR == "layer_error"
    assert ev.RUN_CANCELLED == "run_cancelled"
    event = ev.ProgressEvent(type=ev.LAYER_WARNING, layer="a", step="s", message="m")
    assert event.to_dict() == {
        "type": "layer_warning",
        "layer": "a",
        "step": "s",
        "message": "m",
    }


def test_fa09_cancel_token_is_thread_safe_and_starts_unset():
    token = ev.CancelToken()
    assert token.cancelled is False
    threading.Thread(target=token.cancel).start()
    deadline = threading.Event()
    for _ in range(200):
        if token.cancelled:
            break
        deadline.wait(0.01)
    assert token.cancelled is True


def test_fa09_collect_warnings_captures_text_and_category():
    with ev.collect_warnings() as caught:
        warnings.warn("erste", UserWarning)
        warnings.warn("zweite", RuntimeWarning)
    assert caught == [("erste", "UserWarning"), ("zweite", "RuntimeWarning")]


def test_fa09_same_warning_in_two_successive_runs_is_collected_both_times():
    """Der Wiederholungsschutz der Standardaktion (eine Warnung je Text und
    Zeile einmal je Prozess) darf einen zweiten Lauf mit demselben Problem
    nicht stumm lassen: für Bausteine (geofact.*, Plugins) steht dort
    ``always`` - ohne dass an jeder Grenze am Filter gedreht wird."""
    for module in ("geofact.builtin.operations.fake", "geofact_plugin_fake"):
        emit = _emitter(module, "gleiche Meldung")
        for _ in range(3):
            with ev.collect_warnings() as caught:
                emit()
            assert caught == [("gleiche Meldung", "UserWarning")], module


def test_fa09_collecting_does_not_touch_the_filter_list_at_layer_and_step_boundaries():
    with warnings.catch_warnings():
        ev.install_warning_router()
        before = list(warnings.filters)
        emit = _emitter("geofact.engine.fake", "x")
        for _ in range(25):  # 25 Layer-/Schrittgrenzen
            with ev.collect_warnings():
                emit()
        assert warnings.filters == before
        assert len(before) == len(set(map(id, before)))


def test_fa09_the_always_filter_is_set_once_and_leads_the_filter_list():
    with warnings.catch_warnings():
        warnings.resetwarnings()
        warnings.simplefilter("ignore")
        with ev.collect_warnings():
            pass
        always = [f for f in warnings.filters if f[0] == "always"]
        assert len(always) == 1
        assert warnings.filters[0] is always[0]
        assert always[0][3].pattern == ev.GEOFACT_WARNING_MODULES


def test_fa09_module_pattern_covers_blocks_and_plugins_only():
    pattern = re.compile(ev.GEOFACT_WARNING_MODULES)
    for name in (
        "geofact",
        "geofact.api",
        "geofact.builtin.operations.buffer",
        "geofact_plugin_flatgeobuf",
        "geofact_plugin_paket.hilfe",
    ):
        assert pattern.match(name), name
    for name in (
        "geofact_web.run_manager",
        "geofactory",
        "geofact_plugin_",
        "pandas.core",
        "mygeofact",
        "tests.unit.test_fa09_events",
    ):
        assert not pattern.match(name), name


def test_fa09_collecting_works_when_the_host_ignores_all_warnings():
    """Das Muster ``PYTHONWARNINGS=ignore``: der Host stellt alle Warnungen ab -
    bevor oder nachdem der Router installiert wurde. Die Warnungen der
    Bausteine werden trotzdem gesammelt."""
    emit = _emitter("geofact.builtin.operations.fake", "trotz ignore")
    with warnings.catch_warnings():
        warnings.resetwarnings()
        warnings.simplefilter("ignore")  # Host stellt ab, Router noch nicht installiert
        with ev.collect_warnings() as caught:
            emit()
        assert caught == [("trotz ignore", "UserWarning")]

        warnings.simplefilter("ignore")  # Host stellt erneut ab, nach der Installation
        with ev.collect_warnings() as caught:
            emit()
        assert caught == [("trotz ignore", "UserWarning")]


def test_fa09_collecting_works_under_pythonwarnings_ignore_in_a_fresh_process():
    script = textwrap.dedent("""
        import warnings
        from geofact.engine import events as ev

        def emitter(module_name, text):
            ns = {"__name__": module_name}
            exec(f"import warnings\\ndef emit():\\n    warnings.warn({text!r}, UserWarning)\\n", ns)
            return ns["emit"]

        block = emitter("geofact.builtin.operations.fake", "vom Baustein")
        plugin = emitter("geofact_plugin_demo", "vom Plugin")
        foreign = emitter("fremde_bibliothek", "von aussen")
        with ev.collect_warnings() as caught:
            block(); plugin(); foreign()
        print([text for text, _ in caught])
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
    )
    assert result.returncode == 0, result.stderr
    # Bausteine und Plugins werden gesammelt, die fremde Bibliothek bleibt beim
    # Host-Filter (dokumentierter Rest, siehe Modul-Docstring).
    assert result.stdout.strip() == "['vom Baustein', 'vom Plugin']"


def test_fa09_nested_collectors_each_receive_only_their_own_warnings():
    with ev.collect_warnings() as outer:
        warnings.warn("außen vorher", UserWarning)
        with ev.collect_warnings() as inner:
            warnings.warn("innen", UserWarning)
        warnings.warn("außen nachher", UserWarning)
    assert [m for m, _ in inner] == ["innen"]
    assert [m for m, _ in outer] == ["außen vorher", "außen nachher"]


def test_fa09_two_threads_collect_their_warnings_separately():
    """Zwei gleichzeitige Läufe (Web-Demo): jeder Thread sieht nur seine
    eigenen Warnungen. Die Barriere stellt sicher, dass beide Sammler
    gleichzeitig aktiv sind, bevor irgendeine Warnung ausgelöst wird."""
    barrier = threading.Barrier(2, timeout=10)
    results: dict[str, list[tuple[str, str]]] = {}
    errors: list[BaseException] = []

    def run(name: str) -> None:
        try:
            with ev.collect_warnings() as caught:
                barrier.wait()
                for index in range(3):
                    warnings.warn(f"{name} #{index}", UserWarning)
                barrier.wait()
            results[name] = list(caught)
        except BaseException as exc:  # noqa: BLE001 - im Hauptthread prüfen
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors, errors
    assert [m for m, _ in results["a"]] == ["a #0", "a #1", "a #2"]
    assert [m for m, _ in results["b"]] == ["b #0", "b #1", "b #2"]


def test_fa09_warning_router_is_installed_once_and_idempotent():
    ev.install_warning_router()
    hook = warnings.showwarning
    ev.install_warning_router()
    ev.install_warning_router()
    assert warnings.showwarning is hook
    with ev.collect_warnings():
        assert warnings.showwarning is hook


def test_fa09_warnings_outside_a_collector_are_not_swallowed(monkeypatch):
    """Der Router hängt prozessweit, darf aber außerhalb eines Laufs nichts
    verschlucken: ohne aktiven Sammler geht die Warnung an den vorherigen Hook."""
    seen: list[str] = []
    ev.install_warning_router()
    monkeypatch.setattr(
        ev,
        "_previous_showwarning",
        lambda message, category, filename, lineno, file=None, line=None: seen.append(
            str(message)
        ),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("außerhalb eines Laufs", UserWarning)
        with ev.collect_warnings() as caught:
            warnings.warn("innerhalb", UserWarning)
        warnings.warn("wieder außerhalb", UserWarning)
    assert seen == ["außerhalb eines Laufs", "wieder außerhalb"]
    assert [m for m, _ in caught] == ["innerhalb"]
