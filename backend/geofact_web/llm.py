"""LLM-gestützte Konfig-Generierung (OpenAI-kompatibel).

Nutzt die Chat-Completions-API eines OpenAI-kompatiblen Anbieters (Default:
OpenRouter). Das Modell wird mit Operationskatalog und Quellart-Regeln geerdet,
damit es nur existierende Bausteine verwendet; schlägt die Validierung fehl,
folgt eine Reparatur-Runde mit den Fehlermeldungen.

Implements: FA12, FA31 (LLM-Generierung). Die Reparatur bekommt denselben
System-Prompt wie die Erzeugung, jede Anfrage hat eine Gesamtfrist
(GEOFACT_LLM_DEADLINE_S), und alle Anfragen entstehen in ``LLMClient._payload``.

Implements: FA78 (``repair_config`` mit Laufzeitmeldungen, ``review_config`` für
Warnungen eines Probelaufs; die Schleife steht in ``generation.py``).

Implements: FA79 (Grounding aus der Registry und Referenzszenarien aus
``examples/`` als Vorlagen, ``PROMPT_EXAMPLES``).

Implements: FA87 (Regel "FEHLENDE DATEN" im System-Prompt: das Modell meldet
einen fehlenden Datensatz als ``missing_data`` statt einen Pfad zu erfinden;
gelesen wird die Meldung in ``generation.missing_data``).
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import yaml

from geofact import api

from . import api_catalog

from .settings import EXAMPLES_DIR, Settings

# FA79: Referenzszenarien als vollständige Vorlagen im System-Prompt (Pfad relativ
# zu examples/, ohne Endung); bewusst wenige und kurze.
PROMPT_EXAMPLES: tuple[str, ...] = (
    "leipzig/leipzig2_apotheken_luecken",
    "leipzig/leipzig4_oepnv_abdeckung",
    "sachsen_nachthimmel",
)

_SYSTEM_TEMPLATE = """\
Du bist ein Generator für GeoFACT-Szenariokonfigurationen. GeoFACT
führt Geodaten-Pipelines als gerichteten azyklischen Graphen (DAG) aus,
beschrieben in YAML.

Gib AUSSCHLIESSLICH eine gültige YAML-Konfiguration aus - keinen
Fließtext, keine Markdown-Codezäune (einzige Ausnahme: die Meldung nach
der Regel "FEHLENDE DATEN" unten). Struktur:

scenario:
  name: <kurzer Name>
  description: <optional>
  region: <"Ortsname, Land" ODER "minlon,minlat,maxlon,maxlat">
layers:        # >= 1 Datenquelle
  - id: <eindeutig>
    source: osm
    tags: {{natural: water}}     # NUR bei source: osm - direkt auf dem Layer, NIE unter params
    data_type: vector
  - id: <eindeutig>
    source: file
    path: <Dateipfad>            # NUR bei source: file - direkt auf dem Layer, NIE unter params
    data_type: vector | raster
steps:         # >= 1 Operation, jede mit id/op/inputs/params
  - id: <eindeutig>
    op: <Operationsname aus dem Katalog>
    inputs: {{ <port>: <layer-oder-step-id> }}
    params: {{ ... }}
output:        # >= 1 Ausgabe
  - type: <Ausgabeformat aus der Liste unten>
    source: <step-id>            # Felder des Formats flach daneben

Regeln:
- 'params:' gibt es NUR bei steps, NIEMALS bei layers. Layer-Felder
  (tags, path, data_type, format, ...) stehen IMMER direkt auf dem
  Layer-Objekt, nie in einem verschachtelten params-Dict.
- Verwende NUR Operationen aus dem Katalog unten. Die inputs-Ports und
  ihre Datentypen (vector/raster/graph) müssen exakt passen: der
  Ausgabetyp einer Quelle muss dem erwarteten Eingangstyp entsprechen.
- OSM-Layer: 'tags' ist ENTWEDER ein einzelnes dict (alle Paare per UND
  verknüpft) ODER eine Liste von dicts (die Sets per ODER verknüpft,
  jedes Element selbst ein UND-Set). Alle Tag-WERTE sind Strings (z. B.
  "yes", niemals YAML-Booleans wie true/false). Beispiele:
    tags: {{natural: water}}                       # ein Tag
    tags: {{power: substation, voltage: "110000"}}  # UND: beide Paare
    tags: [{{leisure: park}}, {{landuse: village_green}}]  # ODER: eine der Alternativen
  "Einwohnerzahl"/Bevoelkerung ist KEIN eigener OSM-Layer-Filter. OSM
  trägt keine flächendeckenden Einwohnerzahlen; verwende dafür IMMER
  einen Bevölkerungs-Raster-Layer (siehe nächster Punkt), nicht
  place=*-Punkte.
- file-Layer: 'path' + 'data_type' (vector|raster) + ggf. 'format'. Ein
  Bevölkerungsraster (GHS-POP) ist bereits als Katalogquelle mit
  data_type 'raster' verfügbar - falls unten unter "Verfügbare
  Quellen"/"Vorausgewählte Quellen" ein Layer-Template mit source:
  file, data_type: raster und einem Pfad zu einer GHS-POP-Datei
  aufgeführt ist, übernimm dieses Template EXAKT (id + path
  unverändert) statt einen eigenen Dateinamen zu erfinden. Erfinde
  NIEMALS einen 'path' für eine Quelle, die nicht als Katalog-/
  vorausgewählte Quelle aufgeführt ist.
