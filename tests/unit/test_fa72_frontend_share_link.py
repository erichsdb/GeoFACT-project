"""Implements: FA72 (Szenario per URL teilbar) - Frontend-Teil.

Es gibt kein Frontend-Testwerkzeug im Repo (siehe test_fa54_frontend_lost_run.py).
Diese Vertragstests lesen deshalb den Quelltext und prüfen die Nachbedingungen
von FA72: Parameterformat, Vorrang des Links vor localStorage, ausdrückliche
Fehler, Entfernen des Parameters bei abweichendem Editortext, "Link kopieren",
Vorauswahl in der Szenarioauswahl - und dass nie ein Token oder eine Lauf-ID im Link steht.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
SHARE = FRONTEND / "lib" / "shareLink.ts"
DRIVER = Path(__file__).resolve().parent / "fa72_share_link_driver.mjs"
APP_SHELL = FRONTEND / "components" / "app-shell.tsx"
PICKER = FRONTEND / "components" / "scenario-picker.tsx"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_fa72_param_format_is_prefixed() -> None:
    src = _text(SHARE)
    assert 'SCENARIO_PARAM = "scenario"' in src
    # Präfix example:/user: ist Pflicht, sonst "invalid" (kein Raten).
    assert 'prefix !== "example" && prefix !== "user"' in src
    assert 'kind: "invalid"' in src
    # Beispiel-IDs enthalten "/": je Segment kodieren, "/" und ":" lesbar lassen.
    assert 'split("/").map(encodeURIComponent).join("/")' in src
    assert "${ref.source}:${id}" in src


def test_fa72_link_has_no_token_or_run() -> None:
    src = _text(SHARE)
    code = re.sub(r"//.*", "", src)
    assert "token" not in code.lower()
    assert "runId" not in code and "run_id" not in code
    shell = _text(APP_SHELL)
    start = shell.index("const copyShareLink")
    body = shell[start : shell.index("};", start)]
    assert "buildShareUrl(" in body
    assert "token" not in body.lower() and "runId" not in body


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i : src.index(end, i)]


def test_fa72_url_takes_precedence_over_local_storage() -> None:
    shell = _text(APP_SHELL)
    assert "parseScenarioParam(window.location.search)" in shell
    # Vorrang nur für einen geöffneten Link ("open"), Schritt 1 erzwungen
    assert 'if (linkMode === "open") return "prompt";' in shell
    # die gemerkte runId wird beim Start NICHT mehr vom Parameter abhängig gemacht
    assert (
        "useState<string | null>(() => readLocalStorage(RUN_ID_STORAGE_KEY))" in shell
    )
    open_link = _between(
        shell, "const openLink = async", "useEffect(() => {\n    if (linkMode"
    )
    # erst nach erfolgreichem Laden wird der gemerkte Lauf verworfen
    assert "setRunId(null)" in open_link and 'setStep("prompt")' in open_link
    assert open_link.index("api.loadScenario(") < open_link.index("setRunId(null)")
    mount = _between(
        shell, "useEffect(() => {\n    if (linkMode", "// FA72: nur solange"
    )
    assert "openLink(urlScenario.ref)" in mount
    assert mount.rstrip().endswith("}, []);")


def test_fa72_reload_of_own_tab_keeps_run_and_step() -> None:
    """Review finding 2: reload after "Laden..." must not wipe the stored run."""
    shell = _text(APP_SHELL)
    # "restore": Marke der App stimmt mit dem Parameter überein -> kein Link-Vorrang
    assert 'linkMode === "restore"' in shell
    assert 'if (linkMode === "none" || linkMode === "restore"' in shell
    assert "sessionStorage.setItem(OWN_LINK_STORAGE_KEY" in shell
    # ein unlesbarer Parameter erzwingt ebenfalls nichts
    assert 'linkMode === "open"' in shell and 'urlScenario.kind !== "none"' not in shell


def test_fa72_open_link_effect_runs_once_and_guards_edits() -> None:
    """Review finding 3: StrictMode double effect and edit-during-fetch race."""
    shell = _text(APP_SHELL)
    mount = _between(
        shell, "useEffect(() => {\n    if (linkMode", "// FA72: nur solange"
    )
    assert "linkLoadStarted.current" in mount
    assert "linkLoadStarted.current = true" in mount
    open_link = _between(
        shell, "const openLink = async", "useEffect(() => {\n    if (linkMode"
    )
    assert "const startYaml = configYamlRef.current" in open_link
    guard = open_link.index("configYamlRef.current !== startYaml")
    assert (
        open_link.index("api.loadScenario(")
        < guard
        < open_link.index("applyConfigYaml(")
    )
    # verworfen wird ausdrücklich gemeldet
    assert "notify.error(" in open_link[guard : open_link.index("return;", guard)]


def test_fa72_unknown_id_is_an_explicit_error() -> None:
    shell = _text(APP_SHELL)
    open_link = _between(
        shell, "const openLink = async", "useEffect(() => {\n    if (linkMode"
    )
    catch = open_link[open_link.index("} catch (e) {") :]
    assert "notify.error(" in catch
    # Parameter nur bei 404 entfernen, nie bei 401/Netzwerk (Review finding 1)
    assert 'linkErrorAction(isNotFoundError(e)) === "remove"' in catch
    assert "replaceState" not in catch
    mount = _between(
        shell, "useEffect(() => {\n    if (linkMode", "// FA72: nur solange"
    )
    assert 'urlScenario.kind === "invalid"' in mount
    assert "notify.error(" in mount and "setUrlScenario(null)" in mount


def test_fa72_param_removed_when_editor_diverges() -> None:
    shell = _text(APP_SHELL)
    assert "loadedScenario.yaml === configYaml" in shell
    assert "yaml: config_yaml" in shell
    effect = _between(shell, "const shareRef", "const copyShareLink")
    assert "if (shareRef) setUrlScenario(shareRef)" in effect
    assert "else if (loadedScenario) setUrlScenario(null)" in effect
    # Adresse wird nur über den Helfer gesetzt, der fremde Parameter und Hash erhält
    assert "urlWithScenario(window.location" in shell
    assert (
        re.search(r'replaceState\(null, "", window\.location\.pathname\)', shell)
        is None
    )


def test_fa72_copy_link_button() -> None:
    shell = _text(APP_SHELL)
    assert "Link kopieren" in shell
    assert "navigator.clipboard.writeText(" in shell
    assert "window.location.origin" in shell
    assert "disabled={!shareRef}" in shell
    # Fehler beim Kopieren werden gemeldet
    copy = shell[
        shell.index("const copyShareLink") : shell.index("const handleInsertStep")
    ]
    assert "notify.error(" in copy


def test_fa72_picker_preselects_url_entry() -> None:
    picker = _text(PICKER)
    assert "preselect" in picker
    assert "s.id === preselect.id && s.source === preselect.source" in picker
    assert "preselect={shareRef}" in _text(APP_SHELL)


# --- Logik der reinen Helfer, ausgeführt unter node (Typ-Stripping) ---------


@pytest.fixture(scope="module")
def js() -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    proc = subprocess.run(
        [node, str(DRIVER), str(SHARE)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"node cannot run the TypeScript helpers: {proc.stderr[-300:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_fa72_parse_rejects_unreadable_values(js: dict) -> None:
    for q in (
        "?scenario=example:",
        "?scenario=foo:bar",
        "?scenario=example",
        "?scenario=",
    ):
        assert js["parse"][q]["kind"] == "invalid", q
    assert js["parse"]["?other=1"] == {"kind": "none"}
    assert js["parse"][""] == {"kind": "none"}


def test_fa72_parse_splits_at_first_colon_only(js: dict) -> None:
    assert js["parse"]["?scenario=user:a:b"]["ref"] == {"id": "a:b", "source": "user"}
    assert js["parse"]["?scenario=example:a/b"]["ref"] == {
        "id": "a/b",
        "source": "example",
    }


def test_fa72_build_parse_roundtrip_is_identity(js: dict) -> None:
    assert len(js["roundtrip"]) >= 6
    for case in js["roundtrip"]:
        assert case["parsed"] == {"kind": "ok", "ref": case["ref"]}, case["search"]
    searches = {c["ref"]["id"]: c["search"] for c in js["roundtrip"]}
    # "/" bleibt lesbar, "+" und Umlaute werden kodiert (sonst würde "+" zu " ")
    assert searches["leipzig/leipzig10_erreichbarkeit"].endswith(
        "example:leipzig/leipzig10_erreichbarkeit"
    )
    assert "%2B" in searches["Müller & Söhne+Co 100% ü"]


def test_fa72_other_params_and_hash_survive(js: dict) -> None:
    """Review finding 4: replaceState must not drop other parameters or the hash."""
    k = js["keep_others"]
    assert k["set"] == "?a=1&b=x%20y&scenario=example:leipzig/leipzig10_erreichbarkeit"
    assert k["replace"] == "?a=1&scenario=user:lonely-villages"
    assert k["remove"] == "?a=1&b=2"
    assert k["remove_only"] == "" and k["empty"] == ""
    assert js["url"]["set"] == "/app?a=1&scenario=user:lonely-villages#sec"
    assert js["url"]["remove"] == "/app?a=1#sec"
    assert js["url"]["remove_plain"] == "/"


def test_fa72_reload_during_run_is_restore_not_open(js: dict) -> None:
    """Review finding 2: own address after "Laden..." -> restore (run kept); foreign link -> open."""
    p = js["plan"]
    assert p["restore"] == "restore"
    assert p["open_no_marker"] == "open"
    assert p["open_other_marker"] == "open"
    assert p["none"] == "none"
    # unlesbarer Parameter lädt nichts und überschreibt nichts
    assert p["invalid"] == "invalid"


def test_fa72_failed_load_keeps_param_unless_unknown(js: dict) -> None:
    """Review finding 1: 401 / network error must not destroy a valid link."""
    assert js["error_action"] == {"notfound": "remove", "other": "keep"}
