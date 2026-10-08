"""Implements: FA14 (Tabellen-Konnektor).

Contract-Tests für src/geofact/builtin/sources/table.py und builtin/formats/tables.py.
"""

from pathlib import Path

import geopandas as gpd
import pytest
from pydantic import ValidationError

from geofact.core.scenario import Scenario
from geofact.builtin.sources import table as tables
from geofact.builtin.sources.table import TableLayer
from geofact.builtin.operations import attribute_join as _attribute_join  # noqa: F401
from geofact.plugin_api import LoadContext

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def test_fa14_csv_xy_mapping():
    layer = TableLayer(
        id="ladesaeulen",
        path=str(FIXTURES_DIR / "ladesaeulen.csv"),
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
    )
    result = tables.load(layer, LoadContext())
    assert isinstance(result, gpd.GeoDataFrame)
    assert len(result) == 3
    assert result.crs.to_string() == "EPSG:4326"
    assert result.geometry.iloc[0].x == pytest.approx(13.725123)


def test_fa14_relative_path_resolves_against_base_dir():
    """base_dir (FA4-Pfadauflösung, siehe test_fa04_files.py für die
    vollständige Begründung): gilt auch für Tabellen-Layer."""
    layer = TableLayer(
        id="ladesaeulen",
        path="ladesaeulen.csv",
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
    )
    result = tables.load(layer, LoadContext(base_dir=FIXTURES_DIR))
    assert len(result) == 3


def test_fa14_csv_wkt_mapping():
    layer = TableLayer(
        id="netz_wkt",
        path=str(FIXTURES_DIR / "netz_wkt.csv"),
        geometry={"wkt_column": "geom_wkt"},
    )
    result = tables.load(layer, LoadContext())
    assert isinstance(result, gpd.GeoDataFrame)
    assert len(result) == 2
    assert result.geometry.iloc[0].x == pytest.approx(13.720)


def test_fa14_column_selection_and_rename():
    layer = TableLayer(
        id="ladesaeulen",
        path=str(FIXTURES_DIR / "ladesaeulen.csv"),
        geometry={"x_column": "longitude", "y_column": "latitude"},
        columns={
            "betreiber": {"as": "operator", "type": "string"},
            "leistung_kw": {"as": "power_kw", "type": "float"},
        },
    )
    result = tables.load(layer, LoadContext())
    assert list(result.columns) == ["operator", "power_kw", "geometry"]
    # ungelistete Spalten (id, longitude, latitude) sind verworfen
    assert "id" not in result.columns
    assert "longitude" not in result.columns


def test_fa14_sql_table():
    layer = TableLayer(
        id="netzdaten",
        path=str(FIXTURES_DIR / "netz.sqlite"),
        table="umspannwerke",
        geometry={"wkt_column": "geom_wkt"},
    )
    result = tables.load(layer, LoadContext())
    assert len(result) == 3
    assert result.geometry.iloc[0].x == pytest.approx(13.720)


def test_fa14_sql_query():
    layer = TableLayer(
        id="netzdaten_gefiltert",
        path=str(FIXTURES_DIR / "netz.sqlite"),
        query="SELECT * FROM umspannwerke WHERE spannung >= 220",
        geometry={"wkt_column": "geom_wkt"},
    )
    result = tables.load(layer, LoadContext())
    assert len(result) == 2


def test_fa14_sql_dump_loaded_via_temp_sqlite(tmp_path):
    dump_copy = tmp_path / "netz.sql"
    dump_copy.write_text(
        (FIXTURES_DIR / "netz.sql").read_text(encoding="utf-8"), encoding="utf-8"
    )
    layer = TableLayer(
        id="netzdaten_dump",
        path=str(dump_copy),
        table="umspannwerke",
        geometry={"wkt_column": "geom_wkt"},
    )
    result = tables.load(layer, LoadContext())
    assert len(result) == 3
    # kein Nebeneffekt: neben dem Dump selbst liegt keine zusätzliche Datei
    assert list(tmp_path.iterdir()) == [dump_copy]


def test_fa14_missing_coordinate_column_raises():
    layer = TableLayer(
        id="ladesaeulen",
        path=str(FIXTURES_DIR / "ladesaeulen.csv"),
        geometry={"x_column": "lon_missing", "y_column": "lat_missing"},
    )
    with pytest.raises(ValueError, match="lon_missing"):
        tables.load(layer, LoadContext())


# =====================================================================
# CSV-Dialekt (encoding/delimiter/decimal/thousands)
# =====================================================================