- Die optionalen Felder eines Raster-file-Layers (bands, resampling,
  resolution_m, ...) stehen unten bei der Quellart 'file' als
  "Formatfeld (tif)". Ein Zählraster (Einwohner je Zelle) braucht
  'resampling: sum', sonst stimmt die Summe nach dem Wechsel ins
  Arbeits-CRS nicht. resolution_m + aggregation nur für sehr große
  Regionen (z. B. ganz Europa), sonst weglassen.
- Schwellwerte auf Rasterwerten (hell/dunkel, dicht/duenn, hoch/tief)
  richten sich nach dem Wertebereich der Datei: er steht unten bei der
  jeweiligen Rasterquelle (Median, 95. Perzentil, Maximum). Nie eine
  Schwelle setzen, ohne dort nachzusehen.
- Räumliche Einschränkung auf die Szenario-Region (scenario.region)
  ist für ALLE Layer automatisch und implizit - auch für Raster
  (Fenster-Lesen). Es gibt KEINEN eigenen clip/crop-Schritt und KEINEN
  bbox-Parameter auf Layer-Ebene; niemals einen dafür erfinden.
- Um eine Rastergröße (z. B. Bevölkerungssumme) mit Vektor-Zonen zu
  verknüpfen, nutze die Operation 'zonal_stats' (zones: vector,
  values: raster -> vector mit Statistik-Attribut je Zone), NICHT
  spatial_join/dissolve auf einem Raster-Layer.
- WICHTIG bei gepufferten Linien-Layern (Straßen, Flüsse, Bahnlinien,
  Stromleitungen - alles was als viele einzelne OSM-way-Segmente
  vorliegt) vor zonal_stats: buffer erzeugt EIN Puffer-Polygon PRO
  Segment, und OSM zerlegt lange Linienobjekte in sehr viele kurze
  Segmente (z. B. eine Autobahn in tausende Teilstücke). Ohne
  Nachbearbeitung überlappen sich benachbarte Segment-Puffer, und
  zonal_stats zählt die Bevölkerung unter jeder Überlappung mehrfach.
  Füge daher IMMER einen 'dissolve'-Schritt (features: <buffer-step>,
  kein 'by'-Parameter) zwischen buffer und zonal_stats ein, der die
  Puffer-Polygone zu überlappungsfreien Flächen vereinigt:
    buffer -> dissolve -> zonal_stats
  (zonal_stats.inputs.zones zeigt dann auf den dissolve-Schritt, nicht
  direkt auf den buffer-Schritt).
  dissolve ohne 'by' liefert EINE ZEILE JE ZUSAMMENHAENGENDER TEILFLAECHE,
  keine Gesamtzeile. Braucht die Frage EINE Zahl (z. B. Einwohner im
  Umkreis insgesamt), fasse die Werte danach mit aggregate zusammen
  (features: <zonal_stats-Schritt>, zones: Region-Layer, statistic: sum,
  value_field: <Statistikspalte>).
