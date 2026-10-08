"""Implements: FA76, FA14 (example data of examples/europa/europa_netz_resilienz.yaml).

Downloads the prebuilt PyPSA-Eur transmission network v0.7 (Xiong et al. 2025,
built from OpenStreetMap; Zenodo record 18619025, published 2026-02-12, licence
ODbL 1.0) into examples/data/europa/ (git-ignored): buses.csv, lines.csv,
transformers.csv, links.csv and converters.csv, together about 21 MB. Every file
is checked against the MD5 checksum the record publishes; a mismatch is an
error, an existing file with the right checksum is kept.

The population raster of the example is the global GHS-POP 100 m file
(examples/data/GHS_POP_E2025_GLOBE_R2023A_54009_100_V1_0.tif, 6.5 GB), the same
file as in the Berlin example; see examples/europa/README.md for its download.

Usage:
    uv run python scripts/fetch_europa_data.py
    uv run python scripts/fetch_europa_data.py --out other/dir
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "examples" / "data" / "europa"
RECORD = "18619025"
FILE_URL = "https://zenodo.org/api/records/{record}/files/{name}/content"

# name -> MD5 as published in the Zenodo record (version 0.7)
FILES: dict[str, str] = {
    "buses.csv": "031c30f04dc24e99210b3f5ad8a01c94",
    "lines.csv": "04d1e7cb33835de9c7d8be4716c6a8bf",
    "transformers.csv": "b6730b52097d2691d49231bdd53c7bd4",
    "links.csv": "2ddd515a30cfc19205e6681def579fbd",
    "converters.csv": "fe13a4f336fdc273c808cdfc485b4753",
}


def md5_of(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(name: str, expected_md5: str, out_dir: Path) -> Path:
    """Download one file (kept if already present with the right checksum)."""
    target = out_dir / name
    if target.is_file() and md5_of(target) == expected_md5:
        print(f"  {name}: vorhanden, Prüfsumme stimmt")
        return target
    url = FILE_URL.format(record=RECORD, name=name)
    partial = target.with_name(name + ".part")
    with (
        urllib.request.urlopen(url, timeout=120) as response,
        partial.open("wb") as handle,
    ):
        while chunk := response.read(1 << 20):
            handle.write(chunk)
    actual = md5_of(partial)
    if actual != expected_md5:
        partial.unlink()
        raise RuntimeError(
            f"{name}: Prüfsumme {actual} statt {expected_md5} (Zenodo {RECORD}) - "
            "Download beschädigt oder Datensatz geändert"
        )
    partial.replace(target)
    print(f"  {name}: {target.stat().st_size / 1e6:.1f} MB geladen")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Zielverzeichnis")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"PyPSA-Eur-Netz v0.7 (Zenodo {RECORD}, ODbL 1.0) nach {args.out}")
    for name, md5 in FILES.items():
        fetch(name, md5, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
