"""Implements: FA4 (Dateiformate des Datei-Konnektors: Vektor), FA40.

Die mitgelieferten Vektorformate geojson, shp, gml und gpkg, markiert wie ein
Plugin-Format (z. B. FlatGeobuf, siehe examples/plugin_demo/plugins/flatgeobuf.py).

Mehrschichtige Formate (GeoPackage, GML): das Feld ``layer`` (``LayerOptions``)
wählt die Schicht; eine einschichtige Quelle wird ohne Angabe gelesen, bei
mehreren Schichten ohne Angabe listet der Fehler die vorhandenen auf. GeoJSON und
Shapefile kennen kein ``layer``; die Angabe ist dort ein Konfigurationsfehler.

Alle vier lesen über ``builtin/_vector_io.py::read_vector``: bei
``region_filter: bbox|intersects`` reicht pyogrio die Region als ``bbox`` an den
Treiber (Pushdown); ``path`` kann auch ein GDAL-Pfad (``/vsizip/``, ``/vsicurl/``) sein."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

import pyogrio
from pydantic import BaseModel, ConfigDict, Field

from geofact.builtin._vector_io import read_vector
from geofact.plugin_api import DataType, LayerData, LoadContext, register_file_format

if TYPE_CHECKING:
    from geofact.builtin.sources.file import FileLayer


class LayerOptions(BaseModel):
    """Formatspezifische Felder mehrschichtiger Formate (GeoPackage, GML)."""

    model_config = ConfigDict(extra="forbid")

    layer: Optional[str] = Field(
        None,
        description="Name der zu lesenden Schicht; nötig, sobald die Quelle mehr als eine "
        "Schicht enthält.",
    )


def _read_vector(
    path: Union[Path, str], layer: FileLayer, ctx: LoadContext
) -> LayerData:
    return read_vector(path, layer, ctx)


register_file_format(
    "geojson",
    extensions=("geojson", "json"),
    zip_extensions=("geojson", "json"),
    data_type=DataType.VECTOR,
    description="GeoJSON",
)(_read_vector)
register_file_format(
    "shp",
    extensions=("shp",),
    zip_extensions=("shp",),
    data_type=DataType.VECTOR,
    description="ESRI Shapefile",
)(_read_vector)


@register_file_format(
    "gml",
    extensions=("gml", "xml"),
    zip_extensions=("gml",),
    options_model=LayerOptions,
    data_type=DataType.VECTOR,
    description="Geography Markup Language (Schicht wählbar)",
)
def _read_gml(path: Union[Path, str], layer: FileLayer, ctx: LoadContext) -> LayerData:
    if layer.options.layer is not None:
        # Mehrschichtfähiges Format: layer wird nur durchgereicht, wenn explizit
        # angegeben - sonst greift der Treiber-Default.
        return read_vector(path, layer, ctx, layer=layer.options.layer)
    return read_vector(path, layer, ctx)


@register_file_format(
    "gpkg",
    extensions=("gpkg",),
    zip_extensions=("gpkg",),
    options_model=LayerOptions,
    data_type=DataType.VECTOR,
    description="GeoPackage (mehrschichtig)",
)
def _read_geopackage(
    path: Union[Path, str], layer: FileLayer, ctx: LoadContext
) -> LayerData:
    gpkg_layer = _resolve_gpkg_layer(layer, path)
    return read_vector(path, layer, ctx, layer=gpkg_layer)


def _resolve_gpkg_layer(layer: FileLayer, path: Union[Path, str]) -> str | None:
    """Ermittelt die zu lesende Schicht eines mehrschichtigen GeoPackage.
    None bedeutet: read_file() ohne layer-Argument aufrufen (Default-
    Schicht - nur zulässig, wenn es genau eine Schicht gibt)."""
    if layer.options.layer is not None:
        return layer.options.layer

    layer_info = pyogrio.list_layers(path)
    names = [row[0] for row in layer_info]
    if len(names) == 1:
        return names[0]
    raise ValueError(
        f"Layer '{layer.id}': GeoPackage '{layer.path}' enthält mehrere "
        f"Schichten: {names}. 'layer' in der Konfiguration angeben, um "
        "eindeutig auszuwählen."
    )
