"""GeoFACT Web-Demo-Backend (FastAPI).

Dünne HTTP-Schicht über dem geofact-Kern: Datenquellen-Katalog,
Upload, LLM-gestützte Konfig-Generierung, Validierung, Ausführung mit
Live-Fortschritt (SSE) und Per-Node-Ergebnissen. Bewusst dünn gehalten
(Adapter-Muster), damit Änderungen am Framework nicht in die HTTP-
Schicht durchschlagen - der Kern wird in-process aufgerufen.
"""

__version__ = "0.1.0"
