"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl, { type StyleSpecification } from "maplibre-gl";
import type { AttributionOut, NodeResult } from "@/lib/types";
import { API_BASE } from "@/lib/api";
import { withTokenParam } from "@/lib/auth";
import { formatCount, formatDecimal } from "@/lib/format";

const SCALE = ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"];
// Farbe für Objekte ohne (numerischen) Wert im aktiven Farbfeld - z. B.
// shortest_path-Ergebnisse außerhalb von max_snap_m (FA15), die NaN statt
// einer Distanz haben. Ohne explizite Behandlung fällt ["to-number",
// ["get", field], min] bei fehlendem/NaN-Wert auf den DEFAULT-Parameter
// min zurück - optisch nicht von SCALE[0] ("kleinster echter Wert",
// gleicher Blauton) unterscheidbar.
const NAN_COLOR = "#999999";

// Clientseitiges Sicherheitsnetz. Die eigentliche Begrenzung sitzt im
// Backend (_MAX_NODE_FEATURES, an realen OSM-Layern gemessen); dieser
// Cap greift nur, falls von dort wider Erwarten mehr Objekte kommen.
// MUSS oberhalb des Server-Caps liegen - sonst kappt die Karte still,
// was der Server bewusst ausliefert. MapLibre selbst käme mit deutlich
// mehr Punkten klar; der Engpass ist die Antwortgröße, nicht das
// Rendern.
const MAX_MAP_FEATURES = 200_000;

function capFeatures(fc: GeoJSON.FeatureCollection): { fc: GeoJSON.FeatureCollection; truncated: number } {
  if (fc.features.length <= MAX_MAP_FEATURES) return { fc, truncated: 0 };
  return {
    fc: { ...fc, features: fc.features.slice(0, MAX_MAP_FEATURES) },
    truncated: fc.features.length - MAX_MAP_FEATURES,
  };
}

// Punkt-Clustering (MapLibre/Supercluster). Statt tausender Einzelpunkte
// zeigt die Karte aggregierte Blasen, deren Groesse/Farbe die Anzahl
// abbildet; beim Hineinzoomen zerfallen sie und ab CLUSTER_MAX_ZOOM sind
// die echten Einzelobjekte sichtbar (inkl. Popup/Attributen).
//
// Clustering gilt nur für Punkt-Geometrien: MapLibre verwirft bei
// cluster:true alle Nicht-Punkte einer Source. Vektor-Ergebnisse mischen
// aber Geometrietypen (PostGIS-Backend liefert Punkte/Linien/Flaechen),
// deshalb zwei Sources statt einer - geclusterte Punkte und ungeclusterte
// Flaechen/Linien.
const CLUSTER_RADIUS = 50;
const CLUSTER_MAX_ZOOM = 14;
// Ab dieser Menge lohnt Clustering; darunter sind Einzelpunkte
// aussagekräftiger (und exakt verortet).
const CLUSTER_MIN_POINTS = 200;

// Schwellen für Blasengroesse/-farbe nach Objektanzahl im Cluster.
const CLUSTER_STEPS = [10, 100, 1000];
const CLUSTER_COLORS = ["#60a5fa", "#3b82f6", "#1d4ed8", "#1e3a8a"];
const CLUSTER_RADII = [14, 18, 24, 30];

function splitByGeometry(fc: GeoJSON.FeatureCollection): {
  points: GeoJSON.FeatureCollection;
  others: GeoJSON.FeatureCollection;
} {
  const points: GeoJSON.Feature[] = [];
  const others: GeoJSON.Feature[] = [];
  for (const f of fc.features) {
    const t = f.geometry?.type;
    if (t === "Point" || t === "MultiPoint") points.push(f);
    else if (t) others.push(f);
  }
  return {
    points: { type: "FeatureCollection", features: points },
    others: { type: "FeatureCollection", features: others },
  };
}

// Bis zu diesem Zoom tragen Flaechen/Linien zusätzlich eine Punktmarkierung
// auf ihrem Schwerpunkt. Grund: ein gefiltertes Ergebnis von ein paar
// Gebäude- oder Geländepolygonen ist auf Landesebene nur ein paar Pixel
// groß und fällt neben der Grundkarte praktisch nicht auf, während
// Punktergebnisse durch Radius + weißen Rand sofort sichtbar sind. Ab
// diesem Zoom ist die echte Geometrie groß genug, um für sich zu sprechen,
// und die Marker würden sie nur verdecken.
const CENTROID_MAX_ZOOM = 13;

