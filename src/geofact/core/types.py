"""Implements: FA2 (Datentypen im Pipeline-Graphen), FA58 (LayerProvenance), Grundlage für FA3-FA7, FA10, FA14.

Die Datentypen des Kerns an einer Stelle:

- DataType     vector | raster | graph - die Port-Typen des DAG (FA2)
- RasterLayer  Pixelwerte (ein oder mehrere benannte Bänder) + Georeferenzierung
- Graph        networkx-Graph + CRS (FA10); networkx nur unter TYPE_CHECKING,
               der Kern importiert es zur Laufzeit nicht
- LayerData    Vereinigung aller drei Laufzeit-Datentypen
- LayerProvenance  CRS-Herkunft eines geladenen Layers (FA58)
- data_type_of Laufzeitobjekt -> DataType; unbekannte Objekte sind ein
               Fehler (TypeError), nie stillschweigend ein Graph

Internes Datenmodell:
- Vektor-Layer = GeoDataFrame mit gesetztem CRS
- Raster-Layer = RasterLayer (Pfad + Array + Transform + CRS)
- Graph        = Graph (nx.Graph + CRS)"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Union

import numpy as np
from affine import Affine
from geopandas import GeoDataFrame
from pyproj import CRS

if TYPE_CHECKING:
    import networkx as nx


class DataType(str, Enum):
    VECTOR = "vector"
    RASTER = "raster"
    GRAPH = "graph"


@dataclass
class RasterLayer:
    """Leichtgewichtiger Raster-Layer: Pixelwerte + Georeferenzierung.

    data ist 2D (Zeilen, Spalten) = ein Band, oder 3D (Bänder, Zeilen,
    Spalten) = mehrere Bänder (FA46: Satellitenbilder). band_names benennt die
    Bänder (Länge = Bandzahl, eindeutig, None = unbenannt; ein unbenanntes
    Raster heißt implizit ``band1`` ... ``bandN``). Ein-Band-Raster ohne Namen
    verhalten sich wie vor der Mehrband-Erweiterung.

    path ist informativ (Fehlermeldungen, Provenienz) und kann fehlen,
    wenn der Layer nicht aus einer Datei stammt (z. B. Zwischenergebnis).
    nodata ist der Sentinelwert für "kein Messwert" (z. B. GHS-POPs
    negativer Sentinel, 0 bei Sentinel-2, NaN bei berechneten Rastern) - None,
    wenn die Quelle keinen deklariert. Raster-Operationen (z. B. zonal_stats)
    müssen Zellen mit diesem Wert von Statistiken ausschließen, sonst
    verfälscht der Sentinel das Ergebnis (Grundsatz: keine stillen
    Fehler); ``valid_mask()`` ist dafür der eine Weg."""

    data: np.ndarray
    transform: Affine
    crs: CRS
    path: str | None = None
    nodata: float | None = None
    band_names: list[str] | None = None

    def __post_init__(self) -> None:
        if self.data.ndim not in (2, 3):
            raise ValueError(
                f"RasterLayer.data muss 2D (Zeilen, Spalten) oder 3D (Bänder, Zeilen, Spalten) "
                f"sein, hat aber {self.data.ndim} Dimensionen (Form {self.data.shape})"
            )
        if self.band_names is not None:
            self.band_names = [str(name) for name in self.band_names]
            if len(self.band_names) != self.band_count:
                raise ValueError(
                    f"RasterLayer: {len(self.band_names)} Bandnamen {self.band_names} "
                    f"für {self.band_count} Bänder"
                )
            if len(set(self.band_names)) != len(self.band_names):
                raise ValueError(
                    f"RasterLayer: Bandnamen müssen eindeutig sein: {self.band_names}"
                )

    @property
    def shape(self) -> tuple[int, int]:
        return self.data.shape[-2:]

    @property
    def band_count(self) -> int:
        return 1 if self.data.ndim == 2 else int(self.data.shape[0])

    @property
    def names(self) -> list[str]:
        """Die wirksamen Bandnamen: die deklarierten, sonst ``band1`` ... ``bandN``."""
        if self.band_names is not None:
            return list(self.band_names)
        return [f"band{index}" for index in range(1, self.band_count + 1)]

    def band_index(self, selector: int | str) -> int:
        """0-basierter Index eines Bandes: ein Bandname oder eine 1-basierte
        Nummer (GDAL-/rasterio-Konvention). Unbekannt ist ein Fehler, der die
        vorhandenen Bänder nennt."""
        names = self.names
        if isinstance(selector, str):
            if selector in names:
                return names.index(selector)
        elif isinstance(selector, int) and not isinstance(selector, bool):
            if 1 <= selector <= self.band_count:
                return selector - 1
        raise ValueError(
            f"Band {selector!r} nicht im Raster{self._origin()}: vorhanden sind {names} "
            f"(Nummern 1..{self.band_count})"
        )

    def band_data(self, selector: int | str | None = None) -> np.ndarray:
        """Die Zellwerte EINES Bandes als 2D-Array. Ohne ``selector`` ist das
        nur bei genau einem Band erlaubt - bei mehreren wählt der Kern nicht
        still das erste (Goldene Regel 7)."""
        if selector is None:
            if self.band_count != 1:
                raise ValueError(
                    f"Das Raster{self._origin()} hat {self.band_count} Bänder {self.names}: "
                    "'band' muss das zu verwendende Band nennen"
                )
            return self.data if self.data.ndim == 2 else self.data[0]
        index = self.band_index(selector)
        return self.data if self.data.ndim == 2 else self.data[index]

    def valid_mask(self, selector: int | str | None = None) -> np.ndarray:
        """Boolesche Maske der gültigen Zellen (True = Messwert). Ohne
        ``selector`` für das ganze ``data``-Array (gleiche Form), mit
        ``selector`` für ein Band. Ohne deklariertes nodata sind alle Zellen
        gültig; ein NaN-nodata schließt NaN-Zellen aus."""
        data = self.data if selector is None else self.band_data(selector)
        if self.nodata is None:
            return np.ones(data.shape, dtype=bool)
        if isinstance(self.nodata, float) and np.isnan(self.nodata):
            return ~np.isnan(data)
        return data != self.nodata

    def _origin(self) -> str:
        return f" '{self.path}'" if self.path else ""


class Graph:
    """Dünner Wrapper um networkx.Graph + Knoten-Geometrie/CRS, damit
    graph_to_vector ohne zusätzlichen Kontext einen Punkt-Layer bauen
    kann."""

    def __init__(self, nx_graph: "nx.Graph", crs: CRS | None) -> None:
        self.nx_graph = nx_graph
        self.crs = crs


LayerData = Union[GeoDataFrame, RasterLayer, Graph]


def data_type_of(data: Any) -> DataType:
    """Laufzeitobjekt -> DataType (für Ausgabeformate und die Nachbedingung
    des Executors). Ein unbekanntes Objekt ist ein Fehler: stillschweigend
    'graph' anzunehmen ließe eine falsche Operation erst beim Schreiben
    auffallen (Goldene Regel 7)."""
    if isinstance(data, GeoDataFrame):
        return DataType.VECTOR
    if isinstance(data, RasterLayer):
        return DataType.RASTER
    if isinstance(data, Graph):
        return DataType.GRAPH
    raise TypeError(
        f"Unbekannter Datentyp {type(data).__module__}.{type(data).__name__}: "
        "erwartet GeoDataFrame (vector), RasterLayer (raster) oder Graph (graph)"
    )


# --- CRS-Provenienz je Layer (FA58) ---

Bounds = tuple[float, float, float, float]
# Vereinfachte Geometrie eines Teils für Zeichnungen (FA58): Art
# ("point" | "line" | "polygon") und Koordinatenringe; ein Punkt hat einen Ring
# mit einer Koordinate, eine Linie einen Pfad, ein Polygon Außen- und
# Innenringe. Reine Python-Tupel, keine shapely-Objekte im Kern.
Shape = tuple[str, tuple[tuple[tuple[float, float], ...], ...]]
SHAPE_KINDS = ("point", "line", "polygon")


def _bounds_or_none(value: Any) -> Bounds | None:
    if value is None:
        return None
    values = tuple(float(v) for v in value)
    if len(values) != 4:
        raise ValueError(
            f"Ausdehnung braucht vier Werte (minx, miny, maxx, maxy), erhalten {value!r}"
        )
    return values  # type: ignore[return-value]


@dataclass(frozen=True)
class LayerProvenance:
    """Woher ein geladener Layer kam und wohin er harmonisiert wurde (FA58).

    source_crs ist die Kurzform des Quell-CRS (``EPSG:31468``) oder None, wenn
    die Quelle keines meldet (Tabelle ohne Geometrie); die ``*_label``-Felder
    sind lesbar (``crs_label``). raw_bounds/bounds sind die Ausdehnung vor
    bzw. nach der Transformation in der Einheit des jeweiligen CRS;
    raw_sample/sample eine deterministische Punktstichprobe (x, y) roh bzw.
    harmonisiert. feature_count ist None, wenn nicht zählbar (Raster).

    raw_shapes/shapes (Zusatz FA58, Changelog 99) sind vereinfachte Geometrien
    roh bzw. harmonisiert (``Shape``, deterministisch, Stützpunkte gedeckelt),
    damit Ausgaben wie ``crs_plot`` die echten Geometrien zeichnen können. Sie
    bleiben im Speicher für die Writer und stehen bewusst NICHT in
    ``to_dict`` (Ereignisse und Web-Status bleiben klein); ``from_dict`` liefert
    sie leer."""

    layer: str
    source: str
    source_crs: str | None
    source_crs_label: str
    crs_override: bool
    target_crs: str
    target_crs_label: str
    raw_bounds: Bounds | None
    bounds: Bounds | None
    feature_count: int | None
    raw_sample: list[tuple[float, float]]
    sample: list[tuple[float, float]]
    raw_shapes: tuple[Shape, ...] = ()
    shapes: tuple[Shape, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-taugliche Form (Tupel als Listen) für Ereignisse und Web-Status;
        ohne ``raw_shapes``/``shapes`` (nur für Writer im Speicher)."""
        return {
            "layer": self.layer,
            "source": self.source,
            "source_crs": self.source_crs,
            "source_crs_label": self.source_crs_label,
            "crs_override": self.crs_override,
            "target_crs": self.target_crs,
            "target_crs_label": self.target_crs_label,
            "raw_bounds": list(self.raw_bounds)
            if self.raw_bounds is not None
            else None,
            "bounds": list(self.bounds) if self.bounds is not None else None,
            "feature_count": self.feature_count,
            "raw_sample": [[float(x), float(y)] for x, y in self.raw_sample],
            "sample": [[float(x), float(y)] for x, y in self.sample],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LayerProvenance":
        """Umkehrung von ``to_dict`` (Round-Trip, z. B. für die Web-Schicht)."""
        count = raw.get("feature_count")
        return cls(
            layer=str(raw["layer"]),
            source=str(raw["source"]),
            source_crs=raw.get("source_crs"),
            source_crs_label=str(raw["source_crs_label"]),
            crs_override=bool(raw.get("crs_override", False)),
            target_crs=str(raw["target_crs"]),
            target_crs_label=str(raw["target_crs_label"]),
            raw_bounds=_bounds_or_none(raw.get("raw_bounds")),
            bounds=_bounds_or_none(raw.get("bounds")),
            feature_count=int(count) if count is not None else None,
            raw_sample=[(float(x), float(y)) for x, y in raw.get("raw_sample", [])],
            sample=[(float(x), float(y)) for x, y in raw.get("sample", [])],
        )
