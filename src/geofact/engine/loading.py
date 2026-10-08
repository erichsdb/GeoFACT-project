"""Implements: FA9 (Laden eines Layers), FA5 (Harmonisierung), FA6 (Validierung), FA40 (Aufruf über die Registry), FA56 (Abdeckung je Layer), FA58 (CRS-Provenienz), FA64 (Region je Layer).

Die eine Ladestrecke jedes Layers, gleich für Kernbausteine und Plugins:

    (0) eigene Region des Layers auflösen (FA64, nur mit ``layer.region``)
    (1) Konnektor der Quellart (Rohdaten: Quell-CRS, Rohausdehnung, Stichprobe)
    (2) Transformation (FA5) -> Typprüfung
    (3) Abdeckungsprüfung gegen die Region (FA56)
    (4) Validierung (FA6)
    (5) Provenienz des Layers, wie er in den Store geht -> ``provenance_sink[layer.id]`` (FA58)

Der Executor ruft ``load_layer()`` genau einmal je Layer, wenn der Plan
(core/plan.py) ihn verlangt. Warnungen beim Laden werden als ``LAYER_WARNING``
gemeldet; ein Fehler wird als ``LAYER_ERROR`` gemeldet und als ``LayerLoadError``
(Ursache per ``from``, mit Layer-id und Quellart; Goldene Regel 7) weitergegeben.
Die Ereignisse ``LAYER_LOADING``/``LAYER_LOADED`` sendet der Executor, hier
entstehen nur ihre Details.

- FA64: ein Layer mit ``region`` sieht beim Laden seine Region als ``ctx.region``;
  das Arbeits-CRS bleibt das des Szenarios. Scheitert die Auflösung, ist das ein
  ``LayerLoadError``.
- FA56: schneidet ein Layer die Region im Arbeits-CRS nicht, entsteht eine
  ``RegionWarning``; ein leeres Rasterfenster ist ein Fehler. Quellarten mit
  strengerer eigener Prüfung (``points``, ``geocode``) scheitern schon im Konnektor.
- Hinweis oder Warnung: eine Warnungsklasse mit Namen auf ``Notice`` ist ein
  Hinweis (Ergebnis nicht verfälscht), alles andere eine Warnung;
  ``detail["severity"]`` ist ``info`` bzw. ``warning`` (``warning_severity``).
"""

from __future__ import annotations

import math
import warnings
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from geopandas import GeoDataFrame, GeoSeries
from pyproj import CRS
from rasterio.transform import array_bounds
from shapely.geometry import box

from geofact.core.contracts import SCENARIO_REGION, LayerBase, LoadContext
from geofact.core.crs import crs_label, epsg_code
from geofact.core.errors import LayerLoadError
from geofact.core.errors import RegionWarning  # noqa: F401 - re-export (FA56)
from geofact.core.registry import Registry
from geofact.core.store import LayerStore
from geofact.core.types import (
    Bounds,
    Graph,
    LayerData,
    LayerProvenance,
    RasterLayer,
    Shape,
    data_type_of,
)
from geofact.engine import events as _ev
from geofact.engine import region as region_module
from geofact.engine import validate as validate_module
from geofact.engine.events import ProgressCallback, ProgressEvent

ConnectorOverride = dict[str, Callable[[object], object]]
"""layer id -> Funktion ``layer -> data``, ersetzt den Konnektor (Tests, Offline-Läufe)."""

SAMPLE_MAX_POINTS = 200
"""Obergrenze der Punktstichprobe je Layer (FA58, roh und harmonisiert)."""

SHAPE_MAX_FEATURES = 1000
"""Obergrenze der gezeichneten Objekte je Layer (FA58, ``raw_shapes``/``shapes``)."""

SHAPE_MAX_VERTICES = 5000
"""Obergrenze der Stützpunkte aller Zeichengeometrien je Layer (FA58)."""

SHAPE_SIMPLIFY_DIVISOR = 1500
"""Vereinfachungstoleranz = größere Ausdehnungsseite / Divisor (FA58)."""


def warning_severity(category: str) -> str:
    """``info`` für Hinweise (Klassenname endet auf ``Notice``), sonst ``warning``."""
    return "info" if category.endswith("Notice") else "warning"


# --- Provenienz (FA58) ---


def _crs_code(crs: Any) -> str | None:
    """Kurzform eines CRS: ``EPSG:31468`` oder die pyproj-Zeichenkette."""
    if crs is None:
        return None
    crs = CRS.from_user_input(crs)
    epsg = epsg_code(crs)
    return f"EPSG:{epsg}" if epsg is not None else crs.to_string()