/** Schwerpunkte von Nicht-Punkt-Geometrien als eigene Punkt-Features.
 *
 * Bewusst der Mittelwert der Stützpunkte (kein flächengewichteter
 * Zentroid): das Ergebnis ist nur eine Sichtbarkeitsmarkierung, keine
 * analytische Größe - dafür gibt es die centroid-Operation im Kern.
 * Die Properties werden übernommen, damit Popup und Farbfeld am Marker
 * dasselbe zeigen wie an der Fläche. */
function centroidMarkers(fc: GeoJSON.FeatureCollection): GeoJSON.FeatureCollection {
  const features: GeoJSON.Feature[] = [];
  for (const f of fc.features) {
    let sumLon = 0;
    let sumLat = 0;
    let n = 0;
    // Rekursiv über die verschachtelten Koordinatenarrays (Polygon,
    // MultiPolygon, LineString ... unterscheiden sich nur in der Tiefe).
    const walk = (coords: unknown): void => {
      if (!Array.isArray(coords)) return;
      if (typeof coords[0] === "number" && typeof coords[1] === "number") {
        const [lon, lat] = coords as number[];
        if (Number.isFinite(lon) && Number.isFinite(lat)) {
          sumLon += lon;
          sumLat += lat;
          n += 1;
        }
        return;
      }
      for (const c of coords) walk(c);
    };
    const geom = f.geometry;
    if (!geom || geom.type === "GeometryCollection") continue;
    walk(geom.coordinates);
    if (n === 0) continue;
    features.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: [sumLon / n, sumLat / n] },
      properties: f.properties,
    });
  }
  return { type: "FeatureCollection", features };
}

/** ["step", ...] für Cluster-Symbolik nach point_count.
 *
 * Der Rückgabetyp folgt dem Elementtyp (Farbe -> string, Radius -> number),
 * damit die MapLibre-Paint-Properties beide Aufrufe typkorrekt annehmen. */
function clusterStep<T extends string | number>(values: T[]): T {
  const expr: unknown[] = ["step", ["get", "point_count"], values[0]];
  CLUSTER_STEPS.forEach((threshold, i) => expr.push(threshold, values[i + 1]));
  return expr as unknown as T;
}

type LegendInfo = { label: string; min: number; max: number; single?: boolean; hasMissing?: boolean };

function formatLegendValue(value: number): string {
  return formatDecimal(value, 2);
}

/** Min/Max eines numerischen Feldes in einem Durchlauf.
 *
 * Bewusst als Schleife statt Math.min(...vals): der Spread übergibt jeden
 * Wert als eigenes Argument und sprengt bei großen Ergebnissen (seit dem
 * höheren Server-Limit realistisch) den Argument-Stack der Engine. */
function minMax(fc: GeoJSON.FeatureCollection | undefined, field: string): { min: number; max: number; count: number } {
  let min = Infinity;
  let max = -Infinity;
  let count = 0;
  for (const f of fc?.features ?? []) {
    const v = f.properties?.[field];
    if (typeof v !== "number" || !Number.isFinite(v)) continue;
    if (v < min) min = v;
    if (v > max) max = v;
    count += 1;
  }
  return { min, max, count };
}

function numericRange(fc: GeoJSON.FeatureCollection | undefined, field: string): [number, number] | null {
  if (!fc) return null;
  const { min, max, count } = minMax(fc, field);
  if (count < 2 || min === max) return null;
  return [min, max];
}

function singleValue(fc: GeoJSON.FeatureCollection | undefined, field: string): number | null {
  if (!fc) return null;
  const { min, max, count } = minMax(fc, field);
  if (count === 0) return null;
  return min === max ? min : null;
}

const BASEMAP: StyleSpecification = (() => {
  const custom = process.env.NEXT_PUBLIC_BASEMAP_STYLE;
  if (custom) return custom as unknown as StyleSpecification;
  return {
    version: 8,
    // Ohne "glyphs" schlagen alle Symbol-Layer mit text-field fehl
    // (z. B. die Cluster-Zahlen: "use of text-field requires a style
    // glyphs property") - die Blasen blieben dann stumm und MapLibre
    // loggt bei jedem Rendern einen Fehler.
    glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "Karte: © OpenStreetMap contributors",
      },
    },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#e2e8f0" } },
      { id: "osm", type: "raster", source: "osm", paint: { "raster-opacity": 0.9 } },
    ],
  };
})();

