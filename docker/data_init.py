"""Implements: FA98 (Beispieldaten des Serverpakets).

Fills the example-data volume of docker-compose.server.yml. The backend mounts it at
/app/examples/data (the directory the example YAMLs reference with ``data/...``); this
service mounts it at /target and reads the image's own /app/examples/data:

1. the rasters baked into the image (cut to Saxony by
   scripts/build_demo_data.py) are copied on every run, so a new image brings new rasters;
2. data the image does not hold is downloaded once and checked: the global GHS-POP 2025
   raster (ZIP from the JRC, CRC checked on extraction; it replaces the cut copy, so the
   Berlin and Europe examples work) and the PyPSA-Eur network of the Europe example
   (MD5 per file, as in scripts/fetch_europa_data.py);
3. DATENSTAND.json records source and time per file.

Set ``GEOFACT_GHS_POP_URL=none`` or ``GEOFACT_EUROPA_DATA=none`` to skip a download on
purpose (logged; the cut GHS-POP copy of the image stays, the Berlin and Europe examples
then lack their data).

Commands: ``ensure`` (default: download only what is missing), ``update`` (download
everything again), ``status`` (print DATENSTAND.json). Standard library only - the demo
image carries no extra tools. A failed download or a wrong checksum is an error, never a
silently missing file (golden rule 7).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

IMAGE_DIR = Path(os.environ.get("GEOFACT_DATA_IMAGE_DIR", "/app/examples/data"))
TARGET_DIR = Path(os.environ.get("GEOFACT_DATA_TARGET_DIR", "/target"))
STATE_FILE = "DATENSTAND.json"
CHUNK = 1 << 20

GHS_POP_NAME = "GHS_POP_E2025_GLOBE_R2023A_54009_100_V1_0.tif"
GHS_POP_URL = os.environ.get(
    "GEOFACT_GHS_POP_URL",
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/GHS_POP_GLOBE_R2023A/"
    "GHS_POP_E2025_GLOBE_R2023A_54009_100/V1-0/GHS_POP_E2025_GLOBE_R2023A_54009_100_V1_0.zip",
)

EUROPA_DATA = os.environ.get("GEOFACT_EUROPA_DATA", "zenodo")
EUROPA_URL = "https://zenodo.org/api/records/18619025/files/{name}/content"
# name -> MD5 as published in the Zenodo record (PyPSA-Eur network v0.7)
EUROPA_FILES: dict[str, str] = {
    "buses.csv": "031c30f04dc24e99210b3f5ad8a01c94",
    "lines.csv": "04d1e7cb33835de9c7d8be4716c6a8bf",
    "transformers.csv": "b6730b52097d2691d49231bdd53c7bd4",
    "links.csv": "2ddd515a30cfc19205e6681def579fbd",
    "converters.csv": "fe13a4f336fdc273c808cdfc485b4753",
}

Fetch = Callable[[str, Path], None]


class DataInitError(RuntimeError):
    """A download or check failed; the message names file and source."""


@dataclass(frozen=True)
class Remote:
    """One file that is downloaded instead of shipped in the image."""

    target: str  # path below TARGET_DIR
    url: str
    md5: str | None = None  # None: archive, checked by its CRC on extraction
    member: str | None = None  # file inside the ZIP at url


def remotes(ghs_pop_url: str | None = None, europa: str | None = None) -> list[Remote]:
    ghs_pop_url = GHS_POP_URL if ghs_pop_url is None else ghs_pop_url
    europa = EUROPA_DATA if europa is None else europa
    items = []
    if ghs_pop_url == "none":
        log(
            "GEOFACT_GHS_POP_URL=none: globales GHS-POP-Raster wird nicht geladen "
            "(Berlin- und Europa-Beispiel ohne Bevölkerungsdaten)"
        )
    else:
        items.append(Remote(GHS_POP_NAME, ghs_pop_url, member=GHS_POP_NAME))
    if europa == "none":
        log(
            "GEOFACT_EUROPA_DATA=none: Netzdaten des Europa-Beispiels werden nicht geladen"
        )
    else:
        items += [
            Remote(f"europa/{name}", EUROPA_URL.format(name=name), md5=md5)
            for name, md5 in EUROPA_FILES.items()
        ]
    return items


def log(message: str) -> None:
    print(f"[data-init] {message}", file=sys.stderr, flush=True)


def md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def http_fetch(url: str, dest: Path) -> None:
    """Download url to dest; continues a partial dest with an HTTP Range request."""
    start = dest.stat().st_size if dest.exists() else 0
    request = urllib.request.Request(
        url, headers={"Range": f"bytes={start}-"} if start else {}
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if start and response.status != 206:
                start = 0  # server ignores Range: start over
            mode = "ab" if start else "wb"
            done, last_report = start, start
            with dest.open(mode) as handle:
                for chunk in iter(lambda: response.read(CHUNK), b""):
                    handle.write(chunk)
                    done += len(chunk)
                    if done - last_report >= 500 * CHUNK:
                        log(f"{dest.name}: {done // CHUNK} MB")
                        last_report = done
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and start:
            return  # already complete
        raise DataInitError(
            f"Download fehlgeschlagen: {url} (HTTP {exc.code})"
        ) from exc
    except OSError as exc:
        raise DataInitError(f"Download fehlgeschlagen: {url} ({exc})") from exc


def is_present(item: Remote, target_dir: Path, state: dict) -> bool:
    """Downloaded from item.url and still correct - a copy from the image does not count."""
    path = target_dir / item.target
    if (
        not path.is_file()
        or state["files"].get(item.target, {}).get("quelle") != item.url
    ):
        return False
    return item.md5 is None or md5_of(path) == item.md5


def install(item: Remote, target_dir: Path, fetch: Fetch) -> None:
    """Download one file, check it and move it into place only when it is correct."""
    path = target_dir / item.target
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + (".zip.part" if item.member else ".part"))
    log(f"lade {item.url}")
    fetch(item.url, part)
    if item.member:
        tmp = path.with_name(path.name + ".tmp")
        try:
            with zipfile.ZipFile(part) as archive, archive.open(item.member) as src:
                with tmp.open("wb") as dst:
                    shutil.copyfileobj(
                        src, dst, CHUNK
                    )  # raises BadZipFile on a CRC error
        except (zipfile.BadZipFile, KeyError) as exc:
            part.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
            raise DataInitError(
                f"Archiv {item.url} beschädigt oder ohne {item.member}: {exc}; erneut starten"
            ) from exc
        tmp.replace(path)
        part.unlink()
    else:
        actual = md5_of(part)
        if item.md5 and actual != item.md5:
            part.unlink()
            raise DataInitError(
                f"MD5 von {item.target} passt nicht (erwartet {item.md5}, erhalten {actual})"
            )
        part.replace(path)


def copy_image_files(
    image_dir: Path, target_dir: Path, downloaded: set[str]
) -> list[str]:
    """Copy the files of the image; a file downloaded from the network is never overwritten."""
    copied = []
    if not image_dir.is_dir():
        raise DataInitError(f"Verzeichnis der Image-Daten fehlt: {image_dir}")
    for src in sorted(image_dir.rglob("*")):
        rel = src.relative_to(image_dir).as_posix()
        if not src.is_file() or rel == ".gitkeep":
            continue
        dest = target_dir / rel
        if rel in downloaded:
            continue  # the downloaded original beats the copy in the image
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(rel)
    return copied


def read_state(target_dir: Path) -> dict:
    path = target_dir / STATE_FILE
    if not path.is_file():
        return {"files": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(target_dir: Path, state: dict) -> None:
    path = target_dir / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def run(
    command: str,
    image_dir: Path = IMAGE_DIR,
    target_dir: Path = TARGET_DIR,
    fetch: Fetch = http_fetch,
    items: list[Remote] | None = None,
) -> dict:
    target_dir.mkdir(parents=True, exist_ok=True)
    state = read_state(target_dir)
    if command == "status":
        return state
    now = datetime.now(UTC).isoformat(timespec="seconds")
    items = remotes() if items is None else items
    # Download first: a cut copy from the image must not hide a missing full raster.
    for item in items:
        if command == "update" or not is_present(item, target_dir, state):
            install(item, target_dir, fetch)
            state["files"][item.target] = {"quelle": item.url, "geladen": now}
        else:
            log(f"{item.target} liegt vor")
    image_version = os.environ.get("GEOFACT_VERSION", "unbekannt")
    downloaded = {
        target
        for target, entry in state["files"].items()
        if not entry["quelle"].startswith("Image ")
    }
    for rel in copy_image_files(image_dir, target_dir, downloaded):
        state["files"][rel] = {"quelle": f"Image {image_version}", "geladen": now}
    state["aktualisiert"] = now
    write_state(target_dir, state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "command", nargs="?", default="ensure", choices=("ensure", "update", "status")
    )
    args = parser.parse_args(argv)
    try:
        state = run(args.command)
    except DataInitError as exc:
        log(f"FEHLER: {exc}")
        return 1
    if args.command == "status" and not state["files"]:
        log("noch keine Beispieldaten geladen")
        return 1
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
