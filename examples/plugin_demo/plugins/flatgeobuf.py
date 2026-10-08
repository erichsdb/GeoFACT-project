"""Implements: FA40 (Beispiel-Plugin: Dateiformat).

FlatGeobuf (.fgb) als zusätzliches Dateiformat des Datei-Konnektors.
Diese Datei ist die gesamte Erweiterung: in den Plugin-Ordner legen
(GEOFACT_PLUGIN_PATH), danach ist in jeder Szenario-YAML
``source: file`` mit einer .fgb-Datei nutzbar. Kein Kernmodul wird
geändert.

FlatGeobuf trägt sein CRS in der Datei, deshalb genügt die
Standard-Harmonisierung ('reproject'). Ein Format ohne CRS-Metadaten
würde hier zusätzlich eine eigene Transformation registrieren und per
transform=... angeben (siehe tests/unit/test_fa40_extensions.py, .xyz).
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd

from geofact.plugin_api import DataType, register_file_format


@register_file_format(
    "fgb",
    extensions=("fgb",),
    zip_extensions=("fgb",),
    data_type=DataType.VECTOR,
    description="FlatGeobuf (Beispiel-Plugin)",
)
def read_flatgeobuf(path: Path, layer, ctx) -> gpd.GeoDataFrame:
    return gpd.read_file(path, engine="pyogrio")