const OVERLAY_IDS = [
  "geo-fill",
  "geo-line",
  "geo-centroid",
  "geo-point",
  "geo-cluster",
  "geo-cluster-count",
  "geo-heatmap",
  "graph-edges",
  "graph-nodes",
  "raster-overlay",
];
// Quelle des Rasterbilds (raster.png); an ihr erkennt der Fehler-Handler der
// Karte, dass genau das Bild nicht geladen werden konnte (FA54).
const RASTER_SOURCE_ID = "raster-src";
const SOURCE_IDS = [
  "geo-src",
  "geo-centroid-src",
  "geo-points-src",
  "graph-edges-src",
  "graph-nodes-src",
  RASTER_SOURCE_ID,
];

function graphToGeoJSON(graph: NodeResult["graph"]) {
  const nodeById = new Map<string, { lon: number; lat: number }>();
  const nodeFeatures: GeoJSON.Feature[] = [];
  for (const node of graph?.nodes ?? []) {
    if (node.lon == null || node.lat == null) continue;
    nodeById.set(String(node.id), { lon: node.lon, lat: node.lat });
    nodeFeatures.push({
      type: "Feature",
      geometry: { type: "Point", coordinates: [node.lon, node.lat] },
      properties: node as Record<string, unknown>,
    });
  }
  const edgeFeatures: GeoJSON.Feature[] = [];
  for (const edge of graph?.edges ?? []) {
    const a = nodeById.get(String(edge.source));
    const b = nodeById.get(String(edge.target));
    if (!a || !b) continue;
    edgeFeatures.push({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [[a.lon, a.lat], [b.lon, b.lat]] },
      properties: edge as Record<string, unknown>,
    });
  }
  return {
    nodes: { type: "FeatureCollection", features: nodeFeatures } as GeoJSON.FeatureCollection,
    edges: { type: "FeatureCollection", features: edgeFeatures } as GeoJSON.FeatureCollection,
  };
}

function colorExpression(field: string | undefined, fc: GeoJSON.FeatureCollection, fallback: string) {
  if (!field) return fallback;
  const { min, max, count } = minMax(fc, field);
  if (count < 2 || min === max) return fallback;
  // "case" prüft zuerst, ob das Feature überhaupt einen numerischen Wert
  // im Feld hat, BEVOR interpoliert wird - ["get", field] liefert bei
  // fehlendem/null/NaN-Wert kein "number", dann greift NAN_COLOR statt
  // stillschweigend auf min zu färben (früheres Verhalten von
  // ["to-number", ["get", field], min]: der min-Default sah optisch wie
  // ein echter Minimalwert aus).
  return [
    "case",
    ["==", ["typeof", ["get", field]], "number"],
    [
      "interpolate",
      ["linear"],
      ["get", field],
      min,
      "#2c7bb6",
      (min + max) / 2,
      "#ffffbf",
      max,
      "#d7191c",
    ],
    NAN_COLOR,
  ] as unknown as string;
}

// FA65: Lizenzzeile der Daten eines Knotens für die Karten-Attribution.
// Sie steht als customAttribution im Attribution-Control der Karte, neben dem
// Kachel-Nachweis der Basiskarte; wechselt der Knoten, wird das Control mit
// der neuen Zeile ersetzt (MapLibre-Gegenstück zu Leaflets addAttribution) -
// so steht immer genau die Lizenz des gezeigten Knotens dort.
// Reiner Text (HTML-Sonderzeichen maskiert), weil MapLibre den Wert als HTML setzt.
// "Daten:" bzw. "Karte:" trennen die beiden Nachweise - bei OSM-Daten stand
// sonst zweimal "OpenStreetMap contributors" ohne erkennbaren Unterschied.
function attributionText(attribution: AttributionOut | null | undefined): string | undefined {
  if (!attribution || attribution.licenses.length === 0) return undefined;
  const escape = (t: string) =>
    t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  return `Daten: ${attribution.licenses
    .map((lic) => escape(lic.attribution ? `${lic.attribution} (${lic.name})` : lic.name))
    .join(" · ")}`;
}

