"""Implements: FA65 (Namensnennung: Copyright-Zeichen nur vor einer Rechteinhaber-Nennung).

``License.line`` stellt das Copyright-Zeichen nur einer Rechteinhaber-Nennung
voran; ein Text, der es schon trägt, oder ein Satz mit vorgeschriebenem
Wortlaut (Copernicus) bleibt unverändert."""

from __future__ import annotations

from geofact.core.contracts import License

COPERNICUS = License(
    name="CC-BY-SA-3.0-IGO",
    attribution="Contains modified Copernicus Sentinel data",
    url="https://creativecommons.org/licenses/by-sa/3.0/igo/",
)


def test_fa65_license_line_copernicus_attribution_has_no_copyright_sign():
    assert COPERNICUS.line() == (
        "Contains modified Copernicus Sentinel data "
        "(CC-BY-SA-3.0-IGO, https://creativecommons.org/licenses/by-sa/3.0/igo/)"
    )


def test_fa65_license_line_holder_name_gets_the_copyright_sign():
    osm = License(name="ODbL-1.0", attribution="OpenStreetMap contributors")
    assert osm.line() == "© OpenStreetMap contributors (ODbL-1.0)"


def test_fa65_license_line_keeps_an_existing_copyright_sign_without_doubling():
    assert License(name="dl-de/by-2-0", attribution="© GeoSN 2026").line() == (
        "© GeoSN 2026 (dl-de/by-2-0)"
    )
    assert License(name="CC-BY-4.0", attribution="(c) Stadt Chemnitz").line() == (
        "(c) Stadt Chemnitz (CC-BY-4.0)"
    )


def test_fa65_license_line_german_statement_is_verbatim():
    statement = License(
        name="dl-de/by-2-0", attribution="Quelle: Statistisches Landesamt Sachsen"
    )
    assert statement.line() == "Quelle: Statistisches Landesamt Sachsen (dl-de/by-2-0)"
