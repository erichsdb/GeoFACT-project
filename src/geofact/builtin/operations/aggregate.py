"""Implements: FA8 (räumliche Operationen, Erweiterung aggregate), FA63 (typisierte Vergleiche).

Aggregation je Bezugsgeometrie: zählt oder aggregiert Objekte aus 'features' je
Zone in 'zones' (z. B. Aerzte je Ortsteil). Zonen ohne Treffer erhalten explizit 0
(nicht NaN), so dass ein nachfolgender filter "count == 0" die unterversorgten
Zonen liefert.

FA63: ``value_field`` wird über ``geofact.support.numeric.require_numeric``
gelesen, ``sum`` addiert also Zahlen-Strings statt sie zu verketten; hat kein
Objekt einen numerischen Wert, ist das ein Fehler. Objekte, die in keiner Zone
liegen, und Objekte, die in mehreren Zonen gezählt werden (``predicate:
intersects``), werden je mit einer Warnung gemeldet.
"""

from __future__ import annotations

from typing import ClassVar, Literal, Optional, Self

import warnings

import geopandas as gpd
import numpy as np
from geopandas import GeoDataFrame
from pydantic import Field, model_validator

from geofact.builtin.operations._geometry import _homogenize
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step
from geofact.support.numeric import require_numeric


def _column_safe(value: object) -> str:
    """Kategorie-Wert -> Spaltenname-Suffix. Ersetzt Zeichen, die
    DataFrame.eval() in nachfolgenden filter-Schritten stolpern lassen
    (Leerzeichen, Doppelpunkte, Bindestriche)."""
    text = str(value).strip()
    for ch in (" ", ":", "-", "/", ".", ","):
        text = text.replace(ch, "_")
    return text


def _report_assignment(joined: GeoDataFrame, total: int, predicate: str) -> None:
    """Implements: FA8-Zusatz - unzugeordnete und mehrfach gezählte
    Objekte melden (joined.index = Position des Objekts in features)."""
    per_feature = joined.index.value_counts()
    unassigned = total - len(per_feature)
    if unassigned:
        warnings.warn(
            f"aggregate: {unassigned} von {total} Objekt(en) aus 'features' liegen in "
            f"keiner Zone (predicate: {predicate}) und werden nicht gezählt",
            stacklevel=3,
        )
    multi = int((per_feature > 1).sum())
    if multi:
        hint = (
            " - predicate: within zählt nur Objekte, die ganz in einer Zone liegen"
            if predicate == "intersects"
            else ""
        )
        warnings.warn(
            f"aggregate: {multi} Objekt(e) liegen in mehreren Zonen und werden in jeder "
            f"gezählt (Mehrfachzählung, predicate: {predicate}){hint}",
            stacklevel=3,
        )


class AggregateStep(Step):
    """Aggregiert Objekte aus 'features' je Geometrie in 'zones'
    (FA8-Erweiterung: Aggregation je Bezugsgeometrie). Grundlage für
    Versorgungs-/Lueckenanalysen: "wie viele Aerzte je Ortsteil" bzw.
    "welche Ortsteile haben KEINE Apotheke" (aggregate + filter
    "count == 0"). statistic=count zählt Objekte (Spalte 'count');
    sum/mean aggregieren value_field (Spalte '<statistic>_<value_field>').
    group_by (nur mit count) erzeugt zusätzlich je Kategorie-Wert eine
    Spalte 'count_<wert>' - Zonen ohne Objekte einer Kategorie erhalten 0,
    Lücken werden also explizit sichtbar statt still zu fehlen."""

    op: Literal["aggregate"] = "aggregate"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {
        "features": DataType.VECTOR,
        "zones": DataType.VECTOR,
    }
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        statistic: Literal["count", "sum", "mean"] = Field(
            "count",
            description="count zählt Objekte je Zone; sum/mean aggregieren value_field.",
        )
        value_field: Optional[str] = Field(
            None,
            description="Numerisches Attribut der features (Pflicht bei sum/mean).",
        )
        group_by: Optional[str] = Field(
            None,
            description="Kategorie-Attribut der features (nur mit count): "
            "erzeugt je Wert eine Spalte count_<wert>.",
        )
        predicate: Literal["intersects", "within"] = Field(
            "intersects",
            description="Räumliches Prädikat der Zuordnung features -> zones.",
        )

        @model_validator(mode="after")
        def check_combination(self) -> Self:
            if self.statistic in ("sum", "mean") and not self.value_field:
                raise ValueError(f"statistic '{self.statistic}' benötigt value_field")
            if self.group_by and self.statistic != "count":
                raise ValueError("group_by ist nur mit statistic=count kombinierbar")
            return self

    def produced_fields(self) -> set[str]:
        # Die group_by-Spalten count_<wert> hängen von den Daten ab und sind zur
        # Konfigurationszeit nicht bekannt; sie fehlen hier (wie bei spatial_join).
        statistic = self.params.get("statistic", "count")
        value_field = self.params.get("value_field")
        if statistic == "count":
            return {"count"}
        return {f"{statistic}_{value_field}"}


