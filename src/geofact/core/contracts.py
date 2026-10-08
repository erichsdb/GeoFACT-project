"""Implements: FA1, FA2, FA6, FA40 (Verträge der Bausteine), FA64 (Region je Layer), FA65 (Lizenz je Layer), FA73 (License.id).

Die Basisverträge, die jeder Baustein (Kern oder Plugin) kennt:

- ``LayerBase`` + FA6-Regelmodelle: Felder aller Layer; jede Quellart leitet ihr
  Modell hiervon ab (FA40), mit ``region`` (FA64), ``license`` samt Hook
  ``default_license()`` (FA65) und ``check_before_run()`` (FA4/FA44).
- ``License``: Lizenz und Namensnennung (FA65).
- ``Step``: Basis jeder Operation (``INPUT_PORTS``, ``OUTPUT_TYPE``, ``Params``).
- ``ParamsBase``: Basis der verschachtelten ``class Params(ParamsBase)`` einer Operation.
- ``LoadContext``: was ein Konnektor / eine Transformation über den Lauf weiß.
- Protokolle ``SourceLoader``, ``FileReader``, ``TableReader``, ``Transform``,
  ``OutputWriter``, ``OperationRunner``.

Strikte Konfigurationsverträge (FA2): Layer-Modelle, Validierungsregeln, Schritte
und Parameter lehnen unbekannte Schlüssel ab (``extra='forbid'``); ein Tippfehler
ist ein Fehler mit Position, nie eine stillschweigend ignorierte Angabe.

Ein Step registriert sich nicht selbst: erst ``@register_operation`` (core/registry.py)
macht ihn zu einem Baustein. Die konkreten Layer-Modelle und Step-Klassen liegen
in der Datei ihres Bausteins (FA40), dieses Modul kennt nur die Basisklassen.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Optional, Protocol

from geopandas import GeoDataFrame
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pyproj import CRS

from geofact.core.crs import check_bbox, parse_bbox_literal
from geofact.core.errors import line_errors, located_error
from geofact.core.types import DataType

if TYPE_CHECKING:
    from geofact.core.registry import Registry


# --- Layer-Validierung (FA6) ---


class FieldConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: Optional[float] = None
    max: Optional[float] = None


class PrecisionRule(BaseModel):
    """Mindestanzahl Nachkommastellen (erkennt abgeschnittene Koordinaten).
    Prüft Roh-Strings der Quelle; kein Konnektor reicht sie derzeit durch,
    deshalb lehnt die Layer-Konfiguration eine precision-Regel ab (siehe
    LayerBase)."""

    model_config = ConfigDict(extra="forbid")

    min_decimals: int = Field(..., ge=0)


SCENARIO_REGION = "scenario_region"
"""Wert von ``spatial_check.within`` für die aufgelöste Szenario-Region."""

SCENARIO_REGION_CONTEXT = "scenario_region_text"
"""Schlüssel im Validierungskontext der Layer-Modelle: ``scenario.region`` als
Text (BBox-Literal oder Name), z. B. für eine Default-Lizenz, die davon abhängt
(FA65). Fehlt bei Layern, die ohne Szenario validiert werden."""


class SpatialCheck(BaseModel):
    """Containment-Prüfung: liegen alle Geometrien in einer Referenzregion?
    'scenario_region' nutzt die Region aus scenario.region; alternativ die
    ID eines anderen (Vektor-)Layers dieser Konfiguration."""

    model_config = ConfigDict(extra="forbid")

    within: str = SCENARIO_REGION
    # Überschreibt on_violation des Layers für diese Prüfung.
    on_violation: Optional[Literal["warn", "drop", "abort"]] = None


def _hide_unsupported_rules(schema: dict[str, Any]) -> None:
    """JSON-Schema-Hook: ``precision`` bleibt ein Modellfeld, wird aber von jeder
    Layer-Konfiguration abgelehnt (``LayerBase``); das Schema bewirbt sie nicht."""
    schema.get("properties", {}).pop("precision", None)


class LayerValidation(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra=_hide_unsupported_rules)

    required_fields: list[str] = Field(default_factory=list)
    field_types: dict[str, Literal["integer", "float", "string"]] = Field(
        default_factory=dict
    )
    constraints: dict[str, FieldConstraint] = Field(default_factory=dict)
    precision: dict[str, PrecisionRule] = Field(
        default_factory=dict,
        description="Nicht unterstützt: braucht Roh-Strings der Quelle, die kein Konnektor "
        "liefert - wird in einer Layer-Konfiguration abgelehnt.",
    )
    spatial_check: Optional[SpatialCheck] = None
    on_violation: Literal["warn", "drop", "abort"] = "warn"


# --- Layer (FA3, FA4, FA14, FA40) ---

_VERBATIM_PREFIXES: tuple[str, ...] = (
    "©",
    "(c)",
    "copyright",
    "contains",
    "includes",
    "enthält",
    "enthält",
    "quelle",
    "source",
    "datenquelle",
)
"""Anfänge (klein geschrieben) einer Namensnennung, der ``License.line`` kein
Copyright-Zeichen voranstellt: der Text trägt es schon oder hat vorgeschriebenen
Wortlaut (Copernicus: "Contains modified Copernicus Sentinel data")."""


_IRI_FORBIDDEN = frozenset('<>"{}|\\^`')


class License(BaseModel):
    """Lizenz und Namensnennung eines Datensatzes (FA65). ``name`` ist Pflicht
    (z. B. ``ODbL-1.0``), ``attribution`` der Text der Namensnennung,
    ``url`` ein http(s)-Verweis auf die Lizenz. Unveränderlich und damit
    hashbar: die Vereinigung je Knoten (core/provenance.py) entfernt Duplikate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(..., description="Lizenzname oder SPDX-Kennung, z. B. ODbL-1.0")
    attribution: Optional[str] = Field(
        None, description="Text der Namensnennung, z. B. OpenStreetMap contributors"
    )
    url: Optional[str] = Field(
        None, description="Verweis auf den Lizenztext (http/https)"
    )
    id: Optional[str] = Field(
        None,
        description="Maschinenlesbare Kennung der Lizenz als URI, z. B. "
        "https://dalicc.net/licenselibrary/CC-BY-4.0 (FA73: Grundlage der "
        "Kompatibilitätsprüfung; ohne Angabe wird der Name nachgeschlagen)",
    )

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("license.name darf nicht leer sein")
        return value

    @field_validator("attribution")
    @classmethod
    def _attribution_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError(
                "license.attribution darf nicht leer sein (Feld weglassen)"
            )
        return value

    @field_validator("url", "id")
    @classmethod
    def _url_is_http(cls, value: Optional[str], info: ValidationInfo) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        field = info.field_name
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError(
                f"license.{field} muss mit http:// oder https:// beginnen, ist '{value}'"
            )
        # Zeichen, die in keiner IRI stehen dürfen (RFC 3987); sonst scheitert
        # der RDF-Export (dcterms:license) erst beim Schreiben.
        bad = sorted(
            {char for char in value if char.isspace() or char in _IRI_FORBIDDEN}
        )
        if bad:
            shown = ", ".join(repr(char) for char in bad)
            raise ValueError(
                f"license.{field} '{value}' ist keine gültige Adresse (unerlaubte Zeichen: {shown}) - "
                "Leerzeichen als %20 schreiben"
            )
        return value

    def to_dict(self) -> dict[str, Any]:
        """JSON-taugliche Form: ``name``, ``attribution``, ``url`` und nur wenn
        gesetzt ``id`` (FA73); Ausgaben ohne ``id`` bleiben so unverändert."""
        data = self.model_dump()
        if data.get("id") is None:
            data.pop("id", None)
        return data

    def line(self) -> str:
        """Eine Zeile Namensnennung: ``(c) OpenStreetMap contributors (ODbL-1.0,
        https://...)``; ohne ``attribution`` nur Name und Verweis. Das
        Copyright-Zeichen entfällt bei Texten mit eigenem Wortlaut
        (``_VERBATIM_PREFIXES``)."""
        details = ", ".join(part for part in (self.name, self.url) if part)
        if self.attribution:
            return f"{self._attribution_text()} ({details})"
        return f"{self.name} ({self.url})" if self.url else self.name

    def _attribution_text(self) -> str:
        text = self.attribution or ""
        if text.casefold().startswith(_VERBATIM_PREFIXES):
            return text
        return f"© {text}"


