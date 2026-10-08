# Beispieltabellen

Kleine, eingecheckte Auszüge offener Daten. Sie dienen als Quelle der
Beispiel-Szenarien und erscheinen in der Web-Demo ohne Upload unter
„Beispieldaten“ (FA90, beschrieben in
`backend/geofact_web/data/sample_sources.yaml`). Stand aller Abrufe:
08.10.2026.

## Statistik ohne Koordinaten (Trenner `;`)

Diese Tabellen tragen keine Geometrie. Sie werden als `source: table` ohne
`geometry`-Block eingebunden und per `attribute_join` an Verwaltungsgrenzen
gehängt. Schlüssel ist der NUTS-Code (`nuts`), den der BKG-WFS im
gleichnamigen Attribut führt; `bip_bundeslaender_2025.csv` hat nur den Namen.

| Datei | Inhalt | Quelle, Lizenz |
|---|---|---|
| `bip_bundeslaender_2025.csv` | Bruttoinlandsprodukt je Bundesland 2025 (Mio. Euro) | Destatis VGRdL, dl-de/by-2-0 |
| `bip_kreise_2023.csv` | BIP gesamt und je Einwohner, alle 400 Kreise, 2023 | Eurostat `nama_10r_3gdp`, CC BY 4.0 |
| `einkommen_bundeslaender_2023.csv` | verfügbares Einkommen der privaten Haushalte je Einwohner (Euro), 2023 | Eurostat `nama_10r_2hhinc`, CC BY 4.0 |
| `erwerbslosenquote_bundeslaender_2025.csv` | Erwerbslosenquote der 15- bis 74-Jährigen (Prozent), 2025 | Eurostat `lfst_r_lfu3rt`, CC BY 4.0 |
| `bundestagswahl_2025_bundeslaender.csv` | Wahlbeteiligung und Zweitstimmenanteile je Bundesland (Prozent), Bundestagswahl 23.02.2025, endgültiges Ergebnis | Die Bundeswahlleiterin, dl-de/by-2-0 |
| `inkar_kreise_2025.csv` | 20 Indikatoren der Raumbeobachtung für alle 400 Kreise, je der jüngste Wert | INKAR 2025 (BBSR), dl-de/by-2-0 |

Die Erwerbslosenquote folgt der Arbeitskräfteerhebung und ist nicht die
Arbeitslosenquote der Bundesagentur für Arbeit. In der Wahltabelle fasst
`union_prozent` CDU und CSU zusammen.

Die INKAR-Tabelle hat den fünfstelligen Kreisschlüssel `ags` statt des
NUTS-Codes (Gebietsstand 31.12.2023); er entspricht dem Attribut `ags` der
BKG-Kreisgrenzen. Das Jahr steht im Spaltennamen: `angebotsmiete_eur_je_m2_2024`, `baulandpreis_eur_je_m2_2022`, `wohnflaeche_m2_je_einwohner_2023`, `arbeitslosenquote_prozent_2023`, `sgb2_quote_prozent_2023`, `kinderarmut_je_100_unter_15_2023`, `kaufkraft_eur_je_einwohner_2023`, `hausaerzte_je_10000_einwohner_2022`, `krankenhausbetten_je_1000_einwohner_2022`, `schulabgaenger_ohne_abschluss_prozent_2023`, `pendlersaldo_je_100_beschaeftigte_2023`, `pkw_je_1000_einwohner_2023`, `gigabit_haushalte_prozent_2023`, `fahrzeit_oberzentrum_min_2021`, `fahrzeit_autobahn_min_2024`, `nah_supermarkt_prozent_2023`, `nah_apotheke_prozent_2023`, `nah_hausarzt_prozent_2023`, `nah_grundschule_prozent_2023`, `nah_haltestelle_prozent_2022`.
Die `nah_*`-Spalten nennen den Anteil der Einwohner mit höchstens 1000 m
Luftlinie zur nächsten Einrichtung; Baulandpreise fehlen für 20 Kreise,
die Angebotsmiete ist auf ganze Euro gerundet.

## Punktdaten mit Koordinaten (Trenner `,`)

Diese Tabellen bringen `lon` und `lat` (EPSG:4326) mit und ergeben mit
`geometry: {x_column: lon, y_column: lat, crs: "EPSG:4326"}` direkt einen
Punkt-Layer.

| Datei | Inhalt | Quelle, Lizenz |
|---|---|---|
| `unfaelle_sachsen_2024.csv` | 12 847 Straßenverkehrsunfälle mit Personenschaden in Sachsen, 2024 | Unfallatlas, Statistische Ämter des Bundes und der Länder, dl-de/by-2-0 |
| `ladesaeulen_sachsen_2026.csv` | 3 819 öffentliche Ladeeinrichtungen in Betrieb in Sachsen, Stand 01.09.2026 | Ladesäulenregister der Bundesnetzagentur, CC BY 4.0 |
| `zensus2022_gitter_1km_sachsen.csv` | 11 499 bewohnte 1-km-Gitterzellen in Sachsen als Mittelpunkte: Einwohner, Durchschnittsalter, Nettokaltmiete | Zensus 2022, Statistische Ämter des Bundes und der Länder, dl-de/by-2-0 |
| `pegelstationen_2026.csv` | 729 Pegel der Bundeswasserstraßen mit Gewässer und Flusskilometer, bundesweit | Pegelonline (WSV), dl-de/zero-2-0 |

Spalten der Unfalltabelle:

- `monat` 1–12, `stunde` 0–23, `wochentag` 1 = Sonntag bis 7 = Samstag
- `kategorie` 1 = Unfall mit Getöteten, 2 = mit Schwerverletzten, 3 = mit Leichtverletzten
- `rad`, `pkw`, `fuss`, `krad` 1 = Fahrrad, Pkw, Fußgänger bzw. Kraftrad beteiligt, sonst 0

Spalten der Ladesäulentabelle: `art` (`normal` oder `schnell`),
`ladepunkte` (Anzahl), `leistung_kw` (Nennleistung der Einrichtung),
`jahr_inbetriebnahme`, `ort`, `kreis`. Das Register enthält nur gemeldete
Einrichtungen; der tatsächliche Bestand ist größer.

Spalten des Zensus-Gitters: `einwohner`, `durchschnittsalter` (Jahre),
`miete_eur_je_m2` (durchschnittliche Nettokaltmiete), `wohnungen_vermietet`.
Die Miete fehlt in Zellen mit zu wenigen Mietwohnungen (Geheimhaltung);
ein Punkt steht für die Mitte einer Zelle von 1 km Kantenlänge.

## Flächen

`../geodaten/bundestagswahl_2025_wahlkreise.geojson` enthält die 299
Wahlkreise der Bundestagswahl 2025 mit Wahlbeteiligung, Zweitstimmenanteilen
und `staerkste_partei` (Die Bundeswahlleiterin, dl-de/by-2-0; Geometrie
© GeoBasis-DE / BKG, auf 700 m vereinfacht). Einbindung als `source: file`.

Dieselben Eurostat-Kennzahlen gibt es ohne Datei direkt per URL, siehe
Eintrag `eurostat` im API-Katalog (`backend/geofact_web/data/api_catalog.yaml`).
