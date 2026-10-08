"""Gemeinsame Hilfen der W2-04-Tests (Ladestrecke, Ausgaben): ein Layer-Modell
aus einem Dict bauen und ``load_layer`` mit einem Lauf-Kontext aufrufen."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from geofact.core.contracts import LoadContext
from geofact.core.registry import default_registry
from geofact.engine import region as region_module

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
DRESDEN_BBOX = "13.60,51.01,13.94,51.15"
FAR_AWAY_BBOX = "10.00,48.00,10.20,48.10"


def layer_from(config: dict[str, Any]):
    """Das validierte Layer-Modell eines einzelnen Layers (Modell der Quellart)."""
    registry = default_registry()
    return registry.source(config["source"]).layer_model.model_validate(
        config, context={"registry": registry}
    )


def context(region: str = DRESDEN_BBOX) -> LoadContext:
    resolved = region_module.resolve_region(region)
    return LoadContext(
        region=resolved,
        target_crs=region_module.utm_zone_epsg(resolved),
        registry=default_registry(),
    )


RECORDING_PLUGIN = textwrap.dedent("""
    import json

    from geofact.plugin_api import register_output
    from geofact.core.types import DataType


    @register_output("aufzeichnung", extension="json", accepts=(DataType.VECTOR,))
    def write_recording(data, target, spec):
        provenance = spec.provenance
        target.write_text(json.dumps({
            "provenance": None if provenance is None
            else {key: value.to_dict() for key, value in provenance.items()},
            "attribution": None if spec.attribution is None else spec.attribution.to_dict(),
        }), encoding="utf-8")
""")
"""Plugin-Ausgabeformat, das die vor dem Schreiben gebundene Provenienz und
Attribution als JSON aufzeichnet (FA58/FA65)."""


def fixture_fetcher(name: str):
    """Region-Fetcher aus den eingecheckten Nominatim-Antworten (offline)."""
    path = FIXTURES_DIR / f"nominatim_{name.lower()}.json"

    def fetch(query: str) -> list[dict]:
        if not path.is_file() or query.lower() != name.lower():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    return fetch