def test_fa14_default_dialect_unchanged():
    """Regression: der Default-Dialekt (utf-8, Komma-Trennung, Dezimal-
    punkt) verhält sich exakt wie vor der Dialekt-Erweiterung."""
    layer = TableLayer(
        id="ladesaeulen",
        path=str(FIXTURES_DIR / "ladesaeulen.csv"),
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
    )
    result = tables.load(layer, LoadContext())
    assert len(result) == 3
    assert result.geometry.iloc[0].x == pytest.approx(13.725123)


def test_fa14_csv_encoding_and_delimiter():
    layer = TableLayer(
        id="ladesaeulen_dialekt",
        path=str(FIXTURES_DIR / "ladesaeulen_dialekt.csv"),
        encoding="windows-1252",
        delimiter=";",
        decimal=",",
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
        columns={"betreiber": {"type": "string"}},
    )
    result = tables.load(layer, LoadContext())
    assert len(result) == 2
    assert result["betreiber"].iloc[0] == "Stadtwerke München"
    assert result["betreiber"].iloc[1] == "Energieversorgung Südwest"


def test_fa14_csv_decimal_comma_geometry():
    layer = TableLayer(
        id="ladesaeulen_dialekt",
        path=str(FIXTURES_DIR / "ladesaeulen_dialekt.csv"),
        encoding="windows-1252",
        delimiter=";",
        decimal=",",
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
    )
    result = tables.load(layer, LoadContext())
    assert result.geometry.iloc[0].x == pytest.approx(13.725123)
    assert result.geometry.iloc[0].y == pytest.approx(51.041456)


def test_fa14_csv_decimal_comma_float_column():
    layer = TableLayer(
        id="ladesaeulen_dialekt",
        path=str(FIXTURES_DIR / "ladesaeulen_dialekt.csv"),
        encoding="windows-1252",
        delimiter=";",
        decimal=",",
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
        columns={"leistung_kw": {"as": "power_kw", "type": "float"}},
    )
    result = tables.load(layer, LoadContext())
    assert result["power_kw"].iloc[0] == pytest.approx(22.0)
    assert result["power_kw"].iloc[1] == pytest.approx(50.0)


def test_fa14_csv_thousands_separator():
    layer = TableLayer(
        id="ladesaeulen_tausender",
        path=str(FIXTURES_DIR / "ladesaeulen_tausender.csv"),
        delimiter=";",
        decimal=",",
        thousands=".",
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
        columns={"leistung_kw": {"as": "power_kw", "type": "float"}},
    )
    result = tables.load(layer, LoadContext())
    assert result["power_kw"].iloc[0] == pytest.approx(1234.5)
    assert result["power_kw"].iloc[1] == pytest.approx(2500.0)


def test_fa14_csv_wrong_encoding_raises_with_context():
    layer = TableLayer(
        id="ladesaeulen_dialekt",
        path=str(FIXTURES_DIR / "ladesaeulen_dialekt.csv"),
        encoding="utf-8",
        delimiter=";",
        decimal=",",
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
    )
    with pytest.raises(ValueError, match="ladesaeulen_dialekt"):
        tables.load(layer, LoadContext())


def test_fa14_csv_decimal_equals_thousands_raises():
    with pytest.raises(ValidationError, match="decimal"):
        TableLayer(
            id="ladesaeulen",
            path=str(FIXTURES_DIR / "ladesaeulen_tausender.csv"),
            decimal=",",
            thousands=",",
            geometry={"x_column": "longitude", "y_column": "latitude"},
        )


def test_fa14_csv_quotechar_reads_single_quoted_wkt():
    """FA14-Zusatz: PyPSA-Eur quotes its WKT column with single quotes; with
    quotechar="'" the file loads unchanged, one geometry per row."""
    layer = TableLayer(
        id="lines",
        path=str(FIXTURES_DIR / "europa" / "pypsa_lines_quoted.csv"),
        quotechar="'",
        geometry={"wkt_column": "geometry", "crs": "EPSG:4326"},
    )
    result = tables.load(layer, LoadContext())
    assert list(result["line_id"]) == ["L1", "L2"]
    assert result.geometry.iloc[0].geom_type == "LineString"
    assert len(result.geometry.iloc[0].coords) == 3
    assert result["tags"].iloc[0] == "relation/1;way/2"


def test_fa14_csv_default_quotechar_fails_loudly_on_single_quoted_wkt():
    """Without quotechar the commas inside the WKT split the row: an explicit
    parser error, never a silently shifted table."""
    layer = TableLayer(
        id="lines",
        path=str(FIXTURES_DIR / "europa" / "pypsa_lines_quoted.csv"),
        geometry={"wkt_column": "geometry", "crs": "EPSG:4326"},
    )
    with pytest.raises(Exception, match="Expected|Parse"):
        tables.load(layer, LoadContext())


