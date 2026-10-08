"""Implements: FA4 (Datei-Konnektor), Quellart 'file' für FA40.

Lädt lokale (oder per URL erreichbare) Dateien über das Format aus der Tabelle
'file_format' der Registry: Vektor (builtin/formats/vector.py) und Raster
(builtin/formats/raster.py). Das Format kommt aus dem Feld 'format', sonst aus der
Dateiendung; ein unbekanntes Format meldet einen Fehler mit Layer-id und Pfad.
Neue Dateiformate sind neue Dateien mit @register_file_format (FA40); ein Format
kann eine eigene Transformation mitbringen, die die Standard-Harmonisierung
('reproject') ersetzt.

Zip-Container: ein Pfad auf '.zip' ist kein eigenes Format. Kandidaten sind
Mitglieder, deren Endung zum data_type des Layers passt; genau einer (oder das per
``member`` benannte) wird über /vsizip/ gelesen. Null oder mehrere Kandidaten
ohne ``member`` sind ein Fehler, der die gefundenen Mitglieder auflistet.

Datei-URL: ``path`` darf ``http(s)://`` oder ein GDAL-Pfad (``/vsicurl/``, ...)
sein. Solche Pfade werden nicht gegen base_dir aufgelöst; eine URL wird als
``/vsicurl/<url>`` gelesen. Ein Zip-Container hinter einer URL wird abgelehnt.

region_filter (FA4-Zusatz, nur Vektor): ``none`` (Default) lädt alles und meldet
einmal, wie viele Objekte außerhalb der Region liegen; ``bbox`` lässt den Treiber
nur das Rechteck der Region lesen und filtert danach darauf; ``intersects``
verfeinert auf die Regionsgeometrie. Die Filterung in ``load`` gilt auch für
Plugin-Formate (builtin/_vector_io.py). Raster werden immer im BBox-Fenster der
Region gelesen (ohne Region: ganze Datei); ein region_filter dort ist ein Fehler.

Formatspezifische Felder (z. B. ``layer`` bei GeoPackage und GML) stehen flach in
der YAML und werden gegen das ``options_model`` des Formats geprüft
(builtin/_format_options.py).

``crs`` ist ein Override-CRS für Dateien ohne CRS-Metadaten (FA5); ``crs_override``
(FA55) erzwingt es auch gegen ein gemeldetes CRS. Beides wertet die Transformation
'reproject' aus, nicht diese Datei.

check_before_run(base_dir) meldet eine fehlende lokale Datei vor dem Lauf (FA44);
URLs, GDAL-Pfade und Zip-Mitglieder werden nicht geprüft."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import ClassVar, Literal, Optional, Union

import geopandas as gpd
from pydantic import ValidationError, ValidationInfo, field_validator, model_validator

from geofact.builtin import _vector_io
from geofact.builtin._format_options import (
    FormatOptionLayer,
    check_option_names,
    describe_errors,
    validate_format_options,
)
from geofact.core.registry import FileFormatPlugin, Registry, registry_from_context
from geofact.plugin_api import (
    DataType,
    LayerData,
    LayerValidation,
    LoadContext,
    register_source,
)


# --- Layer-Modell (Vertrag der Quellart, FA40) ---


class FileLayer(FormatOptionLayer):
    """member: wählt bei einem Zip-Container (.zip) explizit die zu
    lesende Datei innerhalb des Archivs aus - nötig, sobald das Archiv
    mehr als einen Vektorformat-Kandidaten enthält (siehe file.py).
    Formatspezifische Felder (z. B. ``layer`` bei GeoPackage und GML: die zu
    lesende Schicht, nötig, sobald die Quelle mehr als eine Schicht
    enthält) stehen flach daneben und werden gegen das ``options_model``
    des Formats geprüft (``layer.options``). crs: Override-CRS
    (z. B. "EPSG:25833"), wird NUR verwendet, wenn die Datei selbst kein CRS
    deklariert - ein in der Datei deklariertes CRS hat immer Vorrang (FA5-Vertrag, siehe reproject.py
    _resolve_source_crs); Mechanismus für die FA5-Vorbedingung "oder die
    Konfiguration gibt das Quell-CRS an" bei Dateien ohne CRS-Metadaten
    (z. B. Shapefile ohne .prj)."""

    FORMAT_KIND: ClassVar[Optional[str]] = "file_format"

    id: str
    source: Literal["file"] = "file"
    path: str
    data_type: DataType
    format: Optional[str] = None  # Name eines Dateiformats (FA40)
    member: Optional[str] = None
    crs: Optional[str] = None
    crs_override: bool = False  # FA55: crs erzwingen (Auswertung: reproject)
    region_filter: _vector_io.RegionFilter = "none"  # FA4-Zusatz
    validation: Optional[LayerValidation] = None

    @field_validator("crs_override")
    @classmethod
    def override_needs_crs(cls, v: bool, info: ValidationInfo) -> bool:
        """FA55: ``crs_override: true`` erzwingt ``crs`` - ohne Angabe gibt es
        nichts zu erzwingen (Fehler an Position ``crs_override``)."""
        if v and not info.data.get("crs"):
            raise ValueError(
                "crs_override: true braucht eine crs-Angabe (z. B. crs: EPSG:25833)"
            )
        return v

    @model_validator(mode="after")
    def region_filter_only_for_vectors(self) -> "FileLayer":
        if self.region_filter != "none" and self.data_type != DataType.VECTOR:
            raise ValueError(
                f"Layer '{self.id}': region_filter '{self.region_filter}' gilt nur für "
                "Vektor-Layer - Raster werden ohnehin im Fenster der Region gelesen; "
                "region_filter entfernen"
            )
        return self

    def check_before_run(self, base_dir: Path | None) -> list[tuple[str, str]]:
        """FA44-Hook: fehlende lokale Datei als (feld, meldung) vor dem Lauf."""
        return _vector_io.missing_local_file(self.path, base_dir)

    @field_validator("data_type")
    @classmethod
    def no_graph_layer(cls, v: DataType) -> DataType:
        if v == DataType.GRAPH:
            raise ValueError("Layer können nicht vom Typ 'graph' sein")
        return v

    @model_validator(mode="after")
    def format_registered(self, info: ValidationInfo) -> "FileLayer":
        """Ein explizit genanntes Format muss registriert sein und zum
        data_type passen (FA40) - vor der Ausführung statt erst beim
        Laden (FA2). Die zusätzlichen Felder des Layers werden gegen das
        Optionsmodell dieses Formats geprüft."""
        registry = registry_from_context(info)
        if self.format is not None:
            plugin = registry.file_format(self.format)
        else:
            suffix = _suffix(self.path)
            remote = _vector_io.is_remote(self.path)
            if suffix == "zip" or (suffix == "" and not remote):
                # Zip: das Format steht erst beim Laden fest; hier nur prüfen, dass
                # der Schlüssel zu einem zip-fähigen Format gehört.
                check_option_names(
                    self.format_extras,
                    [
                        p.options_model
                        for p in registry.entries("file_format")
                        if p.zip_extensions and p.options_model is not None
                    ],
                    owner="eines Formats, das aus Zip-Containern liest",
                )
                return self
            plugin = registry.file_format_for_extension(suffix)
            if plugin is None:
                known = sorted(
                    e for p in registry.entries("file_format") for e in p.extensions
                )
                hint = (
                    " - bei einer URL ohne erkennbare Endung 'format' angeben"
                    if remote
                    else ""
                )
                raise ValueError(
                    f"Layer '{self.id}': unbekanntes Dateiformat '{suffix}' (Pfad: {self.path}); "
                    f"bekannte Endungen: {known}{registry.failed_hint()}{hint}"
                )
        if plugin.data_type != self.data_type:
            raise ValueError(
                f"Layer '{self.id}': Format '{plugin.name}' liefert "
                f"{plugin.data_type.value}, data_type ist aber '{self.data_type.value}'"
            )
        self.bind_options(
            validate_format_options(
                self.format_extras,
                plugin.options_model,
                owner=f"des Dateiformats '{plugin.name}'",
                context=info.context,
            )
        )
        return self


# --- Formatbestimmung und Zip-Container ---


def _suffix(path: str) -> str:
    """Dateiendung ohne Punkt, klein; bei einer URL ohne Query-String/Fragment."""
    name = _vector_io.strip_query(path).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _resolve_format(layer: FileLayer, registry: Registry) -> FileFormatPlugin:
    if layer.format is not None:
        return registry.file_format(layer.format)
    ext = _suffix(layer.path)
    plugin = registry.file_format_for_extension(ext)
    if plugin is None:
        raise ValueError(
            f"Layer '{layer.id}': unbekanntes Dateiformat '{ext}' (Pfad: {layer.path})"
        )
    return plugin


def _zip_candidates(
    names: list[str], registry: Registry, data_type: DataType
) -> list[str]:
    allowed = {
        e
        for p in registry.entries("file_format")
        if p.data_type == data_type
        for e in p.zip_extensions
    }
    return [
        name
        for name in names
        if not name.endswith("/") and Path(name).suffix.lstrip(".").lower() in allowed
    ]


def _resolve_zip_member(layer: FileLayer, zip_path: Path, registry: Registry) -> str:
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()

    if layer.member is not None:
        if layer.member not in names:
            raise ValueError(
                f"Layer '{layer.id}': member '{layer.member}' nicht im Archiv "
                f"'{layer.path}' gefunden. Vorhandene Mitglieder: {sorted(names)}"
            )
        return layer.member

    kind = layer.data_type.value
    candidates = _zip_candidates(names, registry, layer.data_type)
    if len(candidates) == 0:
        raise ValueError(
            f"Layer '{layer.id}': kein Kandidat für data_type '{kind}' im Zip-Container "
            f"'{layer.path}' gefunden. Vorhandene Mitglieder: {sorted(names)}"
        )
    if len(candidates) > 1:
        raise ValueError(
            f"Layer '{layer.id}': mehrere Kandidaten für data_type '{kind}' im Archiv "
            f"'{layer.path}' gefunden: {sorted(candidates)}. "
            "'member' in der Konfiguration angeben, um eindeutig auszuwählen."
        )
    return candidates[0]


def _vsizip_uri(zip_path: Path, member: str) -> str:
    # GDAL erwartet Forward-Slashes; Backslashes unter Windows würden als Teil
    # des Dateinamens gelesen.
    normalized = str(zip_path.resolve()).replace("\\", "/")
    return f"/vsizip/{normalized}/{member}"


def _load_zip(
    layer: FileLayer, path: Path, registry: Registry, ctx: LoadContext
) -> LayerData:
    member = _resolve_zip_member(layer, path, registry)
    uri = _vsizip_uri(path, member)
    # Die Optionen gelten für das Format des Mitglieds (z. B. 'layer' bei
    # GeoPackage/GML, 'bands' bei GeoTIFF); ein Feld, das dieses Format nicht
    # kennt, ist ein Fehler.
    member_format = registry.file_format_for_extension(
        Path(member).suffix.lstrip(".").lower()
    )
    options = _check_options(
        layer,
        member_format.options_model if member_format else None,
        f"des Mitglieds '{member}'",
    )
    member_type = (
        member_format.data_type if member_format is not None else DataType.VECTOR
    )
    if member_type != layer.data_type:
        # Nur mit explizitem 'member' erreichbar: die automatische Wahl
        # berücksichtigt nur Formate dieses data_type.
        raise ValueError(
            f"Layer '{layer.id}': Mitglied '{member}' liefert {member_type.value}, "
            f"data_type ist aber '{layer.data_type.value}'"
        )
    if member_type == DataType.RASTER:
        layer.bind_options(options)
        return member_format.read(uri, layer, ctx)
    read_kwargs = {}
    if getattr(options, "layer", None) is not None:
        read_kwargs["layer"] = options.layer
    return _vector_io.read_vector(uri, layer, ctx, **read_kwargs)


def _check_options(layer: FileLayer, model, owner: str):
    """Prüft die zusätzlichen Felder des Layers beim Laden (Format erst jetzt
    bekannt) und liefert die typisierten Optionen."""
    try:
        return validate_format_options(layer.format_extras, model, owner=owner)
    except ValidationError as exc:
        raise ValueError(f"Layer '{layer.id}': {describe_errors(exc)}") from exc


def _is_zip(path: Path) -> bool:
    return path.suffix.lstrip(".").lower() == "zip"


def _resolve_path(layer: FileLayer, base_dir: Path | None) -> Union[Path, str]:
    """Lokale Datei (gegen base_dir aufgelöst und geprüft) oder - bei URL und
    GDAL-Pfad - der GDAL-Pfad als ``str`` (URL -> /vsicurl/<url>)."""
    if _vector_io.is_remote(layer.path):
        if _suffix(layer.path) == "zip":
            raise ValueError(
                f"Layer '{layer.id}': ein Zip-Container lässt sich nur als lokale Datei "
                f"inspizieren (Pfad: {layer.path}) - Archiv herunterladen oder das "
                "Mitglied direkt als '/vsizip//vsicurl/<url>/<mitglied>' angeben"
            )
        return _vector_io.gdal_path(layer.path)
    path = Path(layer.path)
    if base_dir is not None and not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        raise FileNotFoundError(f"Layer '{layer.id}': Datei nicht gefunden: {path}")
    return path


def load(layer: FileLayer, ctx: LoadContext) -> LayerData:
    """Einziger Einstieg der Quellart (FA45). Relative Pfade gelten gegen
    ctx.base_dir (FA4), None = Prozess-CWD."""
    path = _resolve_path(layer, ctx.base_dir)
    registry = ctx.registry_or_default()

    if isinstance(path, Path) and _is_zip(path):
        return _limit_to_region(layer, _load_zip(layer, path, registry, ctx), ctx)

    fmt = _resolve_format(layer, registry)
    if fmt.data_type != layer.data_type:
        kind = "vektoriell" if fmt.data_type == DataType.VECTOR else "Raster"
        raise ValueError(
            f"Layer '{layer.id}': Format '{fmt.name}' ist {kind}, "
            f"data_type ist aber '{layer.data_type.value}'"
        )
    if layer.options is None:
        layer.bind_options(
            _check_options(layer, fmt.options_model, f"des Dateiformats '{fmt.name}'")
        )
    return _limit_to_region(layer, fmt.read(path, layer, ctx), ctx)


def _limit_to_region(layer: FileLayer, data: LayerData, ctx: LoadContext) -> LayerData:
    """region_filter generisch, unabhängig vom Format (auch Plugin-Formate):
    ``bbox``/``intersects`` filtern, ``none`` meldet Objekte außerhalb EINMAL."""
    if not isinstance(data, gpd.GeoDataFrame) or ctx.region is None:
        return data
    if layer.region_filter == "none":
        _vector_io.warn_outside_region(
            data, ctx, layer_id=layer.id, crs=layer.crs, override=layer.crs_override
        )
        return data
    return _vector_io.refine_to_region(
        data,
        ctx,
        layer.region_filter,
        crs=layer.crs,
        layer_id=layer.id,
        override=layer.crs_override,
    )


def _file_transform(layer: FileLayer, data: LayerData, ctx: LoadContext) -> LayerData:
    """Die Transformation bestimmt das Dateiformat (FA40): ein Format mit
    besonderem Bedarf ersetzt damit die Standard-Harmonisierung. Zip-
    Container enthalten nur Vektorformate und nutzen den Standard."""
    registry = ctx.registry_or_default()
    if _suffix(layer.path) == "zip":
        return registry.resolve_transform("reproject")(layer, data, ctx)
    try:
        fmt = _resolve_format(layer, registry)
    except ValueError:
        # Nur erreichbar, wenn die Daten nicht über load() kamen (Test-Override).
        return registry.resolve_transform("reproject")(layer, data, ctx)
    return registry.resolve_transform(fmt.transform)(layer, data, ctx)


register_source(
    "file",
    FileLayer,
    transform=_file_transform,
    description="Lokale Vektor-/Rasterdatei (Formate siehe Verzeichnis file_format)",
)(load)
