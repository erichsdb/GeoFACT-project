"""Write the OpenAPI specification of the web API to docs/openapi.json.

Implements: FA85

The file is generated from the routes; never edit it by hand. Run this after
every change to a route or a request/response model - the unit test
test_fa81_committed_spec_matches_the_code fails until the file is current.

Usage:
    uv run --extra web python scripts/export_openapi.py          # write the file
    uv run --extra web python scripts/export_openapi.py --check  # exit 1 if outdated
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from geofact_web import openapi  # noqa: E402
from geofact_web.main import create_app  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="compare only, do not write"
    )
    args = parser.parse_args()

    text = openapi.spec_text(create_app())
    path = openapi.SPEC_PATH
    if args.check:
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != text:
            print(
                f"{path} is outdated - run scripts/export_openapi.py", file=sys.stderr
            )
            return 1
        return 0
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