@pytest.mark.parametrize("bad", ["", "''", ","])
def test_fa14_csv_quotechar_must_be_one_char_other_than_the_delimiter(bad):
    with pytest.raises(ValidationError, match="quotechar"):
        TableLayer(
            id="lines",
            path=str(FIXTURES_DIR / "europa" / "pypsa_lines_quoted.csv"),
            quotechar=bad,
            geometry={"wkt_column": "geometry"},
        )


def test_fa14_llm_prompt_lists_the_quotechar_table_field():
    """The web demo's prompt calls its table field list complete - quotechar
    belongs in it (review finding 03.10.2026)."""
    pytest.importorskip("fastapi")
    from geofact_web.llm import build_system_prompt

    prompt = build_system_prompt([], [])
    fields = prompt.split("Felder eines 'table'-Layers", 1)[1].split(
        "- Textdateien", 1
    )[0]
    assert "quotechar?" in fields


# =====================================================================
# Excel (.xlsx)
# =====================================================================


def test_fa14_xlsx_loads():
    layer = TableLayer(
        id="ladesaeulen_xlsx",
        path=str(FIXTURES_DIR / "ladesaeulen.xlsx"),
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
        columns={"betreiber": {"as": "operator", "type": "string"}},
    )
    result = tables.load(layer, LoadContext())
    assert isinstance(result, gpd.GeoDataFrame)
    assert len(result) == 3
    assert result.geometry.iloc[0].x == pytest.approx(13.725123)
    assert result["operator"].iloc[0] == "Stadtwerke"


def test_fa14_xlsx_sheet_param_selects():
    layer = TableLayer(
        id="ladesaeulen_alt",
        path=str(FIXTURES_DIR / "ladesaeulen_zweiblatt.xlsx"),
        sheet="ladesaeulen_alt",
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
        columns={"betreiber": {"as": "operator", "type": "string"}},
    )
    result = tables.load(layer, LoadContext())
    assert len(result) == 1
    assert result["operator"].iloc[0] == "Alt-Betreiber"


def test_fa14_xlsx_multisheet_without_sheet_raises_listing_names():
    layer = TableLayer(
        id="ladesaeulen_zweiblatt",
        path=str(FIXTURES_DIR / "ladesaeulen_zweiblatt.xlsx"),
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
    )
    with pytest.raises(ValueError, match="ladesaeulen_alt"):
        tables.load(layer, LoadContext())


def test_fa14_xls_raises_conversion_hint():
    layer = TableLayer(
        id="ladesaeulen_legacy",
        path="ladesaeulen.xls",
        geometry={"x_column": "longitude", "y_column": "latitude"},
    )
    with pytest.raises(ValueError, match=r"\.xlsx"):
        tables.load(layer, LoadContext())


# =====================================================================
# FA37: Tabelle ohne Geometrie (reine Statistik)
# =====================================================================


def test_fa37_table_without_geometry_loads_as_attribute_layer():
    """Amtliche Statistik (BIP je Bundesland) trägt keine Koordinaten -
    der geometry-Block ist daher optional. Ergebnis ist ein Layer mit
    leerer Geometriespalte, nutzbar als rechte Seite eines
    attribute_join (FA34)."""
    layer = TableLayer(
        id="bip",
        path=str(FIXTURES_DIR / "bip_mini.csv"),
        format="csv",
        delimiter=";",
        columns={"land": {"type": "string"}, "bip_mio_eur": {"type": "float"}},
    )
    assert layer.geometry is None
    gdf = tables.load(layer, LoadContext())
    assert list(gdf.columns) == ["land", "bip_mio_eur", "geometry"]
    assert gdf.geometry.isna().all()
    assert gdf["bip_mio_eur"].tolist() == [886014.0, 854102.0]


def test_fa37_geometry_mapping_still_validated_when_present():
    """Abgrenzung: ist ein geometry-Block angegeben, gilt sein Vertrag
    unverändert (x/y ODER wkt, nicht beides/keins)."""
    with pytest.raises(ValidationError, match="x_column"):
        TableLayer(
            id="t",
            path="x.csv",
            geometry={"crs": "EPSG:4326"},
        )


