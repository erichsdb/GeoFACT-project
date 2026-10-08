"""Implements: gemeinsame Geometrie-Hilfsfunktionen für hexbin (FA8-
Erweiterung, builtin/operations/hexbin.py) und hex_grid (FA22,
builtin/operations/hex_grid.py).

Hilfsmodul ohne eigenen Operationsschritt (wird nicht gescannt): axiale
Sechseck-Mathematik, die hexbin und hex_grid gemeinsam nutzen.
"""

from __future__ import annotations

import math

import numpy as np
from shapely.geometry import Polygon

# Winkel der sechs Ecken eines "pointy-top"-Sechsecks (Spitze oben).
VERTEX_ANGLES = [math.radians(60 * i - 30) for i in range(6)]


def axial_round(qf: np.ndarray, rf: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Cube-Rounding: kontinuierliche Axialkoordinaten -> Zellindizes.
    Rundet in Würfelkoordinaten (x+y+z=0) und korrigiert die Komponente
    mit dem größten Rundungsfehler, damit die Summenbedingung hält."""
    x, z = qf, rf
    y = -x - z
    rx, ry, rz = np.round(x), np.round(y), np.round(z)
    dx, dy, dz = np.abs(rx - x), np.abs(ry - y), np.abs(rz - z)
    fix_x = (dx > dy) & (dx > dz)
    rx[fix_x] = -ry[fix_x] - rz[fix_x]
    fix_z = ~fix_x & (dz > dy)
    rz[fix_z] = -rx[fix_z] - ry[fix_z]
    return rx.astype(int), rz.astype(int)


def hexagon(q: int, r: int, radius: float) -> Polygon:
    cx = radius * math.sqrt(3) * (q + r / 2)
    cy = radius * 1.5 * r
    return Polygon(
        [(cx + radius * math.cos(a), cy + radius * math.sin(a)) for a in VERTEX_ANGLES]
    )
