"""Implements: FA71 (density-based clustering of points: cluster).

DBSCAN in the projected working CRS (FA5), without a new dependency: the eps
neighbourhood comes from one bulk ``shapely.STRtree.query(..., predicate="dwithin",
distance=eps_m)``, the clusters are the connected components of the core points
(union-find).

Semantics (fixed so that results are reproducible):
- neighbour: planar distance <= eps_m, the point itself included;
- core point: at least ``min_samples`` neighbours;
- cluster: connected component of core points that are neighbours;
- border point: not a core point but a neighbour of at least one core point;
  it joins the cluster of its nearest core point (ties: lowest row position),
  so the result does not depend on a traversal order;
- noise: everything else, label -1 (also rows without geometry);
- cluster ids 0, 1, ... in the order of the first row of each cluster.

Non-point geometries enter through their centroid with one summary warning; the
output keeps the original geometry. Rows without geometry are noise, also with a
warning.
"""

from __future__ import annotations

import warnings
from typing import ClassVar, Literal

import numpy as np
import pandas as pd
import shapely
from geopandas import GeoDataFrame
from pydantic import Field, field_validator, model_validator

from geofact.builtin.operations._geometry import require_projected_crs
from geofact.plugin_api import DataType, ParamsBase, Step, register_operation

NOISE = -1


class ClusterStep(Step):
    """Density-based clustering (DBSCAN) of the objects of 'features' in the
    working CRS (metres). Writes the cluster number to 'output_field'
    (default 'cluster_id', noise = -1) and, if 'size_field' is set, the
    number of members of the cluster (0 for noise). Lines and polygons enter
    through their centroid (one warning); geometry, rows and order stay."""

    op: Literal["cluster"] = "cluster"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {"features": DataType.VECTOR}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR

    class Params(ParamsBase):
        eps_m: float = Field(
            ...,
            gt=0,
            description="Nachbarschaftsradius in Metern (Abstand <= eps_m zählt als Nachbar).",
        )
        min_samples: int = Field(
            ...,
            ge=1,
            strict=True,
            description="Mindestanzahl Nachbarn (inkl. des Punkts selbst) für einen Kernpunkt.",
        )
        output_field: str = Field(
            "cluster_id", description="Spalte für die Clusternummer (Rauschen = -1)."
        )
        size_field: str | None = Field(
            None, description="Optionale Spalte für die Clustergröße (Rauschen = 0)."
        )

        @field_validator("output_field", "size_field")
        @classmethod
        def _identifier(cls, value: str | None) -> str | None:
            if value is not None and not value.isidentifier():
                raise ValueError(
                    f"'{value}' ist kein gültiger Spaltenname (Bezeichner)"
                )
            return value

        @model_validator(mode="after")
        def _distinct_fields(self):
            if self.size_field is not None and self.size_field == self.output_field:
                raise ValueError(
                    f"size_field: '{self.size_field}' ist bereits output_field - "
                    "zwei verschiedene Spaltennamen wählen"
                )
            return self

    def produced_fields(self) -> set[str]:
        fields = {self.params.get("output_field", "cluster_id")}
        if self.params.get("size_field"):
            fields.add(self.params["size_field"])
        return fields


def _find(parent: np.ndarray, i: int) -> int:
    root = i
    while parent[root] != root:
        root = parent[root]
    while parent[i] != root:  # path compression
        parent[i], i = root, parent[i]
    return root


def dbscan_labels(points: np.ndarray, eps: float, min_samples: int) -> np.ndarray:
    """DBSCAN labels for an array of shapely points (None = no geometry ->
    noise). Pure function, semantics in the module docstring."""
    n = len(points)
    labels = np.full(n, NOISE, dtype=np.int64)
    valid = np.array([g is not None and not g.is_empty for g in points], dtype=bool)
    if not valid.any():
        return labels
    index = np.flatnonzero(valid)
    geoms = points[index]
    tree = shapely.STRtree(geoms)
    left, right = tree.query(geoms, predicate="dwithin", distance=eps)
    counts = np.bincount(left, minlength=len(geoms))  # self pair included
    core = counts >= min_samples

    parent = np.arange(len(geoms))
    both_core = core[left] & core[right]
    for a, b in zip(left[both_core], right[both_core]):
        ra, rb = _find(parent, int(a)), _find(parent, int(b))
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    local = np.full(len(geoms), NOISE, dtype=np.int64)
    roots = {int(i): _find(parent, int(i)) for i in np.flatnonzero(core)}
    for i, root in roots.items():
        local[i] = root

    # Border points: nearest core neighbour (ties -> lowest position).
    border_pairs = ~core[left] & core[right]
    if border_pairs.any():
        bl, br = left[border_pairs], right[border_pairs]
        dist = shapely.distance(geoms[bl], geoms[br])
        order = np.lexsort((br, dist, bl))
        bl, br = bl[order], br[order]
        first = np.r_[True, bl[1:] != bl[:-1]]
        for i, j in zip(bl[first], br[first]):
            local[int(i)] = roots[int(j)]

    # Renumber 0..k-1 in order of the first row of each cluster.
    mapping: dict[int, int] = {}
    for i, root in enumerate(local):
        if root != NOISE and int(root) not in mapping:
            mapping[int(root)] = len(mapping)
    labels[index] = [mapping[int(r)] if r != NOISE else NOISE for r in local]
    return labels


@register_operation(ClusterStep)
def run_cluster(inputs: dict, params: dict) -> GeoDataFrame:
    """Empty layer -> empty result with the new column(s)."""
    features: GeoDataFrame = inputs["features"]
    eps = float(params["eps_m"])
    min_samples = int(params["min_samples"])
    output_field = params.get("output_field") or "cluster_id"
    size_field = params.get("size_field")

    require_projected_crs(features, op="cluster")
    for field in (output_field, size_field):
        if field is not None and field in features.columns:
            raise ValueError(
                f"cluster: Spalte '{field}' existiert bereits im Eingang 'features' - "
                "anderen Namen für output_field/size_field wählen"
            )

    result = features.copy()
    if features.empty:
        result[output_field] = pd.Series([], dtype="int64")
        if size_field:
            result[size_field] = pd.Series([], dtype="int64")
        return result

    geometry = features.geometry
    missing = geometry.isna() | geometry.is_empty
    non_point = ~missing & (geometry.geom_type != "Point")
    points = geometry.copy()
    if non_point.any():
        types = sorted(set(geometry[non_point].geom_type))
        warnings.warn(
            f"cluster: {int(non_point.sum())} Objekt(e) sind keine Punkte ({types}); "
            "sie gehen über ihren Zentroid in die Clusterbildung ein, die Geometrie "
            "im Ergebnis bleibt unverändert.",
            stacklevel=2,
        )
        points[non_point] = geometry[non_point].centroid
    if missing.any():
        warnings.warn(
            f"cluster: {int(missing.sum())} Objekt(e) ohne Geometrie werden als "
            f"Rauschen ({NOISE}) markiert.",
            stacklevel=2,
        )

    arr = np.array(
        [None if m else g for g, m in zip(points.to_numpy(), missing)], dtype=object
    )
    labels = dbscan_labels(arr, eps, min_samples)
    result[output_field] = labels
    if size_field:
        sizes = pd.Series(labels).map(pd.Series(labels[labels != NOISE]).value_counts())
        result[size_field] = sizes.fillna(0).astype("int64").to_numpy()
    return result
