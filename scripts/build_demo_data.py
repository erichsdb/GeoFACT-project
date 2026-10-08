"""Builds the data package of the local demo (docker-compose.demo.yml).

Not part of the framework - delivery tooling like scripts/run_demo.py; it uses
only `geofact.api`. The package makes the shipped example scenarios run inside
the demo image without network access and without the multi-gigabyte source
rasters:

  demo-data/snapshots/       the snapshots (FA7) the scenarios read, copied
                             from the local snapshot store
  demo-data/examples-data/   the rasters the scenarios reference, cut to
                             Saxony plus a margin (same grid, same cell values)
  demo-data/MANIFEST.md      what is inside, data state per scenario, licenses
  demo-data/geofact-demo-data.tar.gz   the three above, for a release asset

How it finds the snapshots: every scenario runs once against the local store
with the OSM backend of the demo (snapshot keys depend on the backend). Missing
snapshots are fetched during that run. The run records which files of the store
it read; if it fetched anything, a second run records the complete set.

Usage:
    uv run python scripts/build_demo_data.py
    uv run python scripts/build_demo_data.py --store D:/geofact/snapshots --examples-dir D:/geofact/examples
    uv run python scripts/build_demo_data.py examples/chemnitz_route_hbf_tu.yaml   # only these scenarios
    uv run python scripts/build_demo_data.py --verify geofact-backend:demo         # run them offline in the image
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "demo-data"
STATE_FILE = "scenarios.json"
ARCHIVE = "geofact-demo-data.tar.gz"
RUN_TIMEOUT_S = 3600

# Scenarios the package does not cover, with the reason shown in the manifest.
EXCLUDED = {
    "berlin_gruenflaechen_erreichbarkeit.yaml": "Berlin liegt außerhalb des Rasterausschnitts (Sachsen mit Rand)",
    "deutschland_gruenste_grossstadt_osm.yaml": "80 Großstädte, gedacht für OSM auf PostGIS (deutschlandweit)",
    "sachsen_staedte_gruenanteil_sentinel2.yaml": "Sentinel-2-Mosaik aus vier Kacheln, rund 1,5 GB Arbeitsspeicher",
    "europa/europa_netz_resilienz.yaml": "braucht das europaweite Bevölkerungsraster und die Netzdaten aus scripts/fetch_europa_data.py",
    "plugin_demo/szenario_plugins.yaml": "braucht den lokalen Demo-Dienst und GEOFACT_PLUGIN_PATH (siehe examples/plugin_demo)",
}

# Window of the raster subsets in EPSG:4326 (west, south, east, north): Saxony
# with a margin of roughly 40 km for buffers that reach across the border.
SUBSET_BOUNDS = (11.3, 49.7, 15.6, 52.2)

# Rasters the remaining scenarios reference under examples/data/.
RASTERS = {
    "GHS_POP_E2025_GLOBE_R2023A_54009_100_V1_0.tif": "GHS-POP R2023A, Epoche 2025, 100 m (Europäische Kommission, JRC; CC BY 4.0)",
    "ntl_2024_simviirs.tif": "Harmonisierter Nachtlicht-Datensatz DMSP/VIIRS, Jahr 2024, simVIIRS, 1 km (Li et al.; CC BY 4.0)",
    "dem_sachsen_glo90.tif": "Copernicus DEM GLO-90 (ESA/Copernicus; frei mit Quellenangabe)",
}


# =====================================================================
# Worker: one scenario run that records the snapshot files it reads
# =====================================================================


def _worker(scenario: Path, record_to: Path) -> int:
    from geofact import api

    store = Path(os.environ["GEOFACT_SNAPSHOT_DIR"]).resolve()
    seen: set[str] = set()
    real_is_file = Path.is_file

    def recording_is_file(self: Path, *args, **kwargs) -> bool:
        found = real_is_file(self, *args, **kwargs)
        if found:
            try:
                seen.add(self.resolve().relative_to(store).as_posix())
            except ValueError:
                pass
        return found

    # Every source checks its snapshot with Path.is_file() before reading it.
    Path.is_file = recording_is_file  # type: ignore[method-assign]
    warnings: list[str] = []
    result: dict[str, object] = {"ok": False, "error": None}
    try:
        with tempfile.TemporaryDirectory() as out_dir:
            document = api.load_scenario(scenario)
            report = api.run(
                document,
                out_dir=out_dir,
                observer=lambda event: (
                    warnings.append(event.message)
                    if event.type in (api.events.LAYER_WARNING, api.events.STEP_WARNING)
                    else None
                ),
            )
        result["ok"] = True
        result["skipped_outputs"] = [item.reason for item in report.skipped_outputs]
    except Exception as exc:  # noqa: BLE001 - the parent reports the reason per scenario
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        Path.is_file = real_is_file  # type: ignore[method-assign]
    result["files"] = sorted(seen)
    result["warnings"] = warnings
    record_to.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["ok"] else 1


def _run_scenario(scenario: Path, store: Path, backend: str) -> dict:
    env = dict(
        os.environ,
        # scenario 1 needs the shipped domain plugin (examples/plugins)
        GEOFACT_PLUGIN_PATH=os.environ.get("GEOFACT_PLUGIN_PATH")
        or str(REPO_ROOT / "examples" / "plugins"),
        GEOFACT_SNAPSHOT_DIR=str(store),
        GEOFACT_OSM_BACKEND=backend,
        PYTHONIOENCODING="utf-8",
    )
    with tempfile.TemporaryDirectory() as tmp:
        record = Path(tmp) / "record.json"
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(scenario),
                    str(record),
                ],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=RUN_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"Abbruch nach {RUN_TIMEOUT_S} s",
                "files": [],
                "seconds": RUN_TIMEOUT_S,
            }
        seconds = round(time.monotonic() - started, 1)
        if not record.is_file():
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return {
                "ok": False,
                "error": " | ".join(tail) or "kein Ergebnis",
                "files": [],
                "seconds": seconds,
            }
        result = json.loads(record.read_text(encoding="utf-8"))
        result["seconds"] = seconds
        return result


def _store_listing(store: Path) -> set[str]:
    return {p.relative_to(store).as_posix() for p in store.rglob("*") if p.is_file()}


# =====================================================================
# Build
# =====================================================================


def _scenarios(examples_dir: Path, selected: list[Path]) -> list[Path]:
    if selected:
        return [p.resolve() for p in selected]
    found = sorted(examples_dir.rglob("*.yaml"))
    return [p for p in found if "data" not in p.relative_to(examples_dir).parts[:-1]]


def _copy_snapshots(files: list[str], store: Path, target: Path) -> int:
    """Copies each recorded file with its sidecars (same key, other suffix)."""
    copied = 0
    for rel in files:
        source = store / rel
        key = source.name.split(".")[0]
        for sibling in source.parent.glob(f"{key}.*"):
            destination = target / sibling.relative_to(store)
            if (
                destination.is_file()
                and destination.stat().st_size == sibling.stat().st_size
            ):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sibling, destination)
            copied += 1
    return copied


def _subset_raster(source: Path, target: Path) -> dict:
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    with rasterio.open(source) as src:
        bounds = transform_bounds("EPSG:4326", src.crs, *SUBSET_BOUNDS, densify_pts=21)
        window = (
            from_bounds(*bounds, transform=src.transform)
            .round_offsets()
            .round_lengths()
        )
        window = window.intersection(Window(0, 0, src.width, src.height))
        profile = src.profile.copy()
        profile.update(
            height=int(window.height),
            width=int(window.width),
            transform=src.window_transform(window),
            compress="deflate",
            tiled=True,
            blockxsize=512,
            blockysize=512,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(target, "w", **profile) as dst:
            dst.write(src.read(window=window))
            dst.update_tags(**src.tags())
            dst.update_tags(
                GEOFACT_SUBSET=(
                    f"Ausschnitt {SUBSET_BOUNDS} (EPSG:4326) aus {source.name}; "
                    "Raster und Zellwerte unverändert"
                )
            )
            for index in src.indexes:
                dst.update_tags(index, **src.tags(index))
        return {
            "cells": f"{int(window.width)} x {int(window.height)}",
            "mb": round(target.stat().st_size / 1e6, 1),
        }


def _referenced_rasters(scenarios: list[Path]) -> list[str]:
    texts = [p.read_text(encoding="utf-8") for p in scenarios]
    return [name for name in RASTERS if any(name in text for text in texts)]


def build(args: argparse.Namespace) -> int:
    examples_dir = args.examples_dir.resolve()
    store = args.store.resolve()
    if not examples_dir.is_dir():
        print(f"Beispielordner fehlt: {examples_dir}", file=sys.stderr)
        return 1
    store.mkdir(parents=True, exist_ok=True)
    out = args.out.resolve()
    snapshots = out / "snapshots"
    state = _load_state(out)
    state.update(
        backend=args.backend, built_at=_now(), scenarios=state.get("scenarios", {})
    )

    failed = 0
    for scenario in _scenarios(examples_dir, args.scenarios):
        rel = scenario.relative_to(examples_dir).as_posix()
        if rel in EXCLUDED and not args.scenarios:
            state["scenarios"][rel] = {"status": "excluded", "reason": EXCLUDED[rel]}
            continue
        print(f"{rel} ...", flush=True)
        before = _store_listing(store)
        result = _run_scenario(scenario, store, args.backend)
        fetched = len(_store_listing(store) - before)
        if result["ok"] and fetched:
            print(
                f"  {fetched} Dateien neu bezogen, zweiter Lauf für die vollständige Liste",
                flush=True,
            )
            result = _run_scenario(scenario, store, args.backend)
        if not result["ok"]:
            failed += 1
            print(f"  FEHLER: {result['error']}", file=sys.stderr, flush=True)
            state["scenarios"][rel] = {"status": "failed", "reason": result["error"]}
            continue
        copied = _copy_snapshots(result["files"], store, snapshots)
        state["scenarios"][rel] = {
            "status": "ok",
            "seconds": result["seconds"],
            "snapshots": len(result["files"]),
            "skipped_outputs": result.get("skipped_outputs", []),
            "data_state": _data_state(result["files"], store),
        }
        print(
            f"  ok, {result['seconds']} s, {len(result['files'])} Snapshot-Dateien ({copied} kopiert)",
            flush=True,
        )

    usable = [
        examples_dir / rel
        for rel, entry in state["scenarios"].items()
        if entry["status"] == "ok"
    ]
    state["rasters"] = {}
    for name in _referenced_rasters(usable):
        source = examples_dir / "data" / name
        if not source.is_file():
            print(f"Raster fehlt: {source}", file=sys.stderr)
            failed += 1
            continue
        print(f"Raster {name} ...", flush=True)
        state["rasters"][name] = {
            **_subset_raster(source, out / "examples-data" / name),
            "source": RASTERS[name],
        }
    _finish(out, state)
    return 1 if failed else 0


def _data_state(files: list[str], store: Path) -> str:
    """Oldest and newest fetch date of the snapshots a scenario read."""
    dates = []
    for rel in files:
        meta = (store / rel).parent / f"{Path(rel).name.split('.')[0]}.meta.json"
        if meta.is_file():
            try:
                dates.append(
                    json.loads(meta.read_text(encoding="utf-8")).get("fetched_at", "")[
                        :10
                    ]
                )
            except ValueError:
                pass
    dates = sorted(d for d in dates if d)
    if not dates:
        return "-"
    return dates[0] if dates[0] == dates[-1] else f"{dates[0]} bis {dates[-1]}"


# =====================================================================
# Verify: run the scenarios in the demo image without network
# =====================================================================


def verify(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    state = _load_state(out)
    failed = 0
    for rel, entry in state.get("scenarios", {}).items():
        if entry["status"] != "ok":
            continue
        print(f"{rel} ...", flush=True)
        started = time.monotonic()
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                args.verify,
                "geofact",
                "run",
                f"examples/{rel}",
                "--out",
                "/tmp/out",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        seconds = round(time.monotonic() - started, 1)
        if proc.returncode == 0:
            entry["offline"] = {"ok": True, "seconds": seconds}
            print(f"  ok, {seconds} s", flush=True)
        else:
            failed += 1
            lines = (proc.stderr or proc.stdout).strip().splitlines()
            reason = lines[-1] if lines else f"Exit-Code {proc.returncode}"
            entry["offline"] = {
                "ok": False,
                "reason": reason,
                "exit_code": proc.returncode,
            }
            print(
                f"  FEHLER (Exit-Code {proc.returncode}): {reason}",
                file=sys.stderr,
                flush=True,
            )
    state["verified_at"] = _now()
    state["verified_image"] = args.verify
    _finish(out, state)
    return 1 if failed else 0


# =====================================================================
# State, manifest, archive
# =====================================================================


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _load_state(out: Path) -> dict:
    path = out / STATE_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _dir_mb(path: Path) -> float:
    return round(sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) / 1e6, 1)


def _manifest(out: Path, state: dict) -> str:
    lines = [
        "# GeoFACT Demo-Datenpaket",
        "",
        f"Erzeugt {state.get('built_at', '-')} mit `scripts/build_demo_data.py`, "
        f"OSM-Backend `{state.get('backend', '-')}`.",
        "",
        "Das Paket lässt die mitgelieferten Szenarien im Demo-Image ohne Netz laufen: `snapshots/` "
        "sind die gespeicherten Bezüge der Quellen (FA7), `examples-data/` die Raster, auf die die "
        "Szenarien verweisen. Ein Lauf meldet je Quelle, dass er einen Snapshot verwendet, und nennt "
        "dessen Bezugszeitpunkt.",
        "",
        f"Größe: Snapshots {_dir_mb(out / 'snapshots')} MB, Raster {_dir_mb(out / 'examples-data')} MB.",
        "",
        "## Szenarien",
        "",
    ]
    if state.get("verified_at"):
        lines += [
            f"Ohne Netz geprüft {state['verified_at']} im Image `{state['verified_image']}` "
            "(`docker run --network none`).",
            "",
        ]
    lines += [
        "| Szenario | Im Paket | Bezug der Daten | Lauf ohne Netz |",
        "|---|---|---|---|",
    ]
    for rel, entry in sorted(state.get("scenarios", {}).items()):
        offline = entry.get("offline")
        if entry["status"] != "ok":
            label = (
                "nein"
                if entry["status"] == "excluded"
                else "nein (Fehler beim Erzeugen)"
            )
            lines.append(f"| `{rel}` | {label}: {entry['reason']} | - | - |")
            continue
        if offline is None:
            checked = "nicht geprüft"
        elif offline["ok"]:
            checked = f"ok, {offline['seconds']} s"
        else:
            checked = f"Fehler: {offline['reason']}"
        lines.append(f"| `{rel}` | ja | {entry['data_state']} | {checked} |")
    lines += [
        "",
        "## Raster",
        "",
        f"Ausschnitte der Originaldateien auf {SUBSET_BOUNDS} (West, Sued, Ost, Nord in EPSG:4326, Sachsen "
        "mit Rand). Raster, Zellgröße und Zellwerte sind unverändert, deshalb tragen die Dateien den "
        "Namen des Originals; der Ausschnitt steht zusätzlich im TIFF-Tag `GEOFACT_SUBSET`. Außerhalb "
        "des Ausschnitts enthalten sie keine Daten.",
        "",
        "| Datei | Quelle und Lizenz | Zellen | MB |",
        "|---|---|---|---|",
    ]
    for name, entry in sorted(state.get("rasters", {}).items()):
        lines.append(
            f"| `{name}` | {entry['source']} | {entry['cells']} | {entry['mb']} |"
        )
    lines += [
        "",
        "## Lizenzen der Snapshots",
        "",
        "OpenStreetMap-Daten: (c) OpenStreetMap-Mitwirkende, ODbL 1.0. Alle weiteren Quellen (WFS, "
        "Open-Data-Portale, GTFS, Sentinel-2) nennt jeder Lauf mit Lizenz in `ATTRIBUTION.txt` neben "
        "seinen Ausgaben.",
        "",
    ]
    return "\n".join(lines)


def _finish(out: Path, state: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / STATE_FILE).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    manifest = _manifest(out, state)
    (out / "MANIFEST.md").write_text(manifest, encoding="utf-8")
    # The image copies examples-data/ to examples/data/ - the note travels with the rasters.
    (out / "examples-data").mkdir(exist_ok=True)
    (out / "examples-data" / "LIESMICH.md").write_text(manifest, encoding="utf-8")
    archive = out / ARCHIVE
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("snapshots", "examples-data", "MANIFEST.md"):
            tar.add(out / name, arcname=name, filter=_without_gitkeep)
    print(f"{archive} ({round(archive.stat().st_size / 1e6, 1)} MB)")


def _without_gitkeep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    return None if info.name.endswith(".gitkeep") else info


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        return _worker(Path(sys.argv[2]), Path(sys.argv[3]))

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "scenarios",
        nargs="*",
        type=Path,
        help="nur diese Szenarien (Standard: alle unter --examples-dir außer den ausgenommenen)",
    )
    parser.add_argument(
        "--examples-dir",
        type=Path,
        default=REPO_ROOT / "examples",
        help="Beispielordner mit den Rastern unter data/ (Standard: examples/)",
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=REPO_ROOT / ".geofact" / "snapshots",
        help="lokale Snapshot-Ablage, aus der kopiert wird (Standard: .geofact/snapshots)",
    )
    parser.add_argument(
        "--out", type=Path, default=DEMO_DIR, help="Zielordner (Standard: demo-data/)"
    )
    parser.add_argument(
        "--backend",
        choices=("overpass", "postgis"),
        default="overpass",
        help="OSM-Backend der Demo; bestimmt die Snapshot-Schlüssel (Standard: overpass)",
    )
    parser.add_argument(
        "--verify",
        metavar="IMAGE",
        help="nichts erzeugen, sondern die Szenarien des Pakets im Image ohne Netz ausführen",
    )
    args = parser.parse_args()
    return verify(args) if args.verify else build(args)


if __name__ == "__main__":
    sys.exit(main())
