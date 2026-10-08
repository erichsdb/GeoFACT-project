"""Implements: FA42 (REST-Endpunkt als Datenquelle in der Konfiguration), FA64 (``region`` ist die Layer-Region, BBox-Mitsendung heißt ``region_mode``).

Quellart ``source: rest``: der Endpunkt steht direkt im Layer, wie Pfad
und Format bei ``source: file``.

    layers:
      - id: messstellen
        source: rest
        url: "https://portal.example/api/messstellen"
        method: GET                    # GET (Standard) oder POST
        query: {art: pegel}            # Query-Parameter (GET)
        region_mode: bbox              # Region als bbox mitsenden (Standard)
        headers: {Authorization: "Bearer ${GEOFACT_REST_PORTAL_TOKEN}"}
        response:                      # Standard: GeoJSON-FeatureCollection
          format: json
          items: data.results
          geometry: {x_column: lon, y_column: lat}
        paging: {type: next_link}      # OGC API Features; oder type: offset

Die Region geht als BBox in WGS84 mit (``bbox_param``, bei POST im Body); die
Antwort wird nach ``response`` gelesen, bei ``paging`` alle Seiten bis max_pages
(Überschreitung ist ein Fehler statt eines unvollständigen Layers). Danach werden
Objekte außerhalb der Region verworfen (die BBox ist nur Vorfilter). Das Ergebnis
liegt als Snapshot (FA7) vor.

``region_mode`` steuert die Mitsendung (``bbox`` oder ``none``); ``region`` ist wie
bei allen Layern die eigene Layer-Region (FA64). Die alte Schreibweise
``region: bbox|none`` wird mit ``FutureWarning`` auf ``region_mode`` umgesetzt.
"""

from __future__ import annotations

import warnings
from typing import Any, Literal, Optional

from geopandas import GeoDataFrame
from pydantic import Field, model_validator

from geofact.builtin import _rest_client as rest_client
from geofact.plugin_api import DataType, LayerBase, LoadContext, register_source
from geofact.builtin._region_filter import within_region
from geofact.support import snapshot as snapshot_module


class RestLayer(LayerBase):
    """Datenquelle über einen REST-Endpunkt (FA42). Unbekannte Felder sind
    ein Fehler: ein Tippfehler in einer Anfrageangabe darf nicht still
    verschwinden (Goldene Regel 7)."""

    source: Literal["rest"] = "rest"
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR
    url: str
    method: Literal["GET", "POST"] = "GET"
    query: dict[str, Any] = Field(default_factory=dict)
    body: Optional[dict[str, Any]] = None
    headers: dict[str, str] = Field(default_factory=dict)
    region_mode: Literal["bbox", "none"] = Field(
        "bbox",
        description="bbox: Region als BBox mitsenden und danach filtern; none: nichts senden "
        "(früher 'region: bbox|none' - wird weiter angenommen)",
    )
    bbox_param: str = "bbox"
    response: rest_client.ResponseMapping = Field(
        default_factory=rest_client.ResponseMapping
    )
    paging: Optional[rest_client.Paging] = None
    timeout_s: Optional[float] = Field(None, gt=0)

    model_config = {"extra": "forbid"}

    @model_validator(mode="before")
    @classmethod
    def legacy_region_mode(cls, raw: Any) -> Any:
        """Rückwärtskompatibel: ``region: bbox|none`` (vor FA64) meint
        ``region_mode`` - mit Veraltungshinweis. Ein Name oder BBox-Literal in
        ``region`` bleibt die Layer-Region (FA64)."""
        if not isinstance(raw, dict) or raw.get("region") not in ("bbox", "none"):
            return raw
        if "region_mode" in raw:
            raise ValueError(
                f"region: {raw['region']} ist die alte Schreibweise von region_mode - "
                "nur region_mode angeben"
            )
        warnings.warn(
            f"Layer '{raw.get('id')}': 'region: {raw['region']}' ist veraltet, bitte "
            f"'region_mode: {raw['region']}' schreiben ('region' nennt seit FA64 eine "
            "eigene Region des Layers)",
            FutureWarning,
            stacklevel=2,
        )
        migrated = {key: value for key, value in raw.items() if key != "region"}
        migrated["region_mode"] = raw["region"]
        return migrated

    @model_validator(mode="after")
    def check_request(self) -> "RestLayer":
        rest_client.EndpointSpec(url=self.url)  # http(s)-Prüfung
        if self.method == "GET" and self.body is not None:
            raise ValueError(f"Layer '{self.id}': body gilt nur für method: POST")
        if self.paging is not None and self.method != "GET":
            raise ValueError(
                f"Layer '{self.id}': paging wird nur für method: GET unterstützt"
            )
        rest_client.check_env_prefix(
            [self.url, self.query, self.body, self.headers], f"Layer '{self.id}'"
        )
        return self