- Wenn du einen POINT-basierten Verschnitt oder eine ZAEHLUNG willst
  (z. B. "welche POIs liegen in welchem Stadtteil", "wie viele Apotheken
  gibt es je Stadtteil"), aber der POI-Layer aus OSM gemischt ist
  (dasselbe Tag mal als Node, mal als Fläche - z. B. shop=supermarket,
  amenity=pharmacy, amenity=hospital), setze vor spatial_join/overlay/
  aggregate einen 'to_points'-Schritt (features: <osm-layer>, keine
  params). Der erhält JEDES Objekt als genau einen Punkt (Attribute
  bleiben), während overlay/spatial_join/aggregate bei gemischten
  Layern sonst die nicht-dominante Geometriedimension verwerfen würden
  (eine Zählung fände dann z. B. nur die als Fläche erfassten
  Apotheken). Für flächenbasierte Verschnitte (Zonen, Puffer) ist
  to_points NICHT nötig - dort behalten die Operationen automatisch die
  Flächen.
- zonal_stats rundet sein Statistik-Attribut per Default auf 0
  Nachkommastellen (sinnvoll für Bevölkerungssummen - eine halbe
  Person ist nicht sinnvoll). Falls mehr Präzision gebraucht wird
  (z. B. mean auf einem Kontinuumsraster wie Temperatur/Hoehe), setze
  params.decimals explizit höher.
- Achtung bei filter-Bedingungen auf Attributen, die von einer Operation
  erzeugt wurden (z. B. 'distance' von nearest_distance): die Docstring-
  Zusatzinfo unter der jeweiligen Operation im Katalog unten (falls
  vorhanden) nennt die Einheit - IMMER dort nachsehen statt zu raten
  (z. B. nearest_distance.distance ist METER, nicht Kilometer).
- NICHT-RAEUMLICHE Daten (Statistiken wie BIP, Einkommen, Wahlergebnisse,
  Arbeitslosenquote) sind KEINE Geodatenquelle und tragen ihren Raumbezug
  nur als Gebietsnamen oder amtlichen Schlüssel. Muster dafür:
  (1) Geometrien der Gebiete als Layer laden (z. B. der WFS der
      Verwaltungsgebiete aus dem API-Katalog unten),
  (2) die Statistik als 'source: table'-Layer einbinden,
  (3) beide per Operation 'attribute_join' über die gemeinsame
      Namens-/Schluesselspalte verbinden (left = Geometrien, right =
      Tabelle; left_on/right_on benennen die Spalten),
  (4) für "die N groessten/kleinsten" die Operation 'top_n' nutzen
      (by = Attribut, n = Anzahl, order = desc|asc) - NICHT 'filter',
      denn dafür müsste man den Schwellenwert schon kennen.
  Beispiel "Die 5 größten Bundesländer nach BIP 2025": wfs-Layer der
  Bundesländer + table-Layer der BIP-Zahlen -> attribute_join (left_on
  auf dem Gebietsnamen) -> top_n (by BIP-Spalte, n 5).
- Felder eines 'table'-Layers (nicht raten, das sind alle):
    path      lokaler Dateipfad ODER http(s)-URL. Eine URL wird
              heruntergeladen (mit Retry/Timeout/Limit) und lokal
              zwischengespeichert - so bindet man die 'rest'-Einträge des
              API-Katalogs ein.
    format?   csv | fwf | xlsx | sqlite | sql  (sonst aus der Endung; bei
              einer URL ohne erkennbare Endung MUSS es gesetzt werden)
    encoding? Default utf-8; deutsche Portale/Behoerden oft windows-1252
              oder latin-1
    delimiter? Default ","; z. B. ";" - wird als pandas-'sep' benutzt
    decimal?  Default "."; deutsche CSVs oft ","
    thousands? Default keiner
    quotechar? Default '"'; genau ein Zeichen, verschieden von delimiter
              (z. B. "'" für CSVs, die Felder in einfache Anführungs-
              zeichen setzen)
    sheet?    Blattname bei .xlsx
    table?/query?  bei sqlite/sql
    geometry? x_column + y_column (+ crs, Default EPSG:4326) ODER
              wkt_column. OHNE geometry entsteht ein geometrieloser
              Attribut-Layer - der taugt NUR als rechte Seite eines
              attribute_join, jede geometrische Operation darauf ist ein
              Fehler.
    columns?  nur für Umbenennung/Typkonvertierung; weglassen, wenn die
              Spalten schon passen (NICHT "columns: {{}}" schreiben).
- Textdateien mit FESTER SPALTENBREITE (Spalten durch Ausrichtung statt
  durch ein Trennzeichen getrennt - typisch für amtliche Stationslisten)
  brauchen 'format: fwf' plus:
    colspecs  Liste von [start, ende]-Paaren (halboffen, 0-basiert)
    names     Spaltennamen, gleiche Anzahl wie colspecs
    skiprows? Anzahl Kopfzeilen, die übersprungen werden
  Konstruiere für solche Dateien NIEMALS ein Trennzeichen (weder " "
  noch einen Regex): daran scheitern Werte, die selbst Leerzeichen
  enthalten. Stehen die Spaltengrenzen nicht fest, nimm eine andere
  Quelle, statt sie zu raten.
- Es gibt den Quelltyp 'region': ein Layer, der die Szenario-Region als
  einzelnes Polygon liefert (nur 'id' und 'source: region' nötig). Nützlich
  als Zone/Maske, wenn die Frage sich auf die Region als Ganzes bezieht.
- Referenziere in steps.inputs die id einer Layer- oder Step-Quelle.
- Gib realistische Parameter an (z. B. buffer.radius_km als Zahl).
- Wiederholung nur bei echtem Bedarf (sonst flache YAML): 'parameters:'
  deklariert Werte mit Typ und default (z. B. radius: {{type: float,
  default: 1.0}}), referenziert als "${{radius}}". Eine Pipeline, die je
  Element einer festen Liste laufen soll, steht EINMAL unter 'modules:
  {{name: {{args, layers, steps, exports}}}}' (interne ids wie grenze,
  anteil; 'exports' nennt die nach außen sichtbaren); 'instances: [{{id:
  stadt, module: name, foreach: {{var: c, in: "${{liste}}"}}, key: "${{c.id}}",
  with: {{arg: "${{c.feld}}"}}}}]' erzeugt je Element eine Kopie mit ids
  stadt[<key>].<id>. Ein Schritt mit 'inputs: "stadt[*].anteil"' (z. B.
  op: collect) bekommt einen Eingang je Element. 'when:' entfernt einen
  Knoten oder ein Element, wenn der Ausdruck über Parameter falsch ist. Die
  Liste muss feststehen - über Objekte eines Layers kann nicht iteriert
  werden.
- 'license: {{name, attribution, url}}' an einem Layer NUR angeben, wenn die
  Lizenz bekannt ist (OSM, Sentinel und Region haben einen Default) - nie
  eine Lizenz erfinden.
