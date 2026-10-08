"""Implements: FA12 (GeoJSON-/CSV-Export), FA53 (Graphen als Ausgabe), FA55 (Ziel-CRS je Ausgabe),
Ausgabeformate für FA40.

Exportiert einen Ergebnis-Layer als GeoJSON oder als CSV mit der Geometrie als
WKT-Spalte. Eine Graph-Quelle (FA53) wird als Kanten-Layer (Standard) oder
Knoten-Layer (``graph_part: nodes``) geschrieben, siehe builtin/_graph_frames.py.

Ziel-CRS (FA55): die Option ``crs`` nennt das CRS der Datei (z. B. ``EPSG:25833``).
Ohne Angabe schreibt ``geojson`` in EPSG:4326 und ``csv`` im Arbeits-CRS. Ein
unbekanntes CRS wird bei ``validate`` abgelehnt. GeoJSON in einem anderen CRS
als EPSG:4326 trägt das CRS als ``crs``-Member (GDAL), was von RFC 7946
abweicht und deshalb nur auf ausdrückliche Angabe geschieht. Ein Layer ohne
CRS mit gesetztem ``crs`` ist ein Fehler (Ausgabe wird mit Grund übersprungen)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from geopandas import GeoDataFrame
from pydantic import BaseModel, ConfigDict, Field, field_validator
from geofact.builtin._graph_frames import frame_for_output
from geofact.builtin.outputs._target_crs import check_output_crs
from geofact.plugin_api import DataType, register_output


def _to_target_crs(
    gdf: GeoDataFrame, crs: Optional[str], *, explicit: bool
) -> GeoDataFrame:
    if crs is None:
        return gdf
    if gdf.crs is None:
        if explicit:
            raise ValueError(
                f"Layer ohne CRS kann nicht nach '{crs}' geschrieben werden - "
                "Quell-CRS am Layer deklarieren (crs) oder die Option crs weglassen"
            )
        return gdf
    return gdf.to_crs(crs)


def write_geojson(
    gdf: GeoDataFrame,
    path: str | Path,
    crs: Optional[str] = "EPSG:4326",
    *,
    explicit: bool = False,
) -> None:
    """GeoJSON in ``crs`` (Default EPSG:4326). Ein Layer ohne CRS wird nur mit
    ``explicit=True`` (Option ``crs`` gesetzt) abgelehnt, sonst unverändert geschrieben."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _to_target_crs(gdf, crs, explicit=explicit).to_file(path, driver="GeoJSON")


def write_csv(gdf: GeoDataFrame, path: str | Path, crs: Optional[str] = None) -> None:
    """CSV mit der Geometrie als WKT-Spalte; ``crs`` None = CRS des Layers
    (Arbeits-CRS), sonst wird vorher dorthin reprojiziert."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    output = _to_target_crs(gdf, crs, explicit=True).copy()
    output["wkt"] = output.geometry.apply(lambda g: g.wkt if g is not None else None)
    output = output.drop(columns="geometry")
    output.to_csv(path, index=False)


# --- Registrierung als Ausgabeformate (FA40) ---


class ExportOptions(BaseModel):
    """Formatspezifische Felder von geojson/csv: welcher Teil eines Graphen
    geschrieben wird (FA53; an einer Vektor-Quelle ist ``graph_part`` ein
    Fehler) und das Ziel-CRS der Datei (FA55)."""

    model_config = ConfigDict(extra="forbid")

    graph_part: Literal["edges", "nodes"] = "edges"
    crs: Optional[str] = Field(
        None,
        description="Ziel-CRS der Datei, z. B. EPSG:25833 (geojson: Default EPSG:4326, "
        "csv: Default Arbeits-CRS)",
    )

    @field_validator("crs")
    @classmethod
    def _check_crs(cls, value: Optional[str]) -> Optional[str]:
        return check_output_crs(value)


GraphExportOptions = ExportOptions
"""Alias für ``ExportOptions`` (FA53)."""


_VECTOR_OR_GRAPH = (DataType.VECTOR, DataType.GRAPH)


@register_output(
    "geojson",
    extension="geojson",
    accepts=_VECTOR_OR_GRAPH,
    options_model=ExportOptions,
    description="GeoJSON in WGS84 oder im Ziel-CRS crs (Vektor-Layer; Graph als Kanten oder "
    "Knoten, graph_part)",
)
def _write_geojson_output(data, target: Path, spec) -> None:
    crs = getattr(spec.options, "crs", None)
    if crs is None:
        write_geojson(frame_for_output(data, spec), target)
    else:
        write_geojson(frame_for_output(data, spec), target, crs=crs, explicit=True)


@register_output(
    "csv",
    extension="csv",
    accepts=_VECTOR_OR_GRAPH,
    options_model=ExportOptions,
    description="CSV mit Geometrie als WKT (Arbeits-CRS oder Ziel-CRS crs; Graph als Kanten "
    "oder Knoten, graph_part)",
)
def _write_csv_output(data, target: Path, spec) -> None:
    write_csv(
        frame_for_output(data, spec), target, crs=getattr(spec.options, "crs", None)
    )