def _data_crs(data: LayerData) -> CRS | None:
    crs = getattr(data, "crs", None)
    return CRS.from_user_input(crs) if crs is not None else None


def _geometry_of(data: GeoDataFrame) -> GeoSeries | None:
    """Die aktive Geometriespalte oder None (Tabelle ohne Geometrie)."""
    try:
        return data.geometry
    except AttributeError:
        return None


def _finite(values: Any) -> Bounds | None:
    result = tuple(float(v) for v in values)
    if len(result) != 4 or not all(math.isfinite(v) for v in result):
        return None
    return result  # type: ignore[return-value]


def _raster_bounds(raster: RasterLayer) -> Bounds | None:
    rows, cols = raster.shape
    if rows == 0 or cols == 0:
        return None
    return _finite(array_bounds(rows, cols, raster.transform))


def _present_geometry(data: GeoDataFrame) -> GeoSeries | None:
    geometry = _geometry_of(data)
    if geometry is None or len(geometry) == 0:
        return None
    present = geometry[~(geometry.isna() | geometry.is_empty)]
    return present if len(present) else None


def _bounds(data: LayerData) -> Bounds | None:
    if isinstance(data, GeoDataFrame):
        present = _present_geometry(data)
        return _finite(present.total_bounds) if present is not None else None
    if isinstance(data, RasterLayer):
        return _raster_bounds(data)
    return None


def _feature_count(data: LayerData) -> int | None:
    if isinstance(data, GeoDataFrame):
        return int(len(data))
    if isinstance(data, RasterLayer):
        rows, cols = data.shape
        return int(rows * cols)
    if isinstance(data, Graph):
        return int(data.nx_graph.number_of_nodes())
    return None


def _sample(data: LayerData) -> list[tuple[float, float]]:
    """Deterministische Punktstichprobe (FA58): jede k-te Zeile mit
    ``k = ceil(n / SAMPLE_MAX_POINTS)``, ``representative_point``; Raster: vier
    Ecken + Mitte; Graph: keine."""
    if isinstance(data, RasterLayer):
        bounds = _raster_bounds(data)
        if bounds is None:
            return []
        west, south, east, north = bounds
        return [
            (west, south),
            (east, south),
            (east, north),
            (west, north),
            ((west + east) / 2, (south + north) / 2),
        ]
    if not isinstance(data, GeoDataFrame):
        return []
    present = _present_geometry(data)
    if present is None:
        return []
    step = max(1, math.ceil(len(present) / SAMPLE_MAX_POINTS))
    points = present.iloc[::step].iloc[:SAMPLE_MAX_POINTS].representative_point()
    return [
        (float(x), float(y))
        for x, y in zip(points.x, points.y)
        if math.isfinite(x) and math.isfinite(y)
    ]


def _shape_parts(geom) -> list[Shape]:
    """Zerlegt eine shapely-Geometrie in Zeichenteile (``Shape``)."""
    kind = geom.geom_type
    if geom.is_empty:
        return []
    if kind == "Point":
        return [("point", (((float(geom.x), float(geom.y)),),))]
    if kind in ("LineString", "LinearRing"):
        return [("line", (tuple((float(x), float(y)) for x, y, *_ in geom.coords),))]
    if kind == "Polygon":
        rings = [geom.exterior, *geom.interiors]
        return [
            (
                "polygon",
                tuple(
                    tuple((float(x), float(y)) for x, y, *_ in ring.coords)
                    for ring in rings
                ),
            )
        ]
    if hasattr(geom, "geoms"):
        return [part for sub in geom.geoms for part in _shape_parts(sub)]
    return []


def _finite_shape(shape: Shape) -> bool:
    return all(
        math.isfinite(x) and math.isfinite(y) for ring in shape[1] for x, y in ring
    )


