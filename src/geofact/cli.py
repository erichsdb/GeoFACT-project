"""Implements: FA1, FA2 (CLI validate), FA9 (CLI run), FA40 (CLI plugins), FA44 (CLI auf dem Anwendungsfall), FA55 (Arbeits-CRS-Zeilen), FA59 (--param, --print-expanded), FA62 (when-Zeile), FA75 (Expansion-Zeile mit Instanzen), FA65 (Lizenzzeilen), FA66 (Freigabe, --keep-intermediates), FA73 (geofact licenses, validate --check-licenses).

Kommandozeilenschnittstelle:

    geofact validate CONFIG [--param NAME=WERT ...] [--print-expanded] [--check-licenses]
    geofact licenses CONFIG [--param NAME=WERT ...] [--refresh]
    geofact run CONFIG [--out DIR] [--param NAME=WERT ...] [--refresh-snapshots]
                       [--dump-intermediate DIR] [--keep-intermediates] [--allow-skipped]
    geofact plugins

Die CLI ist reine Auslieferung: sie importiert nur ``geofact.api`` (FA45) und
nutzt denselben Anwendungsfall wie die Web-Demo (FA44). ``run`` schreibt die im
Szenario deklarierten Ausgaben (Standard: ``./output/<YAML-Name>/``), Warnungen
und übersprungene Ausgaben gehen nach stderr.

``--param`` überschreibt deklarierte Szenario-Parameter (FA59; Text wird nach
dem deklarierten Typ gelesen). ``validate`` nennt Arbeits-CRS (FA55), Parameter,
Expansion und durch ``when`` entfernte Elemente (FA62) sowie die Lizenzen der
Ausgabequellen (FA65); ``--print-expanded`` gibt das ausgedehnte Szenario als YAML
aus. ``run`` gibt Zwischenergebnisse nach ihrem letzten Leser frei (FA66), außer
bei ``--keep-intermediates`` oder ``--dump-intermediate``.

``licenses`` (und ``validate --check-licenses``) prüft je Ausgabe die Lizenzen der
Quellen zusammen mit der Ausgabelizenz gegen die DALICC-API (FA73, Netz; Antworten
unter ``GEOFACT_SNAPSHOT_DIR/dalicc``, ``--refresh`` fragt neu).

Exit-Codes: 0 = ok; 1 = Konfigurations- oder Laufzeitfehler, auch ein
Dateisystemfehler beim Schreiben (Ursache auf stderr, kein Traceback); 2 = mindestens
eine deklarierte Ausgabe wurde übersprungen (0 mit --allow-skipped); 3 = DALICC
meldet einen Lizenzkonflikt (nicht prüfbare Lizenzen sind kein Konflikt, aber kein
"vereinbar"); ein nicht erreichbarer Dienst ist 1.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from geofact import api

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_SKIPPED_OUTPUT = 2
EXIT_LICENSE_CONFLICT = 3

_LICENSE_STATUS = {
    "compatible": "vereinbar",
    "conflicts": "KONFLIKT",
    "not_checkable": "nicht prüfbar",
    "unreachable": "nicht geprüft (DALICC nicht erreichbar)",
    "no_licenses": "keine Lizenzen",
}


def _print_config_problem(config_path: Path, issues: Sequence[tuple[str, str]]) -> None:
    print(f"Konfiguration ungültig: {config_path}", file=sys.stderr)
    for location, message in issues:
        print(f"  [{location}] {message}", file=sys.stderr)


def _missing_file(config_path: Path) -> bool:
    if config_path.is_file():
        return False
    print(f"Konfigurationsdatei nicht gefunden: {config_path}", file=sys.stderr)
    return True


def _parameters(config_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    """FA59: ``--param NAME=WERT`` -> typisierte Überschreibungen
    (``ConfigError`` mit Position bei falscher Form oder falschem Typ)."""
    return api.parameter_overrides(config_path, list(args.param or []))


def _shown_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _print_parameters(document: api.ScenarioDocument) -> None:
    expansion = document.expansion
    if expansion is None or not expansion.parameters:
        return
    parts = []
    for name in expansion.parameters:
        mark = " (überschrieben)" if name in expansion.overridden else ""
        parts.append(f"{name}={_shown_value(expansion.values.get(name))}{mark}")
    print(f"Parameter: {', '.join(parts)}")


def _print_expansion(document: api.ScenarioDocument) -> None:
    """FA62/FA75: Umfang der Expansion (Instanzen mit Anzahl und Modul) und
    die durch ``when`` entfernten Elemente."""
    expansion = document.expansion
    if expansion is None or expansion.is_trivial():
        return
    instances = (
        ", ".join(
            f"{info.id}{f' x{len(info.keys)}' if info.keys else ''} [{info.module}]"
            for info in expansion.instances
        )
        or "keine"
    )
    print(f"Expansion: {expansion.node_count} Knoten (Instanzen: {instances})")
    for location, expression in expansion.dropped:
        print(f"  übersprungen durch when: {' -> '.join(location)} ({expression})")


def _print_attribution(attribution: api.NodeAttribution) -> None:
    """FA65: Lizenzzeilen der Ausgabequellen und Layer ohne Lizenzangabe."""
    if attribution.empty and not attribution.undeclared:
        return
    print("Datenquellen/Lizenzen:")
    for line in attribution.lines():
        print(f"  {line}")
    if attribution.undeclared:
        print(f"  ohne Lizenzangabe: {', '.join(attribution.undeclared)}")


def _print_output_licenses(document: api.ScenarioDocument) -> None:
    """FA73: die vom Nutzer festgelegten Ausgabelizenzen (ohne Prüfung)."""
    scenario = document.scenario
    lines = []
    for index, spec in enumerate(scenario.output):
        license_, _origin = scenario.output_license_of(spec)
        if license_ is not None:
            lines.append(
                f"  output[{index}] {spec.type} <- {spec.source}: {license_.line()}"
            )
    if lines:
        print(
            "Ausgabelizenzen (vom Nutzer festgelegt, ungeprüft - geofact licenses prüft):"
        )
        for line in lines:
            print(line)


def _print_license_check(report: api.LicenseCheckReport) -> int:
    """FA73: Ergebnis je Ausgabe; Rückgabe ist der Exit-Code."""
    print("Lizenzkompatibilität (DALICC, keine Rechtsauskunft):")
    for check in report.checks:
        target = f"output[{check.index}] {check.type} <- {check.source}"
        print(f"  {target}: {_LICENSE_STATUS[check.status]}")
        if check.output_license is not None:
            origin = (
                "scenario.output_license"
                if check.declared_at == "scenario"
                else "output.license"
            )
            print(
                f"    Ausgabelizenz: {check.output_license.name} ({origin}, vom Nutzer festgelegt)"
            )
        names = ", ".join(lic.name for lic in check.source_licenses) or "-"
        print(f"    Quelllizenzen: {names}")
        if check.undeclared:
            print(f"    ohne Lizenzangabe: {', '.join(check.undeclared)}")
        for conflict in check.conflicts:
            print(f"    {conflict.message}")
        for item in check.uncheckable:
            print(f"    nicht prüfbar: {item.name} - {item.reason}")
        if check.status == "unreachable":
            print(f"    {check.message}", file=sys.stderr)
        if check.checked_at:
            print(f"    geprüft am {check.checked_at}")
    if report.status == "unreachable":
        return EXIT_ERROR
    if report.status == "conflicts":
        return EXIT_LICENSE_CONFLICT
    return EXIT_OK


def cmd_licenses(args: argparse.Namespace) -> int:
    """FA73: ``geofact licenses CONFIG`` - Lizenzkompatibilität je Ausgabe."""
    config_path = Path(args.config)
    if _missing_file(config_path):
        return EXIT_ERROR
    try:
        document = api.load_scenario(
            config_path, parameters=_parameters(config_path, args)
        )
    except api.ConfigError as exc:
        _print_config_problem(config_path, exc.issues)
        return EXIT_ERROR
    return _print_license_check(api.check_licenses(document, refresh=args.refresh))


def cmd_validate(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if _missing_file(config_path):
        return EXIT_ERROR

    try:
        parameters = _parameters(config_path, args)
    except api.ConfigError as exc:
        _print_config_problem(config_path, exc.issues)
        return EXIT_ERROR
    report = api.validate(config_path, parameters=parameters)
    if not report.valid:
        _print_config_problem(config_path, report.issues)
        return EXIT_ERROR

    assert report.plan is not None and report.document is not None
    document = report.document
    crs = document.scenario.scenario.crs
    print(f"valide: {config_path}")
    print(f"Arbeits-CRS: {crs}" + ("" if crs == "auto" else " (deklariert)"))
    _print_parameters(document)
    _print_expansion(document)
    print(f"Ausführungsreihenfolge: {list(report.plan.full_order)}")
    print(f"Benötigte Layer: {sorted(report.plan.required_layers)}")
    print(f"Ungenutzte Layer: {sorted(report.plan.unused_layers)}")
    _print_attribution(api.output_attribution(document, report.plan))
    _print_output_licenses(document)
    if args.print_expanded and document.expansion is not None:
        print("--- ausgedehntes Szenario ---")
        print(
            yaml.safe_dump(
                document.expansion.expanded, allow_unicode=True, sort_keys=False
            ),
            end="",
        )
    if args.check_licenses:
        return _print_license_check(api.check_licenses(document))
    return EXIT_OK


def _print_warning(event: api.ProgressEvent) -> None:
    """Warnungen eines Laufs sofort auf stderr (FA9: nie still verloren)."""
    where = f"Layer '{event.layer}'" if event.layer else f"Schritt '{event.step}'"
    print(f"Warnung ({where}): {event.message}", file=sys.stderr)


def _region_line(detail: dict[str, Any]) -> str:
    """FA55 Nachbedingung 2: ``Region <Name|BBox> (<Fläche> km2) ->
    Arbeits-CRS EPSG:... (auto|deklariert)``."""
    name = detail.get("display_name")
    if not name:
        bbox = detail.get("bbox") or ()
        name = ",".join(f"{value:g}" for value in bbox) or "?"
    mode = "auto" if detail.get("crs_mode") == "auto" else "deklariert"
    area = detail.get("area_km2")
    size = f" ({area:.1f} km2)" if isinstance(area, (int, float)) else ""
    return f"Region {name}{size} -> Arbeits-CRS {detail.get('crs', '?')} ({mode})"


def _observe(event: api.ProgressEvent) -> None:
    if event.type in (api.events.LAYER_WARNING, api.events.STEP_WARNING):
        _print_warning(event)
    elif event.type == api.events.REGION_READY:
        print(_region_line(event.detail or {}))
    elif event.type == api.events.REGION_WARNING:
        print(f"Regionswarnung: {event.message}", file=sys.stderr)


def _describe_file_error(exc: OSError, out_dir: Path) -> str:
    """Meldung für einen Dateisystemfehler beim Schreiben, bei fehlenden Rechten mit Hinweis."""
    reason = exc.strerror or str(exc)
    target = f" '{exc.filename}'" if exc.filename else ""
    hint = (
        " Schreibrechte prüfen oder mit --out ein anderes Verzeichnis wählen."
        if isinstance(exc, PermissionError)
        else ""
    )
    return (
        f"Lauf fehlgeschlagen: Dateizugriff nicht möglich, die Ausgaben wurden nicht "
        f"(vollständig) geschrieben - {reason}{target} (Ausgabeverzeichnis: {out_dir}).{hint}"
    )


def cmd_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    if _missing_file(config_path):
        return EXIT_ERROR

    # Relative Pfade im Szenario gelten gegen die YAML, nicht das Arbeitsverzeichnis
    # (load_scenario setzt base_dir bei einer Datei, FA4).
    try:
        document = api.load_scenario(
            config_path, parameters=_parameters(config_path, args)
        )
    except api.ConfigError as exc:
        _print_config_problem(config_path, exc.issues)
        return EXIT_ERROR
    _print_parameters(document)

    out_dir = Path(args.out) if args.out else Path("output") / config_path.stem
    # FA66 Nachbedingung 4: Freigabe an, außer Zwischenergebnisse werden
    # abgelegt oder ausdrücklich behalten.
    release = not (args.dump_intermediate or args.keep_intermediates)
    if args.dump_intermediate:
        # Vor dem Lauf prüfen, damit ein Fehler nicht erst nach der Analyse auffällt.
        dump = Path(args.dump_intermediate)
        try:
            dump.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"Lauf fehlgeschlagen: Verzeichnis für --dump-intermediate nicht anlegbar - "
                f"{exc.strerror or exc} '{dump}'. Die Analyse wurde nicht gestartet.",
                file=sys.stderr,
            )
            return EXIT_ERROR
    try:
        report = api.run(
            document,
            out_dir=out_dir,
            observer=_observe,
            refresh=args.refresh_snapshots,
            dump_intermediate=args.dump_intermediate,
            release_intermediates=release,
        )
    except api.ConfigError as exc:
        _print_config_problem(config_path, exc.issues)
        return EXIT_ERROR
    except api.RunCancelled:
        print("Lauf abgebrochen", file=sys.stderr)
        return EXIT_ERROR
    except RuntimeError as exc:
        # LayerLoadError, StepExecutionError und andere Laufzeitfehler.
        print(f"Lauf fehlgeschlagen: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        # Dateizugriffe beim Laden wickelt der Executor ein; ein OSError hier stammt
        # vom Schreiben der Ausgaben oder Zwischenergebnisse.
        print(_describe_file_error(exc, out_dir), file=sys.stderr)
        return EXIT_ERROR

    print(f"Ausgeführt: {config_path}")
    print(f"Ausführungsreihenfolge: {report.result.order}")
    print(f"Geladene Layer: {report.result.loaded_layers}")

    written = report.outputs.written if report.outputs is not None else []
    print(f"Ausgaben ({len(written)}) in {out_dir}:")
    for path in written:
        print(f"  {path}")
    skipped = report.skipped_outputs
    for item in skipped:
        print(
            f"Ausgabe übersprungen (output[{item.index}], type={item.type}, "
            f"source={item.source}): {item.reason}",
            file=sys.stderr,
        )

    _print_attribution(report.attribution)
    if report.attribution_file is not None:
        print(f"Namensnennung abgelegt in: {report.attribution_file}")

    if args.dump_intermediate:
        print(f"Zwischenergebnisse abgelegt in: {args.dump_intermediate}")
        for step_id, reason in report.intermediates_skipped:
            print(
                f"Zwischenergebnis '{step_id}' nicht abgelegt: {reason}",
                file=sys.stderr,
            )

    if skipped and not args.allow_skipped:
        return EXIT_SKIPPED_OUTPUT
    return EXIT_OK


_PLUGIN_SECTIONS = (
    ("sources", "Quellarten (layers[].source)"),
    ("file_formats", "Dateiformate (source: file, format)"),
    ("table_formats", "Tabellenformate (source: table, format)"),
    ("transforms", "Transformationen (Harmonisierung nach dem Laden)"),
    ("operations", "Operationen (steps[].op)"),
    ("outputs", "Ausgabeformate (output[].type)"),
)


def cmd_plugins(args: argparse.Namespace) -> int:
    """FA40: zeigt alle erkannten Erweiterungen mit ihrer Herkunft."""
    registry = api.load_extensions()
    listing = api.extension_catalog()
    for key, title in _PLUGIN_SECTIONS:
        print(f"{title}:")
        for entry in listing[key]:
            print(f"  {entry['name']:<24} {entry['origin']}")
    failed = registry.failed
    if failed:
        print("Nicht geladene Plugins:")
        for origin, message in sorted(failed.items()):
            print(f"  {origin}: {message}")
    return EXIT_OK


def _add_param_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--param",
        action="append",
        metavar="NAME=WERT",
        help="Szenario-Parameter überschreiben (wiederholbar; Wert nach deklariertem Typ)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="geofact")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Konfiguration prüfen")
    validate_parser.add_argument("config", help="Pfad zur Szenario-YAML")
    _add_param_argument(validate_parser)
    validate_parser.add_argument(
        "--print-expanded",
        action="store_true",
        help="Das ausgedehnte Szenario (ohne parameters/templates/foreach) als YAML ausgeben",
    )
    validate_parser.add_argument(
        "--check-licenses",
        action="store_true",
        help="Lizenzen je Ausgabe (Quellen + Ausgabelizenz) gegen DALICC prüfen (Netz, FA73)",
    )
    validate_parser.set_defaults(func=cmd_validate)

    licenses_parser = subparsers.add_parser(
        "licenses",
        help="Lizenzkompatibilität je Ausgabe gegen DALICC prüfen (Netz, FA73)",
    )
    licenses_parser.add_argument("config", help="Pfad zur Szenario-YAML")
    _add_param_argument(licenses_parser)
    licenses_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Abgelegte DALICC-Antworten ignorieren und neu fragen",
    )
    licenses_parser.set_defaults(func=cmd_licenses)

    run_parser = subparsers.add_parser(
        "run", help="Szenario ausführen und die deklarierten Ausgaben schreiben"
    )
    run_parser.add_argument("config", help="Pfad zur Szenario-YAML")
    run_parser.add_argument(
        "--out",
        metavar="DIR",
        help="Verzeichnis für die Ausgaben (Standard: ./output/<Name der YAML>/)",
    )
    run_parser.add_argument(
        "--refresh-snapshots",
        action="store_true",
        help="Snapshots (OSM, Region, WFS, ...) erzwungen neu abrufen",
    )
    run_parser.add_argument(
        "--dump-intermediate",
        metavar="DIR",
        help="Zwischenergebnisse (Vektor-Schritte als GeoJSON) in DIR ablegen",
    )
    run_parser.add_argument(
        "--keep-intermediates",
        action="store_true",
        help="Zwischenergebnisse bis zum Laufende im Speicher behalten "
        "(Standard: nach dem letzten Leser freigeben, FA66)",
    )
    _add_param_argument(run_parser)
    run_parser.add_argument(
        "--allow-skipped",
        action="store_true",
        help="Exit-Code 0 auch dann, wenn eine deklarierte Ausgabe übersprungen wurde",
    )
    run_parser.set_defaults(func=cmd_run)

    plugins_parser = subparsers.add_parser(
        "plugins", help="Erkannte Erweiterungen und ihre Herkunft auflisten"
    )
    plugins_parser.set_defaults(func=cmd_plugins)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    api.load_extensions()  # explizit, einmal: Kern + GEOFACT_PLUGIN_PATH (FA40)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
