"""Implements: FA69 (Dublettenbereinigung: deduplicate).

Entfernt Mehrfacheinträge nach Schlüsselspalten (``by``) - typisch für OSM, wo
ein Park als Weg und als Relation, eine Haltestelle als Knoten und als Plattform
vorkommt.

Regel ``keep`` je Gruppe gleicher Schlüssel:
- ``first`` / ``last``: das erste bzw. letzte Objekt in Eingabereihenfolge;
- ``largest_geometry``: sortiert nach Geometriedimension absteigend, dann Fläche,
  dann Länge (ein Polygon schlägt einen Punkt); bei Gleichstand gewinnt das erste.

Zeilen mit fehlendem Wert (NaN/None) in einem ``by``-Feld werden nicht gruppiert
(zwei unbenannte Parks sind keine Dubletten) und bleiben erhalten; ihre Anzahl
steht in einer Warnung. Eine ``DeduplicateNotice`` nennt die entfernte Anzahl.
Die Originalreihenfolge bleibt, ein leerer Layer bleibt leer."""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal

import numpy as np
from geopandas import GeoDataFrame
from pydantic import Field

from geofact.builtin.operations._geometry import geometry_dimensions
from geofact.plugin_api import DataType, ParamsBase, register_operation, Step


class DeduplicateNotice(UserWarning):
    """Hinweis: wie viele Dubletten deduplicate entfernt hat."""


class DeduplicateStep(Step):
    """Dublettenbereinigung (FA69); Vertrag siehe Modul-Docstring."""

    op: Literal["deduplicate"] = "deduplicate"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        by: list[str] = Field(
            ..., min_length=1, description="Schlüsselspalten einer Dublette."
        )
        keep: Literal["first", "last", "largest_geometry"] = Field(
            "first", description="Welches Objekt je Gruppe bleibt."
        )


@register_operation(DeduplicateStep)
def run_deduplicate(inputs: dict, params: dict) -> GeoDataFrame:
    features: GeoDataFrame = inputs["features"]
    by = list(dict.fromkeys(params["by"]))
    rule = params.get("keep", "first")
    if features.empty:
        return features.copy()
    missing = [column for column in by if column not in features.columns]
    if missing:
        available = [
            column for column in features.columns if column != features.geometry.name
        ]
        raise ValueError(
            f"deduplicate: Schluesselspalte(n) {missing} existieren nicht im Layer "
            f"(vorhanden: {sorted(map(str, available))})"
        )

    keys = features[by].reset_index(drop=True)
    without_key = keys.isna().any(axis=1).to_numpy()
    if without_key.any():
        warnings.warn(
            f"deduplicate: {int(without_key.sum())} Objekt(e) ohne Wert in {by} werden nicht "
            "als Dubletten behandelt und bleiben erhalten",
            stacklevel=2,
        )

    positions = np.arange(len(features))
    candidates = keys[~without_key].assign(_position=positions[~without_key])
    if rule == "largest_geometry":
        subset = features.iloc[positions[~without_key]]
        candidates = candidates.assign(
            _dimension=geometry_dimensions(subset).to_numpy(),
            _area=subset.geometry.area.fillna(0.0).to_numpy(),
            _length=subset.geometry.length.fillna(0.0).to_numpy(),
        ).sort_values(
            ["_dimension", "_area", "_length", "_position"],
            ascending=[False, False, False, True],
            kind="mergesort",
        )
        kept = candidates.drop_duplicates(subset=by, keep="first")["_position"]
    else:
        kept = candidates.drop_duplicates(subset=by, keep=rule)["_position"]

    keep_mask = without_key.copy()
    keep_mask[kept.to_numpy()] = True
    result = features.iloc[np.flatnonzero(keep_mask)].copy()
    removed = len(features) - len(result)
    if removed:
        warnings.warn(
            DeduplicateNotice(
                f"deduplicate: {removed} Dublette(n) nach {by} entfernt (Regel '{rule}'; "
                f"Zeilen vorher {len(features)}, nachher {len(result)})"
            ),
            stacklevel=2,
        )
    return result
