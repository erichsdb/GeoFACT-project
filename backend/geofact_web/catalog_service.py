"""Datenquellen-Katalog für das Frontend.

Vier Kategorien:
- OSM-Presets: kuratierte Tag-Filter; der Bezug läuft im Kern über das aktive OSM-Backend.
- Copernicus/Raster: lokale GeoTIFFs aus GEOFACT_COPERNICUS_DIR.
- Uploads: pro Eigentümer isoliert (FA28) und einzeln löschbar.
- Beispieldaten: eingecheckte Auszüge offener Daten unter examples/, beschrieben in
  data/sample_sources.yaml (FA90).

Jede Quelle liefert ein 'layer_template', ein einbaufertiges Fragment für die layers-Liste.

Implements: FA28 (Upload-Eigentümerschaft + Löschung).

Implements: FA79 (``raster_profile``: Kennwerte eines lokalen Rasters für das
LLM-Grounding).

Implements: FA90 (``sample_sources``: mitgelieferte Beispieldaten als Katalogquellen).
"""

from __future__ import annotations

import os
import shutil
import warnings
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .models import CatalogSource
from .settings import Settings

# --- Kuratierte OSM-Presets (Tag-Filter -> Layer-Template) ---

_OSM_PRESETS: list[dict[str, Any]] = [
    {
        "id": "osm.power_substation",
        "label": "Umspannwerke (power=substation)",
        "description": "Punkt-/Flächenobjekte des Stromnetzes.",
        "tags": {"power": "substation"},
    },
    {
        "id": "osm.power_line",
        "label": "Stromleitungen (power=line)",
        "description": "Hochspannungs-Trassen als Linien.",
        "tags": {"power": "line"},
    },
    {
        "id": "osm.roads_primary",
        "label": "Hauptstraßen (highway=primary)",
        "description": "Übergeordnetes Straßennetz.",
        "tags": {"highway": "primary"},
    },
    {
        "id": "osm.parks",
        "label": "Grünflächen (Parks/Gärten)",
        "description": "Parks ODER Gärten ODER Dorfanger (OR-Tag-Alternativen).",
        "tags": [
            {"leisure": "park"},
            {"leisure": "garden"},
            {"landuse": "village_green"},
        ],
    },
    {
        "id": "osm.hospitals",
        "label": "Krankenhäuser (amenity=hospital)",
        "description": "Gesundheitsversorgung.",
        "tags": {"amenity": "hospital"},
    },
    {
        "id": "osm.schools",
        "label": "Schulen (amenity=school)",
        "description": "Bildungseinrichtungen.",
        "tags": {"amenity": "school"},
    },
    {
        "id": "osm.buildings",
        "label": "Gebäude (building=yes)",
        "description": "Gebäudegrundrisse.",
        "tags": {"building": "yes"},
    },
    {
        "id": "osm.water",
        "label": "Gewässer (natural=water)",
        "description": "Seen und Wasserflächen.",
        "tags": {"natural": "water"},
    },
]


def _osm_layer_id(preset_id: str) -> str:
    return preset_id.split(".", 1)[-1]


def osm_presets() -> list[CatalogSource]:
    sources: list[CatalogSource] = []
    for preset in _OSM_PRESETS:
        layer_id = _osm_layer_id(preset["id"])
        sources.append(
            CatalogSource(
                id=preset["id"],
                kind="osm_preset",
                label=preset["label"],
                description=preset["description"],
                data_type="vector",
                layer_template={
                    "id": layer_id,
                    "source": "osm",
                    "tags": preset["tags"],
                },
            )
        )
    return sources


# --- Lokaler Copernicus-/Raster-Katalog ---

_RASTER_EXTS = {".tif", ".tiff", ".geotiff"}


def _raster_label(path: Path) -> str:
    stem = path.stem
    lowered = stem.lower()
    if "ghs_pop" in lowered or "ghs-pop" in lowered:
        return f"Bevölkerung (GHS-POP) - {path.name}"
    if "land" in lowered and "cover" in lowered:
        return f"Landbedeckung - {path.name}"
    return path.name


