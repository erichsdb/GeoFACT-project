"""Darstellung von Laufergebnissen für die Web-Oberfläche (nicht Teil des Kerns).

serialize.py   Ergebnis (Vektor/Raster/Graph) -> JSON für die Karten-/Tabellenansicht
view_columns.py  welche Attributspalten die Ansicht zeigt (Spaltenbudget)

Diese Module lagen bis zum Umbau (FA45) im Kernpaket, obwohl nur das Backend sie
nutzt. Sie lesen den Kern ausschließlich über ``geofact.api``."""
