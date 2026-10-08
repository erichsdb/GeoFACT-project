# Provenienz und Lizenzen im RDF-Export (FA73, FA74)

Stand: 03.10.2026. Maßgeblich ist der Code (`src/geofact/builtin/outputs/rdf.py`,
`src/geofact/core/provenance.py`, `src/geofact/engine/licenses.py`,
`src/geofact/support/dalicc.py`); dieses Dokument beschreibt das Vokabular und zeigt
eine Abfrage.

## Namensräume

| Präfix | IRI | Inhalt |
|---|---|---|
| `geofact:` | `https://geofact.example.org/resource/` | Ressourcen: Features, Datensätze, Quellen, Aktivitäten, Lauf |
| `geofactprop:` | `https://geofact.example.org/property/` | Attribute der Features (eine Eigenschaft je Spalte, unverändert seit FA13) |
| `geofactvocab:` | `https://geofact.example.org/vocab/` | Begriffe von Lauf und Herkunft (`operation`, `parameters`, `sourceType`, …) |
| `geo:` | GeoSPARQL | `geo:Feature`, `geo:hasGeometry`, `geo:asWKT` |
| `dcat:`, `dcterms:` | W3C DCAT, Dublin Core | Datensätze, Lizenzen, Namensnennung, Quellen |
| `prov:` | W3C PROV-O (2013) | Entitäten, Aktivitäten, Agent, Zeitpunkte |

`geofactvocab:` ist bewusst von `geofactprop:` getrennt: eine Ergebnisspalte, die
zufällig `operation` heißt, kollidiert nie mit dem Vokabular der Herkunft.

## Was der Export schreibt

```text
geofact:<source>/<i>              a geo:Feature ; dcterms:isPartOf geofact:dataset/<source>     (FA13/FA65)
geofact:dataset/<source>          a dcat:Dataset, prov:Entity ;
                                  dcterms:license, dcterms:rights …                              (FA65 bzw. FA73)
                                  prov:wasGeneratedBy geofact:activity/<source> ;               (Schritt)
                                  geofactvocab:featureCount n ;
                                  geofactvocab:statistic [ geofactvocab:column "x" ;
                                      geofactvocab:min … ; geofactvocab:max … ; geofactvocab:mean … ] .
geofact:activity/<step>           a prov:Activity ; dcterms:identifier "<step>" ;
                                  geofactvocab:operation "buffer" ;
                                  geofactvocab:parameters "{…}"^^rdf:JSON ;
                                  prov:used <Eingang> ; geofactvocab:run geofact:run/<Szenario>/<Lauf-ID> ;
                                  geofactvocab:warning "Kategorie: Text" .
geofact:entity/<step>             a prov:Entity ; prov:wasGeneratedBy …; prov:wasDerivedFrom <Eingang> .
geofact:source/<layer>            a prov:Entity, dcat:Dataset ; dcterms:identifier "<layer>" ;
                                  geofactvocab:sourceType "osm" ;
                                  geofactvocab:parameters "{\"tags\": […]}"^^rdf:JSON ;
                                  dcterms:license, dcterms:rights ;
                                  geofactvocab:sourceCrs "EPSG:4326" ; geofactvocab:featureCount n ;
                                  geofactvocab:bounds "[minx, miny, maxx, maxy]"^^rdf:JSON ;
                                  geofactvocab:boundsCrs "EPSG:32633" .
geofact:run/<Szenario>/<Lauf-ID> a prov:Activity, geofactvocab:Run ; dcterms:title ;
                                  geofactvocab:region ; geofactvocab:workingCrs ;
                                  prov:startedAtTime, prov:endedAtTime ;
                                  prov:wasAssociatedWith geofact:agent/geofact .
geofact:agent/geofact             a prov:SoftwareAgent ; geofactvocab:version "0.1.0" .
```

Entscheidungen:

- **Parameter als ein JSON-Literal** (`rdf:JSON`, Schlüssel sortiert): Parameter
  sind oft verschachtelt (OSM-Tag-Listen, Spaltenzuordnungen); ein Literal je
  Schlüssel würde diese Struktur zerreißen. Gesucht wird mit `CONTAINS` bzw. nach
  dem Laden mit einem JSON-Parser.
- **Quell-Parameter** sind die deklarierten Felder des Layers ohne `id`, `source`
  und `license` und ohne leere Felder – also z. B. `tags` (OSM), `path` so, wie er
  in der YAML steht, `url` (WFS/REST).
- **Laufmetriken** sind klein gehalten: Objektzahl und je numerischer Spalte
  (ohne Wahrheitswerte) Minimum, Maximum, Mittelwert, NaN ausgelassen.
- **Determinismus:** bis auf den Laufknoten (Lauf-IRI mit UUID, `prov:startedAtTime`/
  `prov:endedAtTime`) sind zwei Läufe derselben Konfiguration graph-isomorph (Tests
  `test_fa74_graph_is_deterministic_except_the_run_node`,
  `test_fa74_run_uri_is_unique_per_run_and_dataset_uris_stay_stable`). Datensatz-,
  Schritt- und Quell-IRIs bleiben stabil.