def test_fa37_geometryless_table_in_scenario_validates():
    """Die Konfiguration ohne geometry-Block ist gültig (FA1)."""
    scenario = Scenario(
        **{
            "scenario": {"name": "bip", "region": "5.8,47.2,15.1,55.1"},
            "layers": [
                {"id": "grenzen", "source": "region", "data_type": "vector"},
                {
                    "id": "bip",
                    "source": "table",
                    "path": "bip.csv",
                    "format": "csv",
                    "columns": {"land": {"type": "string"}},
                },
            ],
            "steps": [
                {
                    "id": "joined",
                    "op": "attribute_join",
                    "inputs": {"left": "grenzen", "right": "bip"},
                    "params": {"left_on": "name", "right_on": "land"},
                },
            ],
            "output": [{"type": "map", "source": "joined"}],
        }
    )
    assert scenario.execution_order() == ["joined"]


# =====================================================================
# FA38 - Tabellarische Quelle über HTTP
# =====================================================================


def test_fa38_url_is_downloaded_not_treated_as_path(tmp_path, monkeypatch):
    r"""Gemeldeter Fehler: eine URL in 'path' wurde als relativer Pfad an
    das Projektverzeichnis gehängt -
    FileNotFoundError: ...\GeoFACT\https:\opendata.dwd.de\..."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snap"))
    calls = []

    def fake_download(url, context=""):
        calls.append(url)
        return b"name,lon,lat\nAlpha,13.7,51.05\nBeta,12.3,51.34\n"

    monkeypatch.setattr(tables.http, "download_limited", fake_download)
    layer = TableLayer(
        id="remote",
        path="https://example.org/daten.csv",
        geometry={"x_column": "lon", "y_column": "lat", "crs": "EPSG:4326"},
    )
    result = tables.load(layer, LoadContext())
    assert calls == ["https://example.org/daten.csv"]
    assert len(result) == 2
    assert result.geometry.iloc[0].x == pytest.approx(13.7)


def test_fa38_url_result_is_cached(tmp_path, monkeypatch):
    """Nachbedingung: der Download wird zwischengespeichert - ein zweiter
    Lauf braucht kein Netz."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snap"))
    calls = []

    def fake_download(url, context=""):
        calls.append(url)
        return b"name,lon,lat\nAlpha,13.7,51.05\n"

    monkeypatch.setattr(tables.http, "download_limited", fake_download)
    layer = TableLayer(
        id="remote",
        path="https://example.org/daten.csv",
        geometry={"x_column": "lon", "y_column": "lat", "crs": "EPSG:4326"},
    )
    tables.load(layer, LoadContext())
    tables.load(layer, LoadContext())
    assert len(calls) == 1, "zweiter Aufruf hätte den Snapshot nutzen müssen"


def test_fa38_local_path_unchanged():
    """Randfall/Regression: ein lokaler Pfad darf sich nicht anders
    verhalten als vor FA38."""
    layer = TableLayer(
        id="ladesaeulen",
        path=str(FIXTURES_DIR / "ladesaeulen.csv"),
        geometry={"x_column": "longitude", "y_column": "latitude", "crs": "EPSG:4326"},
    )
    assert len(tables.load(layer, LoadContext())) == 3