def copernicus_catalog(settings: Settings) -> list[CatalogSource]:
    directory = settings.copernicus_dir
    if not directory.is_dir():
        # Regel 7: ein fehlendes Verzeichnis darf nicht wie ein leerer Katalog aussehen.
        # /api/meta meldet zusätzlich copernicus_dir_exists, hier ist der Log-Eintrag.
        warnings.warn(
            f"Copernicus-/Rasterverzeichnis '{directory}' existiert nicht - "
            f"Rasterkatalog bleibt leer (GEOFACT_COPERNICUS_DIR prüfen).",
            stacklevel=2,
        )
        return []
    sources: list[CatalogSource] = []
    for path in sorted(directory.rglob("*")):
        if path.suffix.lower() not in _RASTER_EXTS:
            continue
        layer_id = _slug(path.stem)
        sources.append(
            CatalogSource(
                id=f"copernicus.{layer_id}",
                kind="copernicus",
                label=_raster_label(path),
                description=f"Lokales Raster: {path.relative_to(directory)}",
                data_type="raster",
                layer_template={
                    "id": layer_id,
                    "source": "file",
                    "path": str(path),
                    "data_type": "raster",
                    "format": "tif",
                },
            )
        )
    return sources


# --- Mitgelieferte Beispieldaten (FA90) ---

_SAMPLES_LABEL = "geofact_web/data/sample_sources.yaml"


class SampleCatalogError(RuntimeError):
    """Die Beschreibung der Beispieldaten fehlt oder ist ungültig."""


@lru_cache(maxsize=4)
def _sample_entries(source: str) -> tuple[dict[str, Any], ...]:
    """Liest und prüft die Beschreibung der Beispieldaten; ``source`` ist ein Pfad
    oder leer für die mitgelieferte Paketdatei."""
    if source:
        path = Path(source)
        if not path.is_file():
            raise SampleCatalogError(
                f"Beispieldaten-Beschreibung '{path}' nicht gefunden (GEOFACT_SAMPLE_SOURCES)."
            )
        label, text = str(path), path.read_text(encoding="utf-8")
    else:
        resource = resources.files("geofact_web").joinpath(
            "data", "sample_sources.yaml"
        )
        if not resource.is_file():
            raise SampleCatalogError(
                f"Beispieldaten-Beschreibung '{_SAMPLES_LABEL}' (Paketdaten) nicht gefunden."
            )
        label, text = _SAMPLES_LABEL, resource.read_text(encoding="utf-8")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SampleCatalogError(f"'{label}' ist kein gültiges YAML: {exc}") from exc
    entries = raw.get("samples") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise SampleCatalogError(
            f"'{label}' braucht einen Schlüssel 'samples' mit einer Liste"
        )
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SampleCatalogError(f"'{label}': Eintrag {index} ist kein Objekt")
        for required in ("id", "label", "layer"):
            if not entry.get(required):
                raise SampleCatalogError(
                    f"'{label}': Eintrag {index} ({entry.get('id') or 'ohne id'}) braucht '{required}'"
                )
        layer = entry["layer"]
        if not isinstance(layer, dict) or not layer.get("id") or not layer.get("path"):
            raise SampleCatalogError(
                f"'{label}': Eintrag '{entry['id']}': 'layer' braucht 'id' und 'path'"
            )
        if (
            Path(str(layer["path"])).is_absolute()
            or ".." in Path(str(layer["path"])).parts
        ):
            raise SampleCatalogError(
                f"'{label}': Eintrag '{entry['id']}': 'path' muss relativ zum Beispielordner "
                f"sein und darf ihn nicht verlassen ({layer['path']})"
            )
        if entry["id"] in seen:
            raise SampleCatalogError(f"'{label}': doppelte id '{entry['id']}'")
        seen.add(entry["id"])
    return tuple(entries)