@register_operation(AggregateStep)
def run_aggregate(inputs: dict, params: dict) -> GeoDataFrame:
    """Zeilenzahl und -reihenfolge von 'zones' bleiben; nur Statistik-Spalten
    kommen hinzu."""
    features: GeoDataFrame = _homogenize(inputs["features"], label="features")
    zones: GeoDataFrame = _homogenize(inputs["zones"], label="zones")

    statistic = params.get("statistic", "count")
    value_field = params.get("value_field")
    group_by = params.get("group_by")
    predicate = params.get("predicate", "intersects")

    # Fehlende Attribute explizit melden statt stillschweigend leer rechnen.
    if not features.empty:
        if statistic in ("sum", "mean") and value_field not in features.columns:
            raise ValueError(
                f"aggregate: value_field '{value_field}' existiert nicht in features "
                f"(vorhanden: {sorted(c for c in features.columns if c != features.geometry.name)})"
            )
        if group_by and group_by not in features.columns:
            raise ValueError(
                f"aggregate: group_by '{group_by}' existiert nicht in features "
                f"(vorhanden: {sorted(c for c in features.columns if c != features.geometry.name)})"
            )

    result = zones.copy()
    if zones.empty:
        return result

    stat_field = "count" if statistic == "count" else f"{statistic}_{value_field}"

    if features.empty:
        # Leerer Eingabelayer: 0 für count/sum, NaN für mean (undefiniert)
        result[stat_field] = 0 if statistic in ("count", "sum") else np.nan
        return result

    if statistic in ("sum", "mean"):
        values = require_numeric(
            features[value_field], where="aggregate", column=value_field
        )
        if not values.notna().any():
            raise ValueError(
                f"aggregate: value_field '{value_field}' hat keinen numerischen Wert "
                f"({len(features)} Objekt(e)) - statistic {statistic} ist nicht berechenbar"
            )
        features = features.assign(**{value_field: values})

    keep = [c for c in {group_by, value_field} if c]
    # Positionsindex: Zuordnung je Objekt zählbar, auch bei doppeltem Index
    probe = features[[*keep, features.geometry.name]].reset_index(drop=True)
    joined = gpd.sjoin(
        probe, zones[[zones.geometry.name]], how="inner", predicate=predicate
    )
    _report_assignment(joined, len(probe), predicate)

    if statistic == "count":
        counts = joined["index_right"].value_counts()
        result["count"] = counts.reindex(zones.index).fillna(0).astype(int)
        if group_by:
            pivot = (
                joined.groupby(["index_right", group_by]).size().unstack(fill_value=0)
            )
            for category in pivot.columns:
                column = f"count_{_column_safe(category)}"
                result[column] = (
                    pivot[category].reindex(zones.index).fillna(0).astype(int)
                )
    else:
        aggregated = joined.groupby("index_right")[value_field].agg(statistic)
        series = aggregated.reindex(zones.index)
        result[stat_field] = series.fillna(0.0) if statistic == "sum" else series

    return result
