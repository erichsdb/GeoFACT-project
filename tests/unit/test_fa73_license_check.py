"""Implements: FA73 (Lizenzkompatibilitaet je Ausgabe ueber DALICC).

Contract tests for ``support/dalicc.py`` (name -> DALICC id, request, cache) and
``engine/licenses.py`` (status per output: compatible / conflicts / not checkable /
unreachable), plus ``geofact licenses`` and ``geofact validate --check-licenses``.
HTTP is replaced by a fake (``get``/``post`` seams or monkeypatched
``support.http``); the one live test is marked ``netcheck``.
"""

from __future__ import annotations

import json

import pytest
from _cli_doubles import base_config, geofact, write_config

from geofact import api
from geofact.support import dalicc, http
from geofact.support.dalicc import LIBRARY_PREFIX, DaliccClient

ODBL_URI = LIBRARY_PREFIX + "OdcOpenDatabaseLicense"
CC_BY_URI = LIBRARY_PREFIX + "CC-BY-4.0"
CC_BY_SA_URI = LIBRARY_PREFIX + "CC-BY-SA-4.0"
CC0_URI = LIBRARY_PREFIX + "Cc010Universal"

LIBRARY = {
    "results": {
        "bindings": [
            {"id": {"value": uri}, "title": {"value": uri.rsplit("/", 1)[1]}}
            for uri in (ODBL_URI, CC_BY_URI, CC_BY_SA_URI, CC0_URI)
        ]
    },
}

CONFLICT = {
    "conflicting_statements": {
        "direct": {
            "0": {
                "statement_1": [
                    CC_BY_URI,
                    "http://www.w3.org/ns/odrl/2/permission",
                    "https://dalicc.net/ns#chargeDistributionFee",
                ],
                "statement_2": [
                    ODBL_URI,
                    "http://www.w3.org/ns/odrl/2/prohibition",
                    "https://dalicc.net/ns#chargeDistributionFee",
                ],
                "reason": "Direct permission-prohibition conflict.",
            }
        },
        "derived": {},
    }
}
NO_CONFLICT = {"conflicting_statements": {"direct": {}, "derived": {}}}


class FakeDalicc:
    """Records calls; answers the list and the check from fixed data."""

    def __init__(self, check_answer=NO_CONFLICT, fail: bool = False):
        self.check_answer = check_answer
        self.fail = fail
        self.gets: list[str] = []
        self.posts: list[dict] = []

    def get(self, url, *, params=None, context, max_retries=3):
        self.gets.append(url)
        if self.fail:
            raise RuntimeError(f"{context} nicht erreichbar (URL: {url})")
        assert url.endswith("/licenselibrary/list") and params == {"limit": 500}
        return LIBRARY

    def post(self, url, *, body, context, max_retries=3):
        self.posts.append(body)
        if self.fail:
            raise RuntimeError(f"{context} nicht erreichbar (URL: {url})")
        assert url.endswith("/compatibilitycheck/")
        return self.check_answer

    def client(self, tmp_path, **kwargs) -> DaliccClient:
        return DaliccClient(
            cache_dir=tmp_path / "dalicc", get=self.get, post=self.post, **kwargs
        )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOFACT_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.chdir(tmp_path)


def _scenario(
    *,
    source_license: dict | None,
    output_license: dict | None = None,
    scenario_license: dict | None = None,
) -> api.ScenarioDocument:
    raw = base_config()
    if source_license is not None:
        raw["layers"][0]["license"] = source_license
    if output_license is not None:
        raw["output"][0]["license"] = output_license
    if scenario_license is not None:
        raw["scenario"]["output_license"] = scenario_license
    return api.load_scenario(raw)


def _check(document, fake, tmp_path):
    from geofact.engine.licenses import check_licenses

    return check_licenses(
        document.scenario, api.plan(document), client=fake.client(tmp_path)
    )


# --- name -> DALICC id ------------------------------------------------------


@pytest.mark.parametrize(
    "name, uri",
    [
        ("ODbL-1.0", ODBL_URI),
        ("odbl-1.0", ODBL_URI),
        ("CC0-1.0", CC0_URI),
        ("CC-BY-4.0", CC_BY_URI),
        ("CC-BY-SA-4.0", CC_BY_SA_URI),
        ("PDDL", LIBRARY_PREFIX + "OdcPublicDomainDedicationAndLicence"),
        ("ODC-By", LIBRARY_PREFIX + "OpenDataCommonsAttributionLicenseV10"),
    ],
)
def test_fa73_common_license_names_map_to_dalicc_ids(name, uri):
    assert dalicc.resolve(name).uri == uri