def _bbox(ctx: LoadContext) -> str:
    return ",".join(f"{v:.6f}" for v in ctx.region.total_bounds)


def _fetch(layer: RestLayer, ctx: LoadContext) -> GeoDataFrame:
    context = f"REST-Quelle (Layer '{layer.id}')"
    url = rest_client.expand_env(layer.url, context)
    rest_client.warn_if_insecure(url, context)
    headers = rest_client.expand_env(layer.headers, context)
    query = rest_client.expand_env(layer.query, context)

    if layer.method == "POST":
        body = rest_client.expand_env(layer.body or {}, context)
        if layer.region_mode == "bbox":
            body = {**body, layer.bbox_param: _bbox(ctx)}
        raw = rest_client.request_limited(
            "POST",
            url,
            context=context,
            headers=headers,
            params=query or None,
            json_body=body,
            timeout=layer.timeout_s,
        )
        items = rest_client.page_items(
            rest_client.parse_json(raw, context), layer.response, context
        )
    else:
        if layer.region_mode == "bbox":
            query = {**query, layer.bbox_param: _bbox(ctx)}
        items = _get_all_pages(layer, url, query, headers, context)

    data = rest_client.items_to_frame(items, layer.response, context)
    if layer.region_mode == "bbox" and not data.empty:
        region = ctx.region.to_crs(data.crs) if data.crs is not None else ctx.region
        data = within_region(data, region)
    return data


def _get_all_pages(
    layer: RestLayer, url: str, query: dict, headers: dict, context: str
) -> list:
    paging = layer.paging
    if paging is not None and paging.type == "offset":
        query = {**query, paging.limit_param: paging.page_size, paging.offset_param: 0}
    items: list = []
    next_url, next_query = url, query
    for page in range(paging.max_pages if paging else 1):
        raw = rest_client.request_limited(
            "GET",
            next_url,
            context=context,
            headers=headers,
            params=next_query,
            timeout=layer.timeout_s,
        )
        body = rest_client.parse_json(raw, context)
        page_items = rest_client.page_items(body, layer.response, context)
        items.extend(page_items)
        if paging is None:
            return items
        if paging.type == "next_link":
            link = rest_client.next_link(body)
            if link is None:
                return items
            # Der next-Link trägt seine Parameter selbst (OGC API Features).
            next_url, next_query = link, None
        else:
            if len(page_items) < paging.page_size:
                return items
            next_query = {
                **next_query,
                paging.offset_param: (page + 1) * paging.page_size,
            }
    raise RuntimeError(
        f"{context}: mehr als {paging.max_pages} Seiten - Abbruch statt eines still "
        "unvollständigen Layers; paging.max_pages erhöhen oder die Anfrage enger fassen"
    )


def _snapshot_payload(layer: RestLayer, ctx: LoadContext) -> dict:
    # Unaufgelöste Werte: Tokens aus ${...} gehören nicht in den Schlüssel.
    dumped = layer.model_dump(
        mode="json",
        exclude={"id", "validation", "timeout_s", "region", "region_mode", "license"},
    )
    # Der Schlüssel bleibt stabil gegenüber älteren Snapshots: region_mode heißt
    # dort 'region', die Layer-Region steckt als Geometrie in region_wkt, und license
    # ist Metadatum und gehört nicht in den Schlüssel.
    dumped["region"] = layer.region_mode
    dumped["region_wkt"] = (
        ctx.region.union_all().wkt if layer.region_mode == "bbox" else None
    )
    return {"kind": "rest_source", **dumped}


@register_source(
    "rest",
    RestLayer,
    description="REST-Endpunkt (GeoJSON oder JSON mit Geometrie-Mapping, OGC-API-Features-Paging)",
)
def load(layer: RestLayer, ctx: LoadContext) -> GeoDataFrame:
    key = snapshot_module.payload_key(_snapshot_payload(layer, ctx))
    try:
        data = snapshot_module.fetch_with_snapshot_key(
            key, lambda: _fetch(layer, ctx), refresh=ctx.refresh
        )
    except RuntimeError as exc:
        raise RuntimeError(
            f"Layer '{layer.id}' konnte nicht geladen werden: {exc}"
        ) from exc
    return data
