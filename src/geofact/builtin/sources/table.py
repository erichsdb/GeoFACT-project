"""Implements: FA14 (Tabellen-Konnektor), Quellart 'table' für FA40, FA44 (Pfad-Hook).

Lädt tabellarische Quellen (CSV, SQL-Dump, SQLite, Excel, feste Spaltenbreite)
und überführt sie anhand des deklarativen Mappings (TableLayer.geometry,
.columns) in Vektor-Layer. Das Lesen der Datei erledigt das Format aus der Tabelle
'table_format' der Registry (builtin/formats/tables.py); dieser Konnektor besitzt
Pfad-/URL-Auflösung, Dialekt-Cast, Spaltenauswahl und Geometrie-Mapping. Ein neues
Tabellenformat ist eine neue Datei mit @register_table_format (FA40).

Alles wird zunächst als String gelesen (dtype=str, auch bei Excel). decimal und
thousands werden erst beim Casten ausgewertet (_normalize_numeric_string), weil
pandas sie nur bei numerischen dtypes beachtet; mit den Defaults (decimal=".",
thousands=None) entfällt die Normalisierung. Legacy-.xls wird nicht unterstützt
(Fehler mit Konvertierungshinweis, siehe _resolve_format).

check_before_run(base_dir) meldet eine fehlende lokale Datei vor dem Lauf (FA44);
URLs werden übersprungen."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Literal, Optional

import geopandas as gpd
import pandas as pd
from pydantic import Field, ValidationError, ValidationInfo, model_validator
from shapely import wkt as shapely_wkt
from shapely.geometry import Point

from geofact.builtin._vector_io import missing_local_file
from geofact.builtin._format_options import (
    FormatOptionLayer,
    check_option_names,
    describe_errors,
    validate_format_options,
)
from geofact.builtin._tabular import ColumnSpec, GeometryMapping
from geofact.core.registry import Registry, TableFormatPlugin, registry_from_context
from geofact.plugin_api import DataType, LayerValidation, LoadContext, register_source
from geofact.support import http, snapshot


# --- Layer-Modell (Vertrag der Quellart, FA40) ---


class TableLayer(FormatOptionLayer):
    """NEU (FA14): Tabellarische Quelle (CSV, SQL-Dump, SQLite, Excel) mit
    deklarativem Spalten-/Geometrie-Mapping.

    CSV-Dialekt (encoding/delimiter/quotechar/decimal/thousands): deutsche
    Open-Data-Portale liefern CSVs typischerweise Semikolon-getrennt,
    windows-1252/latin-1-kodiert, mit Dezimalkomma ("51,3397") und
    teils Punkt als Tausendertrenner ("1.234,5") - die Defaults bilden
    weiterhin den bisherigen Pfad ab (utf-8, Komma-Trennung, Punkt als
    Dezimalzeichen), damit bestehende Konfigurationen unverändert
    funktionieren. ``quotechar`` (Default ``"``) umschließt Textfelder, die
    das Trennzeichen enthalten; PyPSA-Eur-Netzdaten nutzen ``'`` für ihre
    WKT-Spalte.
    Formatspezifische Felder stehen flach daneben und gehören dem Format
    (``options_model`` in builtin/formats/tables.py): ``table``/``query`` bei
    sql und sqlite, ``sheet`` bei xlsx (wählt bei mehreren Blättern das zu
    lesende), ``colspecs``/``names``/``skiprows`` bei fwf. Ein Feld, das das
    Format des Layers nicht kennt, ist ein Fehler mit Position (FA2);
    typisiert sind sie als ``layer.options`` lesbar.
    geometry: optional (FA37). Fehlt es, ist der Layer ein reiner
    Attribut-Layer ohne Geometrie - gedacht für amtliche Statistik, die
    ihren Raumbezug nur als Gebietsname/Schluessel trägt und per
    attribute_join (FA34) mit einer Geometriequelle verbunden wird."""

    FORMAT_KIND: ClassVar[Optional[str]] = "table_format"

    id: str
    source: Literal["table"] = "table"
    path: str
    format: Optional[str] = None  # Tabellenformat (FA40); aus Endung ableitbar
    encoding: str = "utf-8"  # Zeichenkodierung (csv, fwf)
    delimiter: Optional[str] = (
        None  # Spaltentrenner (csv); None -> "," (pandas-Default)
    )
    decimal: str = "."  # Dezimalzeichen (csv)
    thousands: Optional[str] = None  # Tausendertrenner (csv); None -> keiner
    # Feldbegrenzer (csv); PyPSA-Eur quotet WKT mit "'", sonst zerfällt jede Zeile.
    quotechar: str = '"'
    # FA37: optional. Ohne geometry entsteht ein Attribut-Layer, der nur als rechte
    # Seite eines attribute_join (FA34) taugt; geometrische Operationen scheitern explizit.
    geometry: Optional[GeometryMapping] = None
    columns: dict[str, ColumnSpec] = Field(default_factory=dict)
    data_type: Literal[DataType.VECTOR] = DataType.VECTOR  # nach Mapping immer Vektor
    validation: Optional[LayerValidation] = None

    @model_validator(mode="after")
    def check_format_fields(self, info: ValidationInfo) -> "TableLayer":
        """Die zusätzlichen Felder des Layers gegen das Optionsmodell seines
        Formats prüfen (FA2/FA40) - vor der Ausführung statt erst beim
        Laden: ein Feld, das das Format nicht kennt, ist ein Tippfehler."""
        registry = registry_from_context(info)
        plugin = _format_or_none(
            self, registry
        )  # unbekannter Name -> Fehler mit Liste (FA40)
        if plugin is None:
            # Format erst beim Laden bekannt (URL ohne Endung): hier nur, dass
            # der Schlüssel zu irgendeinem Tabellenformat gehört.
            check_option_names(
                self.format_extras,
                [
                    p.options_model
                    for p in registry.entries("table_format")
                    if p.options_model is not None
                ],
                owner="eines Tabellenformats",
            )
            return self
        self.bind_options(
            validate_format_options(
                self.format_extras,
                plugin.options_model,
                owner=f"des Tabellenformats '{plugin.name}'",
                context=info.context,
            )
        )
        return self

    def check_before_run(self, base_dir: Path | None) -> list[tuple[str, str]]:
        """FA44-Hook: fehlende lokale Datei als (feld, meldung) vor dem Lauf."""
        return missing_local_file(self.path, base_dir)

    @model_validator(mode="after")
    def check_csv_dialect(self) -> "TableLayer":
        if len(self.decimal) != 1:
            raise ValueError(
                f"Layer '{self.id}': decimal muss genau ein Zeichen sein, nicht '{self.decimal}'"
            )
        if len(self.quotechar) != 1:
            raise ValueError(
                f"Layer '{self.id}': quotechar muss genau ein Zeichen sein, nicht '{self.quotechar}'"
            )
        if self.quotechar == (self.delimiter or ","):
            raise ValueError(
                f"Layer '{self.id}': quotechar und delimiter müssen sich unterscheiden "
                f"(beide '{self.quotechar}')"
            )
        if self.thousands is not None and self.thousands == self.decimal:
            raise ValueError(
                f"Layer '{self.id}': decimal und thousands müssen sich unterscheiden "
                f"(beide '{self.decimal}')"
            )
        return self


_TYPE_CAST = {
    "integer": "int64",
    "float": "float64",
    "string": "string",
}


def _extension(layer: TableLayer) -> str:
    # Bei einer URL (FA38) darf ein Query-String die Endung nicht verfälschen.
    raw_path = layer.path
    if _is_url(raw_path):
        raw_path = raw_path.split("?", 1)[0].split("#", 1)[0]
    return Path(raw_path).suffix.lstrip(".").lower()


def _format_or_none(layer: TableLayer, registry: Registry) -> TableFormatPlugin | None:
    """Das Format des Layers (Name oder Dateiendung); None, wenn es sich
    nicht bestimmen lässt - dann entscheidet erst das Laden. Ein genannter,
    aber nicht registrierter Name ist ein Fehler."""
    if layer.format is not None:
        return registry.table_format(layer.format)
    return registry.table_format_for_extension(_extension(layer))


def _resolve_format(layer: TableLayer, registry: Registry) -> TableFormatPlugin:
    if layer.format is not None:
        return registry.table_format(layer.format)
    ext = _extension(layer)
    if ext == "xls":
        raise ValueError(
            f"Layer '{layer.id}': Legacy-Excel-Format '.xls' wird nicht unterstützt "
            f"(Pfad: {layer.path}) - bitte in '.xlsx' konvertieren (z. B. in Excel/"
            "LibreOffice über 'Speichern unter')."
        )
    fmt = registry.table_format_for_extension(ext)
    if fmt is None:
        hint = (
            " - bei einer URL ohne erkennbare Endung 'format' explizit setzen"
            if _is_url(layer.path)
            else ""
        )
        raise ValueError(
            f"Layer '{layer.id}': unbekanntes Tabellenformat '{ext}' "
            f"(Pfad: {layer.path}){hint}"
        )
    return fmt


def _is_url(path: str) -> bool:
    return path.startswith(("http://", "https://"))


def _fetch_remote(layer: TableLayer, refresh: bool = False) -> Path:
    """FA38: Remote-Tabelle als Datei-Snapshot (FA7) holen; die Formate erwarten
    einen Dateipfad. ``refresh`` bezieht neu. Geschrieben wird erst unter ``.part``
    und dann umbenannt, damit keine abgeschnittene Datei als Snapshot gilt."""
    key = snapshot.payload_key({"kind": "table", "url": layer.path})
    # Endung ohne Query-String/Fragment (unter Windows kein gültiger Dateiname).
    clean = layer.path.split("?", 1)[0].split("#", 1)[0]
    suffix = Path(clean).suffix
    if not suffix or len(suffix) > 10:
        suffix = ".bin"
    target = snapshot.binary_path(key, suffix=suffix)
    if target.is_file() and not refresh:
        snapshot.announce_hit(key)
        return target
    raw = http.download_limited(layer.path, context=f"Tabelle (Layer '{layer.id}')")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(raw)
    partial.replace(target)
    snapshot.write_meta(key, {"backend": "http"})
    return target


def _read_raw_table(
    layer: TableLayer,
    fmt: TableFormatPlugin,
    base_dir: Path | None = None,
    refresh: bool = False,
) -> pd.DataFrame:
    if _is_url(layer.path):
        path = _fetch_remote(layer, refresh)
    else:
        path = Path(layer.path)
        if base_dir is not None and not path.is_absolute():
            path = base_dir / path
        if not path.is_file():
            raise FileNotFoundError(f"Layer '{layer.id}': Datei nicht gefunden: {path}")
    return fmt.read(layer, path)


def _normalize_numeric_string(series: pd.Series, layer: TableLayer) -> pd.Series:
    """Bringt lokales Zahlenformat (Dezimalkomma, Tausendertrenner) vor dem
    astype-Cast in float-parsebare Form."""
    result = series
    if layer.thousands is not None:
        result = result.str.replace(layer.thousands, "", regex=False)
    if layer.decimal != ".":
        result = result.str.replace(layer.decimal, ".", regex=False)
    return result


def _needs_numeric_normalization(layer: TableLayer) -> bool:
    return layer.decimal != "." or layer.thousands is not None


def _build_geometry(layer: TableLayer, df: pd.DataFrame) -> gpd.GeoSeries:
    mapping = layer.geometry
    if mapping.wkt_column is not None:
        if mapping.wkt_column not in df.columns:
            raise ValueError(
                f"Layer '{layer.id}': WKT-Spalte '{mapping.wkt_column}' fehlt in der Quelle"
            )
        geometry = df[mapping.wkt_column].apply(shapely_wkt.loads)
    else:
        for col in (mapping.x_column, mapping.y_column):
            if col not in df.columns:
                raise ValueError(
                    f"Layer '{layer.id}': Koordinatenspalte '{col}' fehlt in der Quelle"
                )
        x_raw = df[mapping.x_column]
        y_raw = df[mapping.y_column]
        if _needs_numeric_normalization(layer):
            x_raw = _normalize_numeric_string(x_raw, layer)
            y_raw = _normalize_numeric_string(y_raw, layer)
        x = x_raw.astype(float)
        y = y_raw.astype(float)
        geometry = [Point(xi, yi) for xi, yi in zip(x, y)]
    return gpd.GeoSeries(geometry, crs=mapping.crs)


def _apply_column_selection(
    df: pd.DataFrame, columns: dict[str, ColumnSpec], layer: TableLayer
) -> pd.DataFrame:
    if not columns:
        return df.copy()
    selected = pd.DataFrame(index=df.index)
    normalize = _needs_numeric_normalization(layer)
    for source_col, spec in columns.items():
        if source_col not in df.columns:
            raise ValueError(
                f"Spalte '{source_col}' fehlt in der Quelle (columns-Mapping)"
            )
        target_name = spec.as_ or source_col
        target_dtype = _TYPE_CAST[spec.type]
        source_series = df[source_col]
        if normalize and spec.type in ("float", "integer"):
            source_series = _normalize_numeric_string(source_series, layer)
        selected[target_name] = source_series.astype(target_dtype)
    return selected


def load(layer: TableLayer, ctx: LoadContext) -> gpd.GeoDataFrame:
    """Einziger Einstieg der Quellart (FA45). Ein relativer layer.path gilt gegen
    ctx.base_dir (FA4), None = Prozess-CWD."""
    fmt = _resolve_format(layer, ctx.registry_or_default())
    if layer.options is None:
        # Format erst jetzt bekannt (URL ohne Endung): die zusätzlichen Felder
        # des Layers gegen sein Optionsmodell prüfen.
        try:
            layer.bind_options(
                validate_format_options(
                    layer.format_extras,
                    fmt.options_model,
                    owner=f"des Tabellenformats '{fmt.name}'",
                )
            )
        except ValidationError as exc:
            raise ValueError(f"Layer '{layer.id}': {describe_errors(exc)}") from exc
    raw = _read_raw_table(layer, fmt, base_dir=ctx.base_dir, refresh=ctx.refresh)
    attributes = _apply_column_selection(raw, layer.columns, layer)

    if layer.geometry is None:
        # FA37: Attribut-Layer. Eine leere Geometriespalte statt gar keiner hält ihn
        # ein GeoDataFrame (LayerData, serialize, view_columns); geometrische
        # Operationen scheitern daran explizit.
        return gpd.GeoDataFrame(
            attributes, geometry=gpd.GeoSeries([None] * len(attributes), crs=None)
        )

    geometry = _build_geometry(layer, raw)
    return gpd.GeoDataFrame(
        attributes, geometry=geometry.values, crs=layer.geometry.crs
    )


def _table_transform(layer: TableLayer, data, ctx: LoadContext):
    """FA37: ein geometrieloser Attribut-Layer hat nichts zu reprojizieren
    ('reproject' bricht ohne CRS ab); die Quellart wählt ihre Transformation selbst (FA40)."""
    name = "none" if layer.geometry is None else "reproject"
    return ctx.registry_or_default().resolve_transform(name)(layer, data, ctx)


register_source(
    "table",
    TableLayer,
    transform=_table_transform,
    description="Tabellarische Quelle (CSV, SQL, SQLite, Excel, feste Breite) mit Geometrie-Mapping",
)(load)