def _shapes(data: LayerData) -> tuple[Shape, ...]:
    """Vereinfachte Zeichengeometrien eines Vektor-Layers (FA58, deterministisch):
    jede k-te Zeile, Douglas-Peucker relativ zur Ausdehnung, Abbruch nach
    ``SHAPE_MAX_VERTICES``. Raster, Graph und Tabellen ohne Geometrie: keine."""
    if not isinstance(data, GeoDataFrame):
        return ()
    present = _present_geometry(data)
    if present is None:
        return ()
    step = max(1, math.ceil(len(present) / SHAPE_MAX_FEATURES))
    chosen = present.iloc[::step].iloc[:SHAPE_MAX_FEATURES]
    bounds = _finite(chosen.total_bounds)
    if bounds is None:
        return ()
    tolerance = (
        max(bounds[2] - bounds[0], bounds[3] - bounds[1]) / SHAPE_SIMPLIFY_DIVISOR
    )
    if tolerance > 0:
        chosen = chosen.simplify(tolerance, preserve_topology=True)
    shapes: list[Shape] = []
    vertices = 0
    for geom in chosen:
        if geom is None:
            continue
        for shape in _shape_parts(geom):
            if not _finite_shape(shape):
                continue
            vertices += sum(len(ring) for ring in shape[1])
            if vertices > SHAPE_MAX_VERTICES:
                return tuple(shapes)
            shapes.append(shape)
    return tuple(shapes)


def _dataset_bounds(raw: LayerData) -> Bounds | None:
    """Ausdehnung der ganzen Rasterdatei (``raw_bounds``, FA58), nur für lokale
    Dateien; sonst None (dann gilt die Fensterausdehnung)."""
    if not isinstance(raw, RasterLayer) or not raw.path:
        return None
    path = Path(raw.path)
    if not path.is_file():
        return None
    try:
        import rasterio

        with rasterio.open(path) as dataset:
            return _finite(dataset.bounds)
    except Exception:  # noqa: BLE001 - informativ, nie ein Ladefehler
        return None


def _effective_source_crs(layer: LayerBase, raw: LayerData) -> tuple[CRS | None, bool]:
    """Quell-CRS, wie die Transformation es auslegt: ``crs_override`` erzwingt
    ``crs``, sonst gilt das CRS der Quelle und ``crs`` nur ohne Meldung der Quelle
    (FA4/FA55). Zweiter Wert: Override aktiv."""
    declared = getattr(layer, "crs", None)
    has_declared = isinstance(declared, str) and bool(declared.strip())
    override = bool(getattr(layer, "crs_override", False)) and has_declared
    reported = _data_crs(raw)
    if override or (reported is None and has_declared):
        try:
            return CRS.from_user_input(declared), override
        except Exception:  # noqa: BLE001 - eine ungültige Angabe meldet die Transformation
            return reported, override
    return reported, override


def _provenance(layer: LayerBase, raw: LayerData, data: LayerData) -> LayerProvenance:
    source_crs, override = _effective_source_crs(layer, raw)
    target_crs = _data_crs(data)
    source_code = _crs_code(source_crs)
    target_code = _crs_code(target_crs)
    return LayerProvenance(
        layer=layer.id,
        source=layer.source,
        source_crs=source_code,
        source_crs_label=crs_label(source_crs),
        crs_override=override,
        target_crs=target_code if target_code is not None else (source_code or ""),
        target_crs_label=crs_label(
            target_crs if target_crs is not None else source_crs
        ),
        raw_bounds=_dataset_bounds(raw) or _bounds(raw),
        bounds=_bounds(data),
        feature_count=_feature_count(data),
        raw_sample=_sample(raw),
        sample=_sample(data),
        raw_shapes=_shapes(raw),
        shapes=_shapes(data),
    )


def layer_loading_detail(layer: LayerBase) -> dict[str, Any]:
    """Detail des ``LAYER_LOADING``-Ereignisses (FA64): die eigene Region des
    Layers, falls deklariert."""
    region = getattr(layer, "region", None)
    return {"region": region} if isinstance(region, str) and region else {}


def layer_loaded_detail(
    layer_id: str, provenance_sink: dict[str, LayerProvenance] | None
) -> dict[str, Any]:
    """Detail des ``LAYER_LOADED``-Ereignisses (FA58): die Provenienz als Dict."""
    if provenance_sink is None or layer_id not in provenance_sink:
        return {}
    return {"provenance": provenance_sink[layer_id].to_dict()}


# --- Abdeckung der Region (FA56) ---

_WGS84 = CRS.from_epsg(4326)


def _degrees(bounds: Bounds, crs: CRS | None) -> str:
    """Ausdehnung in Grad (EPSG:4326) als ``minlon,minlat,maxlon,maxlat``."""
    if crs is not None and not crs.equals(_WGS84):
        try:
            bounds = tuple(
                GeoSeries([box(*bounds)], crs=crs).to_crs(_WGS84).total_bounds
            )
        except Exception:  # noqa: BLE001 - dann in der Einheit des CRS melden
            return ",".join(f"{v:.4f}" for v in bounds) + f" ({_crs_code(crs)})"
    return ",".join(f"{v:.4f}" for v in bounds)


