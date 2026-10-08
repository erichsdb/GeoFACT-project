"""Implements: FA65 (Default-Lizenzen der Quellarten, die OpenStreetMap-Daten liefern).

``osm`` (Overpass/PostGIS), ``region`` (Nominatim-Grenzen) und ``geocode``
(Nominatim-Orte) liefern Daten aus OpenStreetMap; diese stehen unter der Open
Database License 1.0 und verlangen die Namensnennung
"© OpenStreetMap contributors". Die drei Layer-Modelle geben diese Konstante
aus ihrem Hook ``default_license()`` zurück (NFA4: Default am Layer-Modell,
keine Liste im Kern). Eine in der YAML deklarierte ``license`` ersetzt den
Default vollständig (FA65-Vorbedingung, ausgewertet in
``LayerBase.effective_license``).

Hilfsmodul (führender Unterstrich): registriert nichts und wird von der
Erkennung (FA40) nicht gescannt."""

from __future__ import annotations

from geofact.plugin_api import License


OSM_LICENSE = License(
    name="ODbL-1.0",
    attribution="OpenStreetMap contributors",
    url="https://www.openstreetmap.org/copyright",
)
"""Lizenz aller Daten aus OpenStreetMap (Overpass, PostGIS-Import, Nominatim)."""