- **Nur additiv:** mit `provenance: true` (Default) kommen Tripel hinzu, keine
  FA13/FA65-Tripel ändern sich (`test_fa74_provenance_triples_are_only_additive_and_can_be_switched_off`).
  Für eine Regressionsprüfung bedeutet das: TTL
  ist isomorph, wenn man die FA74-Tripel (alles mit `prov:`- oder `geofactvocab:`-Prädikat,
  die Quell- und Laufknoten sowie `dcterms:isPartOf` auf einen sonst lizenzlosen
  Datensatz) herausnimmt. `provenance: false` schreibt den Graphen wie vor FA74.
- **Lazy Loading bleibt sichtbar:** nur Layer und Schritte auf dem Weg zum
  exportierten Knoten erscheinen; ein nie geladener Layer fehlt.

## Ausgabelizenz (FA73)

Mit `output[i].license` oder `scenario.output_license` trägt das Ergebnis die vom
Nutzer festgelegte Lizenz (`dcterms:license`) und die Zeile
`Lizenz dieses Ergebnisses: … (vom Nutzer festgelegt, ungeprüft|geprüft am …)` als
`dcterms:rights`. Die Lizenzen der Quellen stehen dann an den Quell-Datensätzen
`geofact:source/<layer>`, auf die das Ergebnis mit `dcterms:source` und
`prov:wasDerivedFrom` verweist. Ohne Ausgabelizenz bleiben die Quelllizenzen wie
unter FA65 am Ergebnis.

## Beispielabfrage: Welche OSM-Tags und Parameter erzeugten Feature X?

Die Abfrage geht vom Feature zu seinem Datensatz und von dort über
`prov:wasDerivedFrom*` zu allen Vorgängern – Quellen mit Quellart und Parametern,
Schritte mit Operation und Parametern. Sie läuft in jedem SPARQL-1.1-Prozessor (hier
mit rdflib: `Graph().parse("punkte_im_stadtteil.ttl").query(...)`); der Test
`test_fa74_documented_sparql_finds_the_osm_tags_and_step_parameters_of_a_feature`
führt genau diesen Text aus.

```sparql
PREFIX prov: <http://www.w3.org/ns/prov#>
PREFIX dcterms: <http://purl.org/dc/terms/>
PREFIX geofactvocab: <https://geofact.example.org/vocab/>

SELECT ?kind ?node ?block ?parameters WHERE {
  <https://geofact.example.org/resource/schnitt/0> dcterms:isPartOf ?dataset .
  ?dataset prov:wasDerivedFrom* ?entity .
  {
    ?entity geofactvocab:sourceType ?block ;
            geofactvocab:parameters ?parameters ;
            dcterms:identifier ?node .
    BIND("Quelle" AS ?kind)
  } UNION {
    ?entity prov:wasGeneratedBy ?activity .
    ?activity geofactvocab:operation ?block ;
              geofactvocab:parameters ?parameters ;
              dcterms:identifier ?node .
    BIND("Schritt" AS ?kind)
  }
}
ORDER BY ?kind ?node
```

Für das Schaufenster `examples/chemnitz_datenquellen_lizenzen.yaml` (Feature
`…/resource/punkte_im_stadtteil/0`) liefert sie u. a. die Quelle `stadtteile`
(`osm`, `{"tags": [{"admin_level": "9", "boundary": "administrative", …}]}`), die
Quelle `sentinel2` (`stac`, Collection, Datum, Kachel) und die Schritte `ndvi`,
`gruen` (`raster_calc` mit Ausdruck), `anteil_stadtteile` (`zonal_stats`) und
`punkte_im_stadtteil` (`spatial_join`, `predicate: within`).

Lizenzen aller Quellen eines Ergebnisses:

```text
SELECT ?quelle ?lizenz WHERE {
  <https://geofact.example.org/resource/dataset/punkte_im_stadtteil> prov:wasDerivedFrom* ?quelle .
  ?quelle geofactvocab:sourceType ?typ ; dcterms:license ?lizenz .
}
```

## Lizenzkompatibilität (FA73)

`geofact licenses <yaml>` (bzw. `geofact validate --check-licenses`,
`geofact.api.check_licenses`, Web `POST /api/config/licenses`) prüft je Ausgabe die
Vereinigung der Quelllizenzen zusammen mit der Ausgabelizenz gegen
`POST https://api.dalicc.net/compatibilitycheck/`. Jede Kennung wird vorher mit der
Lizenzliste (`GET /licenselibrary/list?limit=500`) abgeglichen, weil die API eine
unbekannte Kennung stillschweigend als konfliktfrei meldet. Lizenzen ohne
DALICC-Entsprechung (CC-BY-SA-3.0-IGO von Copernicus, Datenlizenz Deutschland,
GeoNutzV) sind „nicht prüfbar“, nie „vereinbar“. Antworten liegen unter
`<GEOFACT_SNAPSHOT_DIR>/dalicc/`; ein späterer Lauf liest nur diese Ablage und
vermerkt „geprüft am …“ an der Ausgabelizenz. DALICC vergleicht die Aussagen der
Lizenzen paarweise (z. B. meldet es CC0-1.0 gegen ODbL-1.0 als Konflikt um
„ChangeLicense“); das Ergebnis ist eine Prüfhilfe, keine Rechtsauskunft.
