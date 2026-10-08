"""Implements: FA19 (Vektor-Layer verlustfrei zusammenführen).

Hängt zwei Vektor-Layer zeilenweise aneinander, ohne räumliches Prädikat.
Das ermöglicht z. B. ein kombiniertes Bahnnetz aus Tram- und S-Bahn-Gleisen
für network_nodes (FA17) und build_network (FA10). Attribute bleiben erhalten
(etwa 'railway'); Spalten, die nur eine Seite hat, werden mit NaN aufgefüllt.
Die dominanten Geometriedimensionen von 'a' und 'b' müssen übereinstimmen.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import geopandas as gpd
import pandas as pd
from geopandas import GeoDataFrame

from geofact.builtin.operations._geometry import _GEOM_DIMENSION
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


def _dominant_dimension(gdf: GeoDataFrame) -> int | None:
    """Höchste vorkommende topologische Dimension (Flaeche=2 > Linie=1 >
    Punkt=0); None bei leerem Layer."""
    if gdf.empty:
        return None
    dims = gdf.geometry.geom_type.map(_GEOM_DIMENSION)
    return dims.max()


class VectorUnionStep(Step):
    """Führt 'a' und 'b' verlustfrei zeilenweise zusammen (FA19) - reine
    Konkatenation ohne räumliches Prädikat: jede Eingabezeile aus beiden
    Layern bleibt als eigene Zeile im Ergebnis erhalten, Geometrie
    unverändert. Abzugrenzen von drei bestehenden Operationen, die
    oberflächlich ähnlich klingen, aber etwas anderes tun: dissolve
    (FA8) vereinigt Geometrien überlappungsfrei (eine Zeile je
    Teilfläche bzw. je Gruppe) und verändert damit das Schema; overlay/spatial_join (FA8) verknüpfen zwei Layer über
    ein räumliches Prädikat und können dabei Zeilen verwerfen oder
    aufteilen; homogenize_geometry (FA18) reduziert EINEN gemischt-
    geometrischen Layer auf seine dominante Dimension, kombiniert aber
    keine zwei Layer. vector_union setzt voraus, dass 'a' und 'b' zur
    selben Geometrie-Familie gehören (beide überwiegend Fläche, beide
    überwiegend Linie, oder beide überwiegend Punkt) - bei
    abweichenden dominanten Dimensionen ist das ein Konfigurationsfehler,
    den die Operation explizit zurückweist statt still zu koerzieren."""

    op: Literal["vector_union"] = "vector_union"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "a": DataType.VECTOR,
        "b": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        """Keine Parameter."""


@register_operation(VectorUnionStep)
def run_vector_union(inputs: dict, params: dict) -> GeoDataFrame:
    """Postbedingung: len(result) == len(a) + len(b); keine Zeile geht verloren."""
    a: GeoDataFrame = inputs["a"]
    b: GeoDataFrame = inputs["b"]

    # Fehlendes CRS auf einer Seite gilt als kompatibel; Reprojizieren ist Sache
    # von FA5, nicht dieser Operation.
    if a.crs is not None and b.crs is not None and a.crs != b.crs:
        raise ValueError(
            f"vector_union: 'a' und 'b' haben unterschiedliches CRS "
            f"({a.crs} vs. {b.crs}) - erst per FA5-Harmonisierung angleichen, "
            "vector_union reprojiziert nicht stillschweigend"
        )

    # Leerer Layer (None) kann keinen Konflikt auslösen.
    dim_a = _dominant_dimension(a)
    dim_b = _dominant_dimension(b)
    if dim_a is not None and dim_b is not None and dim_a != dim_b:
        dim_name = {0: "Punkt", 1: "Linie", 2: "Fläche"}
        raise ValueError(
            f"vector_union: 'a' und 'b' haben unterschiedliche dominante "
            f"Geometriedimension ({dim_name[dim_a]} vs. {dim_name[dim_b]}) - "
            "nicht kompatibel für eine verlustfreie Zusammenführung; bei "
            "gemischt-geometrischen Eingaben vorher homogenize_geometry "
            "(FA18) je Layer einsetzen"
        )

    if a.empty and b.empty:
        columns = list(dict.fromkeys([*a.columns, *b.columns]))
        crs = a.crs if a.crs is not None else b.crs
        empty = gpd.GeoDataFrame(columns=columns, geometry="geometry", crs=crs)
        return empty

    combined = pd.concat([a, b], ignore_index=True, sort=False)
    crs = a.crs if a.crs is not None else b.crs
    result = gpd.GeoDataFrame(combined, geometry="geometry", crs=crs)

    assert len(result) == len(a) + len(b), (
        "vector_union: Zeilenzahl der Ausgabe weicht von len(a) + len(b) ab "
        "- verlustfreie Kernpostbedingung verletzt"
    )
    return result
