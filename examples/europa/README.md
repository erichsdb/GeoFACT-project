# Resilienz des europäischen Übertragungsnetzes

`europa_netz_resilienz.yaml` überträgt Szenario 1 (kritische Knoten des
Hochspannungsnetzes in Sachsen) auf ganz Europa: Welche Höchstspannungsknoten
(220–750 kV) liegen zugleich zentral im Netz und nahe an vielen Menschen?

## Daten beschaffen

Beide Quellen liegen im git-ignorierten Ordner `examples/data/`.

1. **PyPSA-Eur-Netz v0.7** (rund 21 MB, ODbL 1.0) – Skript mit Prüfsummen:

   ```bash
   uv run python scripts/fetch_europa_data.py
   ```

   Es lädt `buses.csv`, `lines.csv`, `transformers.csv`, `links.csv` und
   `converters.csv` aus dem Zenodo-Datensatz
   [18619025](https://zenodo.org/records/18619025) (Xiong et al. 2025,
   „Prebuilt Electricity Network for PyPSA-Eur based on OpenStreetMap Data“,
   veröffentlicht 12.02.2026) nach `examples/data/europa/`.

2. **GHS-POP R2023A, 100 m, global** (6,5 GB, CC BY 4.0) – dieselbe Datei wie in
   Szenario 1: `GHS_POP_E2025_GLOBE_R2023A_54009_100_V1_0.tif` aus dem
   [GHSL-Download](https://human-settlement.emergency.copernicus.eu/download.php?ds=pop)
   (Epoche 2025, Auflösung 100 m, Mollweide) nach `examples/data/`. Die
   mitgelieferten Übersichten (`.ovr`) werden nicht gebraucht und nie gelesen.

## Ausführen

```bash
uv run geofact validate examples/europa/europa_netz_resilienz.yaml
uv run geofact run examples/europa/europa_netz_resilienz.yaml --out output/europa
```

Ergebnis: die 25 kritischsten Umspannwerke als Karte (`.html`), GeoJSON, CSV
und RDF (GeoSPARQL) samt `ATTRIBUTION.txt`. Referenzlauf (03.10.2026, Windows-
Laptop): rund 5,5 Minuten, davon rund 4 Minuten exakte, längengewichtete
Betweenness auf 6 863 Knoten und gut 1 Minute für das Bevölkerungsraster
(lesen bei 1 km aus 100 m, Umprojektion nach EPSG:3035); Spitzenspeicher
rund 2,8 GB.

## Was das Beispiel zeigt

- **Kantenliste statt Fangen (FA76).** PyPSA-Eur liefert die Topologie
  ausdrücklich (`bus0`/`bus1`). `edge_list_network` übernimmt sie exakt:
  Leitungen, Transformatoren, HGÜ-Verbindungen und Umrichter (zusammengeführt
  mit `collect`) ergeben ein einziges zusammenhängendes Netz. Mit
  `build_network` (Linienenden am nächsten Knoten fangen) zerfällt dasselbe Netz
  in über hundert Teile, weil mehrere Spannungsebenen eines Umspannwerks auf
  derselben Koordinate liegen und Transformatoren keine Linien sind.
- **Feldbegrenzer (FA14).** Die CSVs umschließen WKT mit Hochkommas:
  `quotechar: "'"`.
- **Raster in Zielauflösung (FA77).** Europa bei 100 m wären rund 1,6 Mrd.
  Zellen; `resolution_m: 1000` mit `aggregation: sum` summiert je 10 × 10
  Zellen aus der vollen Auflösung (Summe erhalten, 815 Mio. Einwohner im
  Rechteck).
- **Deklariertes Domänenwissen.** Die Bevölkerung geht logarithmiert in die
  Rangliste ein (`calculate_field`, `log(einwohner_20km + 1)`), Gewichte 0,5/0,5;
  roh würde die Rangliste zur Liste der größten Ballungsräume. Mehrere
  Spannungsebenen eines Umspannwerks erscheinen einmal (`deduplicate` nach `tags`).

## Grenzen

Keine Lastflüsse und keine Leitungskapazitäten: Betweenness misst die Lage im
Netz, nicht die Last. Island und Zypern fehlen im Datensatz, Teile von Nicht-EU-
Ländern (UA, TR, MA, …) sind enthalten. Eine Länderauswertung mit ENTSO-E-
Statistiken und GISCO-Ländergrenzen ist nicht Teil des Beispiels: die GISCO-
Grenzen ließen sich auf dem Referenzrechner wegen einer TLS-Prüfung nicht aus
Python laden.
