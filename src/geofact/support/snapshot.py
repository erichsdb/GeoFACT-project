"""Implements: FA7 (Snapshot-Mechanismus), Grundlage für FA16.

Speichert bezogene Daten lokal (GeoPackage unter GEOFACT_SNAPSHOT_DIR) und
verwendet bei wiederholter Ausführung den gespeicherten Stand statt erneut zu
fetchen. Schlüssel = SHA-256 über kanonisches JSON eines vom Aufrufer
gelieferten Payload-Dicts. Er ist quell-unabhängig, damit dieselbe Konfiguration
unabhängig von Backend/Konnektor denselben Snapshot trifft (FA7-Nachbedingung:
gleiche Konfiguration + Snapshot -> gleiche Eingabedaten). Der OSM-spezifische
Teil (Schlüssel aus Tags + Region) liegt in builtin/sources/osm.py; sein
Payload-Format darf sich nicht ändern, sonst treffen bestehende Snapshots einen
anderen Dateinamen (Regressionstest in test_fa16_opendata.py). Dieses Modul ist
Infrastruktur (FA45): es registriert nichts und kennt keinen Konnektor.

OSM-Tag-Schlüssel sind freier Nutzertext (z. B. '3dr:type') und können als
GeoPackage-Spaltennamen ungültig sein (führende Ziffer, Sonderzeichen). Solche
Spalten werden vor dem Schreiben umbenannt; eine Mapping-Datei neben dem .gpkg
macht das beim Lesen rückgängig, damit ein Snapshot dieselben Spaltennamen
liefert wie ein frischer Fetch.

Metadaten (FA7-Zusatz): jeder frische Bezug schreibt neben das .gpkg eine
``<key>.meta.json`` mit ``fetched_at`` (UTC), ``backend`` und ``osm_base``
(Datenstand, soweit bekannt; der Aufrufer liefert beides über ``meta``). Wird
ein Snapshot wiederverwendet, meldet ``SnapshotNotice`` den Bezugszeitpunkt;
Snapshots ohne Metadaten werden gemeldet, nicht abgelehnt."""

from __future__ import annotations

import hashlib
import json
import os
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Union

import geopandas as gpd
from geopandas import GeoDataFrame


DEFAULT_SNAPSHOT_DIR = ".geofact/snapshots"

_VALID_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _snapshot_dir() -> Path:
    return Path(os.environ.get("GEOFACT_SNAPSHOT_DIR") or DEFAULT_SNAPSHOT_DIR)


def snapshot_dir() -> Path:
    """Wurzel der Snapshot-Ablage (``GEOFACT_SNAPSHOT_DIR``); auch andere
    zwischengespeicherte Antworten (z. B. DALICC, FA73) legen sich darunter ab."""
    return _snapshot_dir()


