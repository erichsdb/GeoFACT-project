"""Implements: FA41 (op: rest), FA42 (source: rest), FA43 (type: rest).

Contract-Tests für die REST-Bausteine des Kerns. Endpunkt und Vertrag
stehen jeweils direkt in der Szenario-Konfiguration. Der Dienst ist ein
echter lokaler HTTP-Server (127.0.0.1, zufälliger Port) - kein Mock, kein
Internet.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import ValidationError

from geofact.core.scenario import Scenario
from geofact.builtin import _rest_client as rest_client
from geofact.engine.executor import run_scenario
from geofact.engine.outputs import write_outputs
from geofact.engine.events import STEP_WARNING

BBOX_REGION = "12.30,51.30,12.40,51.36"
INSIDE = [(12.32, 51.31), (12.38, 51.35)]
OUTSIDE = (13.00, 51.00)


def _fc(coords, **props):
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": list(c)},
                "properties": {"nr": i, **props},
            }
            for i, c in enumerate(coords)
        ],
    }


class _Service(BaseHTTPRequestHandler):
    log: list[dict] = []
    flaky_calls = {"n": 0}

    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        _Service.log.append(
            {
                "method": "GET",
                "path": url.path,
                "query": q,
                "auth": self.headers.get("Authorization"),
            }
        )
        port = self.server.server_address[1]
        if url.path == "/geojson":
            self._send(200, _fc(INSIDE + [OUTSIDE], art=q.get("art")))
        elif url.path == "/json":
            self._send(
                200,
                {
                    "data": {
                        "results": [
                            {"name": "A", "lon": 12.32, "lat": 51.31, "meta": {"x": 1}},
                            {"name": "B", "lon": 12.38, "lat": 51.35, "meta": {"x": 2}},
                        ]
                    }
                },
            )
        elif url.path == "/wkt":
            self._send(200, [{"id": 7, "geom": "POINT (12.35 51.33)"}])
        elif url.path == "/ogc":  # OGC API Features: drei Seiten über next-Links
            page = int(q.get("page", 1))
            body = _fc([INSIDE[0]], seite=page)
            if page < 3:
                body["links"] = [
                    {
                        "rel": "next",
                        "href": f"http://127.0.0.1:{port}/ogc?page={page + 1}",
                    }
                ]
            self._send(200, body)
        elif url.path == "/endlos":
            self._send(
                200,
                {
                    **_fc([INSIDE[0]]),
                    "links": [
                        {"rel": "next", "href": f"http://127.0.0.1:{port}/endlos"}
                    ],
                },
            )
        elif url.path == "/offset":
            offset, limit = int(q["offset"]), int(q["limit"])
            total = [(12.31 + i * 0.01, 51.32) for i in range(5)]
            self._send(200, _fc(total[offset : offset + limit]))
        elif url.path == "/wackelig":
            _Service.flaky_calls["n"] += 1
            if _Service.flaky_calls["n"] == 1:
                self._send(503, {"error": "kurz überlastet"})
            else:
                self._send(200, _fc(INSIDE))
        else:
            self._send(500, {"error": "kaputt"})

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        _Service.log.append({"method": "POST", "path": self.path, "body": body})
        if self.path == "/suche":
            self._send(200, _fc(INSIDE, art=body.get("art")))
        elif self.path == "/echo":  # gibt die Features des Eingangs zurück
            self._send(200, body["inputs"]["features"])
        elif (
            self.path == "/ogc-process"
        ):  # Eingaben verschachtelt wie OGC API Processes
            fc = body["inputs"]["geometrie"]["value"]
            for f in fc["features"]:
                f["properties"]["abstand"] = body["inputs"]["abstand"]
            self._send(200, fc)
        elif self.path == "/json-antwort":
            self._send(200, {"treffer": [{"wkt": "POINT (12.35 51.33)", "wert": 3}]})
        elif self.path == "/leer":
            self._send(200, {"type": "FeatureCollection", "features": []})
        elif self.path == "/keinfc":
            self._send(200, {"ergebnis": 42})
        elif self.path == "/kml":
            n = len(body["result"]["features"])
            self._send(
                200,
                f"<kml><name>{body.get('titel', '-')}</name>{n}</kml>".encode(),
                "application/vnd.google-earth.kml+xml",
            )
        else:
            self._send(500, {"error": "kaputt"})


@pytest.fixture(scope="module")
def service():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setattr(rest_client.time, "sleep", lambda s: None)
    _Service.log.clear()
    _Service.flaky_calls["n"] = 0


def _scenario(layers, steps=None, output=None) -> Scenario:
    first = layers[0]["id"]
    return Scenario(
        **{
            "scenario": {"name": "REST", "region": BBOX_REGION},
            "layers": layers,
            "steps": steps
            or [
                {
                    "id": "buffered",
                    "op": "buffer",
                    "inputs": {"geometry": first},
                    "params": {"radius_km": 0.1},
                }
            ],
            "output": output or [{"type": "geojson", "source": "buffered"}],
        }
    )


def _rest_layer(url, **extra) -> dict:
    return {"id": "r", "source": "rest", "url": url, **extra}


# =====================================================================
# FA42: source: rest
# =====================================================================


def test_fa42_geojson_get_with_query_and_region_bbox(service):
    """Happy Path: Query und Regions-BBox gehen an den Dienst, Objekte
    außerhalb der Region werden verworfen, Ergebnis im Arbeits-CRS."""
    result = run_scenario(
        _scenario([_rest_layer(f"{service}/geojson", query={"art": "pegel"})])
    )
    data = result.store["r"]
    assert list(data["nr"]) == [0, 1]
    assert set(data["art"]) == {"pegel"}
    assert data.crs.to_epsg() == 32633
    q = _Service.log[0]["query"]
    assert q["art"] == "pegel"
    assert [round(float(v), 2) for v in q["bbox"].split(",")] == [
        12.30,
        51.30,
        12.40,
        51.36,
    ]


def test_fa42_json_response_with_items_path_and_xy_mapping(service):
    layer = _rest_layer(
        f"{service}/json",
        response={
            "format": "json",
            "items": "data.results",
            "geometry": {"x_column": "lon", "y_column": "lat"},
        },
    )
    data = run_scenario(_scenario([layer])).store["r"]
    assert list(data["name"]) == ["A", "B"]
    assert (
        "meta" not in data.columns
    )  # verschachtelte Werte werden nicht als Spalte übernommen


def test_fa42_json_response_with_wkt_and_root_list(service):
    layer = _rest_layer(
        f"{service}/wkt",
        region="none",
        response={"format": "json", "geometry": {"wkt_column": "geom"}},
    )
    data = run_scenario(_scenario([layer])).store["r"]
    assert list(data["id"]) == [7]


def test_fa42_ogc_next_link_paging_collects_all_pages(service):
    data = run_scenario(
        _scenario([_rest_layer(f"{service}/ogc", paging={"type": "next_link"})])
    ).store["r"]
    assert sorted(data["seite"]) == [1, 2, 3]


def test_fa42_offset_paging(service):
    layer = _rest_layer(f"{service}/offset", paging={"type": "offset", "page_size": 2})
    data = run_scenario(_scenario([layer])).store["r"]
    assert len(data) == 5
    assert [e["query"]["offset"] for e in _Service.log] == ["0", "2", "4"]


def test_fa42_paging_limit_is_an_error_not_a_silent_cut(service):
    """Randfall: mehr Seiten als erlaubt -> expliziter Fehler (Regel 7)."""
    layer = _rest_layer(
        f"{service}/endlos", paging={"type": "next_link", "max_pages": 3}
    )
    with pytest.raises(RuntimeError, match="mehr als 3 Seiten"):
        run_scenario(_scenario([layer]))


def test_fa42_post_with_body_and_bbox(service):
    layer = _rest_layer(f"{service}/suche", method="POST", body={"art": "grundwasser"})
    data = run_scenario(_scenario([layer])).store["r"]
    assert set(data["art"]) == {"grundwasser"}
    assert _Service.log[0]["body"]["bbox"].startswith("12.3")


def test_fa42_snapshot_reuses_data_until_refresh(service):
    scenario = _scenario([_rest_layer(f"{service}/geojson")])
    run_scenario(scenario)
    run_scenario(scenario)
    assert len(_Service.log) == 1
    run_scenario(scenario, refresh_snapshots=True)
    assert len(_Service.log) == 2


def test_fa42_get_is_retried_on_temporary_overload(service):
    data = run_scenario(_scenario([_rest_layer(f"{service}/wackelig")])).store["r"]
    assert len(data) == 2
    assert _Service.flaky_calls["n"] == 2


def test_fa42_unknown_field_is_rejected_before_run(service):
    """Typfehler-Fall: ein vertipptes Feld verschwindet nicht still."""
    with pytest.raises(ValidationError, match="quey"):
        _scenario([_rest_layer(f"{service}/geojson", quey={"art": "pegel"})])
    assert _Service.log == []


def test_fa42_env_vars_without_prefix_are_rejected(service):
    """Sicherheit: eine fremde Szenario-Datei darf keine Server-Geheimnisse
    in die Anfrage einsetzen - nur GEOFACT_REST_-Variablen."""
    with pytest.raises(ValidationError, match="OPENROUTER_API_KEY.*nicht erlaubt"):
        _scenario(
            [
                _rest_layer(
                    f"{service}/geojson",
                    headers={"Authorization": "Bearer ${OPENROUTER_API_KEY}"},
                )
            ]
        )


def test_fa42_prefixed_env_var_is_sent(service, monkeypatch):
    monkeypatch.setenv("GEOFACT_REST_TOKEN", "geheim")
    layer = _rest_layer(
        f"{service}/geojson", headers={"Authorization": "Bearer ${GEOFACT_REST_TOKEN}"}
    )
    run_scenario(_scenario([layer]))
    assert _Service.log[0]["auth"] == "Bearer geheim"


def test_fa42_http_error_names_layer_and_endpoint(service):
    with pytest.raises(RuntimeError, match="Layer 'r'.*/kaputt.*HTTP 500"):
        run_scenario(_scenario([_rest_layer(f"{service}/kaputt")]))


def test_fa42_json_without_geometry_mapping_is_rejected(service):
    with pytest.raises(ValidationError, match="braucht response.geometry"):
        _scenario([_rest_layer(f"{service}/json", response={"format": "json"})])


# =====================================================================
# FA41: op: rest
# =====================================================================


def _rest_step(url, **params) -> dict:
    return {
        "id": "remote",
        "op": "rest",
        "inputs": {"features": "buffered"},
        "params": {"url": url, "input_types": {"features": "vector"}, **params},
    }


def _op_scenario(step, output=None) -> Scenario:
    return _scenario(
        [{"id": "reg", "source": "region"}],
        steps=[
            {
                "id": "buffered",
                "op": "buffer",
                "inputs": {"geometry": "reg"},
                "params": {"radius_km": 0.1},
            },
            step,
        ],
        output=output or [{"type": "geojson", "source": "remote"}],
    )


def test_fa41_default_request_and_roundtrip(service):
    """Happy Path: ohne body gehen die Eingänge als {"inputs": {port: FC}}
    in WGS84; die Antwort landet im Arbeits-CRS der Eingabe."""
    events = []
    result = run_scenario(
        _op_scenario(_rest_step(f"{service}/echo")), on_event=events.append
    )
    remote, local = result.store["remote"], result.store["buffered"]
    assert remote.crs == local.crs
    assert (
        remote.geometry.iloc[0].symmetric_difference(local.geometry.iloc[0]).area < 1.0
    )
    lon, lat = _Service.log[0]["body"]["inputs"]["features"]["features"][0]["geometry"][
        "coordinates"
    ][0][0]
    assert 12.2 < lon < 12.5 and 51.2 < lat < 51.4
    assert any(e.type == STEP_WARNING and "kein HTTPS" in e.message for e in events)


def test_fa41_body_template_places_inputs_like_ogc_processes(service):
    step = _rest_step(
        f"{service}/ogc-process",
        body={
            "inputs": {
                "geometrie": {
                    "value": "@features",
                    "mediaType": "application/geo+json",
                },
                "abstand": 250,
            }
        },
    )
    remote = run_scenario(_op_scenario(step)).store["remote"]
    assert list(remote["abstand"]) == [250]


def test_fa41_json_response_mapping(service):
    step = _rest_step(
        f"{service}/json-antwort",
        response={
            "format": "json",
            "items": "treffer",
            "geometry": {"wkt_column": "wkt"},
        },
    )
    remote = run_scenario(_op_scenario(step)).store["remote"]
    assert list(remote["wert"]) == [3]


def test_fa41_contract_from_config_is_type_checked(service):
    """Typfehler-Fall: der in der Konfiguration deklarierte Vertrag wird wie
    jeder Operationsvertrag geprüft - ein Graph am Vektor-Eingang fällt vor
    dem Lauf auf, der Dienst wird nie angefragt."""
    config = {
        "scenario": {"name": "REST", "region": BBOX_REGION},
        "layers": [{"id": "reg", "source": "region"}, {"id": "l", "source": "region"}],
        "steps": [
            {
                "id": "net",
                "op": "build_network",
                "inputs": {"lines": "l", "nodes": "reg"},
            },
            {
                "id": "remote",
                "op": "rest",
                "inputs": {"features": "net"},
                "params": {
                    "url": f"{service}/echo",
                    "input_types": {"features": "vector"},
                },
            },
        ],
        "output": [{"type": "geojson", "source": "remote"}],
    }
    with pytest.raises(ValidationError, match="erwartet vector, erhält aber graph"):
        Scenario(**config)
    assert _Service.log == []


def test_fa41_inputs_must_match_declared_input_types(service):
    step = _rest_step(f"{service}/echo")
    step["params"]["input_types"] = {"punkte": "vector"}
    with pytest.raises(ValidationError, match="unbekannte Eingänge|fehlende Eingänge"):
        _op_scenario(step)


def test_fa41_placeholder_for_undeclared_input_is_rejected(service):
    step = _rest_step(f"{service}/echo", body={"inputs": {"x": "@gibtsnicht"}})
    with pytest.raises(ValidationError, match="@gibtsnicht"):
        _op_scenario(step)


def test_fa41_raster_input_type_is_rejected(service):
    step = _rest_step(f"{service}/echo")
    step["params"]["input_types"] = {"features": "raster"}
    with pytest.raises(ValidationError) as caught:
        _op_scenario(step)
    assert [error["loc"] for error in caught.value.errors()] == [
        ("steps", 1, "params", "input_types", "features")
    ]


def test_fa41_empty_response_yields_empty_layer(service):
    remote = run_scenario(_op_scenario(_rest_step(f"{service}/leer"))).store["remote"]
    assert len(remote) == 0 and remote.crs is not None


def test_fa41_http_error_names_endpoint(service):
    with pytest.raises(RuntimeError, match="REST-Operation.*/kaputt.*HTTP 500"):
        run_scenario(_op_scenario(_rest_step(f"{service}/kaputt")))


def test_fa41_non_featurecollection_rejected(service):
    with pytest.raises(RuntimeError, match="keine GeoJSON-FeatureCollection"):
        run_scenario(_op_scenario(_rest_step(f"{service}/keinfc")))


# =====================================================================
# FA43: type: rest
# =====================================================================


def _out(url, **extra) -> dict:
    return {
        "type": "rest",
        "source": "buffered",
        "url": url,
        "extension": "kml",
        **extra,
    }


def test_fa43_service_converts_result_and_file_uses_declared_extension(
    service, tmp_path
):
    scenario = _scenario(
        [{"id": "reg", "source": "region"}],
        output=[_out(f"{service}/kml", body={"result": "@result", "titel": "Puffer"})],
    )
    outcome = write_outputs(scenario, run_scenario(scenario), tmp_path / "out")
    assert [p.name for p in outcome.written] == ["00_buffered.kml"]
    assert (
        outcome.written[0].read_text(encoding="utf-8")
        == "<kml><name>Puffer</name>1</kml>"
    )


def test_fa43_default_body_sends_result(service, tmp_path):
    scenario = _scenario(
        [{"id": "reg", "source": "region"}], output=[_out(f"{service}/kml")]
    )
    write_outputs(scenario, run_scenario(scenario), tmp_path / "out")
    assert _Service.log[-1]["body"]["result"]["type"] == "FeatureCollection"


def test_fa43_failing_service_skips_only_this_output(service, tmp_path):
    """Randfall: Dienst antwortet mit Fehler - nur diese Ausgabe entfällt,
    mit Grund; die übrigen entstehen trotzdem."""
    scenario = _scenario(
        [{"id": "reg", "source": "region"}],
        output=[_out(f"{service}/kaputt"), {"type": "geojson", "source": "buffered"}],
    )
    outcome = write_outputs(scenario, run_scenario(scenario), tmp_path / "out")
    assert [p.suffix for p in outcome.written] == [".geojson"]
    assert "HTTP 500" in outcome.skipped[0].reason


def test_fa43_invalid_extension_and_placeholder_rejected(service):
    with pytest.raises(ValidationError, match="extension"):
        _scenario(
            [{"id": "reg", "source": "region"}],
            output=[_out(f"{service}/kml", extension="../x")],
        )
    with pytest.raises(ValidationError, match="@ergebnis"):
        _scenario(
            [{"id": "reg", "source": "region"}],
            output=[_out(f"{service}/kml", body={"r": "@ergebnis"})],
        )
