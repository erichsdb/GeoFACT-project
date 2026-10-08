"""Gemeinsame Test-Doubles für die W2-02-Contract-Tests (Executor und Run).

Kein Testmodul (Unterstrich): pytest sammelt es nicht. ``fake_loader`` ersetzt
``engine.executor.load_layer`` mit der Signatur aus NACHT_SPEC 2.9 (inklusive
``provenance_sink`` und ``region_fetcher``), damit Executor-Verträge ohne
Konnektor, Transformation und Netz prüfbar sind."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import Point

from geofact.core.types import LayerProvenance

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"  # Dresden, UTM-Zone 33N (EPSG:32633)


def points(crs: Any, n: int = 3) -> gpd.GeoDataFrame:
    """n Punkte; Koordinaten passend zum CRS (Grad bzw. Meter)."""
    if crs is not None and CRS.from_user_input(crs).is_geographic:
        coords = [(13.7 + 0.01 * i, 51.05) for i in range(n)]
    else:
        coords = [(410_000 + 100 * i, 5_656_000) for i in range(n)]
    return gpd.GeoDataFrame(
        {"name": [f"p{i}" for i in range(n)]},
        geometry=[Point(x, y) for x, y in coords],
        crs=crs,
    )


def provenance_of(layer_id: str) -> LayerProvenance:
    return LayerProvenance(
        layer=layer_id,
        source="file",
        source_crs="EPSG:4326",
        source_crs_label="EPSG:4326 (WGS 84)",
        crs_override=False,
        target_crs="EPSG:32633",
        target_crs_label="EPSG:32633 (WGS 84 / UTM zone 33N)",
        raw_bounds=(13.7, 51.0, 13.8, 51.1),
        bounds=(410_000.0, 5_656_000.0, 411_000.0, 5_657_000.0),
        feature_count=3,
        raw_sample=[(13.7, 51.05)],
        sample=[(410_000.0, 5_656_000.0)],
    )


def fake_loader(
    data_by_id: dict[str, Any], *, record: list | None = None, skip_provenance=()
):
    """Ersatz für ``load_layer``: liefert ``data_by_id[layer.id]`` unverändert,
    trägt die Provenienz in die Senke ein (außer für ``skip_provenance``) und
    merkt sich (layer, ctx) in ``record``."""

    def load_layer(
        layer,
        ctx,
        registry,
        observer=None,
        *,
        step=None,
        override=None,
        store=None,
        provenance_sink=None,
        region_fetcher=None,
    ):
        if record is not None:
            record.append((layer.id, ctx))
        if provenance_sink is not None and layer.id not in skip_provenance:
            provenance_sink[layer.id] = provenance_of(layer.id)
        return data_by_id[layer.id]

    return load_layer


@dataclass
class FakeRegionInfo:
    """Stub für ``engine.region.RegionInfo`` (W2-01) mit derselben Form."""

    crs: CRS
    crs_mode: str = "declared"
    crs_spec: str = "EPSG:25833"
    bbox: tuple[float, float, float, float] = (13.60, 51.01, 13.94, 51.15)
    area_km2: float = 370.0
    display_name: str | None = None
    osm_class: str | None = None
    osm_type: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "crs": self.crs.to_string(),
            "crs_mode": self.crs_mode,
            "crs_spec": self.crs_spec,
            "bbox": list(self.bbox),
            "area_km2": self.area_km2,
            "display_name": self.display_name,
            "osm_class": self.osm_class,
            "osm_type": self.osm_type,
            "warnings": list(self.warnings),
        }


def file_layer(layer_id: str, path: str | None = None, **extra) -> dict:
    return {
        "id": layer_id,
        "source": "file",
        "data_type": "vector",
        "path": path or str(FIXTURES_DIR / "substations.geojson"),
        **extra,
    }