def payload_key(payload: dict) -> str:
    """SHA-256 über kanonischem JSON eines beliebigen Payload-Dicts
    (deterministisch: sort_keys, ensure_ascii). Grundlage sowohl des
    OSM-Schlüssels (builtin/sources/osm.py, Payload-Form unverändert) als
    auch von wfs/ckan/gtfs/rest/table (FA16, eigene Payload-Form je Quelltyp)."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot_path(key: str) -> Path:
    return _snapshot_dir() / f"{key}.gpkg"


def _meta_path(key: str) -> Path:
    return _snapshot_dir() / f"{key}.meta.json"


class SnapshotNotice(UserWarning):
    """Hinweis (FA7): ein gespeicherter Snapshot wurde statt eines frischen
    Bezugs verwendet - nennt Bezugszeitpunkt, Backend und Datenstand."""


SnapshotMeta = Union[Mapping[str, Any], Callable[[], Mapping[str, Any]], None]
"""Quelleninfo für ``<key>.meta.json``: ein Mapping oder eine Funktion, die
NACH dem Bezug ausgewertet wird (das Backend kennt den Datenstand erst dann)."""

_UNREADABLE = object()


def _read_meta(key: str) -> object:
    path = _meta_path(key)
    if not path.is_file():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _UNREADABLE
    return meta if isinstance(meta, dict) else _UNREADABLE


def snapshot_meta(key: str) -> Optional[dict[str, Any]]:
    """Inhalt von ``<key>.meta.json`` oder None (fehlt oder unlesbar)."""
    meta = _read_meta(key)
    return meta if isinstance(meta, dict) else None


def _write_meta(key: str, meta: SnapshotMeta) -> None:
    extra = dict(meta() if callable(meta) else (meta or {}))
    record = {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "backend": None,
        "osm_base": None,
        **extra,
    }
    _meta_path(key).write_text(
        json.dumps(record, sort_keys=True, ensure_ascii=False), encoding="utf-8"
    )


def _announce_hit(key: str) -> None:
    meta = _read_meta(key)
    head = f"Snapshot {key[:12]} verwendet statt eines frischen Bezugs"
    tail = "--refresh-snapshots bezieht neu"
    if meta is None:
        text = (
            f"{head}; ohne Metadaten (vor dem FA7-Zusatz angelegt) - "
            f"Bezugszeitpunkt unbekannt; {tail}"
        )
    elif meta is _UNREADABLE:
        text = f"{head}; Metadaten unlesbar ({_meta_path(key).name}) - Bezugszeitpunkt unbekannt; {tail}"
    else:
        details = [f"bezogen {meta.get('fetched_at') or 'unbekannt'}"]
        if meta.get("backend"):
            details.append(f"Backend {meta['backend']}")
        if meta.get("osm_base"):
            details.append(f"Datenstand der Quelle {meta['osm_base']}")
        text = f"{head} ({', '.join(details)}); {tail}"
    warnings.warn(text, SnapshotNotice, stacklevel=3)


def announce_hit(key: str) -> None:
    """``SnapshotNotice`` für einen wiederverwendeten Snapshot, der nicht
    über ``fetch_with_snapshot_key`` läuft (z. B. eine Rohdatei unter
    ``binary_path``) - derselbe Text mit Bezugszeitpunkt aus ``<key>.meta.json``."""
    _announce_hit(key)


def write_meta(key: str, meta: SnapshotMeta = None) -> None:
    """``<key>.meta.json`` nach einem frischen Bezug außerhalb von
    ``fetch_with_snapshot_key`` (Bezugszeitpunkt für ``announce_hit``)."""
    _write_meta(key, meta)


def binary_path(key: str, suffix: str = ".bin") -> Path:
    """Ablageort für eine unveränderte heruntergeladene Datei (FA38).

    Anders als bei den GeoPackage-Snapshots wird nichts konvertiert, weil der
    weiterverarbeitende Leser (pandas/sqlite3/openpyxl) das Originalformat
    erwartet; die Endung bleibt für formatabhängige Leser erhalten."""
    return _snapshot_dir() / f"{key}{suffix}"


def _column_map_path(gpkg_path: Path) -> Path:
    return gpkg_path.with_suffix(".columns.json")


def _sanitize_column_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not safe or not re.match(r"^[A-Za-z_]", safe):
        safe = f"col_{safe}"
    return safe


_RESERVED_GPKG_COLUMNS = frozenset({"fid", "geom"})
"""Spaltennamen, die der GPKG-Treiber selbst belegt (Feature-id und
Geometriespalte, ohne Groß-/Kleinschreibung): eine Attributspalte dieses
Namens bricht das Schreiben ab und wird deshalb umbenannt."""


def _nested_path(gpkg_path: Path) -> Path:
    return gpkg_path.with_suffix(".nested.json")


def _is_nested(value: Any) -> bool:
    return isinstance(value, (list, tuple, dict))


def _encode_nested(gdf: GeoDataFrame) -> tuple[GeoDataFrame, list[str]]:
    """Spalten mit Listen-/Mapping-Werten (verschachtelte GeoJSON-Properties)
    als JSON-Text ablegen - GPKG kennt keine verschachtelten Werte. Jeder
    nicht fehlende Wert einer solchen Spalte wird kodiert (auch Text), damit
    das Lesen eindeutig zurückwandelt. Rückgabe: (Frame, Spaltennamen)."""
    columns = [
        col
        for col in gdf.columns
        if col != gdf.geometry.name
        and gdf[col].dtype == object
        and gdf[col].map(_is_nested).any()
    ]
    if not columns:
        return gdf, []
    encoded = gdf.copy()
    for col in columns:
        encoded[col] = gdf[col].map(
            lambda v: (
                None
                if v is None or (isinstance(v, float) and v != v)
                else json.dumps(v, ensure_ascii=False, default=str)
            )
        )
    return encoded, columns


def _decode_nested(gdf: GeoDataFrame, columns: list[str]) -> GeoDataFrame:
    for col in columns:
        if col in gdf.columns:
            gdf[col] = (
                gdf[col]
                .map(lambda v: json.loads(v) if isinstance(v, str) else None)
                .astype(object)
            )
    return gdf


def _sanitize_columns(gdf: GeoDataFrame) -> tuple[GeoDataFrame, dict[str, str]]:
    """Benennt ungültige Spalten um. Gibt (umbenanntes GeoDataFrame,
    Mapping sanitierter->originaler Name) zurück; Mapping ist leer, wenn
    nichts umbenannt werden musste.

    GeoPackage/SQLite vergleicht Spaltennamen case-insensitiv: OSM-Tag-Keys wie
    'FIXME' und 'fixme' kollidieren sonst beim Schreiben, obwohl beide gültig
    sind. Die Kollisionsprüfung ist deshalb case-insensitiv."""
    rename_map: dict[str, str] = {}
    used_names_ci: set[str] = {c.casefold() for c in gdf.columns}
    used_names_ci |= _RESERVED_GPKG_COLUMNS
    for col in gdf.columns:
        needs_rename = col != "geometry" and (
            not _VALID_COLUMN.match(col)
            or col.casefold() in _RESERVED_GPKG_COLUMNS
            or sum(1 for c in gdf.columns if c.casefold() == col.casefold()) > 1
        )
        if not needs_rename:
            continue
        candidate = _sanitize_column_name(col)
        suffix = 1
        while candidate.casefold() in used_names_ci or candidate == col:
            candidate = f"{_sanitize_column_name(col)}_{suffix}"
            suffix += 1
        used_names_ci.add(candidate.casefold())
        rename_map[candidate] = col

    if not rename_map:
        return gdf, {}

    inverse = {original: sanitized for sanitized, original in rename_map.items()}
    renamed = gdf.rename(columns=inverse)
    return renamed, rename_map


def fetch_with_snapshot_key(
    key: str,
    fetch_fn: Callable[[], GeoDataFrame],
    refresh: bool = False,
    meta: SnapshotMeta = None,
) -> GeoDataFrame:
    """Generalisierte Snapshot-Logik (FA16): key ist ein bereits gebildeter
    payload_key()-Hash, fetch_fn liefert bei Cache-Miss die Daten frisch
    (OsmBackend.fetch, wfs.fetch, ckan.fetch, ...).

    FA7-Zusatz: ein frischer Bezug schreibt ``<key>.meta.json`` (``meta``
    liefert Backend/Datenstand); ein Treffer meldet ``SnapshotNotice``. Ein
    fehlgeschlagener Bezug schreibt nichts."""
    path = _snapshot_path(key)
    column_map_path = _column_map_path(path)

    if path.is_file() and not refresh:
        gdf = gpd.read_file(path)
        nested_path = _nested_path(path)
        if nested_path.is_file():
            gdf = _decode_nested(
                gdf, json.loads(nested_path.read_text(encoding="utf-8"))
            )
        if column_map_path.is_file():
            rename_map = json.loads(column_map_path.read_text(encoding="utf-8"))
            gdf = gdf.rename(columns=rename_map)
        _announce_hit(key)
        return gdf

    result = fetch_fn()
    path.parent.mkdir(parents=True, exist_ok=True)

    to_write, rename_map = _sanitize_columns(result)
    to_write, nested = _encode_nested(to_write)
    to_write.to_file(path, driver="GPKG")
    if rename_map:
        column_map_path.write_text(json.dumps(rename_map), encoding="utf-8")
    elif column_map_path.is_file():
        column_map_path.unlink()
    nested_path = _nested_path(path)
    if nested:
        nested_path.write_text(json.dumps(nested), encoding="utf-8")
    elif nested_path.is_file():
        nested_path.unlink()
    _write_meta(key, meta)

    return result
