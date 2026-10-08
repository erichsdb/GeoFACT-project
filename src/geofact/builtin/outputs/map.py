"""Implements: FA12 (Karte), FA49 (Raster auf der Karte), FA53 (Graphen als Ausgabe),
FA65 (Namensnennung auf der Karte), FA73 (Ausgabelizenz in der Fußzeile), Ausgabeformat 'map' für FA40.

Erzeugt eine interaktive Folium-Karte: jedes Feature hat ein Popup mit seinen
Attributen, mit ``color_field`` werden die Features nach einer Farbskala
eingefärbt. Mehrere Layer sind einzeln an-/abschaltbar (LayerControl). Eine
Graph-Quelle (FA53) wird als Kanten- oder Knoten-Layer (``graph_part: nodes``)
gezeichnet; für Netze mit zehntausenden Kanten ist GeoJSON handlicher.

``normalize_by_area`` (Default) färbt Polygone nach Wert pro Fläche, sonst
wäre die Färbung bei unterschiedlich großen Zonen ein Flächen-Artefakt;
``false``, wenn das Farb-Feld schon ein Anteil oder eine Dichte ist (FA46).

Raster (FA49): ein Band (bei Mehrband-Raster Pflicht) wird als Bild-Overlay mit
``colormap`` eingefärbt (``vmin``/``vmax`` oder 2.-98. Perzentil), Nodata ist
transparent. Das Bild wird nach EPSG:3857 gebracht und auf höchstens
``MAX_OVERLAY_PIXELS`` Zellen verkleinert; volle Auflösung liefert ``geotiff``.

Lizenzen (FA65/FA73): ``spec.attribution`` ergibt eine Fußzeile und ergänzt die
Leaflet-Attribution der Kachelebene, ``spec.output_license`` eine eigene Zeile.
Eine Option ``crs`` gibt es bewusst nicht (FA55): die Karte zeigt Web-Mercator/WGS84.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Optional, Union

import branca.colormap
import folium
import numpy as np
import pandas as pd
from folium.raster_layers import ImageOverlay
from geopandas import GeoDataFrame
from pydantic import BaseModel, ConfigDict, Field
from rasterio.transform import array_bounds
from rasterio.warp import (
    Resampling,
    calculate_default_transform,
    reproject,
    transform_bounds,
)

from geofact.builtin._graph_frames import frame_for_output
from geofact.plugin_api import DataType, RasterLayer, register_output

_COLOR_SCALE = ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"]
_METRIC_CRS_FOR_AREA = "EPSG:3857"
_LAYER_PALETTE = [
    "#3388ff",
    "#e6194b",
    "#3cb44b",
    "#f58231",
    "#911eb4",
    "#46f0f0",
    "#f032e6",
]
# Farbe für Objekte mit NaN im color_field (z. B. shortest_path außerhalb von
# max_snap_m, FA15). Neutrales Grau, damit "keine Daten" nicht wie der kleinste
# Wert (_COLOR_SCALE[0], blau) aussieht.
_NAN_COLOR = "#999999"


def _color_for_value(value: float, vmin: float, vmax: float) -> str:
    if vmax == vmin:
        return _COLOR_SCALE[0]
    fraction = (value - vmin) / (vmax - vmin)
    index = min(int(fraction * len(_COLOR_SCALE)), len(_COLOR_SCALE) - 1)
    return _COLOR_SCALE[index]


def _popup_html(row, drop_columns: set[str]) -> str:
    fields = [
        f"<b>{col}:</b> {val}" for col, val in row.items() if col not in drop_columns
    ]
    return "<br>".join(fields) if fields else "(keine Attribute)"


def _legend_html(entries: list[tuple[str, str, float, float, bool]]) -> str:
    """entries: (Layer-Name, Farbfeld-Label, vmin, vmax, has_missing) je eingefärbtem
    Layer; has_missing hängt den Grau-Eintrag "keine Daten" an."""
    n = len(_COLOR_SCALE)
    blocks = []
    for layer_name, color_field, vmin, vmax, has_missing in entries:
        steps = []
        for i, color in enumerate(_COLOR_SCALE):
            lo = vmin + (vmax - vmin) * i / n
            hi = vmin + (vmax - vmin) * (i + 1) / n
            steps.append(
                f'<div style="display:flex;align-items:center;margin:2px 0;">'
                f'<span style="background:{color};width:16px;height:16px;'
                f'display:inline-block;margin-right:6px;border:1px solid #333;"></span>'
                f"<span>{lo:.2f} - {hi:.2f}</span></div>"
            )
        if has_missing:
            steps.append(
                f'<div style="display:flex;align-items:center;margin:2px 0;">'
                f'<span style="background:{_NAN_COLOR};width:16px;height:16px;'
                f'display:inline-block;margin-right:6px;border:1px solid #333;"></span>'
                f"<span>keine Daten</span></div>"
            )
        blocks.append(
            f'<div style="margin-bottom:8px;">'
            f'<div style="font-weight:bold;margin-bottom:4px;">{layer_name}: {color_field}</div>'
            + "".join(steps)
            + "</div>"
        )
    return (
        '<div style="position:fixed;bottom:20px;left:20px;z-index:9999;'
        "background:white;padding:10px;border:1px solid #333;border-radius:4px;"
        "font-family:sans-serif;font-size:12px;box-shadow:0 1px 4px rgba(0,0,0,0.3);"
        'max-height:80vh;overflow-y:auto;">' + "".join(blocks) + "</div>"
    )


def _compute_color_values(
    gdf: GeoDataFrame,
    gdf_wgs84: GeoDataFrame,
    color_field: str | None,
    normalize_by_area: bool,
) -> tuple[pd.Series | None, str | None]:
    if color_field is None or color_field not in gdf_wgs84.columns:
        return None, None

    is_polygonal = gdf_wgs84.geometry.geom_type.isin(["Polygon", "MultiPolygon"]).all()
    if normalize_by_area and is_polygonal and gdf.crs is not None:
        area_km2 = gdf.to_crs(_METRIC_CRS_FOR_AREA).geometry.area / 1_000_000
        with_area = area_km2.replace(0, pd.NA)
        return gdf_wgs84[color_field] / with_area, f"{color_field} pro km²"
    return gdf_wgs84[color_field], color_field


def _draw_features(
    gdf_wgs84: GeoDataFrame,
    target,
    color_values: pd.Series | None,
    vmin: float | None,
    vmax: float | None,
    fixed_color: str | None,
) -> None:
    for idx, row in gdf_wgs84.iterrows():
        popup = folium.Popup(_popup_html(row, drop_columns={"geometry"}), max_width=300)
        color = fixed_color or "#3388ff"
        if vmin is not None:
            cell = color_values.iloc[idx]
            if cell == cell:  # nicht NaN
                color = _color_for_value(float(cell), vmin, vmax)
            else:
                color = _NAN_COLOR

        geom = row.geometry
        if geom is None:
            continue
        if geom.geom_type == "Point":
            folium.CircleMarker(
                location=[geom.y, geom.x],
                radius=6,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.8,
                popup=popup,
            ).add_to(target)
        else:
            folium.GeoJson(
                geom.__geo_interface__,
                style_function=lambda _f, c=color: {
                    "color": c,
                    "fillColor": c,
                    "fillOpacity": 0.4,
                },
                popup=popup,
            ).add_to(target)
            # Zusätzliche Punktmarkierung: kleine Polygone sind im Ausgangszoom
            # einer landesweiten Karte kaum zu finden. representative_point()
            # statt centroid, weil er garantiert innerhalb der Fläche liegt.
            try:
                anchor = geom.representative_point()
            except Exception:  # pragma: no cover - nur bei kaputter Geometrie
                anchor = None
            if anchor is not None:
                folium.CircleMarker(
                    location=[anchor.y, anchor.x],
                    radius=4,
                    color=color,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.9,
                    weight=1,
                    popup=folium.Popup(
                        _popup_html(row, drop_columns={"geometry"}), max_width=300
                    ),
                ).add_to(target)


def render_map(
    gdf: GeoDataFrame, color_field: str | None = None, normalize_by_area: bool = True
) -> folium.Map:
    if gdf.empty:
        return folium.Map(location=[51.05, 13.73], zoom_start=8)

    gdf_wgs84 = gdf.to_crs("EPSG:4326") if gdf.crs is not None else gdf
    # Eindeutiger Positions-Index: nach spatial_join/dissolve können Index-Werte
    # wiederholt sein, was die .iloc-Lookups mehrdeutig macht.
    gdf_wgs84 = gdf_wgs84.reset_index(drop=True)
    gdf_for_area = gdf.reset_index(drop=True)
    centroid = gdf_wgs84.union_all().centroid
    fmap = folium.Map(location=[centroid.y, centroid.x], zoom_start=11)

    color_values, legend_label = _compute_color_values(
        gdf_for_area, gdf_wgs84, color_field, normalize_by_area
    )
    vmin = vmax = None
    has_missing = False
    if color_values is not None:
        valid = color_values.dropna()
        if not valid.empty:
            vmin, vmax = float(valid.min()), float(valid.max())
            has_missing = valid.shape[0] < color_values.shape[0]

    _draw_features(gdf_wgs84, fmap, color_values, vmin, vmax, fixed_color=None)

    if vmin is not None:
        legend = _legend_html([(color_field, legend_label, vmin, vmax, has_missing)])
        fmap.get_root().html.add_child(folium.Element(legend))

    return fmap


def render_multi_layer_map(
    layers: dict[str, GeoDataFrame],
    color_field: str | dict[str, str] | None = None,
    normalize_by_area: bool = True,
) -> folium.Map:
    """Zeichnet jeden Layer als eigene FeatureGroup (per LayerControl schaltbar).
    Ein Layer bekommt eine feste Palettenfarbe oder, wenn er das Farbfeld hat,
    wie bei render_map eine Färbung mit eigener Legende.

    color_field ist ein Feldname für alle Layer oder ein dict
    {layer_name: field_name} für je Layer eigene Farbfelder."""
    non_empty = {name: gdf for name, gdf in layers.items() if not gdf.empty}
    if not non_empty:
        return folium.Map(location=[51.05, 13.73], zoom_start=8)

    all_wgs84 = [
        (gdf.to_crs("EPSG:4326") if gdf.crs is not None else gdf).reset_index(drop=True)
        for gdf in non_empty.values()
    ]
    combined_bounds_geom = pd.concat([g.geometry for g in all_wgs84]).union_all()
    centroid = combined_bounds_geom.centroid
    fmap = folium.Map(location=[centroid.y, centroid.x], zoom_start=11)

    legend_entries = []
    for palette_idx, (name, gdf) in enumerate(non_empty.items()):
        gdf_wgs84 = (
            gdf.to_crs("EPSG:4326") if gdf.crs is not None else gdf
        ).reset_index(drop=True)
        gdf_for_area = gdf.reset_index(drop=True)
        group = folium.FeatureGroup(name=name, show=True)

        layer_color_field = (
            color_field.get(name) if isinstance(color_field, dict) else color_field
        )
        color_values, legend_label = _compute_color_values(
            gdf_for_area, gdf_wgs84, layer_color_field, normalize_by_area
        )
        vmin = vmax = None
        fixed_color = None
        if color_values is not None:
            valid = color_values.dropna()
            if not valid.empty:
                vmin, vmax = float(valid.min()), float(valid.max())
                has_missing = valid.shape[0] < color_values.shape[0]
                legend_entries.append((name, legend_label, vmin, vmax, has_missing))
        else:
            fixed_color = _LAYER_PALETTE[palette_idx % len(_LAYER_PALETTE)]

        _draw_features(gdf_wgs84, group, color_values, vmin, vmax, fixed_color)
        group.add_to(fmap)

    folium.LayerControl(collapsed=False).add_to(fmap)

    if legend_entries:
        fmap.get_root().html.add_child(folium.Element(_legend_html(legend_entries)))

    return fmap


def attach_attribution(
    fmap: folium.Map, lines: Sequence[str] | None, result_license: str | None = None
) -> folium.Map:
    """FA65: Namensnennung der Daten auf der Karte - als Fußzeile und in der
    Leaflet-Attribution jeder Kachelebene (nach dem Kachel-Nachweis, der bleibt).
    FA73: ``result_license`` (Zeile der Ausgabelizenz) als eigene Zeile der
    Fußzeile. Ohne beides unverändert."""
    if not lines and not result_license:
        return fmap
    escaped = [html.escape(line, quote=False) for line in lines or ()]
    result_div = (
        '<div class="geofact-output-license">'
        + html.escape(result_license, quote=False)
        + "</div>"
        if result_license
        else ""
    )
    if not escaped:
        fmap.get_root().html.add_child(folium.Element(_footer(result_div)))
        return fmap
    for child in fmap._children.values():
        if isinstance(child, folium.TileLayer):
            tile_credit = child.options.get("attribution")
            data_credit = "Daten: " + "; ".join(escaped)
            child.options["attribution"] = (
                f"{tile_credit} | {data_credit}" if tile_credit else data_credit
            )
    fmap.get_root().html.add_child(
        folium.Element(_footer("Daten: " + " &middot; ".join(escaped) + result_div))
    )
    return fmap


def _footer(content: str) -> str:
    """Fußzeile der Karte (FA65/FA73) mit festem Stil."""
    return (
        '<div class="geofact-attribution" style="position:fixed;bottom:22px;right:10px;'
        "z-index:9998;background:rgba(255,255,255,0.85);padding:2px 6px;border-radius:3px;"
        'font-family:sans-serif;font-size:11px;max-width:60vw;">' + content + "</div>"
    )


def write_map(
    gdf: GeoDataFrame,
    path: str | Path,
    color_field: str | None = None,
    normalize_by_area: bool = True,
    attribution: Sequence[str] | None = None,
    result_license: str | None = None,
) -> None:
    fmap = render_map(gdf, color_field=color_field, normalize_by_area=normalize_by_area)
    attach_attribution(fmap, attribution, result_license)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fmap.save(str(path))


def write_multi_layer_map(
    layers: dict[str, GeoDataFrame],
    path: str | Path,
    color_field: str | dict[str, str] | None = None,
    normalize_by_area: bool = True,
    attribution: Sequence[str] | None = None,
) -> None:
    fmap = render_multi_layer_map(
        layers, color_field=color_field, normalize_by_area=normalize_by_area
    )
    attach_attribution(fmap, attribution)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fmap.save(str(path))


# --- Raster als Bild-Overlay (FA49) ---

MAX_OVERLAY_PIXELS = 1_000_000
"""Obergrenze der Zellen des Overlay-Bildes (Kartendatei bleibt handhabbar)."""

_COLORMAPS: dict[str, list[str]] = {
    "viridis": [
        "#440154",
        "#482878",
        "#3e4a89",
        "#31688e",
        "#26828e",
        "#1f9e89",
        "#35b779",
        "#6dcd59",
        "#b4de2c",
        "#fde725",
    ],
    "rdylgn": ["#a50026", "#f46d43", "#fee08b", "#d9ef8b", "#66bd63", "#006837"],
    "gray": ["#000000", "#ffffff"],
}


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _colorize(
    values: np.ndarray, colormap: str, vmin: float, vmax: float
) -> np.ndarray:
    """(Zeilen, Spalten) Werte -> (Zeilen, Spalten, 4) uint8-RGBA; NaN transparent.
    Lineare Interpolation zwischen den Stützfarben der Farbtabelle."""
    stops = np.array([_hex_to_rgb(c) for c in _COLORMAPS[colormap]], dtype=np.float64)
    span = vmax - vmin if vmax > vmin else 1.0
    fraction = np.clip((np.nan_to_num(values, nan=vmin) - vmin) / span, 0.0, 1.0)
    position = fraction * (len(stops) - 1)
    low = np.floor(position).astype(int)
    high = np.minimum(low + 1, len(stops) - 1)
    weight = (position - low)[..., None]
    rgb = stops[low] * (1 - weight) + stops[high] * weight
    alpha = np.where(np.isnan(values), 0, 255).astype(np.uint8)
    return np.dstack([rgb.astype(np.uint8), alpha])


def _overlay_grid(
    raster: RasterLayer, band_data: np.ndarray
) -> tuple[np.ndarray, tuple]:
    """Das Band nach EPSG:3857 gebracht und auf höchstens MAX_OVERLAY_PIXELS
    Zellen verkleinert: (float32-Array mit NaN für Nodata, Bounds in 3857)."""
    if raster.crs is None:
        raise ValueError("Raster ohne CRS kann nicht auf die Karte gelegt werden")
    rows, cols = band_data.shape
    bounds = array_bounds(rows, cols, raster.transform)
    transform, width, height = calculate_default_transform(
        raster.crs, "EPSG:3857", cols, rows, *bounds
    )
    if width * height > MAX_OVERLAY_PIXELS:
        factor = float(np.sqrt(width * height / MAX_OVERLAY_PIXELS))
        transform = transform * transform.scale(factor, factor)
        width, height = int(np.ceil(width / factor)), int(np.ceil(height / factor))
    categorical = np.issubdtype(raster.data.dtype, np.integer)
    destination = np.full((height, width), np.nan, dtype=np.float32)
    reproject(
        source=band_data,
        destination=destination,
        src_transform=raster.transform,
        src_crs=raster.crs,
        dst_transform=transform,
        dst_crs="EPSG:3857",
        resampling=Resampling.nearest if categorical else Resampling.average,
        src_nodata=np.nan,
        dst_nodata=np.nan,
    )
    return destination, array_bounds(height, width, transform)


def render_raster_map(
    raster: RasterLayer,
    band: Union[int, str, None] = None,
    colormap: str = "viridis",
    vmin: Optional[float] = None,
    vmax: Optional[float] = None,
    opacity: float = 0.8,
) -> folium.Map:
    """Raster-Layer als farbiges Bild-Overlay auf einer Folium-Karte (FA49)."""
    valid = raster.valid_mask(band)
    source = raster.band_data(band).astype(np.float32)
    source[~valid] = np.nan
    values = source[np.isfinite(source)]
    if values.size == 0:
        raise ValueError(
            "Das Raster hat keine gültigen Zellen - es gibt nichts zu zeichnen"
        )
    if vmin is None:
        vmin = float(np.percentile(values, 2))
    if vmax is None:
        vmax = float(np.percentile(values, 98))
    if vmax < vmin:
        raise ValueError(f"vmax ({vmax}) darf nicht kleiner als vmin ({vmin}) sein")

    grid, (left, bottom, right, top) = _overlay_grid(raster, source)
    west, south, east, north = transform_bounds(
        "EPSG:3857", "EPSG:4326", left, bottom, right, top
    )
    image = _colorize(grid, colormap, vmin, vmax)

    index = raster.band_index(band) if band is not None else 0
    name = raster.names[index]
    fmap = folium.Map(location=[(south + north) / 2, (west + east) / 2], zoom_start=11)
    ImageOverlay(
        image=image,
        bounds=[[south, west], [north, east]],
        opacity=opacity,
        name=name,
    ).add_to(fmap)
    fmap.fit_bounds([[south, west], [north, east]])
    branca.colormap.LinearColormap(
        _COLORMAPS[colormap],
        vmin=vmin,
        vmax=vmax if vmax > vmin else vmin + 1,
        caption=name,
    ).add_to(fmap)
    return fmap


def write_raster_map(
    raster: RasterLayer,
    path: str | Path,
    attribution: Sequence[str] | None = None,
    result_license: str | None = None,
    **options,
) -> None:
    fmap = render_raster_map(raster, **options)
    attach_attribution(fmap, attribution, result_license)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fmap.save(str(path))


# --- Registrierung als Ausgabeformat (FA40) ---


class MapOptions(BaseModel):
    """Formatspezifische Felder einer map-Ausgabe in der Szenario-YAML.
    color_field und normalize_by_area gelten für Vektor-Layer (und für Graphen,
    die als Kanten- oder Knoten-Layer gezeichnet werden; graph_part, FA53); band,
    colormap, vmin und vmax für Raster-Layer (FA49)."""

    model_config = ConfigDict(extra="forbid")

    color_field: str | None = None
    normalize_by_area: bool = Field(
        True,
        description="Nur Vektor: Polygone nach Wert je Fläche färben (Default). "
        "false, wenn das Farb-Feld schon ein Anteil oder eine Dichte ist.",
    )
    graph_part: Literal["edges", "nodes"] = Field(
        "edges",
        description="Nur Graph-Quelle (FA53): Kanten (Default) oder Knoten zeichnen.",
    )
    band: Optional[Union[int, str]] = Field(
        None,
        description="Nur Raster: Band (Name oder 1-basierte Nummer); Pflicht bei "
        "Mehrband-Raster.",
    )
    colormap: Literal["viridis", "rdylgn", "gray"] = Field(
        "viridis", description="Nur Raster: Farbtabelle."
    )
    vmin: Optional[float] = Field(
        None,
        description="Nur Raster: Wert der untersten Farbe (Default: 2. Perzentil).",
    )
    vmax: Optional[float] = Field(
        None,
        description="Nur Raster: Wert der obersten Farbe (Default: 98. Perzentil).",
    )


@register_output(
    "map",
    extension="html",
    options_model=MapOptions,
    accepts=(DataType.VECTOR, DataType.RASTER, DataType.GRAPH),
    description="Interaktive HTML-Karte (Folium): Vektor optional eingefärbt nach color_field, "
    "Raster als Farb-Overlay, Graph als Kanten oder Knoten (graph_part)",
)
def _write_output(data, target: Path, spec) -> None:
    options: MapOptions = spec.options
    attribution = getattr(spec, "attribution", None)
    lines = attribution.lines() if attribution is not None else None
    output_license = getattr(spec, "output_license", None)
    result_license = output_license.line() if output_license is not None else None
    if isinstance(data, RasterLayer):
        write_raster_map(
            data,
            target,
            attribution=lines,
            result_license=result_license,
            band=options.band,
            colormap=options.colormap,
            vmin=options.vmin,
            vmax=options.vmax,
        )
        return
    write_map(
        frame_for_output(data, spec),
        target,
        color_field=options.color_field,
        normalize_by_area=options.normalize_by_area,
        attribution=lines,
        result_license=result_license,
    )
