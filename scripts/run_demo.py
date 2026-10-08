"""Demo script: runs a scenario for real and writes its declared outputs.

Not part of the test suite - a hands-on way to see GeoFACT work. A thin wrapper
around the application use case (`geofact.api.run`, FA44): the scenario config
declares its own data sources and outputs, this script only picks the config and
the output directory. The same as `geofact run CONFIG --out output`.

Usage:
    uv run python scripts/run_demo.py
    uv run python scripts/run_demo.py examples/szenario3_gruenflaechen_bevoelkerung.yaml
    uv run python scripts/run_demo.py examples/sachsen_nachthimmel.yaml --backend postgis
    uv run python scripts/run_demo.py examples/szenario3_gruenflaechen_bevoelkerung.yaml --dump-intermediate output/szenario3_debug
"""

import argparse
import os
import sys
from pathlib import Path

from geofact import api

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIO = REPO_ROOT / "examples" / "szenario2_gruenflaechen.yaml"
OUTPUT_DIR = REPO_ROOT / "output"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario", nargs="?", type=Path, default=DEFAULT_SCENARIO)
    parser.add_argument(
        "--backend",
        choices=("overpass", "postgis"),
        help="OSM-Backend (setzt GEOFACT_OSM_BACKEND; postgis braucht GEOFACT_PG_DSN)",
    )
    parser.add_argument(
        "--dump-intermediate", metavar="DIR", help="Zwischenergebnisse in DIR ablegen"
    )
    args = parser.parse_args()

    if args.backend:
        os.environ["GEOFACT_OSM_BACKEND"] = args.backend

    try:
        document = api.load_scenario(args.scenario)
        report = api.run(
            document,
            out_dir=OUTPUT_DIR,
            observer=lambda event: (
                print(f"  Warnung: {event.message}", file=sys.stderr)
                if event.type in (api.events.LAYER_WARNING, api.events.STEP_WARNING)
                else None
            ),
            dump_intermediate=args.dump_intermediate,
        )
    except api.ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(
        f"Szenario '{document.scenario.scenario.name}': Schritte {report.result.order}"
    )
    for path in report.outputs.written if report.outputs else []:
        print(f"  Ausgabe: {path}")
    for item in report.skipped_outputs:
        print(
            f"  Ausgabe übersprungen (output[{item.index}], {item.type}): {item.reason}"
        )
    return 2 if report.skipped_outputs else 0


if __name__ == "__main__":
    sys.exit(main())