def sample_sources(settings: Settings) -> list[CatalogSource]:
    """FA90: die mitgelieferten Beispieldaten als Katalogquellen. ``path`` im
    Layer-Fragment wird zum absoluten Pfad unter ``settings.examples_dir`` (wie bei
    den lokalen Rastern). Fehlt eine beschriebene Datei, entfällt genau dieser
    Eintrag mit einer Warnung (Goldene Regel 7)."""
    sources: list[CatalogSource] = []
    for entry in _sample_entries(os.environ.get("GEOFACT_SAMPLE_SOURCES", "")):
        layer = dict(entry["layer"])
        path = settings.examples_dir / str(layer["path"])
        if not path.is_file():
            warnings.warn(
                f"Beispieldaten '{entry['id']}': Datei '{path}' fehlt - der Eintrag "
                f"erscheint nicht im Katalog.",
                stacklevel=2,
            )
            continue
        layer["path"] = str(path)
        description = " ".join(str(entry.get("description") or "").split())
        join_hint = " ".join(str(entry.get("join_hint") or "").split())
        if join_hint:
            description = f"{description} {join_hint}".strip()
        sources.append(
            CatalogSource(
                id=f"sample.{entry['id']}",
                kind="sample",
                label=str(entry["label"]),
                description=description or None,
                data_type="raster" if layer.get("data_type") == "raster" else "vector",
                layer_template=layer,
            )
        )
    return sources


def reset_sample_cache() -> None:
    """Cache leeren (Tests, die GEOFACT_SAMPLE_SOURCES umschalten)."""
    _sample_entries.cache_clear()


# --- Uploads ---

_VECTOR_EXTS = {
    ".geojson": "geojson",
    ".json": "geojson",
    ".gpkg": "gpkg",
    ".shp": "shp",
}
_TABLE_EXTS = {".csv": "csv", ".sqlite": "sqlite", ".db": "sqlite", ".sql": "sql"}

# FA28: Der Eigentümer eines Uploads steht als Sidecar-Datei im Upload-Unterverzeichnis (kein DB-Backend).
_OWNER_SIDECAR = ".owner"


def _owner_of(upload_dir: Path) -> str:
    """Eigentümer eines Upload-Unterverzeichnisses; ohne Sidecar 'anonymous', der
    gleiche Default wie in auth.py ohne GEOFACT_WEB_USERS."""
    sidecar = upload_dir / _OWNER_SIDECAR
    try:
        return sidecar.read_text(encoding="utf-8").strip() or "anonymous"
    except FileNotFoundError:
        return "anonymous"


def write_upload_owner(upload_dir: Path, owner: str) -> None:
    """Wird von routers/uploads.py direkt nach dem Anlegen des Unterverzeichnisses aufgerufen (FA28)."""
    (upload_dir / _OWNER_SIDECAR).write_text(owner, encoding="utf-8")


def _slug(name: str) -> str:
    return (
        "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name).strip("_")
        or "layer"
    )


def upload_source(path: Path) -> CatalogSource | None:
    """Baut aus einer hochgeladenen Datei ein Katalog-/Layer-Template."""
    ext = path.suffix.lower()
    layer_id = _slug(path.stem)
    if ext in _RASTER_EXTS:
        template = {
            "id": layer_id,
            "source": "file",
            "path": str(path),
            "data_type": "raster",
            "format": "tif",
        }
        data_type = "raster"
    elif ext in _VECTOR_EXTS:
        template = {
            "id": layer_id,
            "source": "file",
            "path": str(path),
            "data_type": "vector",
            "format": _VECTOR_EXTS[ext],
        }
        data_type = "vector"
    elif ext in _TABLE_EXTS:
        # Platzhalter für das Geometrie-Mapping, das der Nutzer im Editor ausfüllt.
        template = {
            "id": layer_id,
            "source": "table",
            "path": str(path),
            "format": _TABLE_EXTS[ext],
            "geometry": {"x_column": "lon", "y_column": "lat", "crs": "EPSG:4326"},
            "columns": {},
        }
        data_type = "vector"
    else:
        return None
    # Der Name des Upload-Unterverzeichnisses macht die id eindeutig, auch bei
    # gleichnamigen Dateien zweier Nutzer.
    return CatalogSource(
        id=f"upload.{path.parent.name}.{path.name}",
        kind="upload",
        label=path.name,
        description=f"Hochgeladen ({data_type})",
        data_type=data_type,  # type: ignore[arg-type]
        layer_template=template,
    )


