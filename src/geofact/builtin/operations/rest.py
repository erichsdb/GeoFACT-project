"""Implements: FA41 (REST-Endpunkt als Operation in der Konfiguration).

Operation ``op: rest``: der Endpunkt und sein Vertrag stehen direkt im Schritt.

    steps:
      - id: versorgungsgebiet
        op: rest
        inputs: {features: einzugsgebiete}
        params:
          url: "https://dienst.example/huelle"
          input_types: {features: vector}     # was der Dienst bekommt
          output_type: vector                 # was er zurückgibt
          body:                               # Anfrage-Vorlage (optional)
            inputs: {geometrie: "@features"}  # @port -> GeoJSON des Eingangs
            bezeichnung: "Versorgungsgebiet"
          response: {format: geojson}         # wie ResponseMapping (FA42)

Der Vertrag (input_types/output_type) wird wie bei jeder Operation vor der
Ausführung geprüft (FA2). RestStep liefert ihn über input_ports()/output_type()
statt über Klassenvariablen; die Parameter sind das Modell ``RestCall``
(``Params = RestCall``).

Ohne body sendet GeoFACT {"inputs": {port: FeatureCollection}}; mit body ist die
Anfrage frei formbar (z. B. OGC API Processes, synchron). Eingaben gehen in WGS84
(RFC 7946), die Antwort wird ins Arbeits-CRS der ersten Eingabe zurückprojiziert.

Lizenz (FA65): ``license`` deklariert die Lizenz der Antwort; ohne Angabe steht der
Schritt in der Namensnennung unter "ohne Lizenzangabe".

Grenzen: nur Vektordaten, nur synchron, POST ohne Wiederholung, keine
Zwischenspeicherung (ein Rechenergebnis hängt von allen Parametern ab).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional, Self

from geopandas import GeoDataFrame
from pydantic import Field, model_validator

from geofact.builtin import _rest_client as rest_client
from geofact.plugin_api import DataType, License, ParamsBase, register_operation, Step


class RestCall(ParamsBase):
    """Inhalt von params eines rest-Schrittes (die Parameter der Operation)."""

    url: str = Field(..., description="Endpunkt; ${GEOFACT_REST_...} für Zugangsdaten")
    method: Literal["POST", "GET"] = "POST"
    input_types: dict[str, Literal["vector"]] = Field(
        ...,
        min_length=1,
        description="Eingänge des Dienstes (Name -> Datentyp), z. B. {features: vector}",
    )
    output_type: Literal["vector"] = Field(
        "vector", description="was der Dienst zurückgibt"
    )
    body: Optional[dict[str, Any]] = Field(
        None, description='Anfrage-Vorlage; "@eingang" wird durch GeoJSON ersetzt'
    )
    query: dict[str, Any] = Field(default_factory=dict, description="Query-Parameter")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP-Header")
    response: rest_client.ResponseMapping = Field(
        default_factory=rest_client.ResponseMapping,
        description="Antwortform: {format: geojson} oder {format: json, items, geometry}",
    )
    timeout_s: Optional[float] = Field(None, gt=0, description="Zeitlimit in Sekunden")
    license: Optional[License] = Field(
        None,
        description="Lizenz der vom Dienst gelieferten Daten (FA65); ohne Angabe steht "
        "der Schritt unter 'ohne Lizenzangabe'",
    )

    @model_validator(mode="after")
    def check_call(self) -> Self:
        rest_client.EndpointSpec(url=self.url)
        if self.method == "GET" and self.body is not None:
            raise ValueError("body gilt nur für method: POST")
        unknown = rest_client.placeholders_in(self.body) - set(self.input_types)
        if unknown:
            raise ValueError(
                f"body verweist auf unbekannte Eingänge {sorted('@' + u for u in unknown)}, "
                f"deklariert sind {sorted(self.input_types)}"
            )
        rest_client.check_env_prefix(
            [self.url, self.query, self.body, self.headers], "params"
        )
        return self

    def request_body(self) -> dict:
        if self.body is not None:
            return self.body
        return {"inputs": {port: f"@{port}" for port in self.input_types}}


class RestStep(Step):
    """Externer Dienst als Operation (FA41). Der Vertrag steht in params:
    input_types (Eingang -> Datentyp) und output_type; url ist der
    Endpunkt, body eine optionale Anfrage-Vorlage mit Platzhaltern
    "@<eingang>", response beschreibt die Antwort (Standard: GeoJSON)."""

    op: Literal["rest"] = "rest"
    INPUT_PORTS: ClassVar[dict[str, DataType]] = {}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR
    Params: ClassVar[type[ParamsBase]] = RestCall

    def input_ports(self) -> dict[str, DataType]:
        call: RestCall = self.params_model  # type: ignore[assignment]
        return {port: DataType(t) for port, t in call.input_types.items()}

    def output_type(self) -> DataType:
        call: RestCall = self.params_model  # type: ignore[assignment]
        return DataType(call.output_type)

    def brings_external_data(self) -> bool:
        """FA65: die Antwort des Dienstes ersetzt die Eingaben - fremde Daten."""
        return True

    def external_license(self) -> Optional[License]:
        call: RestCall = self.params_model  # type: ignore[assignment]
        return call.license


@register_operation(RestStep)
def run_rest(inputs: dict[str, GeoDataFrame], params: dict) -> GeoDataFrame:
    call = RestCall(**params)
    context = "REST-Operation"
    url = rest_client.expand_env(call.url, context)
    rest_client.warn_if_insecure(url, context)
    collections = {
        port: rest_client.to_feature_collection(gdf, f"{context}, Eingang '{port}'")
        for port, gdf in inputs.items()
    }
    body = rest_client.fill_placeholders(
        rest_client.expand_env(call.request_body(), context), collections
    )
    raw = rest_client.request_limited(
        call.method,
        url,
        context=context,
        headers=rest_client.expand_env(call.headers, context),
        params=rest_client.expand_env(call.query, context) or None,
        json_body=body if call.method == "POST" else None,
        timeout=call.timeout_s,
    )
    parsed = rest_client.parse_json(raw, f"{context} ({url})")
    items = rest_client.page_items(parsed, call.response, f"{context} ({url})")
    result = rest_client.items_to_frame(items, call.response, f"{context} ({url})")
    working_crs = next(iter(inputs.values())).crs
    return result.to_crs(working_crs)
