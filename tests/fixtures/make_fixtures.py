"""Implements: Grundlage fuer FA3-FA7, FA14 (Test-Fixtures).

Einmalig ausfuehrbares, eingechecktes Skript. Erzeugt deterministisch
(feste Koordinaten, kein Zufall) Mini-Testdaten < 100 KB fuer einen
Dresden-Ausschnitt (EPSG:4326). Ausfuehren mit:

    uv run python tests/fixtures/make_fixtures.py

Die erzeugten Dateien werden eingecheckt; das Skript bleibt im Repo,
damit die Fixtures bei Bedarf reproduzierbar neu erzeugt werden koennen.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from shapely.geometry import LineString, Point, Polygon

FIXTURES_DIR = Path(__file__).resolve().parent
CRS = "EPSG:4326"

# =====================================================================
# Umspannwerke (substations): 5 Punkte, Dresden-Ausschnitt
# s4: voltage < 10000 (Constraint-Verletzung szenario1)
# s5: voltage fehlt (required_fields-Verletzung szenario1), unverbunden
# =====================================================================

SUBSTATIONS = [
    {"id": "s1", "lon": 13.720, "lat": 51.040, "voltage": 110000},
    {"id": "s2", "lon": 13.740, "lat": 51.045, "voltage": 220000},
    {"id": "s3", "lon": 13.730, "lat": 51.055, "voltage": 380000},
    {"id": "s4", "lon": 13.750, "lat": 51.050, "voltage": 5000},
    {"id": "s5", "lon": 13.760, "lat": 51.060, "voltage": None},
]


def make_substations() -> gpd.GeoDataFrame:
    gdf = gpd.GeoDataFrame(
        [{"id": r["id"], "voltage": r["voltage"]} for r in SUBSTATIONS],
        geometry=[Point(r["lon"], r["lat"]) for r in SUBSTATIONS],
        crs=CRS,
    )
    return gdf


def write_substations() -> None:
    gdf = make_substations()
    gdf.to_file(FIXTURES_DIR / "substations.geojson", driver="GeoJSON")
    gdf.to_file(FIXTURES_DIR / "substations.gpkg", driver="GPKG")
    gdf.to_file(FIXTURES_DIR / "substations.shp", driver="ESRI Shapefile")


# =====================================================================
# power_lines: 4 Linien, verbinden s1-s2-s3-s4 im Ring (4 der 5 Punkte);
# s5 bleibt unverbunden (disconnected-component-Testfall FA10)
# =====================================================================


def write_power_lines() -> None:
    by_id = {r["id"]: (r["lon"], r["lat"]) for r in SUBSTATIONS}
    edges = [("s1", "s2"), ("s2", "s3"), ("s3", "s4"), ("s4", "s1")]
    gdf = gpd.GeoDataFrame(
        [{"id": f"line_{a}_{b}"} for a, b in edges],
        geometry=[LineString([by_id[a], by_id[b]]) for a, b in edges],
        crs=CRS,
    )
    gdf.to_file(FIXTURES_DIR / "power_lines.geojson", driver="GeoJSON")


# =====================================================================
# population.tif: 10x10-Raster ueber dem Dresden-Ausschnitt, Wert=100 je
# Zelle -> Summe pro vollstaendig ueberdeckter Zone bekannt (10000 gesamt)
# =====================================================================


def write_population_raster() -> None:
    size = 10
    data = np.full((size, size), 100, dtype="int32")
    # Bounding Box grosszuegig um alle Umspannwerke gelegt
    min_lon, max_lon = 13.70, 13.78
    min_lat, max_lat = 51.03, 51.07
    transform = Affine(
        (max_lon - min_lon) / size,
        0,
        min_lon,
        0,
        -(max_lat - min_lat) / size,
        max_lat,
    )
    with rasterio.open(
        FIXTURES_DIR / "population.tif",
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="int32",
        crs=CRS,
        transform=transform,
    ) as dst:
        dst.write(data, 1)


# =====================================================================
# population_nodata.tif: wie population.tif (10x10, Wert=100), aber die
# rechte Haelfte (Spalten 5-9) ist mit dem Nodata-Sentinel -200 belegt -
# fuer den zonal_stats-Nodata-Ausschluss-Test (FA8) und den Reprojektion-
# Nodata-Erhaltungstest (FA5). Summe der GUELTIGEN Zellen (linke Haelfte,
# 5x10 * 100) ist bekannt: 5000.
# =====================================================================

POPULATION_NODATA_SENTINEL = -200.0


def write_population_nodata_raster() -> None:
    size = 10
    data = np.full((size, size), 100, dtype="float64")
    data[:, 5:] = POPULATION_NODATA_SENTINEL
    min_lon, max_lon = 13.70, 13.78
    min_lat, max_lat = 51.03, 51.07
    transform = Affine(
        (max_lon - min_lon) / size,
        0,
        min_lon,
        0,
        -(max_lat - min_lat) / size,
        max_lat,
    )
    with rasterio.open(
        FIXTURES_DIR / "population_nodata.tif",
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="float64",
        crs=CRS,
        transform=transform,
        nodata=POPULATION_NODATA_SENTINEL,
    ) as dst:
        dst.write(data, 1)


# =====================================================================
# substations_container.zip: substations als Shapefile, gezippt - EIN
# Vektorformat-Kandidat (die 4 Shapefile-Nebendateien .dbf/.shx/.prj/.cpg
# zaehlen nicht als Kandidat, s. files.py _ZIP_VECTOR_EXTENSIONS) - fuer
# den Zip-Container-Happy-Path (FA4).
# =====================================================================


def write_substations_zip() -> None:
    zip_path = FIXTURES_DIR / "substations_container.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w") as archive:
        for suffix in ("shp", "shx", "dbf", "prj", "cpg"):
            src = FIXTURES_DIR / f"substations.{suffix}"
            if src.exists():
                archive.write(src, arcname=src.name)


# =====================================================================
# ambiguous_container.zip: substations als Shapefile UND als GeoJSON im
# selben Archiv - ZWEI Vektorformat-Kandidaten, fuer den Ambiguitaets-
# Fehlerfall (member noetig) und den member-Parameter-Auswahltest (FA4).
# =====================================================================


def write_ambiguous_zip() -> None:
    zip_path = FIXTURES_DIR / "ambiguous_container.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w") as archive:
        for suffix in ("shp", "shx", "dbf", "prj", "cpg"):
            src = FIXTURES_DIR / f"substations.{suffix}"
            if src.exists():
                archive.write(src, arcname=src.name)
        archive.write(
            FIXTURES_DIR / "substations.geojson", arcname="substations.geojson"
        )


# =====================================================================
# empty_container.zip: ein Archiv ohne jeden Vektorformat-Kandidaten
# (nur eine harmlose Textdatei) - fuer den Leere-Kandidatenliste-
# Fehlerfall (FA4).
# =====================================================================


def write_empty_zip() -> None:
    zip_path = FIXTURES_DIR / "empty_container.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("readme.txt", "kein Geodatenformat in diesem Archiv\n")


# =====================================================================
# multilayer.gpkg: zwei benannte Schichten (substations, power_lines) im
# selben GeoPackage - fuer den Mehrschicht-Fehlerfall ohne 'layer' und
# den layer-Parameter-Auswahltest (FA4).
# =====================================================================


def write_multilayer_gpkg() -> None:
    gpkg_path = FIXTURES_DIR / "multilayer.gpkg"
    gpkg_path.unlink(missing_ok=True)
    substations_gdf = make_substations()
    substations_gdf.to_file(gpkg_path, driver="GPKG", layer="substations")

    by_id = {r["id"]: (r["lon"], r["lat"]) for r in SUBSTATIONS}
    edges = [("s1", "s2"), ("s2", "s3"), ("s3", "s4"), ("s4", "s1")]
    lines_gdf = gpd.GeoDataFrame(
        [{"id": f"line_{a}_{b}"} for a, b in edges],
        geometry=[LineString([by_id[a], by_id[b]]) for a, b in edges],
        crs=CRS,
    )
    lines_gdf.to_file(gpkg_path, driver="GPKG", layer="power_lines")


# =====================================================================
# minimal.gml: 2 Punkte (s1, s2 aus SUBSTATIONS) im GDAL-OGR-GML-
# Ausgabedialekt (Schema, das der GML-Treiber ohne separates .xsd lesen
# kann) - von Hand geschrieben statt per gpd.to_file() erzeugt, damit
# kein GDAL-generiertes .gfs-Cache-Sidecar im Repo landet (FA4).
# =====================================================================


def write_minimal_gml() -> None:
    p1 = SUBSTATIONS[0]
    p2 = SUBSTATIONS[1]
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<ogr:FeatureCollection
     xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
     xmlns:ogr="http://ogr.maptools.org/"
     xmlns:gml="http://www.opengis.net/gml">
  <gml:boundedBy>
    <gml:Box>
      <gml:coord><gml:X>{p1["lon"]}</gml:X><gml:Y>{p1["lat"]}</gml:Y></gml:coord>
      <gml:coord><gml:X>{p2["lon"]}</gml:X><gml:Y>{p2["lat"]}</gml:Y></gml:coord>
    </gml:Box>
  </gml:boundedBy>
  <gml:featureMember>
    <ogr:minimal fid="1">
      <ogr:geometryProperty><gml:Point srsName="EPSG:4326"><gml:coordinates>{p1["lon"]},{p1["lat"]}</gml:coordinates></gml:Point></ogr:geometryProperty>
      <ogr:id>{p1["id"]}</ogr:id>
    </ogr:minimal>
  </gml:featureMember>
  <gml:featureMember>
    <ogr:minimal fid="2">
      <ogr:geometryProperty><gml:Point srsName="EPSG:4326"><gml:coordinates>{p2["lon"]},{p2["lat"]}</gml:coordinates></gml:Point></ogr:geometryProperty>
      <ogr:id>{p2["id"]}</ogr:id>
    </ogr:minimal>
  </gml:featureMember>
</ogr:FeatureCollection>
"""
    (FIXTURES_DIR / "minimal.gml").write_text(content, encoding="utf-8")