export default function MapView({
  result,
  colorField,
  showOverlays = true,
  onRasterError,
  attribution,
}: {
  result: NodeResult | null;
  colorField?: string;
  showOverlays?: boolean;
  // FA54: das Rasterbild (raster.png) ist eine eigene Anfrage, die auch dann
  // scheitern kann, wenn das Knoten-Ergebnis davor ankam (z. B. weil eine
  // andere Backend-Instanz antwortet). MapLibre meldet das nur als Karten-Fehler;
  // der Aufrufer entscheidet, was daraus folgt (NodeInspector prüft den Lauf).
  onRasterError?: () => void;
  // FA65: Lizenzen des gezeigten Knotens.
  attribution?: AttributionOut | null;
}) {
  const container = useRef<HTMLDivElement>(null);
  const wrapper = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const loadedRef = useRef(false);
  // Ref statt Prop im Karten-Effekt: der Fehler-Handler wird einmal beim
  // Anlegen der Karte registriert und muss trotzdem den aktuellen Callback sehen.
  const onRasterErrorRef = useRef(onRasterError);
  // FA65: aktuelle Lizenzen und das Control, das sie zeigt (s. Effekt unten).
  const attributionRef = useRef(attribution);
  const attributionControlRef = useRef<maplibregl.AttributionControl | null>(null);
  useEffect(() => {
    onRasterErrorRef.current = onRasterError;
  }, [onRasterError]);
  const [shownCount, setShownCount] = useState(0);
  const [trueTotal, setTrueTotal] = useState(0);
  const [clusterActive, setClusterActive] = useState(false);
  // Darstellung punktreicher Layer: Cluster-Blasen (Default) oder
  // Dichte-Heatmap. Der Umschalter erscheint nur, wenn genug Punkte für
  // eine der beiden Aggregat-Darstellungen vorhanden sind.
  const [pointMode, setPointMode] = useState<"cluster" | "heatmap">("cluster");
  const [denseLayer, setDenseLayer] = useState(false);

  function clearOverlays(map: maplibregl.Map) {
    for (const id of OVERLAY_IDS) if (map.getLayer(id)) map.removeLayer(id);
    for (const id of SOURCE_IDS) if (map.getSource(id)) map.removeSource(id);
  }

  function fit(map: maplibregl.Map, bounds?: [number, number, number, number] | null) {
    if (!bounds) return;
    const [minx, miny, maxx, maxy] = bounds;
    if ([minx, miny, maxx, maxy].some((v) => !Number.isFinite(v))) return;
    map.fitBounds([[minx, miny], [maxx, maxy]], { padding: 40, maxZoom: 14, duration: 500 });
  }

  function popup(map: maplibregl.Map, layerId: string) {
    map.on("click", layerId, (e: maplibregl.MapLayerMouseEvent) => {
      const feature = e.features?.[0];
      if (!feature) return;
      const props = feature.properties || {};
      const entries = Object.entries(props).filter(([k]) => k !== "geometry");

      // Baut den Inhalt als DOM-Knoten statt als HTML-String: Werte aus
      // (ggf. hochgeladenen/fremden) Feature-Properties könnten sonst
      // HTML/Skripte enthalten (z. B. <img onerror=...>) und würden von
      // setHTML() ausgeführt. textContent parst Werte nie als Markup.
      const container = document.createElement("div");
      if (entries.length === 0) {
        container.textContent = "(keine Attribute)";
      } else {
        for (const [k, v] of entries) {
          const row = document.createElement("div");
          const label = document.createElement("b");
          label.textContent = k;
          row.appendChild(label);
          row.appendChild(document.createTextNode(`: ${String(v)}`));
          container.appendChild(row);
        }
      }
      new maplibregl.Popup().setLngLat(e.lngLat).setDOMContent(container).addTo(map);
    });
    map.on("mouseenter", layerId, () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", layerId, () => (map.getCanvas().style.cursor = ""));
  }

  /** Klick auf eine Cluster-Blase zoomt so weit, dass sie zerfällt. */
  function clusterZoom(map: maplibregl.Map, layerId: string, sourceId: string) {
    map.on("click", layerId, (e: maplibregl.MapLayerMouseEvent) => {
      const feature = e.features?.[0];
      const clusterId = feature?.properties?.cluster_id;
      if (clusterId == null) return;
      const source = map.getSource(sourceId) as maplibregl.GeoJSONSource | undefined;
      if (!source) return;
      Promise.resolve(source.getClusterExpansionZoom(clusterId as number))
        .then((zoom) => {
          const geometry = feature?.geometry;
          if (geometry?.type !== "Point") return;
          map.easeTo({ center: geometry.coordinates as [number, number], zoom, duration: 400 });
        })
        .catch((err) => console.error("[MapView] getClusterExpansionZoom:", err));
    });
    map.on("mouseenter", layerId, () => (map.getCanvas().style.cursor = "pointer"));
    map.on("mouseleave", layerId, () => (map.getCanvas().style.cursor = ""));
  }

  function render() {
    const map = mapRef.current;
    if (!map) return;
    clearOverlays(map);
    if (!result) {
      setShownCount(0);
      setTrueTotal(0);
      setClusterActive(false);
      setDenseLayer(false);
      return;
    }

    const kind = result.describe.kind;

    if (kind === "vector" && result.geojson) {
      // Das Backend begrenzt bereits serverseitig (siehe
      // backend/geofact_web/routers/runs.py, _MAX_NODE_FEATURES) - der
      // clientseitige Cap hier ist nur ein Sicherheitsnetz; der wahre
      // Objektzähler kommt aus describe() (unbeschränkt berechnet).
      const { fc: geojson } = capFeatures(result.geojson);
      setShownCount(geojson.features.length);
      setTrueTotal(result.describe.kind === "vector" ? result.describe.feature_count : geojson.features.length);

      // Punkte und Flaechen/Linien in getrennte Sources: cluster:true
      // verwirft Nicht-Punkte, deshalb dürfen beide nicht in derselben
      // Source liegen.
      const { points, others } = splitByGeometry(geojson);
      const fill = colorExpression(colorField, geojson, "#3b82f6");
      const dense = points.features.length >= CLUSTER_MIN_POINTS;
      const heatmap = dense && pointMode === "heatmap";
      const clustered = dense && !heatmap;
      setDenseLayer(dense);
      setClusterActive(clustered);

      if (others.features.length > 0) {
        map.addSource("geo-src", { type: "geojson", data: others });
        map.addLayer({
          id: "geo-fill",
          type: "fill",
          source: "geo-src",
          filter: ["==", ["geometry-type"], "Polygon"],
          paint: { "fill-color": fill, "fill-opacity": 0.45, "fill-outline-color": "#1e3a8a" },
        });
        map.addLayer({
          id: "geo-line",
          type: "line",
          source: "geo-src",
          filter: ["==", ["geometry-type"], "LineString"],
          paint: { "line-color": fill, "line-width": 2.5 },
        });
        popup(map, "geo-fill");
        popup(map, "geo-line");

        // Punktmarkierung auf dem Schwerpunkt jeder Flaeche/Linie, damit ein
        // kleines Flächenergebnis auf Landesebene überhaupt auffällt
        // (siehe CENTROID_MAX_ZOOM). Blendet beim Hineinzoomen aus, sobald
        // die Geometrie selbst groß genug ist.
        map.addSource("geo-centroid-src", { type: "geojson", data: centroidMarkers(others) });
        map.addLayer({
          id: "geo-centroid",
          type: "circle",
          source: "geo-centroid-src",
          maxzoom: CENTROID_MAX_ZOOM + 1,
          paint: {
            "circle-radius": 6,
            "circle-color": fill,
            "circle-stroke-color": "#fff",
            "circle-stroke-width": 1,
            "circle-opacity": [
              "interpolate", ["linear"], ["zoom"],
              CENTROID_MAX_ZOOM - 1, 1,
              CENTROID_MAX_ZOOM + 1, 0,
            ],
            "circle-stroke-opacity": [
              "interpolate", ["linear"], ["zoom"],
              CENTROID_MAX_ZOOM - 1, 1,
              CENTROID_MAX_ZOOM + 1, 0,
            ],
          },
        });
        popup(map, "geo-centroid");
      }

      if (points.features.length > 0) {
        map.addSource("geo-points-src", {
          type: "geojson",
          data: points,
          cluster: clustered,
          clusterRadius: CLUSTER_RADIUS,
          clusterMaxZoom: CLUSTER_MAX_ZOOM,
        });
        if (clustered) {
          map.addLayer({
            id: "geo-cluster",
            type: "circle",
            source: "geo-points-src",
            filter: ["has", "point_count"],
            paint: {
              "circle-color": clusterStep(CLUSTER_COLORS),
              "circle-radius": clusterStep(CLUSTER_RADII),
              "circle-opacity": 0.8,
              "circle-stroke-color": "#fff",
              "circle-stroke-width": 1.5,
            },
          });
          map.addLayer({
            id: "geo-cluster-count",
            type: "symbol",
            source: "geo-points-src",
            filter: ["has", "point_count"],
            layout: {
              "text-field": ["get", "point_count_abbreviated"],
              // Muss ein Font sein, den der glyphs-Endpoint des BASEMAP-
              // Styles tatsächlich anbietet (MapLibre-Demotiles).
              "text-font": ["Open Sans Semibold"],
              "text-size": 12,
              "text-allow-overlap": true,
            },
            paint: { "text-color": "#fff" },
          });
          clusterZoom(map, "geo-cluster", "geo-points-src");
        }
        if (heatmap) {
          // Dichte-Heatmap statt Cluster-Blasen: färbt Regionen nach
          // Punktdichte ein. Blendet beim Hineinzoomen aus, während die
          // Einzelpunkte (geo-point, minzoom unten) einblenden.
          map.addLayer({
            id: "geo-heatmap",
            type: "heatmap",
            source: "geo-points-src",
            maxzoom: CLUSTER_MAX_ZOOM + 1,
            paint: {
              // Radius/Intensitaet bewusst zurückhaltend: bei stadtweiten
              // Punktwolken (z. B. ~2000 Haltestellen) sättigt eine zu
              // heiße Heatmap sonst großflächig ins Rot und
              // differenziert nicht mehr.
              "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 6, 8, 12, 20],
              "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 6, 0.3, 12, 0.9],
              "heatmap-color": [
                "interpolate",
                ["linear"],
                ["heatmap-density"],
                0, "rgba(44,123,182,0)",
                0.2, "#abd9e9",
                0.45, "#ffffbf",
                0.7, "#fdae61",
                1, "#d7191c",
              ],
              "heatmap-opacity": [
                "interpolate", ["linear"], ["zoom"],
                CLUSTER_MAX_ZOOM - 1, 0.85,
                CLUSTER_MAX_ZOOM + 1, 0,
              ],
            },
          });
        }
        map.addLayer({
          id: "geo-point",
          type: "circle",
          source: "geo-points-src",
          // Bei aktivem Clustering nur die noch nicht aggregierten
          // Einzelpunkte zeichnen; ohne Clustering alle. Im Heatmap-Modus
          // erscheinen Einzelpunkte erst, wenn die Heatmap ausblendet.
          ...(clustered ? { filter: ["!", ["has", "point_count"]] } : {}),
          ...(heatmap ? { minzoom: CLUSTER_MAX_ZOOM } : {}),
          paint: { "circle-radius": 6, "circle-color": fill, "circle-stroke-color": "#fff", "circle-stroke-width": 1 },
        });
        popup(map, "geo-point");
      }
      fit(map, result.describe.bounds);
    } else if (kind === "graph" && result.graph) {
      // Graph-Knoten werden bewusst NICHT geclustert: Blasen würden die
      // Endpunkte der Kanten verdecken und die Topologie - der eigentliche
      // Inhalt dieser Ansicht - unlesbar machen.
      const { nodes: allNodes, edges: allEdges } = graphToGeoJSON(result.graph);
      const { fc: nodes, truncated: cut } = capFeatures(allNodes);
      setClusterActive(false);
      setDenseLayer(false);
      setShownCount(nodes.features.length);
      setTrueTotal(result.describe.kind === "graph" ? result.describe.node_count : allNodes.features.length);
      const keptIds = cut > 0 ? new Set(nodes.features.map((f) => f.properties?.id)) : null;
      const edges = keptIds
        ? { ...allEdges, features: allEdges.features.filter((f) => keptIds.has(f.properties?.source) && keptIds.has(f.properties?.target)) }
        : allEdges;
      map.addSource("graph-edges-src", { type: "geojson", data: edges });
      map.addSource("graph-nodes-src", { type: "geojson", data: nodes });
      map.addLayer({
        id: "graph-edges",
        type: "line",
        source: "graph-edges-src",
        paint: { "line-color": "#94a3b8", "line-width": 1.5 },
      });
      const nodeColor = colorExpression(colorField || "betweenness", nodes, "#f59e0b");
      map.addLayer({
        id: "graph-nodes",
        type: "circle",
        source: "graph-nodes-src",
        paint: {
          "circle-radius": 6,
          "circle-color": nodeColor,
          "circle-stroke-color": "#0b1220",
          "circle-stroke-width": 1.5,
        },
      });
      popup(map, "graph-nodes");
      const b = boundsOf(nodes);
      fit(map, b);
    } else if (kind === "raster" && result.raster_png_url) {
      setShownCount(0);
      setTrueTotal(0);
      setClusterActive(false);
      setDenseLayer(false);
      const bounds = result.describe.bounds;
      if (bounds) {
        const [minx, miny, maxx, maxy] = bounds;
        map.addSource(RASTER_SOURCE_ID, {
          type: "image",
          url: apiAbsolute(result.raster_png_url),
          coordinates: [
            [minx, maxy],
            [maxx, maxy],
            [maxx, miny],
            [minx, miny],
          ],
        });
        map.addLayer({ id: "raster-overlay", type: "raster", source: RASTER_SOURCE_ID, paint: { "raster-opacity": 0.8 } });
        fit(map, bounds);
      }
    }
  }

  useEffect(() => {
    if (!container.current || mapRef.current) return;
    const map = new maplibregl.Map({
      container: container.current,
      style: BASEMAP,
      center: [13.73, 51.05],
      zoom: 7,
      // Eigenes Attribution-Control (s. Effekt unten), damit die Lizenz des
      // gezeigten Knotens dazukommen kann.
      attributionControl: false,
    });
    const attributionControl = new maplibregl.AttributionControl({
      compact: true,
      customAttribution: attributionText(attributionRef.current),
    });
    map.addControl(attributionControl, "bottom-right");
    attributionControlRef.current = attributionControl;
    map.addControl(new maplibregl.NavigationControl({}), "top-right");
    map.addControl(
      new maplibregl.FullscreenControl({ container: wrapper.current ?? undefined }),
      "top-right"
    );
    map.on("error", (e) => {
      console.error("[MapView] maplibre error:", e.error ?? e);
      if ((e as { sourceId?: string }).sourceId === RASTER_SOURCE_ID) onRasterErrorRef.current?.();
    });
    map.on("load", () => {
      loadedRef.current = true;
      render();
    });
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
      loadedRef.current = false;
      attributionControlRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // FA65: Lizenzzeile wechselt mit dem Knoten -> Control mit neuer
  // customAttribution ersetzen (MapLibre kennt kein nachträgliches Setzen).
  // Abhängigkeit ist der Text, nicht das Objekt: ein neu geladener Status mit
  // denselben Lizenzen ersetzt das Control nicht.
  const attributionLine = attributionText(attribution);
  useEffect(() => {
    attributionRef.current = attribution;
    const map = mapRef.current;
    const old = attributionControlRef.current;
    if (!map || !old) return;
    const next = new maplibregl.AttributionControl({
      compact: true,
      customAttribution: attributionText(attribution),
    });
    map.removeControl(old);
    map.addControl(next, "bottom-right");
    attributionControlRef.current = next;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attributionLine]);

  useEffect(() => {
    if (loadedRef.current) render();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, colorField, pointMode]);

  const legend = useMemo((): LegendInfo | null => {
    if (!result) return null;
    const kind = result.describe.kind;
    if (kind === "vector" && colorField && result.geojson) {
      const fc = result.geojson;
      const hasMissing = minMax(fc, colorField).count < fc.features.length;
      const range = numericRange(fc, colorField);
      if (range) return { label: colorField, min: range[0], max: range[1], hasMissing };
      const value = singleValue(fc, colorField);
      return value !== null ? { label: colorField, min: value, max: value, single: true, hasMissing } : null;
    }
    if (kind === "graph" && result.graph) {
      const field = colorField || "betweenness";
      const nodesFc: GeoJSON.FeatureCollection = {
        type: "FeatureCollection",
        features: result.graph.nodes.map((n) => ({
          type: "Feature",
          geometry: { type: "Point", coordinates: [0, 0] },
          properties: n as Record<string, unknown>,
        })),
      };
      const hasMissing = minMax(nodesFc, field).count < nodesFc.features.length;
      const range = numericRange(nodesFc, field);
      if (range) return { label: field, min: range[0], max: range[1], hasMissing };
      const value = singleValue(nodesFc, field);
      return value !== null ? { label: field, min: value, max: value, single: true, hasMissing } : null;
    }
    if (kind === "raster" && result.describe.stats) {
      return { label: "Rasterwert", min: result.describe.stats.p2, max: result.describe.stats.p98 };
    }
    return null;
  }, [result, colorField]);

  return (
    <div ref={wrapper} className="map-fullscreen-wrapper relative h-full w-full">
      <div ref={container} className="h-full w-full" />
      {showOverlays && trueTotal > shownCount && (
        <div className="absolute right-3 top-3 z-10 rounded-lg border border-amber-500 bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-900 shadow-lg dark:border-amber-400 dark:bg-amber-950 dark:text-amber-200">
          {formatCount(shownCount)} von {formatCount(trueTotal)} Objekten angezeigt · oben nachladen
          oder vollständig im ZIP-Download
        </div>
      )}
      {showOverlays && clusterActive && trueTotal <= shownCount && (
        <div className="absolute right-3 top-3 z-10 rounded-lg border bg-card/95 px-3 py-1.5 text-xs text-muted-foreground shadow-lg backdrop-blur">
          {formatCount(shownCount)} Objekte · Punkte gruppiert · hineinzoomen oder Gruppe anklicken zeigt
          Einzelobjekte
        </div>
      )}
      {showOverlays && denseLayer && (
        <div
          role="radiogroup"
          aria-label="Darstellung"
          className="absolute bottom-3 right-3 z-10 flex overflow-hidden rounded-lg border bg-card/95 text-xs shadow-lg backdrop-blur"
        >
          <button
            type="button"
            role="radio"
            aria-checked={pointMode === "cluster"}
            onClick={() => setPointMode("cluster")}
            className={`px-3 py-1.5 transition-colors ${
              pointMode === "cluster"
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-accent"
            }`}
          >
            Gruppen
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={pointMode === "heatmap"}
            onClick={() => setPointMode("heatmap")}
            className={`px-3 py-1.5 transition-colors ${
              pointMode === "heatmap"
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:bg-accent"
            }`}
          >
            Dichte
          </button>
        </div>
      )}
      {showOverlays && legend && (
        <div className="absolute bottom-3 left-3 z-10 rounded-lg border bg-card/95 p-3 text-xs shadow-lg backdrop-blur">
          <div className="mb-1 font-semibold text-card-foreground">{legend.label}</div>
          {legend.single ? (
            <div className="text-muted-foreground">{formatLegendValue(legend.min)}</div>
          ) : (
            <>
              <div className="flex h-2 w-40 overflow-hidden rounded">
                {SCALE.map((c) => (
                  <div key={c} className="flex-1" style={{ background: c }} />
                ))}
              </div>
              <div className="mt-0.5 flex w-40 justify-between text-muted-foreground">
                <span>{formatLegendValue(legend.min)}</span>
                <span>{formatLegendValue(legend.max)}</span>
              </div>
            </>
          )}
          {legend.hasMissing && (
            <div className="mt-1.5 flex items-center gap-1.5 text-muted-foreground">
              <span
                className="inline-block size-2.5 rounded-sm border"
                style={{ background: NAN_COLOR }}
              />
              <span>keine Daten</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function boundsOf(fc: GeoJSON.FeatureCollection): [number, number, number, number] | null {
  let minx = Infinity,
    miny = Infinity,
    maxx = -Infinity,
    maxy = -Infinity;
  for (const f of fc.features) {
    if (f.geometry.type !== "Point") continue;
    const [x, y] = (f.geometry as GeoJSON.Point).coordinates;
    minx = Math.min(minx, x);
    miny = Math.min(miny, y);
    maxx = Math.max(maxx, x);
    maxy = Math.max(maxy, y);
  }
  if (!Number.isFinite(minx)) return null;
  return [minx, miny, maxx, maxy];
}

function apiAbsolute(path: string): string {
  // raster_png_url kommt roh vom Backend (kein Token) - <img>/MapLibre-
  // Image-Source können keinen Authorization-Header setzen, siehe
  // lib/auth.ts::withTokenParam und die passende Server-Ausnahme für
  // genau diese Route (backend/geofact_web/auth.py).
  if (path.startsWith("http")) return withTokenParam(path);
  return withTokenParam(`${API_BASE}${path}`);
}
