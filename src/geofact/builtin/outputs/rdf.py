"""Implements: FA13 (RDF/GeoSPARQL-Export), FA65 (Lizenzen im RDF), FA73 (Ausgabelizenz im RDF), FA74 (PROV-O-Herkunft und Laufmetriken), Ausgabeformat 'rdf' für FA40.

Überführt einen Ergebnis-Layer in GeoSPARQL-Tripel (Turtle): je Feature ein
geo:Feature mit geo:hasGeometry/geo:asWKT (EPSG:4326, WKT-Literal mit
CRS84-Präfix) und den Attributen als Datatype-Properties. Ein Layer ohne CRS
bricht den Export ab, statt eine falsch beschriftete Geometrie zu schreiben.

Lizenzen (FA65): Mit ``spec.attribution`` beschreibt ein ``dcat:Dataset``
(``geofact:dataset/<layer>``) den Layer mit ``dcterms:license`` und
``dcterms:rights``; jedes Feature ist ``dcterms:isPartOf`` davon.
Ausgabelizenz (FA73): Mit ``spec.output_license`` trägt das Ergebnis diese
Lizenz, die Quelllizenzen wandern an eigene Quell-Datensätze, auf die
``dcterms:source`` und ``prov:wasDerivedFrom`` verweisen.

Provenienz (FA74, Option ``provenance``, Default an): Mit ``spec.lineage``
kommen additive PROV-O-Tripel dazu: ``prov:Activity`` je Schritt
(``geofactvocab:operation``, ``geofactvocab:parameters`` als ``rdf:JSON``),
``prov:Entity`` je Zwischenergebnis und Quell-Layer, sowie der Lauf
``geofact:run/<Szenarioname>/<Lauf-ID>`` mit Zeitstempeln und GeoFACT als
``prov:SoftwareAgent``. ``geofactprop:`` sind die Attribute der Features,
``geofactvocab:`` die Begriffe von Lauf und Herkunft (getrennt, damit eine
Spalte ``operation`` nie mit dem Vokabular kollidiert).
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Literal as TypingLiteral, Optional

from geopandas import GeoDataFrame
from pandas.api.types import is_bool_dtype, is_numeric_dtype
from pydantic import BaseModel, ConfigDict
from rdflib import RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCAT, DCTERMS, GEO, PROV

from geofact.plugin_api import (
    License,
    NodeAttribution,
    OutputLicense,
    OutputSkipped,
    RunLineage,
    register_output,
)

GEOFACT = Namespace("https://geofact.example.org/resource/")
GEOFACT_PROP = Namespace("https://geofact.example.org/property/")
GEOFACT_VOCAB = Namespace("https://geofact.example.org/vocab/")

# CRS84 (WGS84, Lon/Lat) ist das implizite Default-CRS eines geo:wktLiteral
# (GeoSPARQL 11-052r4, 8.3.2). Es wird explizit vorangestellt, damit die Datei
# für sich selbst eindeutig ist.
CRS84_URI = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"


def _wkt_literal(geom) -> Literal:
    return Literal(f"<{CRS84_URI}> {geom.wkt}", datatype=GEO.wktLiteral)


def _sanitize_predicate_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)


def _license_object(license_: License) -> URIRef | Literal:
    return URIRef(license_.url) if license_.url else Literal(license_.name)


def _add_licenses(graph: Graph, node: URIRef, licenses) -> None:
    for license_ in licenses:
        graph.add((node, DCTERMS.license, _license_object(license_)))
        graph.add((node, DCTERMS.rights, Literal(license_.line())))


def _bind_dataset_namespaces(graph: Graph) -> None:
    graph.bind("dcat", DCAT)
    graph.bind("dcterms", DCTERMS)


def _add_dataset(graph: Graph, layer_name: str, attribution: NodeAttribution) -> URIRef:
    """FA65: ``dcat:Dataset`` des Layers mit Lizenzen und Namensnennungen."""
    _bind_dataset_namespaces(graph)
    dataset = GEOFACT[f"dataset/{layer_name}"]
    graph.add((dataset, RDF.type, DCAT.Dataset))
    _add_licenses(graph, dataset, attribution.licenses)
    return dataset


def _json_literal(value: Any) -> Literal:
    """Deterministisches JSON (Schlüssel sortiert) als ``rdf:JSON``-Literal."""
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return Literal(text, datatype=RDF.JSON)


def _slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", text.strip()).strip("-")
    return slug or "lauf"


def _entity(lineage: RunLineage, node_id: str, exported: str) -> URIRef:
    """Entität eines Knotens: Quell-Layer, das Ergebnis selbst oder ein
    Zwischenergebnis."""
    node = lineage.nodes[node_id]
    if node.kind == "layer":
        return GEOFACT[f"source/{node_id}"]
    if node_id == exported:
        return GEOFACT[f"dataset/{exported}"]
    return GEOFACT[f"entity/{node_id}"]


def _source_nodes(lineage: Optional[RunLineage], exported: str) -> list[str]:
    """Knoten mit eigener Lizenz auf dem Weg zum Ergebnis (Quell-Layer und
    Schritte, die Daten von außen einbringen), nach Id sortiert."""
    if lineage is None or exported not in lineage.nodes:
        return []
    return [
        node_id
        for node_id in lineage.ancestors(exported)
        if lineage.nodes[node_id].license is not None
        and (lineage.nodes[node_id].kind == "layer" or node_id != exported)
    ]


def _add_output_license(
    graph: Graph,
    dataset: URIRef,
    layer_name: str,
    output_license: OutputLicense,
    attribution: Optional[NodeAttribution],
    lineage: Optional[RunLineage],
) -> None:
    """FA73: Ausgabelizenz am Ergebnis, Quelllizenzen an Quell-Datensätzen."""
    graph.bind("prov", PROV)
    graph.add((dataset, DCTERMS.license, _license_object(output_license.license)))
    graph.add((dataset, DCTERMS.rights, Literal(output_license.line())))
    covered: set[License] = set()
    for node_id in _source_nodes(lineage, layer_name):
        assert lineage is not None
        node = lineage.nodes[node_id]
        assert node.license is not None
        source = _entity(lineage, node_id, layer_name)
        graph.add((source, RDF.type, DCAT.Dataset))
        _add_licenses(graph, source, [node.license])
        graph.add((dataset, DCTERMS.source, source))
        graph.add((dataset, PROV.wasDerivedFrom, source))
        covered.add(node.license)
    remaining = [
        lic
        for lic in (attribution.licenses if attribution is not None else ())
        if lic not in covered
    ]
    for number, license_ in enumerate(remaining, start=1):
        source = GEOFACT[f"dataset/{layer_name}/source/{number}"]
        graph.add((source, RDF.type, DCAT.Dataset))
        _add_licenses(graph, source, [license_])
        graph.add((dataset, DCTERMS.source, source))
        graph.add((dataset, PROV.wasDerivedFrom, source))


def _number(value: float) -> Literal:
    return Literal(float(value), datatype=XSD.double)


def _add_statistics(graph: Graph, dataset: URIRef, gdf: GeoDataFrame) -> None:
    """FA74: Merkmalszahl und je numerischer Spalte min/max/Mittelwert."""
    graph.add(
        (dataset, GEOFACT_VOCAB.featureCount, Literal(len(gdf), datatype=XSD.integer))
    )
    geometry_name = gdf.geometry.name
    for column in sorted((col for col in gdf.columns if col != geometry_name), key=str):
        series = gdf[column]
        if is_bool_dtype(series) or not is_numeric_dtype(series):
            continue
        values = series.astype(float)
        values = values[values.map(math.isfinite)]
        if values.empty:
            continue
        statistic = BNode()
        graph.add((dataset, GEOFACT_VOCAB.statistic, statistic))
        graph.add((statistic, GEOFACT_VOCAB.column, Literal(str(column))))
        graph.add((statistic, GEOFACT_VOCAB.min, _number(values.min())))
        graph.add((statistic, GEOFACT_VOCAB.max, _number(values.max())))
        graph.add((statistic, GEOFACT_VOCAB.mean, _number(values.mean())))


def _datetime(value: str) -> Literal:
    return Literal(value, datatype=XSD.dateTime)


def _add_run(graph: Graph, lineage: RunLineage) -> tuple[URIRef, URIRef]:
    """FA74: der Lauf als ``prov:Activity`` und GeoFACT als ``prov:SoftwareAgent``."""
    run_path = f"run/{_slug(lineage.scenario_name)}"
    if lineage.run_id:
        run_path += f"/{_slug(lineage.run_id)}"
    run = GEOFACT[run_path]
    agent = GEOFACT["agent/geofact"]
    graph.add((run, RDF.type, PROV.Activity))
    graph.add((run, RDF.type, GEOFACT_VOCAB.Run))
    graph.add((run, DCTERMS.title, Literal(lineage.scenario_name)))
    graph.add((run, GEOFACT_VOCAB.region, Literal(lineage.region)))
    if lineage.working_crs:
        graph.add((run, GEOFACT_VOCAB.workingCrs, Literal(lineage.working_crs)))
    if lineage.started_at:
        graph.add((run, PROV.startedAtTime, _datetime(lineage.started_at)))
    if lineage.finished_at:
        graph.add((run, PROV.endedAtTime, _datetime(lineage.finished_at)))
    graph.add((run, PROV.wasAssociatedWith, agent))
    graph.add((agent, RDF.type, PROV.SoftwareAgent))
    graph.add((agent, RDFS.label, Literal("GeoFACT")))
    graph.add((agent, GEOFACT_VOCAB.version, Literal(lineage.version)))
    return run, agent


def _add_source_entity(graph: Graph, entity: URIRef, node) -> None:
    """FA74: Quell-Layer mit Quellart, Parametern, Lizenz und CRS-Provenienz (FA58)."""
    graph.add((entity, RDF.type, DCAT.Dataset))
    graph.add((entity, DCTERMS.identifier, Literal(node.id)))
    graph.add((entity, GEOFACT_VOCAB.sourceType, Literal(node.block)))
    graph.add((entity, GEOFACT_VOCAB.parameters, _json_literal(dict(node.parameters))))
    if node.license is not None:
        _add_licenses(graph, entity, [node.license])
    provenance = node.provenance
    if provenance is not None:
        if provenance.source_crs:
            graph.add((entity, GEOFACT_VOCAB.sourceCrs, Literal(provenance.source_crs)))
        if provenance.feature_count is not None:
            graph.add(
                (
                    entity,
                    GEOFACT_VOCAB.featureCount,
                    Literal(provenance.feature_count, datatype=XSD.integer),
                )
            )
        if provenance.bounds is not None:
            graph.add(
                (
                    entity,
                    GEOFACT_VOCAB.bounds,
                    _json_literal([float(v) for v in provenance.bounds]),
                )
            )
            graph.add((entity, GEOFACT_VOCAB.boundsCrs, Literal(provenance.target_crs)))
    for message in node.warnings:
        graph.add((entity, GEOFACT_VOCAB.warning, Literal(message)))


def _add_provenance(
    graph: Graph,
    dataset: URIRef,
    layer_name: str,
    lineage: RunLineage,
    gdf: GeoDataFrame,
) -> None:
    """FA74: PROV-O des Wegs von den Quellen zum Ergebnis und Laufmetadaten."""
    graph.bind("prov", PROV)
    graph.bind("geofactvocab", GEOFACT_VOCAB)
    _bind_dataset_namespaces(graph)
    run, agent = _add_run(graph, lineage)

    graph.add((dataset, RDF.type, DCAT.Dataset))
    graph.add((dataset, RDF.type, PROV.Entity))
    _add_statistics(graph, dataset, gdf)
    if lineage.nodes[layer_name].kind == "layer":
        # Ausgabe direkt aus einem Layer: das Ergebnis ist eine Kopie der Quelle
        graph.add(
            (dataset, PROV.wasDerivedFrom, _entity(lineage, layer_name, layer_name))
        )

    for node_id in lineage.ancestors(layer_name):
        node = lineage.nodes[node_id]
        entity = _entity(lineage, node_id, layer_name)
        graph.add((entity, RDF.type, PROV.Entity))
        if node.kind == "layer":
            _add_source_entity(graph, entity, node)
            continue
        activity = GEOFACT[f"activity/{node_id}"]
        graph.add((activity, RDF.type, PROV.Activity))
        graph.add((activity, DCTERMS.identifier, Literal(node_id)))
        graph.add((activity, GEOFACT_VOCAB.run, run))
        graph.add((activity, GEOFACT_VOCAB.operation, Literal(node.block)))
        graph.add(
            (activity, GEOFACT_VOCAB.parameters, _json_literal(dict(node.parameters)))
        )
        graph.add((activity, PROV.wasAssociatedWith, agent))
        for message in node.warnings:
            graph.add((activity, GEOFACT_VOCAB.warning, Literal(message)))
        graph.add((entity, PROV.wasGeneratedBy, activity))
        for _port, source in node.inputs:
            used = _entity(lineage, source, layer_name)
            graph.add((activity, PROV.used, used))
            graph.add((entity, PROV.wasDerivedFrom, used))
        if node.license is not None and node_id != layer_name:
            # Schritt mit Daten von außen (op: rest): seine Lizenz am Zwischenergebnis
            _add_licenses(graph, entity, [node.license])


def build_graph(
    gdf: GeoDataFrame,
    layer_name: str = "features",
    attribution: NodeAttribution | None = None,
    *,
    output_license: OutputLicense | None = None,
    lineage: RunLineage | None = None,
    provenance: bool = True,
) -> Graph:
    """GeoSPARQL-Graph eines Vektor-Layers (FA13) mit Lizenzen (FA65),
    Ausgabelizenz (FA73) und - mit ``lineage`` und ``provenance`` - PROV-O
    (FA74)."""
    graph = Graph()
    graph.bind("geo", GEO)
    graph.bind("geofact", GEOFACT)
    graph.bind("geofactprop", GEOFACT_PROP)

    if gdf.crs is None:
        raise ValueError(
            f"Layer '{layer_name}': RDF-Export erfordert ein gesetztes CRS, "
            "um die WKT-Geometrien korrekt als CRS84 (GeoSPARQL-Default) "
            "auszuzeichnen - der Layer hat keins (siehe FA5/CRS-Harmonisierer)."
        )
    output = gdf.to_crs("EPSG:4326")
    with_lineage = provenance and lineage is not None and layer_name in lineage.nodes
    dataset: URIRef | None = None
    if output_license is not None:
        _bind_dataset_namespaces(graph)
        dataset = GEOFACT[f"dataset/{layer_name}"]
        graph.add((dataset, RDF.type, DCAT.Dataset))
        _add_output_license(
            graph, dataset, layer_name, output_license, attribution, lineage
        )
    elif attribution is not None and not attribution.empty:
        dataset = _add_dataset(graph, layer_name, attribution)
    elif with_lineage:
        dataset = GEOFACT[f"dataset/{layer_name}"]
    if with_lineage:
        assert lineage is not None and dataset is not None
        _add_provenance(graph, dataset, layer_name, lineage, gdf)

    for idx, row in output.iterrows():
        feature_uri = GEOFACT[f"{layer_name}/{idx}"]
        geometry_uri = GEOFACT[f"{layer_name}/{idx}/geometry"]

        graph.add((feature_uri, RDF.type, GEO.Feature))
        if dataset is not None:
            graph.add((feature_uri, DCTERMS.isPartOf, dataset))
        graph.add((feature_uri, GEO.hasGeometry, geometry_uri))
        graph.add((geometry_uri, RDF.type, GEO.Geometry))
        graph.add((geometry_uri, GEO.asWKT, _wkt_literal(row.geometry)))

        for col, value in row.items():
            if col == "geometry" or value is None:
                continue
            if isinstance(value, float) and value != value:  # NaN
                continue
            predicate = GEOFACT_PROP[_sanitize_predicate_name(col)]
            if isinstance(value, bool):
                literal = Literal(value, datatype=XSD.boolean)
            elif isinstance(value, int):
                literal = Literal(value, datatype=XSD.integer)
            elif isinstance(value, float):
                literal = Literal(value, datatype=XSD.double)
            else:
                literal = Literal(str(value))
            graph.add((feature_uri, predicate, literal))

    return graph


def write_turtle(
    gdf: GeoDataFrame,
    path: str | Path,
    layer_name: str = "features",
    attribution: NodeAttribution | None = None,
    *,
    output_license: OutputLicense | None = None,
    lineage: RunLineage | None = None,
    provenance: bool = True,
) -> None:
    graph = build_graph(
        gdf,
        layer_name=layer_name,
        attribution=attribution,
        output_license=output_license,
        lineage=lineage,
        provenance=provenance,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    graph.serialize(destination=str(path), format="turtle")


# --- Registrierung als Ausgabeformat (FA40) ---


class RdfOptions(BaseModel):
    """Formatspezifische Felder einer rdf-Ausgabe in der Szenario-YAML."""

    model_config = ConfigDict(extra="forbid")

    vocabulary: Optional[TypingLiteral["geosparql"]] = None
    provenance: bool = True
    """FA74: PROV-O-Herkunft und Laufmetriken schreiben (nur additive Tripel)."""


@register_output(
    "rdf",
    extension="ttl",
    options_model=RdfOptions,
    description="RDF-Tripel nach GeoSPARQL (Turtle), mit PROV-O-Herkunft (provenance)",
)
def _write_output(gdf: GeoDataFrame, target: Path, spec) -> None:
    options: Optional[RdfOptions] = spec.options
    try:
        write_turtle(
            gdf,
            target,
            layer_name=spec.source or "features",
            attribution=getattr(spec, "attribution", None),
            output_license=getattr(spec, "output_license", None),
            lineage=getattr(spec, "lineage", None),
            provenance=options.provenance if options is not None else True,
        )
    except ValueError as exc:
        # Layer ohne CRS (build_graph weist das zurück, damit WKT-Literale
        # nicht fälschlich als CRS84 gelten) - diese eine Ausgabe entfällt
        # sichtbar, andere Ausgaben laufen weiter (Goldene Regel 7).
        raise OutputSkipped(str(exc)) from exc
