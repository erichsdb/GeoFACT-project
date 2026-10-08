"""Implements: FA84 (Darstellung: heller Modus als Standard, dunkler Modus im Einstellungsdialog).

Reine Quelltextverträge - im Repo gibt es kein Frontend-Testwerkzeug (siehe
test_fa54_frontend_lost_run.py).
"""

from __future__ import annotations

from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _read(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_fa84_light_is_the_default_and_the_system_preference_is_ignored() -> None:
    layout = _read("app", "layout.tsx")
    assert 'defaultTheme="light"' in layout
    assert "enableSystem={false}" in layout
    assert 'defaultTheme="system"' not in layout


def test_fa84_a_choice_from_before_the_light_default_is_not_taken_over() -> None:
    layout = _read("app", "layout.tsx")
    # nicht der Standardschlüssel "theme" von next-themes, unter dem alte Wahlen liegen
    assert 'const THEME_STORAGE_KEY = "geofact:theme";' in layout
    assert "storageKey={THEME_STORAGE_KEY}" in layout


def test_fa84_light_pages_opt_out_of_forced_darkening() -> None:
    assert "enableColorScheme={false}" in _read("app", "layout.tsx")
    css = _read("app", "globals.css")
    root = css[css.index(":root {") : css.index(".dark {")]
    assert "color-scheme: only light;" in root
    assert "color-scheme: dark;" in css[css.index(".dark {") :]


def test_fa84_dark_mode_is_a_switch_in_the_settings_dialog() -> None:
    dialog = _read("components", "generation-settings.tsx")
    assert 'id="settings-dark-mode"' in dialog
    assert "checked={darkMode} onCheckedChange={setDarkMode}" in dialog
    assert 'useState(resolvedTheme === "dark")' in dialog
    # gilt erst mit "Übernehmen", wie die übrigen Einstellungen
    save = dialog[dialog.index("const save = ") : dialog.index("const reset = ")]
    assert 'setTheme(darkMode ? "dark" : "light")' in save


def test_fa84_reset_goes_back_to_light() -> None:
    dialog = _read("components", "generation-settings.tsx")
    reset = dialog[dialog.index("const reset = ") : dialog.index("const defaultLlm")]
    assert "setDarkMode(false)" in reset


def test_fa84_header_has_no_separate_theme_button() -> None:
    shell = _read("components", "app-shell.tsx")
    assert "ThemeToggle" not in shell
    assert not (FRONTEND / "components" / "theme-toggle.tsx").exists()
