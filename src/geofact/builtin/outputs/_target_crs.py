"""Implements: FA55 (Ziel-CRS je Ausgabe: gemeinsame Prüfung der Option ``crs``).

Hilfsmodul der Ausgabeformate ``geojson``, ``csv`` und ``geotiff`` (registriert
nichts, wird von der Erkennung nicht gescannt)."""

from __future__ import annotations

from typing import Optional

from pyproj import CRS


def check_output_crs(value: Optional[str]) -> Optional[str]:
    """Die Option ``crs`` einer Ausgabe muss pyproj bekannt sein. None bleibt None
    (Default des Formats)."""
    if value is None:
        return None
    try:
        CRS.from_user_input(value)
    except Exception as exc:  # noqa: BLE001 - pyproj meldet CRSError oder TypeError
        raise ValueError(f"unbekanntes CRS '{value}' (pyproj: {exc})") from None
    return value