def _region_degrees(ctx: LoadContext) -> str:
    if ctx.region is None:
        return "keine Region"
    bounds = _finite(ctx.region.total_bounds)
    return (
        _degrees(bounds, _data_crs(ctx.region)) if bounds is not None else "unbekannt"
    )


def _check_raster_window(layer: LayerBase, raw: LayerData, ctx: LoadContext) -> None:
    """Leeres Rasterfenster (FA56): die Region liegt außerhalb der Rasterdatei;
    Fehler mit beiden Ausdehnungen, bevor die Transformation an einem leeren
    Gitter scheitert."""
    if not isinstance(raw, RasterLayer) or _raster_bounds(raw) is not None:
        return
    dataset = _dataset_bounds(raw)
    data_extent = (
        _degrees(dataset, _data_crs(raw)) if dataset is not None else "unbekannt"
    )
    raise ValueError(
        f"Layer '{layer.id}': das Rasterfenster ist leer - die Region (Ausdehnung "
        f"{_region_degrees(ctx)} Grad) liegt außerhalb der Rasterdaten (Ausdehnung "
        f"{data_extent} Grad); Region oder Datei prüfen"
    )


def _check_region_coverage(layer: LayerBase, data: LayerData, ctx: LoadContext) -> None:
    """FA56: schneidet die Ausdehnung des Layers die Region nicht, entsteht eine
    ``RegionWarning`` mit beiden Ausdehnungen in Grad. Leere Layer, Tabellen ohne
    Geometrie und Graphen werden nicht geprüft."""
    if ctx.region is None or isinstance(data, Graph):
        return
    bounds = _bounds(data)
    crs = _data_crs(data)
    if bounds is None or crs is None:
        return
    try:
        region_bounds = _finite(ctx.region.to_crs(crs).total_bounds)
    except Exception:  # noqa: BLE001 - Region nicht abbildbar: meldet die Regionsprüfung
        return
    if region_bounds is None:
        return
    minx, miny, maxx, maxy = bounds
    rminx, rminy, rmaxx, rmaxy = region_bounds
    if minx <= rmaxx and rminx <= maxx and miny <= rmaxy and rminy <= maxy:
        return
    warnings.warn(
        f"Layer '{layer.id}': die Daten (Ausdehnung {_degrees(bounds, crs)} Grad) "
        f"überdecken die Region (Ausdehnung {_region_degrees(ctx)} Grad) nicht - "
        "falsche Region, falsches Quell-CRS oder falsche Datei?",
        RegionWarning,
        stacklevel=2,
    )


def _layer_context(
    layer: LayerBase, ctx: LoadContext, region_fetcher: region_module.Fetcher | None
) -> LoadContext:
    """FA64: mit ``layer.region`` sieht der Konnektor diese Region; das
    Arbeits-CRS bleibt das des Szenarios. Nur ein nicht leerer Text zählt."""
    region = getattr(layer, "region", None)
    if not isinstance(region, str) or not region.strip():
        return ctx
    try:
        resolved = region_module.resolve_region(
            region, fetcher=region_fetcher, refresh=ctx.refresh
        )
    except Exception as exc:  # noqa: BLE001 - wird zum LayerLoadError mit Ursache
        raise ValueError(
            f"Region '{region}' konnte nicht aufgelöst werden: {exc}"
        ) from exc
    return replace(ctx, region=resolved)


# --- Validierung (FA6) und Typprüfung ---


def _reference_for_validation(
    layer: LayerBase, ctx: LoadContext, store: LayerStore | None
):
    """Referenzgeometrie der räumlichen Prüfung (FA6) im Arbeits-CRS. Bei
    ``scenario_region`` (mit eigener Layer-Region diese, FA64) wird die Region
    (EPSG:4326) ins Arbeits-CRS gebracht, sonst vergläche contains() Grad gegen
    Meter und verwürfe jedes Objekt. Sonst der zuvor geladene Referenz-Layer."""
    if layer.validation is None or layer.validation.spatial_check is None:
        return None
    if ctx.region is None or ctx.target_crs is None:
        raise ValueError(
            "spatial_check braucht Region und Arbeits-CRS im LoadContext "
            "(nur innerhalb eines Laufs verfügbar)"
        )
    within = layer.validation.spatial_check.within
    if within == SCENARIO_REGION:
        return ctx.region.to_crs(ctx.target_crs)
    if store is None or within not in store:
        raise ValueError(
            f"spatial_check.within='{within}': der Referenz-Layer ist nicht geladen "
            "(der Ausführungsplan lädt ihn vor diesem Layer)"
        )
    reference = store[within]
    if not isinstance(reference, GeoDataFrame):
        raise TypeError(
            f"spatial_check.within='{within}': der Referenz-Layer muss ein Vektor-Layer sein, "
            f"ist aber {type(reference).__name__}"
        )
    if reference.crs is None:
        raise ValueError(
            f"spatial_check.within='{within}': der Referenz-Layer hat kein CRS"
        )
    return reference.to_crs(ctx.target_crs)