@pytest.mark.parametrize(
    "name", ["CC-BY-SA-3.0-IGO", "dl-de/by-2-0", "dl-de/zero-2-0", "GeoNutzV"]
)
def test_fa73_licenses_missing_in_dalicc_are_not_mappable(name):
    resolution = dalicc.resolve(name)
    assert resolution.uri is None
    assert "fehlt in der DALICC-Bibliothek" in resolution.reason


def test_fa73_unknown_name_asks_for_an_explicit_id():
    resolution = dalicc.resolve("Hausrecht-1.0")
    assert resolution.uri is None
    assert "license.id" in resolution.reason


def test_fa73_explicit_id_wins_over_the_name_and_must_be_a_dalicc_uri():
    assert dalicc.resolve("Eigene", CC_BY_URI).uri == CC_BY_URI
    assert dalicc.resolve("ODbL-1.0", "https://example.org/x").uri is None


def test_fa73_license_id_must_be_an_http_uri():
    with pytest.raises(Exception, match="license.id muss mit http"):
        api.License(name="CC-BY-4.0", id="CC-BY-4.0")
    assert api.License(name="X", id=CC_BY_URI).id == CC_BY_URI


def test_fa73_license_to_dict_omits_a_missing_id_and_keeps_a_given_one():
    plain = api.License(name="CC0-1.0", attribution="Eigene Erhebung")
    assert plain.to_dict() == {
        "name": "CC0-1.0",
        "attribution": "Eigene Erhebung",
        "url": None,
    }
    assert api.License(name="X", id=CC_BY_URI).to_dict()["id"] == CC_BY_URI


# --- status per output -------------------------------------------------------


def test_fa73_compatible_when_dalicc_reports_no_conflict(tmp_path):
    fake = FakeDalicc()
    document = _scenario(
        source_license={"name": "CC-BY-SA-4.0"}, output_license={"name": "ODbL-1.0"}
    )
    [check] = _check(document, fake, tmp_path).checks
    assert check.status == "compatible"
    assert fake.posts == [{"licenses": sorted([CC_BY_SA_URI, ODBL_URI])}]
    assert check.checked_at is not None
    assert check.note().startswith("geprüft am ") and check.note().endswith(
        "vereinbar (DALICC)"
    )


def test_fa73_conflicts_carry_reason_statements_and_a_german_message(tmp_path):
    fake = FakeDalicc(check_answer=CONFLICT)
    document = _scenario(
        source_license={"name": "CC-BY-4.0"}, output_license={"name": "ODbL-1.0"}
    )
    report = _check(document, fake, tmp_path)
    [check] = report.checks
    assert report.status == "conflicts" and check.status == "conflicts"
    [conflict] = check.conflicts
    assert conflict.kind == "direct"
    assert conflict.reason == "Direct permission-prohibition conflict."
    assert conflict.statement_1[0] == CC_BY_URI and conflict.statement_2[0] == ODBL_URI
    assert conflict.message == (
        "Direkter Konflikt: CC-BY-4.0 erlaubt „chargeDistributionFee“, ODbL-1.0 verbietet "
        "„chargeDistributionFee“ (DALICC: Direct permission-prohibition conflict.)"
    )
    assert check.to_dict()["conflicts"][0]["message"] == conflict.message


def test_fa73_unmappable_license_is_not_checkable_never_compatible(tmp_path):
    fake = FakeDalicc()
    document = _scenario(
        source_license={"name": "dl-de/by-2-0"}, output_license={"name": "CC-BY-4.0"}
    )
    [check] = _check(document, fake, tmp_path).checks
    assert check.status == "not_checkable"
    assert [item.name for item in check.uncheckable] == ["dl-de/by-2-0"]
    assert fake.posts == []  # one checkable license left: nothing to compare


def test_fa73_unknown_dalicc_id_is_not_checkable_because_the_api_would_report_no_conflict(
    tmp_path,
):
    fake = FakeDalicc()
    document = _scenario(
        source_license={"name": "Tippfehler", "id": LIBRARY_PREFIX + "CC-BY-4.O"},
        output_license={"name": "ODbL-1.0"},
    )
    [check] = _check(document, fake, tmp_path).checks
    assert check.status == "not_checkable"
    assert "nicht vorhanden" in check.uncheckable[0].reason
    assert fake.posts == []


def test_fa73_service_unreachable_is_its_own_status(tmp_path):
    fake = FakeDalicc(fail=True)
    document = _scenario(
        source_license={"name": "CC-BY-4.0"}, output_license={"name": "ODbL-1.0"}
    )
    report = _check(document, fake, tmp_path)
    assert report.status == "unreachable"
    assert "nicht erreichbar" in report.checks[0].message