# =====================================================================
# ladesaeulen.csv: x/y-Spalten, 1 Zeile mit abgeschnittener Koordinate
# (wenige Nachkommastellen), 1 Zeile mit fehlender Spalte (leistung_kw)
# =====================================================================


def write_ladesaeulen_csv() -> None:
    lines = [
        "id,longitude,latitude,betreiber,leistung_kw",
        "l1,13.725123,51.041456,Stadtwerke,22.0",
        "l2,13.735,51.046,Stadtwerke,50.0",  # abgeschnittene Koordinate (3 Dezimalstellen)
        "l3,13.745678,51.048765,,",  # fehlende Spalte (leistung_kw leer)
    ]
    (FIXTURES_DIR / "ladesaeulen.csv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


# =====================================================================
# netz_wkt.csv: WKT-Spalte statt x/y
# =====================================================================


def write_netz_wkt_csv() -> None:
    lines = [
        "id,spannung,geom_wkt",
        "n1,110,POINT (13.720 51.040)",
        "n2,220,POINT (13.740 51.045)",
    ]
    (FIXTURES_DIR / "netz_wkt.csv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


# =====================================================================
# ladesaeulen_dialekt.csv: windows-1252-kodiert, Semikolon-getrennt,
# Dezimalkomma bei den Koordinaten, Umlaut in der Betreiber-Spalte -
# fuer den CSV-Dialekt-Test (FA14-Erweiterung: encoding/delimiter/decimal)
# =====================================================================


def write_ladesaeulen_dialekt_csv() -> None:
    lines = [
        "id;longitude;latitude;betreiber;leistung_kw",
        "l1;13,725123;51,041456;Stadtwerke München;22,0",
        "l2;13,735000;51,046000;Energieversorgung Südwest;50,0",
    ]
    (FIXTURES_DIR / "ladesaeulen_dialekt.csv").write_bytes(
        ("\n".join(lines) + "\n").encode("windows-1252")
    )


# =====================================================================
# ladesaeulen_tausender.csv: Dezimalkomma UND Punkt als Tausendertrenner
# (z. B. "1.234,5") - fuer den Tausendertrenner-Test (FA14-Erweiterung)
# =====================================================================


def write_ladesaeulen_tausender_csv() -> None:
    lines = [
        "id;longitude;latitude;betreiber;leistung_kw",
        "l1;13,725123;51,041456;Stadtwerke;1.234,5",
        "l2;13,735000;51,046000;Stadtwerke;2.500,0",
    ]
    (FIXTURES_DIR / "ladesaeulen_tausender.csv").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


# =====================================================================
# ladesaeulen.xlsx: einblaettrige Excel-Arbeitsmappe, gleicher Inhalt wie
# ladesaeulen.csv - fuer den Excel-Happy-Path (FA14-Erweiterung)
# ladesaeulen_zweiblatt.xlsx: zwei Blaetter (ladesaeulen, ladesaeulen_alt)
# - fuer den Mehrblatt-Fehlerfall und den sheet-Parameter-Auswahltest
# =====================================================================


def write_ladesaeulen_xlsx() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ladesaeulen"
    sheet.append(["id", "longitude", "latitude", "betreiber", "leistung_kw"])
    sheet.append(["l1", 13.725123, 51.041456, "Stadtwerke", 22.0])
    sheet.append(["l2", 13.735, 51.046, "Stadtwerke", 50.0])
    sheet.append(["l3", 13.745678, 51.048765, None, None])
    workbook.save(FIXTURES_DIR / "ladesaeulen.xlsx")


def write_ladesaeulen_zweiblatt_xlsx() -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "ladesaeulen"
    sheet.append(["id", "longitude", "latitude", "betreiber", "leistung_kw"])
    sheet.append(["l1", 13.725123, 51.041456, "Stadtwerke", 22.0])
    sheet.append(["l2", 13.735, 51.046, "Stadtwerke", 50.0])

    alt_sheet = workbook.create_sheet("ladesaeulen_alt")
    alt_sheet.append(["id", "longitude", "latitude", "betreiber", "leistung_kw"])
    alt_sheet.append(["l0", 13.700000, 51.030000, "Alt-Betreiber", 11.0])

    workbook.save(FIXTURES_DIR / "ladesaeulen_zweiblatt.xlsx")


# =====================================================================
# netz.sqlite + netz.sql: gleiche Mini-Tabelle (umspannwerke) als SQLite-
# Datei und als SQL-Dump
# =====================================================================


def write_netz_sql() -> None:
    sqlite_path = FIXTURES_DIR / "netz.sqlite"
    sqlite_path.unlink(missing_ok=True)
    conn = sqlite3.connect(sqlite_path)
    conn.execute("CREATE TABLE umspannwerke (id TEXT, spannung INTEGER, geom_wkt TEXT)")
    rows = [
        ("n1", 110, "POINT (13.720 51.040)"),
        ("n2", 220, "POINT (13.740 51.045)"),
        ("n3", 380, "POINT (13.730 51.055)"),
    ]
    conn.executemany("INSERT INTO umspannwerke VALUES (?, ?, ?)", rows)
    conn.commit()

    dump_lines = [
        "CREATE TABLE umspannwerke (id TEXT, spannung INTEGER, geom_wkt TEXT);"
    ]
    for row in rows:
        dump_lines.append(
            f"INSERT INTO umspannwerke VALUES ('{row[0]}', {row[1]}, '{row[2]}');"
        )
    (FIXTURES_DIR / "netz.sql").write_text(
        "\n".join(dump_lines) + "\n", encoding="utf-8"
    )

    conn.close()


# =====================================================================
# parks.geojson / residential_buildings.geojson: Mini-Datensatz fuer
# Szenario 2 (Gruenflaechen-Erreichbarkeit), E2E-Test WP 2.4.
# Gebaeude b1/b2 nah an einem Park (< 300 m), b3 mittel (< 500 m),
# b4 weit weg (> 1000 m) - deckt alle vier classify-Klassen ab.
# =====================================================================


def write_parks() -> None:
    park_a = Polygon(
        [
            (13.7300, 51.0500),
            (13.7320, 51.0500),
            (13.7320, 51.0515),
            (13.7300, 51.0515),
            (13.7300, 51.0500),
        ]
    )
    park_b = Polygon(
        [
            (13.7500, 51.0600),
            (13.7515, 51.0600),
            (13.7515, 51.0612),
            (13.7500, 51.0612),
            (13.7500, 51.0600),
        ]
    )
    gdf = gpd.GeoDataFrame(
        {"id": ["park_a", "park_b"]}, geometry=[park_a, park_b], crs=CRS
    )
    gdf.to_file(FIXTURES_DIR / "parks.geojson", driver="GeoJSON")


def write_residential_buildings() -> None:
    # ca. 150m, 450m, 800m, 1300m vom naechsten Park entfernt (grob, EPSG:4326)
    buildings = [
        {"id": "b1", "lon": 13.7310, "lat": 51.0518},
        {"id": "b2", "lon": 13.7330, "lat": 51.0522},
        {"id": "b3", "lon": 13.7360, "lat": 51.0530},
        {"id": "b4", "lon": 13.7450, "lat": 51.0560},
    ]
    gdf = gpd.GeoDataFrame(
        [{"id": b["id"]} for b in buildings],
        geometry=[Point(b["lon"], b["lat"]) for b in buildings],
        crs=CRS,
    )
    gdf.to_file(FIXTURES_DIR / "residential_buildings.geojson", driver="GeoJSON")


# =====================================================================
# sachsen_region.geojson: grobes Referenzpolygon, umschliesst alle
# obigen Punkte (fuer spatial_check-Tests, FA6)
# =====================================================================


def write_sachsen_region() -> None:
    polygon = Polygon(
        [
            (13.60, 50.95),
            (13.90, 50.95),
            (13.90, 51.15),
            (13.60, 51.15),
            (13.60, 50.95),
        ]
    )
    gdf = gpd.GeoDataFrame(
        [{"name": "Sachsen (Ausschnitt)"}], geometry=[polygon], crs=CRS
    )
    gdf.to_file(FIXTURES_DIR / "sachsen_region.geojson", driver="GeoJSON")


# =====================================================================
# Leipzig-Tramnetz (FA15 E2E, leipzig10_erreichbarkeit.yaml): 4 Knoten
# A-B-C-D in einer Reihe (500m Abstand, EPSG:4326-Approximation um
# 12.37/51.34), zzgl. ein FUENFTER, bewusst unangebundener Ortsteil
# (weiter als max_snap_m vom Netz entfernt) - deckt den NaN/Warnungs-Fall
# von shortest_path ohne zusaetzlichen Testcode ab.
# =====================================================================

LEIPZIG_TRAM_STOPS = [
    {"id": "hbf", "name": "Leipzig Hauptbahnhof", "lon": 12.3820, "lat": 51.3450},
    {"id": "markt", "name": "Markt", "lon": 12.3760, "lat": 51.3400},
    {
        "id": "wilhelm",
        "name": "Wilhelm-Leuschner-Platz",
        "lon": 12.3700,
        "lat": 51.3350,
    },
    {"id": "connewitz", "name": "Connewitz, Kreuz", "lon": 12.3640, "lat": 51.3300},
]


def write_leipzig_tram_stops() -> None:
    gdf = gpd.GeoDataFrame(
        [
            {"id": r["id"], "name": r["name"], "railway": "tram_stop"}
            for r in LEIPZIG_TRAM_STOPS
        ],
        geometry=[Point(r["lon"], r["lat"]) for r in LEIPZIG_TRAM_STOPS],
        crs=CRS,
    )
    gdf.to_file(FIXTURES_DIR / "leipzig_tram_stops.geojson", driver="GeoJSON")


def write_leipzig_tram_lines() -> None:
    by_id = {r["id"]: (r["lon"], r["lat"]) for r in LEIPZIG_TRAM_STOPS}
    edges = [("hbf", "markt"), ("markt", "wilhelm"), ("wilhelm", "connewitz")]
    gdf = gpd.GeoDataFrame(
        [{"id": f"gleis_{a}_{b}", "railway": "tram"} for a, b in edges],
        geometry=[LineString([by_id[a], by_id[b]]) for a, b in edges],
        crs=CRS,
    )
    gdf.to_file(FIXTURES_DIR / "leipzig_tram_lines.geojson", driver="GeoJSON")


def write_leipzig_ortsteile() -> None:
    # Zentren als Punkte (repraesentativ, wie to_points sie erzeugen
    # wuerde) statt echter Ortsteil-Polygone - genuegt fuer den
    # shortest_path/isochrone-E2E-Test, spart eine Polygon-Fixture.
    # 'zentrum' liegt nah an Connewitz (< 500m); 'abseits' ist bewusst
    # weit vom Netz entfernt (> max_snap_m im Beispielszenario).
    zentren = [
        {"id": "ortsteil_zentrum", "geometry": Point(12.3630, 51.3295)},
        {"id": "ortsteil_abseits", "geometry": Point(12.2000, 51.2000)},
    ]
    gdf = gpd.GeoDataFrame(
        [{"id": r["id"]} for r in zentren],
        geometry=[r["geometry"] for r in zentren],
        crs=CRS,
    )
    gdf.to_file(FIXTURES_DIR / "leipzig_ortsteile.geojson", driver="GeoJSON")


def write_leipzig_hbf() -> None:
    hbf = next(r for r in LEIPZIG_TRAM_STOPS if r["id"] == "hbf")
    gdf = gpd.GeoDataFrame(
        [{"id": "hbf", "name": hbf["name"], "railway": "station"}],
        geometry=[Point(hbf["lon"], hbf["lat"])],
        crs=CRS,
    )
    gdf.to_file(FIXTURES_DIR / "leipzig_hbf.geojson", driver="GeoJSON")


def write_nominatim_leipzig() -> None:
    # Synthetische Nominatim-Antwort im Format von nominatim_dresden.json,
    # grobes Rechteck um alle obigen Leipzig-Fixture-Koordinaten.
    payload = [
        {
            "place_id": 654321,
            "licence": "Data (c) OpenStreetMap contributors, ODbL 1.0",
            "osm_type": "relation",
            "osm_id": 62649,
            "boundingbox": ["51.15", "51.40", "12.15", "12.50"],
            "lat": "51.3397",
            "lon": "12.3731",
            "display_name": "Leipzig, Sachsen, Deutschland",
            "class": "boundary",
            "type": "administrative",
            "importance": 0.78,
            "geojson": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [12.15, 51.15],
                        [12.50, 51.15],
                        [12.50, 51.40],
                        [12.15, 51.40],
                        [12.15, 51.15],
                    ]
                ],
            },
        }
    ]
    (FIXTURES_DIR / "nominatim_leipzig.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


# =====================================================================
# FA16 (Open-Data-Konnektor): synthetische WFS-GetFeature- und
# CKAN-API-Antworten fuer offline-Unit-/E2E-Tests. 3 Spielplaetze
# (playground_1..3) nahe Connewitz (siehe Leipzig-Tram-Fixtures oben),
# damit ein spatial_check/aggregate-Beispielszenario dieselbe Region
# plausibel abdeckt.
# =====================================================================

PLAYGROUNDS = [
    {
        "id": "playground_1",
        "name": "Spielplatz Connewitz",
        "lon": 12.3650,
        "lat": 51.3305,
    },
    {
        "id": "playground_2",
        "name": "Spielplatz Wilhelm-Leuschner",
        "lon": 12.3710,
        "lat": 51.3355,
    },
    {"id": "playground_3", "name": "Spielplatz Markt", "lon": 12.3765, "lat": 51.3405},
]


def write_leipzig_playgrounds_osm() -> None:
    # OSM-Quelle kennt alle 3 (Community-Daten vollstaendiger als die
    # amtliche CKAN-Quelle, die nur 2 kennt - siehe write_ckan_fixtures) -
    # plus einen vierten, rein OSM-exklusiven Spielplatz.
    extra = {
        "id": "playground_4",
        "name": "Spielplatz Wasserturm",
        "lon": 12.3600,
        "lat": 51.3260,
    }
    all_playgrounds = PLAYGROUNDS + [extra]
    gdf = gpd.GeoDataFrame(
        [
            {"id": r["id"], "name": r["name"], "leisure": "playground"}
            for r in all_playgrounds
        ],
        geometry=[Point(r["lon"], r["lat"]) for r in all_playgrounds],
        crs=CRS,
    )
    gdf.to_file(FIXTURES_DIR / "leipzig_playgrounds_osm.geojson", driver="GeoJSON")


def write_leipzig_ortsteile_polygon() -> None:
    # EIN grobes Ortsteil-Polygon, das alle Leipzig-Tram-/Spielplatz-
    # Fixture-Koordinaten umschliesst - genuegt fuer aggregate(zones=...)
    # im FA16-Beispielszenario (leipzig_ortsteile.geojson oben ist
    # bewusst Punkt-Geometrie fuer den FA15-Anwendungsfall und daher hier
    # nicht wiederverwendet).
    polygon = Polygon(
        [(12.34, 51.31), (12.40, 51.31), (12.40, 51.36), (12.34, 51.36), (12.34, 51.31)]
    )
    gdf = gpd.GeoDataFrame(
        [{"id": "ortsteil_zentrum_sued"}], geometry=[polygon], crs=CRS
    )
    gdf.to_file(FIXTURES_DIR / "leipzig_ortsteile_polygon.geojson", driver="GeoJSON")


def write_wfs_getfeature_response() -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": r["id"], "name": r["name"]},
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            }
            for r in PLAYGROUNDS
        ],
    }
    (FIXTURES_DIR / "wfs_getfeature_response.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def write_ckan_fixtures() -> None:
    resource_show = {
        "success": True,
        "result": {
            "id": "res-spielplaetze-001",
            "format": "GeoJSON",
            "url": "https://opendata.leipzig.de/dataset/spielplaetze/resource/res-spielplaetze-001/download/spielplaetze.geojson",
        },
    }
    (FIXTURES_DIR / "ckan_resource_show.json").write_text(
        json.dumps(resource_show, indent=2), encoding="utf-8"
    )

    package_show = {
        "success": True,
        "result": {
            "id": "pkg-spielplaetze",
            "name": "spielplaetze",
            "resources": [
                {
                    "id": "res-spielplaetze-csv",
                    "format": "CSV",
                    "url": "https://opendata.leipzig.de/dataset/spielplaetze/resource/csv/download/spielplaetze.csv",
                },
                {
                    "id": "res-spielplaetze-001",
                    "format": "GeoJSON",
                    "url": "https://opendata.leipzig.de/dataset/spielplaetze/resource/res-spielplaetze-001/download/spielplaetze.geojson",
                },
            ],
        },
    }
    (FIXTURES_DIR / "ckan_package_show.json").write_text(
        json.dumps(package_show, indent=2), encoding="utf-8"
    )

    resource_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": r["id"], "name": r["name"], "amtlich": True},
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            }
            for r in PLAYGROUNDS[:2]  # amtliche Quelle kennt nur 2 von 3 (Luecke)
        ],
    }
    (FIXTURES_DIR / "ckan_resource.geojson").write_text(
        json.dumps(resource_geojson, indent=2), encoding="utf-8"
    )


def main() -> None:
    write_substations()
    write_power_lines()
    write_population_raster()
    write_population_nodata_raster()
    write_substations_zip()
    write_ambiguous_zip()
    write_empty_zip()
    write_multilayer_gpkg()
    write_minimal_gml()
    write_ladesaeulen_csv()
    write_ladesaeulen_dialekt_csv()
    write_ladesaeulen_tausender_csv()
    write_ladesaeulen_xlsx()
    write_ladesaeulen_zweiblatt_xlsx()
    write_netz_wkt_csv()
    write_netz_sql()
    write_parks()
    write_residential_buildings()
    write_sachsen_region()
    write_leipzig_tram_stops()
    write_leipzig_tram_lines()
    write_leipzig_ortsteile()
    write_leipzig_hbf()
    write_nominatim_leipzig()
    write_leipzig_playgrounds_osm()
    write_leipzig_ortsteile_polygon()
    write_wfs_getfeature_response()
    write_ckan_fixtures()
    print(f"Fixtures erzeugt in {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
