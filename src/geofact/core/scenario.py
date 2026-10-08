"""Implements: FA1, FA2, FA9 (DAG-Logik, Lazy Loading), FA40 (Auflösung über die Registry), FA55/FA56 (scenario.crs, BBox-Prüfung), FA58/FA65 (Provenienz und Attribution an der Ausgabe), FA59/FA62/FA75 (Anschluss der Makro-Expansion, Bericht genau einmal), FA73 (Ausgabelizenz), FA74 (Herkunft an der Ausgabe).

GeoFACT - Szenario als gerichteter azyklischer Graph (DAG)

``Scenario`` (Meta, Layer, Schritte, Ausgaben) ist die Wurzel des
Konfigurationsformats. Die Datei prüft den DAG (Kahn, Porttypen) und
delegiert die Planung an ``ExecutionPlan`` (core/plan.py).

- Lazy Loading: Deklarierte, aber nicht erreichbare Layer werden nie geladen;
  ``required_sources()`` und ``load_schedule()`` lesen den einen Plan.
- Strikte Verträge (FA2): Unbekannte Schlüssel sind überall ein Fehler
  (extra='forbid'); jeder Fehler nennt seine Position (``layers -> 1 -> tags``).
  Ausgabe und Datei-/Tabellen-Layer tragen formatspezifische Felder flach und
  prüfen sie gegen das ``options_model`` des Formats. Vor der Ausführung
  muss jede Ausgabe eine Quelle haben, deren Datentyp das Format schreiben
  kann, und die Dateinamen müssen eindeutig sein.
- Offene Erweiterungspunkte (FA40): Quellarten, Operationen und
  Ausgabeformate werden zur Validierungszeit in der Registry nachgeschlagen
  (aus ``context={"registry": reg}`` oder die Standard-Registry). Diese Datei
  kennt keinen konkreten Baustein.
- Expansion vor der Validierung (FA59, FA62, FA75): ``expand_macros`` dehnt
  Parameter, Module, Instanzen, Selektoren und ``when`` genau einmal aus
  (core/expansion.py); Fehler nennen die Fachposition.
"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializeAsAny,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails

from geofact.core.contracts import SCENARIO_REGION_CONTEXT, LayerBase, License, Step
from geofact.core.crs import (
    AUTO,
    check_bbox,
    parse_bbox_literal,
    parse_crs_spec,
    require_projected_metric,
)
from geofact.core.errors import (
    ConfigError,
    custom_line_error,
    german_message,
    line_errors,
    located_error,
)
from geofact.core.expansion import ExpansionReport, expand
from geofact.core.plan import ExecutionPlan, layer_dependencies, topological_order
from geofact.core.registry import Registry, default_registry, registry_from_context
from geofact.core.types import DataType, LayerProvenance

if TYPE_CHECKING:
    from geofact.core.provenance import NodeAttribution, OutputLicense, RunLineage


# --- Auflösung über die Registry (FA40) ---


def resolve_layer(
    raw: dict, registry: Registry | None = None, context: dict | None = None
) -> LayerBase:
    """Löst einen rohen Layer-Dict über die Registry zum Layer-Modell auf (FA40).
    registry None = Standard-Registry; context geht an die Modellvalidierung."""
    if not isinstance(raw, dict):
        return raw  # bereits eine Layer-Instanz (programmatische Nutzung)
    source = raw.get("source")
    if source is None:
        raise ValueError(f"Layer '{raw.get('id', '?')}': Feld 'source' fehlt")
    registry = registry if registry is not None else default_registry()
    plugin = registry.source(source)
    return plugin.layer_model.model_validate(raw, context=context)


def resolve_step(
    raw: dict, registry: Registry | None = None, context: dict | None = None
) -> Step:
    """Löst einen rohen Step-Dict über die Registry zur Step-Klasse auf. Der
    Lookup ersetzt eine geschlossene Union, damit Plugins diese Datei nicht
    ändern müssen (FA11/NFA4). registry None = Standard-Registry."""
    if not isinstance(raw, dict):
        return raw  # bereits eine Step-Instanz (z. B. programmatische Nutzung)
    op = raw.get("op")
    if op is None:
        raise ValueError(f"Schritt '{raw.get('id', '?')}': Feld 'op' fehlt")
    registry = registry if registry is not None else default_registry()
    step_class = registry.operation(op).step_model
    return step_class.model_validate(raw, context=context)


# --- Ausgabe ---


class OutputSpec(BaseModel):
    """type: Name eines registrierten Ausgabeformats (FA40). source: Id des
    Layers oder Schritts, dessen Ergebnis geschrieben wird (Pflicht). Weitere,
    formatspezifische Felder (z. B. color_field bei map) stehen flach in der
    YAML; sie werden gegen das ``options_model`` des Formats geprüft
    (unbekannte Felder sind ein Fehler) und sind danach typisiert als
    ``spec.options`` lesbar (None bei einem Format ohne Optionen).

    license (FA73, optional): die Lizenz, die der Nutzer auf dieses abgeleitete
    Ergebnis legt (auf eigenes Risiko); ohne Angabe gilt
    ``scenario.output_license``, ohne beides bleibt die Ausgabe wie vor FA73."""

    model_config = ConfigDict(extra="allow")

    type: str
    source: str
    path: Optional[str] = None
    license: Optional[License] = Field(
        None,
        description="Lizenz, unter die der Nutzer dieses Ergebnis stellt (FA73); "
        "überschreibt scenario.output_license",
    )

    _options: Optional[BaseModel] = PrivateAttr(default=None)
    _provenance: Optional[dict[str, LayerProvenance]] = PrivateAttr(default=None)
    _attribution: Optional["NodeAttribution"] = PrivateAttr(default=None)
    _output_license: Optional["OutputLicense"] = PrivateAttr(default=None)
    _lineage: Optional["RunLineage"] = PrivateAttr(default=None)

    @property
    def options(self) -> Optional[BaseModel]:
        """Die geprüften formatspezifischen Felder (Instanz des
        ``options_model`` des Formats), inklusive ihrer Defaults."""
        return self._options

    @property
    def provenance(self) -> Optional[dict[str, LayerProvenance]]:
        """CRS-Provenienz der in diesem Lauf geladenen Layer (FA58), vom
        Ausgabeschritt vor dem Schreiben gebunden; None außerhalb eines Laufs.
        Die Writer-Signatur ``(data, target, spec)`` bleibt dadurch gleich."""
        return self._provenance

    @property
    def attribution(self) -> Optional["NodeAttribution"]:
        """Lizenzen der Quelle dieser Ausgabe (FA65), vor dem Schreiben
        gebunden; None außerhalb eines Laufs."""
        return self._attribution

    def bind_provenance(self, mapping: Optional[Mapping[str, LayerProvenance]]) -> None:
        self._provenance = dict(mapping) if mapping is not None else None

    def bind_attribution(self, attribution: Optional["NodeAttribution"]) -> None:
        self._attribution = attribution

    @property
    def output_license(self) -> Optional["OutputLicense"]:
        """Wirksame Ausgabelizenz mit Prüfvermerk (FA73), vor dem Schreiben
        gebunden; None ohne Ausgabelizenz oder außerhalb eines Laufs."""
        return self._output_license

    @property
    def lineage(self) -> Optional["RunLineage"]:
        """Herkunft des Laufs (FA74), vor dem Schreiben gebunden; None
        außerhalb eines Laufs."""
        return self._lineage

    def bind_output_license(self, output_license: Optional["OutputLicense"]) -> None:
        self._output_license = output_license

    def bind_lineage(self, lineage: Optional["RunLineage"]) -> None:
        self._lineage = lineage

    def target_name(self, index: int, extension: str) -> str:
        """Dateiname dieser Ausgabe (FA2): der Name aus ``path`` oder
        ``<index:02d>_<source>.<extension>``. Dieselbe Regel nutzt
        ``engine/outputs.py`` beim Schreiben und ``validate_dag`` bei der
        Prüfung auf eindeutige Namen."""
        if self.path:
            return PurePath(self.path).name
        return f"{index:02d}_{self.source or 'output'}.{extension}"

    @model_validator(mode="after")
    def check_format_options(self, info: ValidationInfo) -> "OutputSpec":
        registry = registry_from_context(info)
        try:
            plugin = registry.output(self.type)
        except ValueError as exc:
            raise located_error(
                "OutputSpec",
                [
                    custom_line_error(
                        "unknown_output_type", str(exc), ("type",), self.type
                    )
                ],
            ) from None
        extras = dict(self.model_extra or {})
        model = plugin.options_model
        known = sorted(model.model_fields) if model is not None else []
        errors: list[InitErrorDetails] = []
        for key in extras:
            if key not in known:
                allowed = (
                    f"erlaubt sind {known}"
                    if known
                    else "dieses Format hat keine weiteren Felder"
                )
                errors.append(
                    custom_line_error(
                        "unknown_option",
                        f"unbekanntes Feld für Ausgabeformat '{self.type}' ({allowed})",
                        (key,),
                        extras[key],
                    )
                )
        if errors:
            raise located_error("OutputSpec", errors)
        if model is not None:
            try:
                self._options = model.model_validate(extras, context=info.context)
            except ValidationError as exc:
                raise located_error("OutputSpec", line_errors(exc)) from None
        return self


# --- Wurzel: Szenario als DAG ---


class ScenarioMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: Optional[str] = None
    region: str
    crs: str = Field(
        AUTO,
        description="Arbeits-CRS des Laufs: auto (UTM-Zone aus dem Regionszentroid), "
        "equal_area (EPSG:6933), equal_area_europe (EPSG:3035) oder eine "
        "projizierte Angabe in Metern wie EPSG:25833",
    )
    densify_km: Optional[float] = Field(
        None,
        gt=0,
        description="Linien und Polygonkanten vor der Reprojektion auf diese Kantenlänge "
        "(km) verdichten; ohne Angabe keine Verdichtung",
    )
    output_license: Optional[License] = Field(
        None,
        description="Lizenz, unter die der Nutzer alle Ergebnisse ohne eigene "
        "output[i].license stellt (FA73, auf eigenes Risiko)",
    )

    @field_validator("region")
    @classmethod
    def check_region_literal(cls, value: str) -> str:
        """FA56: ein BBox-Literal wird schon bei ``validate`` geprüft; ein Name
        bleibt unverändert (Auflösung erst im Lauf)."""
        values = parse_bbox_literal(value)
        if values is not None:
            check_bbox(values, text=value)
        return value

    @field_validator("crs")
    @classmethod
    def check_working_crs(cls, value: str) -> str:
        """FA55: ``auto``, ein Preset oder ein projiziertes CRS in Metern."""
        value = str(value).strip()
        crs = parse_crs_spec(value)
        if crs is not None:
            require_projected_metric(crs, what=value)
        return value


def _resolve_items(
    raw_items: Any,
    resolver: Callable[..., Any],
    registry: Registry,
    context: dict | None,
    key_field: str,
) -> Any:
    """Löst Layer bzw. Schritte einzeln auf und sammelt alle Fehler mit der
    Position des Elements. ``key_field`` wählt den Baustein (``source``/``op``)."""
    if not isinstance(raw_items, list):
        return raw_items  # Pydantic meldet "muss eine Liste sein"
    resolved: list[Any] = []
    errors: list[InitErrorDetails] = []
    for index, raw in enumerate(raw_items):
        try:
            resolved.append(resolver(raw, registry, context))
        except ValidationError as exc:
            errors.extend(line_errors(exc, (index,)))
        except ValueError as exc:
            # kein Pydantic-Fehler: Baustein nicht registriert oder Feld fehlt
            errors.append(
                custom_line_error(
                    "unknown_block",
                    str(exc),
                    (index, key_field),
                    raw.get(key_field) if isinstance(raw, dict) else None,
                )
            )
        except (TypeError, AttributeError, KeyError) as exc:
            # Fehler im Validator eines Bausteins (Plugin): Position bleibt erhalten
            errors.append(
                custom_line_error(
                    "block_error",
                    f"{type(exc).__name__}: {exc}",
                    (index,),
                    raw if isinstance(raw, dict) else None,
                )
            )
    if errors:
        raise located_error("Scenario", errors)
    return resolved


_TYPE_LABELS = {"vector": "Vektor", "raster": "Raster", "graph": "Graph"}
_FIRST_POSITION = re.compile(r"zuerst bei (layers|steps) -> (\d+)\)")
_INSTANCE_NAME = re.compile(
    r"^(?P<instance>[A-Za-z_][A-Za-z0-9_]*)\[(?P<key>[A-Za-z0-9_-]+)\]$"
)


def _first_position(message: str) -> tuple[Any, ...]:
    """Die erste Stelle einer doppelten ID aus der Meldung von validate_dag."""
    match = _FIRST_POSITION.search(message)
    return (match.group(1), int(match.group(2))) if match else ("?",)


def _relocated(exc: ValidationError, report: ExpansionReport) -> list[InitErrorDetails]:
    """FA75 Nachbedingung 3: Fehler an die Fachposition umschreiben
    (``report.relocate``); eine doppelte ID nennt beide Stellen."""
    details = line_errors(exc)
    errors = exc.errors()
    for detail, error in zip(details, errors):
        detail["loc"] = report.relocate(tuple(error["loc"]))
        source = error.get("input")
        if error.get("type") == "duplicate_id" and isinstance(source, str):
            first = " -> ".join(
                map(str, report.relocate(_first_position(str(error.get("msg", "")))))
            )
            detail.update(
                custom_line_error(
                    "duplicate_id",
                    f"ID '{source}' ist doppelt (zuerst bei {first})",
                    detail["loc"],
                    source,
                )
            )
        if (
            error.get("type") == "unknown_source"
            and isinstance(source, str)
            and source in report.dropped_ids
        ):
            detail.update(
                custom_line_error(
                    "unknown_source",
                    f"('{source}' wurde durch when: {report.dropped_ids[source]} entfernt) "
                    + str(error.get("msg", "")),
                    detail["loc"],
                    source,
                )
            )
    return _collapse_instances(details, errors, report)


def _collapse_instances(
    details: list[InitErrorDetails], errors: list[Any], report: ExpansionReport
) -> list[InitErrorDetails]:
    """FA75 Nachbedingung 3: derselbe Fehler in allen Elementen einer Instanz
    wird einmal gemeldet (Instanz-ids im Text werden zu ``stadt[*]``)."""
    alive = {
        info.id: [k for k in info.keys if k not in info.dropped_keys]
        for info in report.instances
    }
    groups: dict[tuple, list[tuple[int, str]]] = {}
    for position, (detail, error) in enumerate(zip(details, errors)):
        loc = tuple(detail["loc"])
        match = (
            _INSTANCE_NAME.match(str(loc[1]))
            if len(loc) > 2 and loc[0] == "instances"
            else None
        )
        if match is None or match.group("instance") not in alive:
            continue
        instance, key = match.group("instance"), match.group("key")
        kind = detail.get("type")
        text = kind.message() if hasattr(kind, "message") else german_message(error)
        text = text.replace(f"{instance}[{key}]", f"{instance}[*]")
        groups.setdefault((instance, loc[2:], text), []).append((position, key))
    replaced: dict[int, InitErrorDetails] = {}
    merged: set[int] = set()
    for (instance, rest, text), members in groups.items():
        keys = alive[instance]
        if len(keys) > 1 and {key for _, key in members} == set(keys):
            replaced[members[0][0]] = custom_line_error(
                "repeated_in_instances",
                f"{text} (in allen {len(keys)} Instanzen von {instance})",
                ("instances", instance, *rest),
            )
            merged.update(position for position, _ in members)
    return [
        replaced.get(i, detail)
        for i, detail in enumerate(details)
        if i in replaced or i not in merged
    ]


class Scenario(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    scenario: ScenarioMeta
    layers: list[SerializeAsAny[LayerBase]] = Field(..., min_length=1)
    steps: list[SerializeAsAny[Step]] = Field(..., min_length=1)
    output: list[OutputSpec] = Field(..., min_length=1)

    _expansion: Optional[ExpansionReport] = PrivateAttr(default=None)

    @field_validator("layers", mode="before")
    @classmethod
    def resolve_layers(cls, raw_layers: Any, info: ValidationInfo) -> Any:
        context = info.context
        meta = info.data.get("scenario")
        if isinstance(meta, ScenarioMeta):
            # Layer-Validatoren sehen scenario.region als Text (FA65: Default-Lizenz
            # der Quellart region hängt davon ab, ob die Region ein BBox-Literal ist)
            context = {**(context or {}), SCENARIO_REGION_CONTEXT: meta.region}
        return _resolve_items(
            raw_layers, resolve_layer, registry_from_context(info), context, "source"
        )

    @field_validator("steps", mode="before")
    @classmethod
    def resolve_steps(cls, raw_steps: Any, info: ValidationInfo) -> Any:
        return _resolve_items(
            raw_steps, resolve_step, registry_from_context(info), info.context, "op"
        )

    @model_validator(mode="after")
    def validate_dag(self, info: ValidationInfo) -> "Scenario":
        produced_type: dict[str, DataType] = {}
        first_seen: dict[str, str] = {}
        duplicate_errors: list[InitErrorDetails] = []
        positions = [
            ("layers", index, layer.id) for index, layer in enumerate(self.layers)
        ]
        positions += [
            ("steps", index, step.id) for index, step in enumerate(self.steps)
        ]
        for section, index, node_id in positions:
            if node_id in first_seen:
                # loc an der zweiten Stelle; expand_macros schreibt beide auf die
                # Fachposition um
                duplicate_errors.append(
                    custom_line_error(
                        "duplicate_id",
                        f"ID '{node_id}' ist doppelt (zuerst bei {first_seen[node_id]})",
                        (section, index, "id"),
                        node_id,
                    )
                )
            else:
                first_seen[node_id] = f"{section} -> {index}"
        if duplicate_errors:
            raise located_error("Scenario", duplicate_errors)
        for layer in self.layers:
            produced_type[layer.id] = layer.data_type

        layer_ids = {lay.id for lay in self.layers}
        step_by_id = {s.id: s for s in self.steps}
        step_index = {s.id: i for i, s in enumerate(self.steps)}

        # spatial_check.within: scenario_region oder ein anderer Vektor-Layer,
        # ohne Selbstbezug und Zyklus (der Plan lädt ihn zuerst, core/plan.py).
        layer_by_id = {lay.id: lay for lay in self.layers}
        within_errors: list[InitErrorDetails] = []
        for index, layer in enumerate(self.layers):
            where = ("layers", index, "validation", "spatial_check", "within")
            for ref in layer_dependencies(layer):
                problem = None
                if ref not in layer_ids:
                    problem = (
                        f"Layer '{layer.id}': spatial_check.within referenziert "
                        f"unbekannten Layer '{ref}' (oder 'scenario_region' nutzen)"
                    )
                elif ref == layer.id:
                    problem = f"Layer '{layer.id}': spatial_check.within verweist auf sich selbst"
                elif layer_by_id[ref].data_type != DataType.VECTOR:
                    problem = (
                        f"Layer '{layer.id}': spatial_check.within='{ref}' muss ein "
                        f"Vektor-Layer sein, ist aber {layer_by_id[ref].data_type.value}"
                    )
                if problem is not None:
                    within_errors.append(
                        custom_line_error("invalid_reference", problem, where, ref)
                    )
        if within_errors:
            raise located_error("Scenario", within_errors)
        for layer in self.layers:
            chain = [layer.id]
            while True:
                refs = layer_dependencies(layer_by_id[chain[-1]])
                if not refs:
                    break
                if refs[0] in chain:
                    raise ValueError(
                        "spatial_check.within: zyklische Referenz "
                        + " -> ".join([*chain, refs[0]])
                    )
                chain.append(refs[0])

        edges: dict[str, set[str]] = {}
        input_errors: list[InitErrorDetails] = []
        for index, step in enumerate(self.steps):
            edges[step.id] = set()
            for port_name, source_id in step.inputs.items():
                if source_id not in produced_type and source_id not in step_by_id:
                    input_errors.append(
                        custom_line_error(
                            "unknown_source",
                            f"Schritt '{step.id}': Eingang '{port_name}' "
                            f"referenziert unbekannte Quelle '{source_id}'",
                            ("steps", index, "inputs", port_name),
                            source_id,
                        )
                    )
                edges[step.id].add(source_id)
            produced_type[step.id] = step.output_type()
        if input_errors:
            raise located_error("Scenario", input_errors)

        order = topological_order(edges, step_by_id)

        errors: list[InitErrorDetails] = []
        resolved: dict[str, DataType] = {lay.id: lay.data_type for lay in self.layers}
        for sid in order:
            step = step_by_id[sid]
            for port_name, source_id in step.inputs.items():
                expected = step.input_ports()[port_name]
                actual = resolved.get(source_id, produced_type.get(source_id))
                if actual != expected:
                    errors.append(
                        custom_line_error(
                            "port_type_mismatch",
                            f"Schritt '{step.id}': Eingang '{port_name}' erwartet "
                            f"{expected.value}, erhält aber {actual.value} von '{source_id}'",
                            ("steps", step_index[sid], "inputs", port_name),
                            source_id,
                        )
                    )
            resolved[sid] = step.output_type()

        # Ausgaben: Quelle und Datentyp schon hier prüfen statt als Laufzeit-Skip (FA2).
        registry = registry_from_context(info)
        for index, out in enumerate(self.output):
            if out.source not in resolved:
                errors.append(
                    custom_line_error(
                        "unknown_source",
                        f"Ausgabe referenziert unbekannte Quelle '{out.source}'",
                        ("output", index, "source"),
                        out.source,
                    )
                )
                continue
            accepts = registry.output(out.type).accepts
            if resolved[out.source] not in accepts:
                wanted = " oder ".join(sorted(_TYPE_LABELS[t.value] for t in accepts))
                actual = _TYPE_LABELS[resolved[out.source].value]
                errors.append(
                    custom_line_error(
                        "output_type_mismatch",
                        f"Ausgabeformat '{out.type}' schreibt nur {wanted}, die Quelle "
                        f"'{out.source}' liefert aber {actual}",
                        ("output", index, "source"),
                        out.source,
                    )
                )
        errors.extend(self._check_output_names(registry))
        errors.extend(self._check_output_references(layer_ids))
        if errors:
            raise located_error("Scenario", errors)

        return self

    def _check_output_names(self, registry: Registry) -> list[InitErrorDetails]:
        """FA2: zwei Ausgaben dürfen nicht in dieselbe Datei schreiben. Verglichen
        wird ohne Groß-/Kleinschreibung (Windows/macOS: dieselbe Datei)."""
        errors: list[InitErrorDetails] = []
        seen: dict[str, int] = {}
        for index, out in enumerate(self.output):
            extension = getattr(out.options, "extension", None)
            if not extension:
                try:
                    extension = registry.output(out.type).extension
                except ValueError:
                    continue  # unbekanntes Format: schon von OutputSpec gemeldet
            name = out.target_name(index, extension)
            if out.path is not None and name in ("", ".", ".."):
                # '.', '..' oder ein Pfad mit Schrägstrich am Ende nennen ein
                # Verzeichnis, keine Datei.
                errors.append(
                    custom_line_error(
                        "invalid_output_path",
                        f"path '{out.path}' nennt keinen Dateinamen - z. B. 'ergebnis.{extension}'",
                        ("output", index, "path"),
                        out.path,
                    )
                )
                continue
            key = name.casefold()
            if key in seen:
                errors.append(
                    custom_line_error(
                        "duplicate_output_name",
                        f"Dateiname '{name}' wird auch von output -> {seen[key]} geschrieben - "
                        "path eindeutig wählen",
                        ("output", index, "path" if out.path else "source"),
                        out.path or out.source,
                    )
                )
            else:
                seen[key] = index
        return errors

    def _check_output_references(self, layer_ids: set[str]) -> list[InitErrorDetails]:
        """Ausgabeoptionen, die Layer nennen (``referenced_ids()`` am Optionsmodell),
        dürfen nur deklarierte und tatsächlich geladene Layer nennen; ein
        ungelesener Layer wird nie geladen (Lazy Loading)."""
        errors: list[InitErrorDetails] = []
        loaded: set[str] | None = None
        for index, out in enumerate(self.output):
            referenced = getattr(out.options, "referenced_ids", None)
            if not callable(referenced):
                continue
            for position, ref in enumerate(referenced() or []):
                if ref not in layer_ids:
                    errors.append(
                        custom_line_error(
                            "unknown_layer",
                            f"unbekannter Layer '{ref}'",
                            ("output", index, "layers", position),
                            ref,
                        )
                    )
                    continue
                if loaded is None:
                    loaded = self._loaded_layer_ids()
                if ref not in loaded:
                    errors.append(
                        custom_line_error(
                            "unloaded_layer",
                            f"Layer '{ref}' wird von keinem Schritt und keiner Ausgabe gelesen und "
                            "deshalb nicht geladen (Lazy Loading) - als source einer Ausgabe "
                            "oder Eingabe eines Schritts verwenden",
                            ("output", index, "layers", position),
                            ref,
                        )
                    )
        return errors

    def _loaded_layer_ids(self) -> set[str]:
        """Die Layer, die ein Lauf lädt; dieselbe Regel wie
        ``ExecutionPlan.required_layers`` (core/plan.py)."""
        layer_by_id = {layer.id: layer for layer in self.layers}
        step_by_id = {step.id: step for step in self.steps}
        layers: set[str] = set()
        seen_steps: set[str] = set()
        stack = [out.source for out in self.output if out.source]
        while stack:
            node = stack.pop()
            if node in layer_by_id:
                if node not in layers:
                    layers.add(node)
                    stack.extend(layer_dependencies(layer_by_id[node]))
            elif node in step_by_id and node not in seen_steps:
                seen_steps.add(node)
                stack.extend(step_by_id[node].inputs.values())
        return layers

    @model_validator(mode="wrap")
    @classmethod
    def expand_macros(cls, raw: Any, handler: Any, info: ValidationInfo) -> "Scenario":
        """FA59/FA62/FA75: Makro-Expansion vor der Validierung, genau einmal; der
        Bericht hängt am Szenario (``expansion``). Als letzter Modell-Validator
        umschließt er alle anderen (auch ``validate_dag``)."""
        if not isinstance(raw, dict):
            return handler(raw)
        overrides = (info.context or {}).get("parameters") if info is not None else None
        try:
            report = expand(raw, overrides)
        except ConfigError as exc:
            raise located_error(
                "Scenario",
                [
                    custom_line_error("expansion_error", message, (location,), None)
                    for location, message in exc.issues
                ],
            ) from None
        try:
            scenario = handler(report.expanded)
        except ValidationError as exc:
            raise located_error("Scenario", _relocated(exc, report)) from None
        scenario._expansion = report
        return scenario

    @property
    def expansion(self) -> Optional[ExpansionReport]:
        """Bericht der einen Expansion (FA75); None, wenn nicht aus einem dict validiert."""
        return self._expansion

    # --- Lazy Loading: der Plan steht in core/plan.py ---

    def output_license_of(self, spec: OutputSpec) -> tuple[Optional[License], str]:
        """FA73: wirksame Ausgabelizenz einer Ausgabe und wo sie deklariert ist -
        ``output[i].license`` vor ``scenario.output_license``; ohne beide
        ``(None, "")``."""
        if spec.license is not None:
            return spec.license, "output"
        if self.scenario.output_license is not None:
            return self.scenario.output_license, "scenario"
        return None, ""

    def execution_order(self) -> list[str]:
        """Topologische Reihenfolge aller Schritte; ausgeführt wird nur ``ExecutionPlan.order``."""
        return list(ExecutionPlan.of(self).full_order)

    def required_sources(self) -> dict:
        """Erreichbarkeitsanalyse rückwärts vom Output: welche Layer/Schritte gebraucht werden."""
        return ExecutionPlan.of(self).required()

    def load_schedule(self) -> dict[str, list[str]]:
        """Ordnet jedem Schritt die Layer zu, die unmittelbar vor ihm geladen werden."""
        return ExecutionPlan.of(self).schedule()

    def plan(self, registry: Registry | None = None) -> ExecutionPlan:
        """Der Ausführungsplan dieses Szenarios (siehe core/plan.py)."""
        return ExecutionPlan.of(self, registry)