_ID_FORBIDDEN = frozenset("/\\#?%")


def check_node_id(value: str) -> str:
    """Eine Layer- oder Schritt-id ist ein Name, kein Pfad: sie wird zu Dateinamen
    und zu einem URL-Segment der Web-Demo. Nicht erlaubt sind ``/``, Backslash,
    ``#``, ``?``, ``%``, Steuerzeichen sowie ``.`` und ``..``."""
    if not value.strip():
        raise ValueError("id darf nicht leer sein")
    if value in (".", ".."):
        raise ValueError(f"id '{value}' ist nicht erlaubt (Verzeichnisname)")
    bad = sorted(
        {char for char in value if char in _ID_FORBIDDEN or not char.isprintable()}
    )
    if bad:
        shown = ", ".join(repr(char) for char in bad)
        raise ValueError(
            f"id '{value}' enthält nicht erlaubte Zeichen ({shown}) - Buchstaben, Ziffern, "
            "'_', '.' und '-' verwenden"
        )
    return value


class LayerBase(BaseModel):
    """Gemeinsame Felder aller Layer. Jede Quellart (Kern oder Plugin)
    leitet ihr Layer-Modell hiervon ab und legt ``source`` per Literal
    auf ihren Namen fest (FA40). Unbekannte Schlüssel sind ein Fehler
    (``extra='forbid'``, gilt für alle abgeleiteten Modelle).

    ``region`` (FA64) gibt einem Layer eine eigene Region; ``license``
    (FA65) seine Lizenz - ohne Angabe gilt ``default_license()`` der
    Quellart (Hook, den eine Quellart überschreibt; keine Liste im Kern)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    data_type: DataType
    validation: Optional[LayerValidation] = None
    region: Optional[str] = Field(
        None,
        description="Eigene Region nur für diesen Layer (BBox-Literal oder Name); "
        "Standard: scenario.region",
    )
    license: Optional[License] = Field(
        None,
        description="Lizenz und Namensnennung der Daten; ohne Angabe gilt der Default der "
        "Quellart (z. B. ODbL-1.0 für OSM)",
    )

    @field_validator("id")
    @classmethod
    def _check_layer_id(cls, value: str) -> str:
        return check_node_id(value)

    @field_validator("region")
    @classmethod
    def _check_layer_region(cls, value: Optional[str]) -> Optional[str]:
        """FA64/FA56: nicht leer; ein BBox-Literal wird wie ``scenario.region`` geprüft."""
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError(
                "region darf nicht leer sein (Feld weglassen = scenario.region)"
            )
        values = parse_bbox_literal(value)
        if values is not None:
            check_bbox(values, text=value)
        return value

    def default_license(self) -> Optional[License]:
        """Lizenz der Quellart, wenn der Layer keine deklariert (FA65). Hook:
        eine Quellart mit bekannter Lizenz (OSM: ODbL-1.0) überschreibt ihn."""
        return None

    def effective_license(self) -> Optional[License]:
        """Die wirksame Lizenz: die deklarierte ersetzt den Default vollständig."""
        return self.license if self.license is not None else self.default_license()

    def check_before_run(self, base_dir: Path | None) -> list[tuple[str, str]]:
        """Vorab-Prüfung vor einem Lauf (FA4/FA44), z. B. fehlende Datei:
        Liste von ``(feld, meldung)``. Default: nichts zu prüfen."""
        return []

    @model_validator(mode="after")
    def check_validation_supported(self) -> "LayerBase":
        """FA6: nur Regeln, die ausgeführt werden; keine, die still nie greift (Goldene Regel 7)."""
        rules = self.validation
        if rules is None:
            return self
        if self.data_type != DataType.VECTOR:
            raise ValueError(
                f"Layer '{self.id}': 'validation' ist nur für Vektor-Layer definiert (FA6), "
                f"data_type ist {self.data_type.value}"
            )
        if rules.precision:
            raise ValueError(
                f"Layer '{self.id}': validation.precision ({sorted(rules.precision)}) wird nicht "
                "unterstützt - die Regel braucht die Roh-Strings der Quelle, die kein Konnektor "
                "liefert. Regel entfernen (Wertebereich: constraints, Datentyp: field_types)."
            )
        return self


# --- Parameter einer Operation ---


class ParamsBase(BaseModel):
    """Basis der Parameter einer Operation (FA2): jede Operation deklariert
    ihre Parameter als verschachtelte ``class Params(ParamsBase)`` - Typen,
    Grenzen (``Field(gt=0)``), Auswahl (``Literal``), Defaults und
    Querprüfungen (``model_validator``) stehen dort an EINER Stelle.
    Unbekannte Schlüssel sind ein Fehler (Goldene Regel 7). Pflichtparameter
    sind die Felder ohne Default; Katalog, Editor und LLM-Beschreibung
    werden aus ``Params.model_json_schema()`` abgeleitet."""

    model_config = ConfigDict(extra="forbid")


# --- Schritte: Contract via ClassVar ---


class Step(BaseModel):
    """Basis jeder Operation. Der Vertrag steht in der Unterklasse:
    ``INPUT_PORTS`` (benannte Eingänge mit Datentyp), ``OUTPUT_TYPE`` und
    die verschachtelte ``class Params(ParamsBase)``. Nach der Validierung ist
    ``params`` das geprüfte Dict INKLUSIVE der Defaults (``Params(...)
    .model_dump()``) - die Ausführungsfunktion bekommt genau dieses Dict."""

    model_config = ConfigDict(extra="forbid")

    id: str
    op: str
    inputs: dict[str, str] = Field(default_factory=dict)
    params: dict = Field(default_factory=dict)

    INPUT_PORTS: ClassVar[dict[str, DataType]] = {}
    OUTPUT_TYPE: ClassVar[DataType] = DataType.VECTOR
    Params: ClassVar[type[ParamsBase]] = ParamsBase

    _params_model: Optional[ParamsBase] = PrivateAttr(default=None)

    @field_validator("id")
    @classmethod
    def _check_step_id(cls, value: str) -> str:
        return check_node_id(value)

    @property
    def params_model(self) -> ParamsBase:
        """Die geprüfte Parameter-Instanz (``Params``) dieses Schrittes."""
        if self._params_model is None:
            self._params_model = type(self).Params.model_validate(self.params)
        return self._params_model

    def produced_fields(self) -> set[str]:
        """Attributspalten, die dieser Schritt seiner Ausgabe hinzufügt.

        Die Web-Ansicht (FA12, view_columns.py) zeigt diese Spalten immer an,
        auch wenn sie dünn besetzt sind (z. B. das Statistikfeld von zonal_stats).
        Default: der Name aus ``params['output_field']``. Schritte mit anders
        benanntem Ergebnisfeld überschreiben die Methode."""
        output_field = self.params.get("output_field")
        if isinstance(output_field, str):
            return {output_field}
        return set()

    def input_ports(self) -> dict[str, DataType]:
        """Benannte Eingänge mit Datentyp; Standard ist ``INPUT_PORTS``. Eine
        Operation, deren Vertrag erst die Konfiguration festlegt (op: rest, FA41),
        überschreibt dies; die Typprüfung (FA2) liest immer hier."""
        return type(self).INPUT_PORTS

    def output_type(self) -> DataType:
        """Ergebnistyp dieses Schrittes, analog zu input_ports()."""
        return type(self).OUTPUT_TYPE

    def brings_external_data(self) -> bool:
        """FA65: True, wenn der Schritt Daten von außerhalb des DAG einbringt (op: rest);
        die Attribution nennt ihn dann wie einen Layer."""
        return False

    def external_license(self) -> Optional[License]:
        """FA65: Lizenz der von außen eingebrachten Daten (nur relevant, wenn
        ``brings_external_data()``); None = nicht deklariert."""
        return None

    @model_validator(mode="after")
    def check_ports_and_params(self, info: ValidationInfo) -> "Step":
        """Parameter gegen ``Params`` prüfen (Fehler mit Position ``params -> <name>``),
        dann Eingänge gegen den Port-Vertrag."""
        try:
            validated = type(self).Params.model_validate(
                self.params, context=info.context
            )
        except ValidationError as exc:
            raise located_error(exc.title, line_errors(exc, ("params",))) from None
        self._params_model = validated
        self.params = validated.model_dump()

        expected = set(self.input_ports().keys())
        given = set(self.inputs.keys())
        missing = expected - given
        if missing:
            raise ValueError(
                f"Schritt '{self.id}' ({self.op}): fehlende Eingänge {missing}"
            )
        unknown = given - expected
        if unknown:
            raise ValueError(
                f"Schritt '{self.id}' ({self.op}): unbekannte Eingänge {unknown}"
            )
        return self


# --- Laufzeitkontext für Konnektoren und Transformationen ---


@dataclass
class LoadContext:
    """Was ein Konnektor und eine Transformation über den Lauf wissen.

    region: aufgelöste Szenario-Region, immer EPSG:4326 (region.py).
    target_crs: metrisches Arbeits-CRS (UTM-Zone der Region, FA5).
    Beide setzt der Executor immer; None nur bei direktem Aufruf eines
    Konnektors außerhalb eines Laufs (z. B. files.load() in Tests).
    refresh: Snapshots neu beziehen (FA7).
    base_dir: Verzeichnis der Szenario-YAML für relative Pfade (FA4).
    registry: Verzeichnis der Bausteine dieses Laufs (FA40). Ein Baustein,
    der andere Bausteine braucht (der Datei-Konnektor die Dateiformate und
    Transformationen), schlägt sie hier nach. None = Standard-Registry
    des Prozesses (``registry_or_default()``).
    densify_km: Kantenverdichtung vor der Reprojektion (``scenario.densify_km``,
    FA55); None = keine.
    options: Laufzeitoptionen der Konnektoren für diesen Lauf (FA82), Schlüssel
    ``<quellart>.<name>`` (z. B. ``osm.backend``). Sie gehören nicht in die
    Szenario-YAML; welche Schlüssel es gibt und was sie bedeuten, legt der
    Konnektor fest, der Kern reicht sie nur durch."""

    region: Optional[GeoDataFrame] = None
    target_crs: Optional[CRS] = None
    refresh: bool = False
    base_dir: Optional[Path] = None
    registry: Optional["Registry"] = None
    densify_km: Optional[float] = None
    options: Mapping[str, str] = field(default_factory=dict)

    def registry_or_default(self) -> "Registry":
        if self.registry is not None:
            return self.registry
        from geofact.core.registry import default_registry

        return default_registry()


# --- Schnittstellen der registrierten Funktionen ---
# Der Vertrag einer Funktion ist ihre Aufrufsignatur, nicht ihr Name; so kann eine
# Funktion unter mehreren Namen hängen (z. B. ein Leser für geojson, shp, gml).
# register_* prüft beim Markieren die Parameterzahl.


class SourceLoader(Protocol):
    def __call__(self, layer: Any, ctx: LoadContext, /) -> Any: ...


class FileReader(Protocol):
    def __call__(self, path: Path, layer: Any, ctx: LoadContext, /) -> Any: ...


class TableReader(Protocol):
    def __call__(self, layer: Any, path: Path, /) -> Any: ...


class Transform(Protocol):
    def __call__(self, layer: Any, data: Any, ctx: LoadContext, /) -> Any: ...


class OutputWriter(Protocol):
    def __call__(self, data: Any, target: Path, spec: Any, /) -> None: ...


class OperationRunner(Protocol):
    def __call__(self, inputs: dict[str, Any], params: dict, /) -> Any: ...