def list_uploads(settings: Settings, owner: str) -> list[CatalogSource]:
    """Nur Uploads von 'owner' (FA28); jeder liegt in einem eigenen Unterverzeichnis."""
    sources: list[CatalogSource] = []
    for upload_dir in sorted(settings.uploads_dir.glob("*")):
        if not upload_dir.is_dir() or _owner_of(upload_dir) != owner:
            continue
        for path in sorted(upload_dir.glob("*")):
            if path.is_file() and path.name != _OWNER_SIDECAR:
                source = upload_source(path)
                if source is not None:
                    sources.append(source)
    return sources


def find_source(settings: Settings, source_id: str, owner: str) -> CatalogSource | None:
    """Liefert eine Quelle nur, wenn sie öffentlich oder ein Upload von 'owner' ist (FA28);
    fremde Uploads sind nicht auffindbar, wie fremde Läufe (FA24)."""
    for source in (
        *osm_presets(),
        *copernicus_catalog(settings),
        *sample_sources(settings),
        *list_uploads(settings, owner),
    ):
        if source.id == source_id:
            return source
    return None


def delete_upload(settings: Settings, source_id: str, owner: str) -> bool:
    """Löscht einen Upload von 'owner' vollständig (Datei + Unterverzeichnis).

    Gibt False zurück, wenn der Upload nicht existiert oder einem anderen Nutzer gehört;
    der Router meldet beides als 404, damit die Existenz fremder Uploads nichts verrät (FA28).
    """
    source = find_source(settings, source_id, owner)
    if source is None or source.kind != "upload":
        return False
    path = Path(source.layer_template["path"])
    upload_dir = path.parent
    if upload_dir.parent != settings.uploads_dir:  # pragma: no cover - defensiv
        return False
    shutil.rmtree(upload_dir, ignore_errors=True)
    return True


def source_schema_hint(source: CatalogSource) -> str | None:
    """Kompakter Schema-Hinweis fürs LLM-Grounding (Attributspalten bzw. Tag-Keys der
    Quelle); billig und fehlertolerant."""
    template = source.layer_template
    src = template.get("source")
    layer_id = template.get("id")

    if src == "osm":
        tags = template.get("tags")
        keys: list[str] = []
        if isinstance(tags, dict):
            keys = list(tags.keys())
        elif isinstance(tags, list):
            for entry in tags:
                if isinstance(entry, dict):
                    keys.extend(entry.keys())
        keys = sorted(set(keys))
        return (
            f"Layer '{layer_id}' (osm): Tag-Attribute u. a. {', '.join(keys)}"
            if keys
            else None
        )

    path = template.get("path")
    if not path:
        return None
    p = Path(str(path))
    try:
        if p.suffix.lower() in {".geojson", ".json"}:
            import json

            with p.open("r", encoding="utf-8") as fh:
                doc = json.load(fh)
            feats = doc.get("features") if isinstance(doc, dict) else None
            if feats:
                cols = sorted((feats[0].get("properties") or {}).keys())
                return (
                    f"Layer '{layer_id}': Attribute {', '.join(cols)}" if cols else None
                )
        elif p.suffix.lower() == ".csv":
            with p.open("r", encoding="utf-8") as fh:
                header = fh.readline().strip()
            if header:
                return f"Layer '{layer_id}' (csv): Spalten {header}"
    except Exception as exc:  # noqa: BLE001 - Hinweis ist optional, LLM-Grounding
        # darf nicht am Schema-Hinweis scheitern, aber auch nicht stillschweigend (Regel 7).
        warnings.warn(
            f"Schema-Hinweis für Quelle '{layer_id}' ({p}) konnte nicht "
            f"ermittelt werden: {exc}",
            stacklevel=2,
        )
        return None
    return None


# --- Rasterkennwerte (FA79) ---

# Stichprobe: _PROFILE_GRID x _PROFILE_GRID Blöcke von _PROFILE_BLOCK Zellen
# Kantenlänge, gleichmäßig über die Datei verteilt (rund 1 Mio. Zellen).
_PROFILE_GRID = 8
_PROFILE_BLOCK = 128


def _fmt(value: float) -> str:
    return f"{value:.4g}"