def test_fa38_query_string_does_not_break_format_detection(tmp_path, monkeypatch):
    """Randfall: '.../daten.csv?stand=2026' endet nicht auf '.csv' - die
    Endung muss trotzdem erkannt werden."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setattr(
        tables.http,
        "download_limited",
        lambda url, context="": b"name,lon,lat\nAlpha,13.7,51.05\n",
    )
    layer = TableLayer(
        id="remote",
        path="https://example.org/daten.csv?stand=2026",
        geometry={"x_column": "lon", "y_column": "lat", "crs": "EPSG:4326"},
    )
    assert len(tables.load(layer, LoadContext())) == 1


def test_fa38_url_without_extension_demands_explicit_format(tmp_path, monkeypatch):
    """Nachbedingung: keine stille Annahme, sondern ein Hinweis auf
    'format' (Goldene Regel 7)."""
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snap"))
    layer = TableLayer(
        id="remote",
        path="https://example.org/api/export",
        geometry={"x_column": "lon", "y_column": "lat", "crs": "EPSG:4326"},
    )
    with pytest.raises(ValueError, match="format"):
        tables.load(layer, LoadContext())


# =====================================================================
# FA39 - Feste Spaltenbreite
# =====================================================================


def test_fa39_fixed_width_reads_declared_columns():
    """Happy Path: Spalten stehen unter den deklarierten Namen."""
    layer = TableLayer(
        id="stationen",
        path=str(FIXTURES_DIR / "stationen_fwf.txt"),
        format="fwf",
        encoding="latin-1",
        skiprows=2,
        colspecs=[
            (0, 5),
            (6, 14),
            (15, 23),
            (24, 37),
            (37, 49),
            (49, 59),
            (60, 100),
            (101, 141),
        ],
        names=[
            "Stations_id",
            "von_datum",
            "bis_datum",
            "Stationshoehe",
            "geoBreite",
            "geoLaenge",
            "Stationsname",
            "Bundesland",
        ],
        geometry={"x_column": "geoLaenge", "y_column": "geoBreite", "crs": "EPSG:4326"},
    )
    result = tables.load(layer, LoadContext())
    assert len(result) == 3
    assert result.geometry.iloc[0].x == pytest.approx(12.37)


def test_fa39_values_may_contain_spaces():
    """Der eigentliche Grund für fwf: ein Trennzeichen (auch ein
    Regex) zerlegt Werte, die selbst Leerzeichen enthalten."""
    layer = TableLayer(
        id="stationen",
        path=str(FIXTURES_DIR / "stationen_fwf.txt"),
        format="fwf",
        encoding="latin-1",
        skiprows=2,
        colspecs=[
            (0, 5),
            (6, 14),
            (15, 23),
            (24, 37),
            (37, 49),
            (49, 59),
            (60, 100),
            (101, 141),
        ],
        names=[
            "Stations_id",
            "von_datum",
            "bis_datum",
            "Stationshoehe",
            "geoBreite",
            "geoLaenge",
            "Stationsname",
            "Bundesland",
        ],
    )
    result = tables.load(layer, LoadContext())
    assert "Leipzig (Stadt)" in set(result["Stationsname"])
    assert "Nordrhein-Westfalen" in set(result["Bundesland"])


def test_fa39_colspecs_and_names_must_match():
    """Vorbedingung: ungleiche Länge ist ein Fehler, kein stilles
    Verwerfen von Spalten."""
    with pytest.raises(ValidationError, match="gleich lang"):
        TableLayer(
            id="stationen",
            path="x.txt",
            format="fwf",
            colspecs=[(0, 5), (6, 14)],
            names=["a", "b", "c"],
        )


def test_fa39_fwf_without_colspecs_is_an_error():
    """Vorbedingung: Breiten werden bewusst nicht geraten."""
    with pytest.raises(ValidationError, match="colspecs"):
        TableLayer(id="stationen", path="x.txt", format="fwf")


def test_fa39_invalid_colspec_is_rejected():
    """Randfall: absteigende/negative Grenzen."""
    with pytest.raises(ValidationError, match="Spaltengrenze"):
        TableLayer(
            id="stationen",
            path="x.txt",
            format="fwf",
            colspecs=[(10, 5)],
            names=["a"],
        )


def test_fa39_colspecs_rejected_for_non_fwf_format():
    """Randfall: colspecs bei format csv ist ein Konfigurationsfehler -
    sonst wundert man sich, warum sie wirkungslos bleiben."""
    with pytest.raises(
        ValidationError, match="kein Feld des Tabellenformats 'csv'"
    ) as caught:
        TableLayer(
            id="stationen",
            path="x.csv",
            format="csv",
            colspecs=[(0, 5)],
            names=["a"],
        )
    assert {error["loc"] for error in caught.value.errors()} == {
        ("colspecs",),
        ("names",),
    }


def test_fa38_url_refresh_downloads_again_and_hit_is_announced(tmp_path, monkeypatch):
    """Nachtreview 02.10. (Runde 2): ``ctx.refresh`` wurde für eine
    Remote-Tabelle ignoriert (veraltete Datei nur per Hand löschbar), und die
    Wiederverwendung meldete keinen ``SnapshotNotice``."""
    from geofact.support.snapshot import SnapshotNotice

    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snap"))
    content = {"value": b"name,lon,lat\nalt,13.7,51.05\n"}
    monkeypatch.setattr(
        tables.http, "download_limited", lambda url, context="": content["value"]
    )
    layer = TableLayer(
        id="remote",
        path="https://example.org/d.csv",
        geometry={"x_column": "lon", "y_column": "lat", "crs": "EPSG:4326"},
    )
    assert list(tables.load(layer, LoadContext())["name"]) == ["alt"]
    content["value"] = b"name,lon,lat\nneu,13.7,51.05\n"
    with pytest.warns(SnapshotNotice):
        assert list(tables.load(layer, LoadContext())["name"]) == ["alt"]
    assert list(tables.load(layer, LoadContext(refresh=True))["name"]) == ["neu"]
    assert not list((tmp_path / "snap").glob("*.part"))