def test_fa73_without_any_license_there_is_nothing_to_check(tmp_path):
    fake = FakeDalicc()
    [check] = _check(_scenario(source_license=None), fake, tmp_path).checks
    assert check.status == "no_licenses"
    assert fake.gets == [] and fake.posts == []


def test_fa73_undeclared_source_with_output_license_is_not_checkable_never_compatible(
    tmp_path,
):
    fake = FakeDalicc()
    document = _scenario(source_license=None, output_license={"name": "CC-BY-4.0"})
    [check] = _check(document, fake, tmp_path).checks
    assert check.undeclared == ("umspannwerke",)
    assert check.status == "not_checkable"
    assert "ohne deklarierte Lizenz: umspannwerke" in check.message
    assert "vereinbar (DALICC)" not in (check.note() or "")


def test_fa73_undeclared_source_does_not_hide_a_conflict(tmp_path):
    raw = base_config()
    raw["layers"][0]["license"] = {"name": "CC-BY-4.0"}
    raw["layers"].append({**raw["layers"][0], "id": "ohne_lizenz"})
    raw["layers"][1].pop("license")
    raw["steps"].append(
        {
            "id": "zuschnitt",
            "op": "clip",
            "inputs": {"features": "einzug", "mask": "ohne_lizenz"},
        }
    )
    raw["output"] = [
        {
            "type": "geojson",
            "source": "zuschnitt",
            "path": "z.geojson",
            "license": {"name": "ODbL-1.0"},
        }
    ]
    document = api.load_scenario(raw)
    [check] = _check(document, FakeDalicc(check_answer=CONFLICT), tmp_path).checks
    assert check.undeclared == ("ohne_lizenz",)
    assert check.status == "conflicts"
    assert "ohne deklarierte Lizenz: ohne_lizenz" in check.message


def test_fa73_run_stays_unchecked_when_only_another_scenarios_check_is_cached(tmp_path):
    from geofact.engine.licenses import output_license_for

    other = _scenario(
        source_license={"name": "CC-BY-4.0"}, output_license={"name": "ODbL-1.0"}
    )
    fake = FakeDalicc()
    _check(other, fake, tmp_path)  # leaves licenselist.json and the CC-BY+ODbL check
    assert (tmp_path / "dalicc" / "licenselist.json").is_file()

    unchecked = _scenario(
        source_license={"name": "ODbL-1.0"}, output_license={"name": "ODbL-1.0"}
    )
    offline = DaliccClient(cache_dir=tmp_path / "dalicc", offline=True)
    scenario = unchecked.scenario
    result = output_license_for(
        scenario, api.plan(unchecked), scenario.output[0], client=offline
    )
    assert result is not None and result.check is None

    # after an explicit check of exactly this set the run names the date
    _check(unchecked, fake, tmp_path)
    assert len(fake.posts) == 1  # a single licence is never sent to DALICC
    result = output_license_for(
        scenario, api.plan(unchecked), scenario.output[0], client=offline
    )
    assert result.check is not None and result.check.startswith("geprüft am ")


def test_fa73_refresh_fetches_the_library_once_per_check_run(tmp_path):
    raw = base_config()
    raw["layers"][0]["license"] = {"name": "CC-BY-4.0"}
    raw["output"][0]["license"] = {"name": "ODbL-1.0"}
    raw["output"].append({**raw["output"][0], "path": "zweite.geojson"})
    document = api.load_scenario(raw)
    fake = FakeDalicc()
    from geofact.engine.licenses import check_licenses

    report = check_licenses(
        document.scenario,
        api.plan(document),
        client=fake.client(tmp_path, refresh=True),
    )
    assert len(report.checks) == 2
    assert (len(fake.gets), len(fake.posts)) == (1, 1)


def test_fa73_output_license_overrides_the_scenario_default(tmp_path):
    document = _scenario(
        source_license=None,
        output_license={"name": "CC0-1.0"},
        scenario_license={"name": "ODbL-1.0"},
    )
    scenario = document.scenario
    assert scenario.output_license_of(scenario.output[0])[0].name == "CC0-1.0"
    default_only = _scenario(
        source_license=None, scenario_license={"name": "ODbL-1.0"}
    ).scenario
    license_, origin = default_only.output_license_of(default_only.output[0])
    assert (license_.name, origin) == ("ODbL-1.0", "scenario")


# --- cache under the snapshot dir ----------------------------------------------