def _expect_declared_type(layer: LayerBase, data: object, stage: str) -> None:
    """Nachbedingung einer Ladestufe: das Ergebnis hat den deklarierten Datentyp
    (``layer.data_type``); die Kantenprüfung (FA2) verlässt sich darauf."""
    actual = data_type_of(data)
    if actual != layer.data_type:
        raise TypeError(
            f"{stage} lieferte {actual.value}, deklariert ist data_type: {layer.data_type.value}"
        )


def load_layer(
    layer: LayerBase,
    ctx: LoadContext,
    registry: Registry,
    observer: ProgressCallback | None = None,
    *,
    step: str | None = None,
    override: Callable[[object], object] | None = None,
    store: LayerStore | None = None,
    provenance_sink: dict[str, LayerProvenance] | None = None,
    region_fetcher: region_module.Fetcher | None = None,
) -> LayerData:
    """Lädt einen Layer: Region (FA64) -> Konnektor -> Transformation ->
    Abdeckung (FA56) -> Validierung (FA6) -> Provenienz (FA58).

    observer: empfängt LAYER_WARNING bzw. LAYER_ERROR; step benennt den
    Schritt, vor dem der Layer geladen wird (None: nur ein Output liest ihn).
    override: ersetzt den Konnektor (Tests). store: der LayerStore des Laufs,
    aus dem ein Referenz-Layer der räumlichen Prüfung gelesen wird.
    provenance_sink: bekommt die ``LayerProvenance`` unter ``layer.id`` (FA58).
    region_fetcher: Testnaht der Regionsauflösung wie bei ``run()`` (FA64).

    Nachbedingung: das Ergebnis hat den in der Konfiguration deklarierten
    Datentyp (``layer.data_type``). Jeder Fehler wird zu ``LayerLoadError``."""
    emit = observer or _ev._noop
    failure: BaseException | None = None
    data: LayerData | None = None

    with _ev.collect_warnings() as caught:
        try:
            # Konnektor und Transformation kommen aus der Registry (FA40);
            # Kern und Plugin sind hier nicht unterscheidbar.
            source = registry.source(layer.source)
            ctx = _layer_context(layer, ctx, region_fetcher)
            raw = override(layer) if override is not None else source.load(layer, ctx)
            _expect_declared_type(layer, raw, f"Konnektor '{layer.source}'")
            _check_raster_window(layer, raw, ctx)

            # FA5 oder die Alternative der Quellart (z. B. 'none' für Tabellen, FA37).
            data = registry.resolve_transform(source.transform)(layer, raw, ctx)
            _expect_declared_type(layer, data, "Transformation")
            _check_region_coverage(layer, data, ctx)

            if layer.validation is not None:
                if not isinstance(data, GeoDataFrame):
                    raise TypeError(
                        "validation ist nur für Vektor-Layer definiert (FA6), "
                        f"der Layer ist {layer.data_type.value}"
                    )
                result = validate_module.validate(
                    layer.id,
                    data,
                    layer.validation,
                    region_geometry=_reference_for_validation(layer, ctx, store),
                )
                data = result.gdf

            # Provenienz nach einer eventuell verwerfenden Validierung.
            if provenance_sink is not None:
                provenance_sink[layer.id] = _provenance(layer, raw, data)
        except Exception as exc:  # noqa: BLE001 - wird gemeldet und als LayerLoadError weitergereicht
            failure = exc

    for message, category in caught:
        emit(
            ProgressEvent(
                type=_ev.LAYER_WARNING,
                layer=layer.id,
                step=step,
                message=message,
                detail={"category": category, "severity": warning_severity(category)},
            )
        )

    if failure is not None:
        error = LayerLoadError(layer.id, layer.source, failure)
        emit(
            ProgressEvent(
                type=_ev.LAYER_ERROR,
                layer=layer.id,
                step=step,
                error=str(error),
            )
        )
        raise error from failure

    assert data is not None
    return data