- FEHLENDE DATEN: Braucht die Frage einen Datensatz, den weder OSM-Tags
  noch eine unten aufgeführte Quelle liefern (verfügbare und
  vorausgewählte Quellen, API-Katalog, lokale Raster) - z. B. eine
  Statistik, Messwerte oder Standorte, die nur der Nutzer besitzt -, dann
  erfinde KEINEN Pfad, keine URL und keinen Ersatz-Layer. Gib stattdessen
  NUR dieses YAML-Dokument aus, ohne scenario/layers/steps/output daneben:
    missing_data:
      - name: <kurzer Name des fehlenden Datensatzes>
        reason: <wofür die Frage ihn braucht, ein Satz>
        hint: <was der Nutzer hochladen kann: Inhalt, Raumbezug, Format>
        alternative: <die Frage des Nutzers so umformuliert, dass sie nennt,
          woran gemessen wird, und sich mit OSM oder einer aufgeführten
          Quelle beantworten lässt; weglassen, wenn es keinen sinnvollen
          Ersatz gibt>
  Beispiel für alternative: zu "die reichsten Wohnviertel" (keine
  Einkommensdaten vorhanden) passt "die Wohnviertel mit den meisten
  Einfamilienhäusern" - ein Ersatzmass aus OSM, das der Nutzer als solches
  erkennt. Schlage nur ein Ersatzmass vor, das tatsächlich verfügbar ist.
  Der Nutzer wird darüber informiert, kann den Datensatz hochladen und die
  Erzeugung neu starten; ein hochgeladener Datensatz steht dann unter den
  vorausgewählten Quellen. Melde das NUR, wenn die Frage ohne den
  Datensatz nicht beantwortbar ist - nicht bei Daten, die OSM, der
  API-Katalog oder eine aufgeführte Quelle liefern, und nicht bei bloßer
  Unsicherheit über Attributnamen.
- Denke nicht länger nach als nötig: die Struktur oben, der
  Operationskatalog und die Quellenliste sind vollständig. Wenn eine
  Angabe fehlt, triff die naheliegende Annahme und schreibe die YAML -
  rate nicht über Dateiformate, Spaltenbreiten oder Trennzeichen, die
  hier nicht dokumentiert sind.

Verfügbare Operationen (op: input_ports -> output_type; Pflichtparameter):
{operations}

Ausgabeformate (type: angenommene Datentypen -> Dateiendung; Felder):
{outputs}

Quellarten und ihre Felder (flach am Layer; "?" = optional). Jeder Layer
hat außerdem id (Pflicht), source und optional region (eigene Region nur
für diesen Layer), license, when:
{sources}

Datei- und Tabellenformate (Feld 'format', sonst aus der Dateiendung):
{formats}

{examples}

{api_catalog}

{selected_sources}

