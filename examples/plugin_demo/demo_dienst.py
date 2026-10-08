"""Implements: FA41, FA42, FA43 (Demo-Dienst für die REST-Bausteine).

Minimaler HTTP-Dienst mit je einem Endpunkt für die drei REST-Bausteine,
die examples/plugin_demo/szenario_plugins.yaml in der Konfiguration
deklariert:

- GET  /messstellen  Datenquelle (source: rest): Punkte im Raster der BBox
- POST /huelle       Operation (op: rest): konvexe Hülle der Eingabe
- POST /kml          Ausgabe (type: rest): rechnet GeoJSON in KML um

Liegt bewusst NICHT im Plugin-Ordner (sonst würde er als Plugin geladen).

    uv run python examples/plugin_demo/demo_dienst.py      # Port 8765
    set GEOFACT_REST_DEMO_PORT=8765
    set GEOFACT_PLUGIN_PATH=examples/plugin_demo/plugins
    uv run geofact run examples/plugin_demo/szenario_plugins.yaml
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

from shapely.geometry import mapping, shape
from shapely.ops import unary_union


def _messstellen(bbox: str, art: str) -> dict:
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox.split(","))
    features = []
    for i in range(4):
        for j in range(3):
            lon = min_lon + (max_lon - min_lon) * (i + 0.5) / 4
            lat = min_lat + (max_lat - min_lat) * (j + 0.5) / 3
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {"nr": len(features) + 1, "art": art},
                }
            )
    return {"type": "FeatureCollection", "features": features}


def _huelle(payload: dict) -> dict:
    features = payload["inputs"]["features"]["features"]
    geometries = [shape(f["geometry"]) for f in features if f.get("geometry")]
    result = []
    if geometries:
        result.append(
            {
                "type": "Feature",
                "geometry": mapping(unary_union(geometries).convex_hull),
                "properties": {
                    "name": payload.get("bezeichnung", "Hülle"),
                    "anzahl_objekte": len(geometries),
                },
            }
        )
    return {"type": "FeatureCollection", "features": result}


def _kml_geometry(geom) -> str:
    if geom.geom_type == "Point":
        return f"<Point><coordinates>{geom.x},{geom.y}</coordinates></Point>"
    if geom.geom_type == "Polygon":
        ring = " ".join(f"{x},{y}" for x, y in geom.exterior.coords)
        return (
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            f"{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        )
    parts = "".join(_kml_geometry(g) for g in getattr(geom, "geoms", []))
    return f"<MultiGeometry>{parts}</MultiGeometry>"


def _kml(payload: dict) -> str:
    titel = escape(str(payload.get("titel") or "GeoFACT-Ergebnis"))
    placemarks = []
    for feature in payload["result"]["features"]:
        props = feature.get("properties") or {}
        name = escape(str(props.get("name", props.get("nr", ""))))
        placemarks.append(
            f"<Placemark><name>{name}</name>{_kml_geometry(shape(feature['geometry']))}</Placemark>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{titel}</name>{''.join(placemarks)}</Document></kml>"
    )


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, content_type: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 - Name vorgegeben
        url = urlparse(self.path)
        if url.path != "/messstellen":
            self.send_error(404, "GET nur /messstellen")
            return
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self._send(
            json.dumps(_messstellen(query["bbox"], query.get("art", "pegel"))),
            "application/geo+json",
        )

    def do_POST(self):  # noqa: N802 - Name vorgegeben
        payload = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
        )
        if self.path == "/huelle":
            self._send(json.dumps(_huelle(payload)), "application/geo+json")
        elif self.path == "/kml":
            self._send(_kml(payload), "application/vnd.google-earth.kml+xml")
        else:
            self.send_error(404, "POST nur /huelle oder /kml")


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"Demo-Dienst auf http://127.0.0.1:{port} (/messstellen, /huelle, /kml)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