@lru_cache(maxsize=64)
def _raster_profile(path: str, mtime_ns: int, size: int) -> str:
    """Kennwerte einer Rasterdatei; ``mtime_ns`` und ``size`` gehören nur zum
    Cache-Schlüssel (eine ersetzte Datei wird neu gelesen)."""
    import numpy as np
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(path) as ds:
        block_w, block_h = min(_PROFILE_BLOCK, ds.width), min(_PROFILE_BLOCK, ds.height)
        samples = []
        for row in range(_PROFILE_GRID):
            for col in range(_PROFILE_GRID):
                # Blockmitte bei (i + 0.5) / n; volle Auflösung, nie die Übersichten der Datei.
                x = int((col + 0.5) / _PROFILE_GRID * ds.width - block_w / 2)
                y = int((row + 0.5) / _PROFILE_GRID * ds.height - block_h / 2)
                window = Window(
                    max(0, min(x, ds.width - block_w)),
                    max(0, min(y, ds.height - block_h)),
                    block_w,
                    block_h,
                )
                samples.append(ds.read(1, window=window, masked=True).astype("float64"))
        crs = ds.crs.to_string() if ds.crs else "ohne CRS"
        res_x, res_y = abs(ds.res[0]), abs(ds.res[1])
        if ds.crs is not None and ds.crs.is_geographic:
            cell = f"{res_x:.6g} x {res_y:.6g} Grad (rund {_fmt(res_y * 111_320)} m in Nord-Sued-Richtung)"
        else:
            cell = f"{_fmt(res_x)} x {_fmt(res_y)} m"
        unit = ds.units[0] if ds.units and ds.units[0] else None
        nodata, bands = ds.nodata, ds.count

    stacked = np.ma.concatenate([s.ravel() for s in samples])
    values = stacked.compressed()
    values = values[np.isfinite(values)]
    head = f"CRS {crs}, Zelle {cell}, {bands} Band" + ("" if bands == 1 else "er")
    if nodata is not None:
        head += f", Nodata {_fmt(nodata)}"
    if unit:
        head += f", Einheit {unit}"
    if values.size == 0:
        return f"{head}. Stichprobe ohne gültige Zelle."
    share = 100.0 * values.size / stacked.size
    text = (
        f"{head}. Wertebereich Band 1 (Stichprobe aus {len(samples)} Blöcken über die GANZE "
        f"Datei, nicht nur die Region; {share:.0f} % gültig): Minimum {_fmt(values.min())}, "
        f"Median {_fmt(float(np.median(values)))}, 95. Perzentil "
        f"{_fmt(float(np.percentile(values, 95)))}, Maximum {_fmt(values.max())}"
    )
    nonzero = values[values != 0]
    if 0 < nonzero.size < values.size / 2:
        # überwiegend Nullzellen (Meer, unbewohnt): der Median wäre 0 und
        # sagte nichts über die Skala der belegten Zellen
        text += (
            f"; nur Zellen ungleich 0 ({100.0 * nonzero.size / values.size:.0f} %): Median "
            f"{_fmt(float(np.median(nonzero)))}, 95. Perzentil "
            f"{_fmt(float(np.percentile(nonzero, 95)))}"
        )
    return text + "."


def raster_profile(source: CatalogSource) -> str | None:
    """Implements: FA79 - Kennwerte eines lokalen Rasters als Grounding-Hinweis.

    CRS, Zellgröße, Bandzahl, Nodata und Wertebereich von Band 1 aus einer
    Stichprobe in voller Auflösung, damit das Modell Schwellwerte nicht ohne Kenntnis
    der Skala setzt. Je Datei und Änderungszeit einmal berechnet. Nicht lesbar:
    None mit Warnung (Regel 7), die Generierung läuft weiter."""
    template = source.layer_template
    if template.get("data_type") != "raster" or not template.get("path"):
        return None
    path = Path(str(template["path"]))
    try:
        stat = path.stat()
        profile = _raster_profile(str(path), stat.st_mtime_ns, stat.st_size)
    except Exception as exc:  # noqa: BLE001 - der Hinweis ist optional
        warnings.warn(
            f"Rasterkennwerte für '{template.get('id')}' ({path}) konnten nicht ermittelt "
            f"werden: {exc}",
            stacklevel=2,
        )
        return None
    return (
        f"Kennwerte der Rasterquelle '{template.get('id')}' ({source.label}): {profile}"
    )