def test_fa73_answers_are_cached_and_reused_without_network(tmp_path):
    fake = FakeDalicc(check_answer=CONFLICT)
    document = _scenario(
        source_license={"name": "CC-BY-4.0"}, output_license={"name": "ODbL-1.0"}
    )
    first = _check(document, fake, tmp_path).checks[0]
    assert (len(fake.gets), len(fake.posts)) == (1, 1)
    cached = sorted(path.name for path in (tmp_path / "dalicc").iterdir())
    assert cached[-1] == "licenselist.json" and cached[0].startswith("check-")

    second = _check(document, fake, tmp_path).checks[0]
    assert (len(fake.gets), len(fake.posts)) == (1, 1)
    assert second.to_dict() == first.to_dict()

    offline = DaliccClient(cache_dir=tmp_path / "dalicc", offline=True)
    from geofact.engine.licenses import check_licenses

    assert (
        check_licenses(document.scenario, api.plan(document), client=offline).status
        == "conflicts"
    )


def test_fa73_offline_client_without_cache_never_asks_the_network(tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline darf nicht fragen")

    client = DaliccClient(
        cache_dir=tmp_path / "leer", offline=True, get=forbidden, post=forbidden
    )
    assert client.library() is None
    assert client.check([CC_BY_URI, ODBL_URI]) is None


def test_fa73_default_cache_lives_under_the_snapshot_dir(tmp_path):
    assert DaliccClient().cache_dir == tmp_path / "snapshots" / "dalicc"


# --- API and CLI ---------------------------------------------------------------


@pytest.fixture()
def fake_http(monkeypatch):
    fake = FakeDalicc(check_answer=CONFLICT)
    monkeypatch.setattr(http, "get_json", fake.get)
    monkeypatch.setattr(http, "post_json", fake.post)
    return fake


def test_fa73_api_check_licenses_reports_per_output(fake_http):
    document = _scenario(
        source_license={"name": "CC-BY-4.0"}, output_license={"name": "ODbL-1.0"}
    )
    report = api.check_licenses(document)
    assert isinstance(report, api.LicenseCheckReport)
    assert json.loads(json.dumps(report.to_dict()))["status"] == "conflicts"


def _config(tmp_path, output_license: dict | None):
    raw = base_config()
    raw["layers"][0]["license"] = {"name": "CC-BY-4.0", "attribution": "Stadt Beispiel"}
    if output_license is not None:
        raw["output"][0]["license"] = output_license
    return write_config(tmp_path / "konfig", raw)


def test_fa73_cli_licenses_prints_conflicts_and_exits_3(
    tmp_path, monkeypatch, capsys, fake_http
):
    code, stdout, stderr = geofact(
        monkeypatch, capsys, "licenses", _config(tmp_path, {"name": "ODbL-1.0"})
    )
    assert code == 3, stderr
    assert "output[0] geojson <- einzug: KONFLIKT" in stdout
    assert "Ausgabelizenz: ODbL-1.0 (output.license, vom Nutzer festgelegt)" in stdout
    assert "Direkter Konflikt: CC-BY-4.0 erlaubt" in stdout


def test_fa73_cli_validate_check_licenses_and_unreachable_service(
    tmp_path, monkeypatch, capsys
):
    fake = FakeDalicc(fail=True)
    monkeypatch.setattr(http, "get_json", fake.get)
    monkeypatch.setattr(http, "post_json", fake.post)
    config = _config(tmp_path, {"name": "ODbL-1.0"})
    code, stdout, stderr = geofact(
        monkeypatch, capsys, "validate", config, "--check-licenses"
    )
    assert code == 1
    assert "valide:" in stdout
    assert "nicht geprüft (DALICC nicht erreichbar)" in stdout
    assert "DALICC nicht erreichbar" in stderr


def test_fa73_cli_validate_without_flag_names_the_output_license_and_never_asks(
    tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("validate ohne --check-licenses fragt nicht")

    monkeypatch.setattr(http, "get_json", forbidden)
    monkeypatch.setattr(http, "post_json", forbidden)
    code, stdout, _ = geofact(
        monkeypatch, capsys, "validate", _config(tmp_path, {"name": "ODbL-1.0"})
    )
    assert code == 0
    assert "Ausgabelizenzen (vom Nutzer festgelegt" in stdout
    assert "output[0] geojson <- einzug: ODbL-1.0" in stdout


# --- live --------------------------------------------------------------------


@pytest.mark.netcheck
def test_fa73_live_dalicc_finds_the_cc_by_odbl_conflict_and_knows_the_ids(tmp_path):
    client = DaliccClient(cache_dir=tmp_path / "live")
    library = client.library()
    for uri in (ODBL_URI, CC_BY_URI, CC_BY_SA_URI, CC0_URI):
        assert uri in library, uri
    record = client.check([ODBL_URI, CC_BY_URI])
    assert record["response"]["conflicting_statements"]["direct"], record