{data_hints}
"""


def _param_text(params: dict[str, dict]) -> str:
    if not params:
        return "(keine)"
    parts = []
    for name, spec in params.items():
        flag = "" if spec.get("required") else "?"
        detail = spec.get("type", "")
        if spec.get("choices"):
            detail = "|".join(str(c) for c in spec["choices"])
        if "default" in spec:
            detail += f"={spec['default']}"
        parts.append(f"{name}{flag}:{detail}")
    return ", ".join(parts)


def _catalog_text() -> str:
    lines = []
    for entry in api.operation_catalog():
        ports = (
            ", ".join(f"{p}:{t}" for p, t in entry["input_ports"].items()) or "(keine)"
        )
        params = _param_text(entry.get("params", {}))
        lines.append(
            f"- {entry['op']}: [{ports}] -> {entry['output_type']}; params: {params}"
        )
        doc = (entry.get("doc") or "").strip()
        if doc:
            # Eingerückt, damit der Docstring als Zusatzinfo zur Operation erkennbar bleibt.
            for line in doc.splitlines():
                lines.append(f"    {line.strip()}")
    return "\n".join(lines)


# Felder, die jeder Layer bzw. jede Ausgabe trägt; sie stehen einmal im Prompttext.
_COMMON_LAYER_FIELDS = frozenset(
    {"id", "source", "region", "license", "when", "validation"}
)
_COMMON_OUTPUT_FIELDS = frozenset({"type", "source", "when"})


def _schema_type(prop: dict[str, Any], defs: dict[str, Any]) -> str:
    """Kurzform des Typs einer JSON-Schema-Eigenschaft (FA79)."""
    if "enum" in prop:
        return "|".join(str(v) for v in prop["enum"])
    if "const" in prop:
        return str(prop["const"])
    if "$ref" in prop:
        name = prop["$ref"].rsplit("/", 1)[-1]
        target = defs.get(name, {})
        if "enum" in target:
            return "|".join(str(v) for v in target["enum"])
        fields = ", ".join(target.get("properties", {}))
        return f"object mit {fields}" if fields else name
    options = prop.get("anyOf") or prop.get("allOf")
    if options:
        kinds = [_schema_type(o, defs) for o in options if o.get("type") != "null"]
        return " oder ".join(dict.fromkeys(k for k in kinds if k))
    return str(prop.get("type", ""))


def _schema_description(prop: dict[str, Any]) -> str:
    if prop.get("description"):
        return str(prop["description"])
    parts = [
        str(o["description"]) for o in prop.get("anyOf", []) if o.get("description")
    ]
    return " / ".join(parts)


def _field_lines(
    schema: dict[str, Any], defs: dict[str, Any], skip: frozenset[str]
) -> list[str]:
    """Die Felder eines Layer- oder Ausgabemodells, eines je Zeile."""
    required = set(schema.get("required", []))
    lines = []
    for name, prop in schema.get("properties", {}).items():
        if name in skip or "const" in prop:
            continue  # ein fester Wert (data_type: vector) ist keine Wahl
        flag = "" if name in required else "?"
        detail = _schema_type(prop, defs)
        default = prop.get("default")
        if default not in (None, "", [], {}):
            detail += f"={default}"
        description = " ".join(_schema_description(prop).split())
        lines.append(
            f"    {name}{flag}: {detail}" + (f" - {description}" if description else "")
        )
    return lines


def _source_name(schema: dict[str, Any]) -> str | None:
    prop = schema.get("properties", {}).get("source", {})
    name = prop.get("const", prop.get("default"))
    return name if isinstance(name, str) else None


def _sources_text() -> str:
    """Implements: FA79 - Quellarten mit ihren Feldern aus der Registry, damit auch
    Plugin-Quellarten ohne Änderung dieses Textes erscheinen."""
    schema = api.scenario_json_schema()
    defs = schema.get("$defs", {})
    models: dict[str, dict[str, Any]] = {}
    for ref in schema["properties"]["layers"]["items"].get("anyOf", []):
        model = defs.get(ref["$ref"].rsplit("/", 1)[-1], {})
        name = _source_name(model)
        if name is not None:
            models[name] = model
    lines = []
    for source in api.source_types():
        lines.append(f"- {source['source']}: {source['description']}")
        lines.extend(
            _field_lines(models.get(source["source"], {}), defs, _COMMON_LAYER_FIELDS)
        )
    return "\n".join(lines)


def _outputs_text() -> str:
    """Implements: FA79 - Ausgabeformate mit Datentypen, Endung und Feldern aus der Registry."""
    schema = api.scenario_json_schema()
    defs = schema.get("$defs", {})
    output_def = defs.get(
        schema["properties"]["output"]["items"]["$ref"].rsplit("/", 1)[-1], {}
    )
    properties = output_def.get("properties", {})
    lines = []
    for entry in api.extension_catalog()["outputs"]:
        accepts = ", ".join(entry["accepts"])
        lines.append(
            f"- {entry['name']}: [{accepts}] -> .{entry['extension']}; {entry['description']}"
        )
        own = {
            name: properties[name] for name in entry["options"] if name in properties
        }
        lines.extend(_field_lines({"properties": own}, defs, _COMMON_OUTPUT_FIELDS))
    lines.append("  Jede Ausgabe: source (Pflicht), path? (Dateiname), license?")
    return "\n".join(lines)


def _formats_text() -> str:
    """Implements: FA79 - Datei- und Tabellenformate aus der Registry."""
    catalog = api.extension_catalog()

    def line(entry: dict[str, Any], kind: str) -> str:
        endings = ", ".join(f".{e}" for e in entry["extensions"]) or "nur per format"
        return f"- {entry['name']} ({kind}; {endings}): {entry['description']}"

    lines = [
        line(entry, f"source: file, {entry['data_type']}")
        for entry in catalog["file_formats"]
    ]
    lines.extend(line(entry, "source: table") for entry in catalog["table_formats"])
    return "\n".join(lines)


def _without_header_comment(text: str) -> str:
    """Entfernt den Kopfkommentar eines Beispiels; Kommentare an den Schritten bleiben."""
    lines = text.splitlines()
    start = 0
    while start < len(lines) and (
        not lines[start].strip() or lines[start].lstrip().startswith("#")
    ):
        start += 1
    return "\n".join(lines[start:]).strip()


def _examples_text() -> str:
    """Implements: FA79 - Referenzszenarien aus ``examples/`` als Vorlagen.

    Eine fehlende Datei aus ``PROMPT_EXAMPLES`` ist ein expliziter Fehler (Goldene Regel 7)."""
    blocks = [
        "Beispiele: vollständige, geprüfte Konfigurationen als Vorlage für Aufbau "
        "und Schreibweise. Übernimm das Muster, nicht den Inhalt. Dateipfade in den "
        "Beispielen gelten relativ zu ihrem Ordner - für eigene Rasterquellen den "
        'Pfad aus dem Template unter "Verfügbare Rasterquelle" nehmen.',
    ]
    for example_id in PROMPT_EXAMPLES:
        path = (EXAMPLES_DIR / example_id).with_suffix(".yaml")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LLMError(
                f"Vorlage '{example_id}' für den System-Prompt fehlt ({path}): {exc}"
            ) from exc
        blocks.append(f"--- Beispiel {example_id} ---\n{_without_header_comment(text)}")
    return "\n\n".join(blocks)


def _selected_sources_text(selected: list[dict[str, Any]]) -> str:
    if not selected:
        return "Der Nutzer hat keine Quellen vorausgewählt - schlage passende OSM-Tags vor."
    # Die Vorauswahl ist ein Angebot, kein Befehl: als Pflicht gelesen, baut das Modell
    # auch unpassende Quellen ein.
    blocks = [
        "Der Nutzer hat diese Quellen in der Oberfläche vorausgewählt. Das ist "
        "ein VORSCHLAG, keine Pflicht: nimm davon genau das auf, was die Frage "
        "wirklich braucht, und lass den Rest kommentarlos weg. Baue nie einen "
        "Layer ein, den kein Schritt verwendet. Wenn eine Vorauswahl nicht "
        "passt, wähle stattdessen eine geeignete Quelle (OSM-Tags oder einen "
        "Eintrag aus dem API-Katalog oben).",
    ]
    for template in selected:
        blocks.append(
            yaml.safe_dump(template, allow_unicode=True, sort_keys=False).strip()
        )
    return "\n".join(blocks)


def _api_catalog_text() -> str:
    """Kuratierter API-Katalog als Grounding-Text (FA36). Fehlt der Katalog, bleibt der
    Abschnitt leer; die Generierung wird nicht blockiert, die Meldung erscheint an /api/apis."""
    return api_catalog.prompt_text()


def build_system_prompt(
    selected_templates: list[dict[str, Any]], data_hints: list[str]
) -> str:
    hints = ""
    if data_hints:
        hints = (
            "Bekannte Attribute der Quellen (nutze exakt diese Namen in filter/ranking/classify):\n"
            + "\n".join(f"- {h}" for h in data_hints)
        )
    return _SYSTEM_TEMPLATE.format(
        operations=_catalog_text(),
        outputs=_outputs_text(),
        sources=_sources_text(),
        formats=_formats_text(),
        examples=_examples_text(),
        api_catalog=_api_catalog_text(),
        selected_sources=_selected_sources_text(selected_templates),
        data_hints=hints,
    )


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```(?:ya?ml)?\s*\n(.*)\n```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


class LLMError(RuntimeError):
    pass


def _deadline_error(deadline_s: float) -> LLMError:
    return LLMError(
        f"LLM-Anfrage nach {deadline_s:g} s abgebrochen: keine vollständige Antwort "
        "innerhalb der Gesamtfrist je Anfrage (GEOFACT_LLM_DEADLINE_S). Der Anbieter "
        "antwortet zu langsam oder hängt - bitte erneut versuchen oder ein anderes "
        "Modell wählen."
    )


def _with_deadline(
    produce: Callable[[], Iterator[Any]], deadline_s: float
) -> Iterator[Any]:
    """Implements: FA31 - Gesamtfrist für eine LLM-Anfrage.

    Der httpx-``timeout`` begrenzt nur jede Leseoperation; ein Anbieter, der
    tropfenweise sendet, hielte die Anfrage beliebig lange offen. Darum läuft
    ``produce`` in einem Hintergrund-Thread, und der Aufrufer wartet höchstens
    ``deadline_s`` insgesamt. Danach kommt ein expliziter ``LLMError``; der Thread
    endet beim nächsten Element oder am Lese-Timeout und schließt die Verbindung."""
    items: queue.Queue = queue.Queue()
    stop = threading.Event()

    def worker() -> None:
        try:
            for item in produce():
                if stop.is_set():
                    return
                items.put(("item", item))
        except BaseException as exc:  # an den Aufrufer weiterreichen
            items.put(("error", exc))
        else:
            items.put(("end", None))

    threading.Thread(target=worker, name="geofact-llm-request", daemon=True).start()
    end = time.monotonic() + deadline_s
    try:
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise _deadline_error(deadline_s)
            try:
                kind, value = items.get(timeout=remaining)
            except queue.Empty:
                raise _deadline_error(deadline_s) from None
            if kind == "item":
                yield value
            elif kind == "error":
                raise value
            else:
                return
    finally:
        stop.set()


def _user_message(prompt: str, region: str | None) -> str:
    return f"{prompt}\n\nRegion: {region}" if region else prompt


# Felder, unter denen Anbieter Reasoning-Tokens im Streaming-Delta liefern (OpenRouter:
# 'reasoning', andere: 'reasoning_content'); beide lesen, sonst bleibt der Kanal leer.
_REASONING_DELTA_FIELDS = ("reasoning", "reasoning_content")


def _stream_chunk_deltas(chunk: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Implements: FA31 - zerlegt ein Streaming-Chunk in getaggte Deltas.

    Reasoning-Chunks tragen content=None, ein Lesen nur von delta.content
    verwürfe sie. Eigene Funktion, damit sie ohne HTTP testbar ist."""
    choices = chunk.get("choices") or [{}]
    delta = choices[0].get("delta") or {}

    for field_name in _REASONING_DELTA_FIELDS:
        value = delta.get(field_name)
        if isinstance(value, str) and value:
            yield ("thinking", value)

    content = delta.get("content")
    if isinstance(content, str) and content:
        yield ("token", content)


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def model(self) -> str:
        return self._settings.llm_model

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        # OpenRouter-Attribution (optional).
        if "openrouter" in self._settings.llm_base_url:
            headers["HTTP-Referer"] = "https://github.com/erichsdb/GeoFACT-project"
            headers["X-Title"] = "GeoFACT Web Demo"
        return headers

    def _require_key(self) -> None:
        if not self._settings.llm_configured:
            raise LLMError(
                "Kein LLM-API-Key konfiguriert (OPENAI_API_KEY / OPENROUTER_API_KEY setzen)."
            )

    @property
    def _url(self) -> str:
        return self._settings.llm_base_url.rstrip("/") + "/chat/completions"

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Blockierende, nicht streamende Anfrage (läuft unter _with_deadline)."""
        try:
            with httpx.Client(timeout=120) as client:
                response = client.post(self._url, headers=self._headers(), json=payload)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:  # pragma: no cover - Netz
            raise LLMError(
                f"LLM-Anbieter antwortete mit {exc.response.status_code}: {exc.response.text[:400]}"
            ) from exc
        except httpx.HTTPError as exc:  # pragma: no cover - Netz
            raise LLMError(f"LLM-Anfrage fehlgeschlagen: {exc}") from exc

    def _stream_lines(self, payload: dict[str, Any]) -> Iterator[str]:
        """Blockierende Streaming-Anfrage, liefert die SSE-Zeilen (läuft unter
        _with_deadline)."""
        try:
            with httpx.Client(timeout=300) as client:
                with client.stream(
                    "POST", self._url, headers=self._headers(), json=payload
                ) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise LLMError(
                            f"LLM-Anbieter antwortete mit {response.status_code}: {response.text[:400]}"
                        )
                    yield from response.iter_lines()
        except httpx.HTTPError as exc:  # pragma: no cover - Netz
            raise LLMError(f"LLM-Anfrage fehlgeschlagen: {exc}") from exc

    def _payload(
        self,
        messages: list[dict[str, str]],
        *,
        stream: bool = False,
        retry: bool = False,
    ) -> dict[str, Any]:
        """Implements: FA31 - die eine Stelle, an der eine Anfrage entsteht, damit
        Erzeugung, Streaming und Reparatur dieselben Angaben tragen. ``provider``
        bindet an GEOFACT_LLM_PROVIDER (ohne Ausweichen); ohne Angabe fehlt das Feld."""
        payload: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "temperature": 0.2,
            # Ohne Limit generiert ein Modell bis zum Anbieter-Default (NFA2).
            "max_tokens": self._settings.llm_max_tokens,
        }
        if stream:
            payload["stream"] = True
        if self._settings.llm_reasoning:
            # OpenRouter-Form; Anbieter ohne Reasoning ignorieren das Feld.
            payload["reasoning"] = {"enabled": True}
        if retry:
            # Wiederholung nach leerer Antwort: doppeltes Limit, knappes Denken. Reasoning
            # ganz abzuschalten geht nicht überall (400 "Reasoning is mandatory").
            payload["max_tokens"] = 2 * self._settings.llm_max_tokens
            if self._settings.llm_reasoning:
                payload["reasoning"] = {"effort": "low"}
        if self._settings.llm_providers:
            payload["provider"] = {
                "order": list(self._settings.llm_providers),
                "allow_fallbacks": False,
            }
        return payload

    def _chat(self, messages: list[dict[str, str]]) -> str:
        """Eine nicht streamende Anfrage. Denk-Tokens zählen zu max_tokens, ein
        verbrauchtes Limit ergibt eine leere Antwort. Dann folgt eine Wiederholung
        mit doppeltem Limit; bleibt sie leer, ist das ein expliziter Fehler."""
        self._require_key()
        content, finish_reason = self._ask(self._payload(messages))
        if not content:
            content, finish_reason = self._ask(self._payload(messages, retry=True))
        if not content:
            raise LLMError(
                f"LLM-Antwort ohne Inhalt (finish_reason: {finish_reason}) - das Modell "
                "hat sein Limit auch in der Wiederholung verbraucht, vermutlich in der Denkphase; "
                "GEOFACT_LLM_MAX_TOKENS erhöhen oder GEOFACT_LLM_REASONING=false setzen."
            )
        return content

    def _ask(self, payload: dict[str, Any]) -> tuple[str, Any]:
        """(Inhalt, finish_reason) einer Anfrage; der Inhalt ist "" statt None."""
        (data,) = _with_deadline(
            lambda: iter([self._post_json(payload)]), self._settings.llm_deadline_s
        )
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover
            raise LLMError(
                f"Unerwartete LLM-Antwortstruktur: {json.dumps(data)[:400]}"
            ) from exc
        text = content.strip() if isinstance(content, str) else ""
        return (content if text else ""), choice.get("finish_reason")

    def generate_config(
        self,
        prompt: str,
        selected_templates: list[dict[str, Any]],
        region: str | None,
        data_hints: list[str] | None = None,
    ) -> str:
        system = build_system_prompt(selected_templates, data_hints or [])
        content = self._chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": _user_message(prompt, region)},
            ]
        )
        return _strip_code_fences(content)

    def stream_generate_config(
        self,
        prompt: str,
        selected_templates: list[dict[str, Any]],
        region: str | None,
        data_hints: list[str] | None = None,
    ) -> Iterator[tuple[str, str]]:
        """Implements: FA31 - streamt die Modellantwort als getaggte Chunks.

        Yields ("token", delta) für YAML-Text und ("thinking", delta) für
        Reasoning. Getrennte Kanäle, weil das Reasoning nicht in die erzeugte
        YAML gehört, aber während langer Generierungen sichtbar sein soll."""
        system = build_system_prompt(selected_templates, data_hints or [])
        yield from self._stream_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": _user_message(prompt, region)},
            ]
        )

    def _stream_chat(self, messages: list[dict[str, str]]) -> Iterator[tuple[str, str]]:
        """Eine streamende Anfrage als getaggte Chunks ("token" | "thinking")."""
        self._require_key()
        payload = self._payload(messages, stream=True)
        # Gesamtfrist über den ganzen Strom, nicht je Leseoperation (FA31).
        lines = _with_deadline(
            lambda: self._stream_lines(payload), self._settings.llm_deadline_s
        )
        try:
            for line in lines:
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                yield from _stream_chunk_deltas(chunk)
        finally:
            lines.close()

    def repair_config(
        self,
        previous_yaml: str,
        issues: list[str],
        selected_templates: list[dict[str, Any]] | None = None,
        data_hints: list[str] | None = None,
        prompt: str | None = None,
        region: str | None = None,
        runtime: bool = False,
    ) -> str:
        """Implements: FA12/FA31 - eine Reparatur-Runde mit demselben System-Prompt wie
        die Erzeugung, sonst erfindet das Modell Operationen und Ports. Die
        ursprüngliche Anfrage geht mit. runtime (FA78): die Meldungen stammen aus
        einem Probelauf, nicht aus der Prüfung."""
        content = self._chat(
            self._repair_messages(
                previous_yaml,
                issues,
                selected_templates,
                data_hints,
                prompt,
                region,
                runtime,
            )
        )
        return _strip_code_fences(content)

    def stream_repair_config(
        self,
        previous_yaml: str,
        issues: list[str],
        selected_templates: list[dict[str, Any]] | None = None,
        data_hints: list[str] | None = None,
        prompt: str | None = None,
        region: str | None = None,
        runtime: bool = False,
    ) -> Iterator[tuple[str, str]]:
        """Implements: FA78 - ``repair_config`` als Strom getaggter Chunks
        ("token" | "thinking"); der Aufrufer setzt die Tokens zusammen."""
        yield from self._stream_chat(
            self._repair_messages(
                previous_yaml,
                issues,
                selected_templates,
                data_hints,
                prompt,
                region,
                runtime,
            )
        )

    def _repair_messages(
        self,
        previous_yaml: str,
        issues: list[str],
        selected_templates: list[dict[str, Any]] | None,
        data_hints: list[str] | None,
        prompt: str | None,
        region: str | None,
        runtime: bool,
    ) -> list[dict[str, str]]:
        system = build_system_prompt(selected_templates or [], data_hints or [])
        issue_text = "\n".join(f"- {i}" for i in issues)
        request = (
            f"Ursprüngliche Anfrage:\n{_user_message(prompt, region)}\n\n"
            if prompt
            else ""
        )
        if runtime:
            # FA78: die Konfiguration ist gültig, bricht aber auf echten Daten ab.
            problem = (
                f"Diese Konfiguration ist formal gültig, bricht aber beim Lauf auf den "
                f"echten Daten ab:\n\n{previous_yaml}\n\n"
                f"Fehlermeldung des Laufs:\n{issue_text}\n\n"
                "Die Meldung nennt den Schritt und oft die vorhandenen Attribute oder "
                "Geometriearten - richte dich danach, statt einen Namen zu raten. "
            )
        else:
            problem = (
                f"Diese Konfiguration ist ungültig:\n\n{previous_yaml}\n\n"
                f"Validierungsfehler:\n{issue_text}\n\n"
            )
        user = (
            f"{request}{problem}"
            "Korrigiere genau diese Fehler. Verwende nur Operationen, Ports, "
            "Parameter und Quellarten aus dem Katalog im System-Prompt. "
            "Gib die korrigierte, vollständige YAML-Konfiguration aus."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def review_config(
        self,
        config_yaml: str,
        warnings: list[str],
        selected_templates: list[dict[str, Any]] | None = None,
        data_hints: list[str] | None = None,
        prompt: str | None = None,
        region: str | None = None,
    ) -> str:
        """Implements: FA78 - Durchsicht der Warnungen eines Probelaufs.

        Auch eine durchgelaufene Konfiguration kann die Frage falsch beantworten.
        Das Modell darf sie unverändert zurückgeben; ob eine geänderte Fassung
        gilt, entscheidet der Aufrufer am Probelauf."""
        content = self._chat(
            self._review_messages(
                config_yaml, warnings, selected_templates, data_hints, prompt, region
            )
        )
        return _strip_code_fences(content)

    def stream_review_config(
        self,
        config_yaml: str,
        warnings: list[str],
        selected_templates: list[dict[str, Any]] | None = None,
        data_hints: list[str] | None = None,
        prompt: str | None = None,
        region: str | None = None,
    ) -> Iterator[tuple[str, str]]:
        """Implements: FA78 - ``review_config`` als Strom getaggter Chunks."""
        yield from self._stream_chat(
            self._review_messages(
                config_yaml, warnings, selected_templates, data_hints, prompt, region
            )
        )

    def _review_messages(
        self,
        config_yaml: str,
        warnings: list[str],
        selected_templates: list[dict[str, Any]] | None,
        data_hints: list[str] | None,
        prompt: str | None,
        region: str | None,
    ) -> list[dict[str, str]]:
        system = build_system_prompt(selected_templates or [], data_hints or [])
        warning_text = "\n".join(f"- {w}" for w in warnings)
        request = (
            f"Ursprüngliche Anfrage:\n{_user_message(prompt, region)}\n\n"
            if prompt
            else ""
        )
        user = (
            f"{request}"
            f"Diese Konfiguration lief auf den echten Daten durch:\n\n{config_yaml}\n\n"
            f"Der Lauf hat dabei gewarnt:\n{warning_text}\n\n"
            "Prüfe, ob eine Warnung bedeutet, dass das Ergebnis die Anfrage falsch "
            "beantwortet (verworfene Objekte, leeres Ergebnis, unverbundenes Netz, "
            "falsche Geometrieart). Wenn ja, korrigiere die Ursache. Wenn die Warnungen "
            "das Ergebnis nicht verfälschen, gib die Konfiguration UNVERAENDERT zurück. "
            "Gib in jedem Fall nur die vollständige YAML-Konfiguration aus."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
