"""Implements: FA43 (REST-Endpunkt als Ausgabeformat in der Konfiguration).

Ausgabeformat ``type: rest``: ein Dienst erhält das Ergebnis, rechnet es um
(etwa nach KML) und sendet es zurück. Der Endpunkt steht in der Output-Deklaration:

    output:
      - type: rest
        source: versorgungsgebiet
        url: "https://konverter.example/kml"
        extension: kml                          # Endung der abgelegten Antwort
        body: {result: "@result", titel: "Versorgungsgebiet"}

Ohne body sendet GeoFACT {"result": FeatureCollection}; "@result" in einer
body-Vorlage wird durch das Ergebnis als GeoJSON (WGS84) ersetzt. Die Antwort
wird unverändert mit der deklarierten Endung abgelegt und nicht geprüft.

Ist der Dienst nicht erreichbar oder antwortet mit einem Fehler, wird nur diese
Ausgabe mit Grund übersprungen (OutputSkipped), die übrigen entstehen trotzdem.
Nur Vektordaten, wie bei op: rest (FA41).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

from geopandas import GeoDataFrame
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geofact.builtin import _rest_client as rest_client
from geofact.plugin_api import OutputSkipped, register_output


class RestOutputOptions(BaseModel):
    """Formatspezifische Felder einer rest-Ausgabe."""

    model_config = ConfigDict(extra="forbid")

    url: str
    method: Literal["POST", "PUT"] = "POST"
    # Nur Buchstaben/Ziffern: eine Endung darf keinen Pfad außerhalb des
    # Ausgabeverzeichnisses erzeugen.
    extension: str = Field(..., pattern=r"^[A-Za-z0-9]+$")
    body: Optional[dict[str, Any]] = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: Optional[float] = Field(None, gt=0)

    @model_validator(mode="after")
    def check_request(self) -> "RestOutputOptions":
        rest_client.EndpointSpec(url=self.url)
        unknown = rest_client.placeholders_in(self.body) - {"result"}
        if unknown:
            raise ValueError(
                f"body verweist auf unbekannte Platzhalter {sorted('@' + u for u in unknown)}, "
                "erlaubt ist nur @result"
            )
        rest_client.check_env_prefix([self.url, self.body, self.headers], "output")
        return self


@register_output(
    "rest",
    extension="bin",
    options_model=RestOutputOptions,
    description="Ergebnis an einen Dienst senden; dessen Antwort wird als Datei abgelegt",
)
def write_rest(gdf: GeoDataFrame, target: Path, spec) -> None:
    options = spec.options
    context = f"REST-Ausgabe ({spec.source})"
    try:
        url = rest_client.expand_env(options.url, context)
        rest_client.warn_if_insecure(url, context)
        template = options.body if options.body is not None else {"result": "@result"}
        body = rest_client.fill_placeholders(
            rest_client.expand_env(template, context),
            {"result": rest_client.to_feature_collection(gdf, context)},
        )
        raw = rest_client.request_limited(
            options.method,
            url,
            context=context,
            headers=rest_client.expand_env(options.headers, context),
            json_body=body,
            timeout=options.timeout_s,
        )
    except (RuntimeError, ValueError) as exc:
        raise OutputSkipped(str(exc)) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
