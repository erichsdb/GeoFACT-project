"""Implements: FA98 (Serverpaket: Werkzeug, OSM-Datenbank Deutschland, Beispieldaten).

Contract-Tests zu den Nachbedingungen von FA98. Sie laufen
offline: die Compose-Datei und der Workflow werden als YAML gelesen, Stil und
Importskript als Text geprüft, data_init.py und wait_ready.py mit Fakes für Download und
Datenbank ausgeführt. Den echten Import gegen PostGIS prüft das nicht; er wurde mit
lokal gebauten Images und dem Sachsen-Extrakt von Hand durchgespielt.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.server.yml"
IMPORT_SH = REPO_ROOT / "docker" / "osm" / "import.sh"
STYLE = REPO_ROOT / "docker" / "osm" / "pipeline.lua"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "images.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.server.example"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "docker" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


data_init = _load("data_init")
wait_ready = _load("wait_ready")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def caddyfile(compose) -> str:
    return compose["configs"]["caddyfile"]["content"]


# --- (1) ein Netz, nur der Proxy nach außen ----------------------------------------------


def test_fa98_all_services_share_exactly_one_network(compose):
    assert set(compose["networks"]) == {"geofact"}
    assert compose["networks"]["geofact"]["name"] == "geofact"
    for name, service in compose["services"].items():
        assert service.get("networks") == ["geofact"], name


def test_fa98_only_the_proxy_publishes_ports(compose):
    published = {name for name, svc in compose["services"].items() if svc.get("ports")}
    assert published == {"proxy"}
    assert "expose" not in compose["services"]["postgis"]


def test_fa98_package_has_the_six_services(compose):
    assert set(compose["services"]) == {
        "postgis",
        "osm-import",
        "data-init",
        "backend",
        "frontend",
        "proxy",
    }


# --- (2) Start erst mit vollständigen Daten, solange eine Einrichtungsseite ---------------


def test_fa98_backend_waits_for_data_inside_the_container(compose):
    backend = compose["services"]["backend"]
    assert backend["command"] == ["python", "/app/docker/wait_ready.py", "geofact-api"]
    # kein service_completed_successfully: "up -d" darf nicht stundenlang blockieren
    deps = backend.get("depends_on", [])
    assert "osm-import" not in deps and "data-init" not in deps


def test_fa98_frontend_starts_after_the_backend_answers(compose):
    command = " ".join(compose["services"]["frontend"]["command"])
    assert "http://backend:8000/api/health" in command
    assert 'require("/app/server.js")' in command


def test_fa98_proxy_does_not_wait_and_shows_a_setup_page(compose, caddyfile):
    assert "depends_on" not in compose["services"]["proxy"]
    assert "handle_errors 502 503 504" in caddyfile
    assert "GeoFACT wird eingerichtet" in caddyfile
    assert "logs -f osm-import" in caddyfile


def test_fa98_wait_ready_names_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(wait_ready, "osm_ready", lambda dsn: False)
    todo = wait_ready.missing("postgresql://x", tmp_path)
    assert len(todo) == 2
    assert "data-init" in todo[0] and "osm-import" in todo[1]
    (tmp_path / "DATENSTAND.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(wait_ready, "osm_ready", lambda dsn: True)
    assert wait_ready.missing("postgresql://x", tmp_path) == []


def test_fa98_wait_ready_treats_an_unreachable_database_as_not_ready(monkeypatch):
    def refuse(*args, **kwargs):
        raise wait_ready.psycopg.OperationalError("connection refused")

    monkeypatch.setattr(wait_ready.psycopg, "connect", refuse)
    assert wait_ready.osm_ready("postgresql://x") is False


def test_fa98_wait_ready_without_dsn_is_an_error(monkeypatch, capsys):
    monkeypatch.delenv("GEOFACT_PG_DSN", raising=False)
    assert wait_ready.main(["geofact-api"]) == 2
    assert "GEOFACT_PG_DSN" in capsys.readouterr().err


# --- (3) Schema des PostGIS-Imports, im search_path ----------------------------------


def test_fa98_style_defines_the_three_tables_of_the_core():
    style = STYLE.read_text(encoding="utf-8")
    from geofact.builtin.sources.osm import _TABLE_OF_KIND

    for table in _TABLE_OF_KIND.values():
        assert f"'{table}'" in style, table
    assert "define_node_table('osm_points'" in style  # node_id
    assert "define_way_table('osm_lines'" in style  # way_id
    assert "id_column = 'osm_id', type_column = 'osm_type'" in style
    assert style.count("type = 'jsonb'") == 3
    for geom_type in ("'point'", "'linestring'", "'multipolygon'"):
        assert f"type = {geom_type}, projection = SRID" in style
    assert "local SRID = 4326" in style
    assert "{ column = 'area_m2', type = 'real' }" in style
    assert "spherical_area()" in style


def test_fa98_style_indexes_geometry_with_gist_and_tags_with_gin():
    style = STYLE.read_text(encoding="utf-8")
    assert "{ column = 'geom', method = 'gist'" in style
    assert "{ column = 'tags', method = 'gin' }" in style
    assert style.count("indexes = indexes()") == 3


def test_fa98_osm_schema_comes_first_in_the_search_path():
    script = IMPORT_SH.read_text(encoding="utf-8")
    assert "SET search_path = $LIVE, public" in script
    assert "LIVE=osm" in script


# --- (4) Import ins Staging-Schema, Tausch in einer Transaktion ---------------------------


def test_fa98_import_writes_to_a_staging_schema():
    script = IMPORT_SH.read_text(encoding="utf-8")
    assert "STAGE=osm_import" in script
    assert '--schema="$STAGE"' in script
    assert "--create --slim --drop --output=flex" in script


def test_fa98_swap_happens_in_one_transaction():
    script = IMPORT_SH.read_text(encoding="utf-8")
    swap = script[script.index("BEGIN;") : script.index("COMMIT;")]
    assert "ALTER SCHEMA $LIVE RENAME TO $OLD" in swap
    assert "ALTER SCHEMA $STAGE RENAME TO $LIVE" in swap
    # die Standmarke entsteht vor dem Tausch, also nur bei vollständigem Import
    assert script.index("CREATE TABLE $STAGE.geofact_import") < script.index("BEGIN;")


def test_fa98_failed_osm2pgsql_is_an_error_and_keeps_the_old_stand():
    script = IMPORT_SH.read_text(encoding="utf-8")
    assert "set -eu" in script
    assert (
        '|| die "osm2pgsql fehlgeschlagen - der bisherige Stand bleibt unverändert"'
        in script
    )


def test_fa98_only_one_import_at_a_time():
    script = IMPORT_SH.read_text(encoding="utf-8")
    assert "flock -n 9 || die" in script


# --- (5) MD5 und fortsetzbarer Download --------------------------------------------------


def test_fa98_download_checks_md5_and_resumes():
    script = IMPORT_SH.read_text(encoding="utf-8")
    assert 'MD5_URL="${GEOFACT_OSM_PBF_MD5_URL:-$URL.md5}"' in script
    assert "curl -fL --retry 5 --retry-delay 10 -C -" in script
    assert "MD5-Summe passt nicht" in script
    # Meldungen auf stderr, stdout von download() ist die Summe
    assert re.search(r"log\(\) \{ printf .* >&2; \}", script)


@pytest.mark.skipif(
    shutil.which("bash") is None, reason="keine Shell für die Syntaxprüfung"
)
def test_fa98_import_script_is_valid_shell():
    # über stdin und als Bytes: unter Windows ist "bash" oft die WSL-Shell, die keine
    # Windows-Pfade kennt, und der Textmodus machte aus LF ein CRLF
    result = subprocess.run(
        ["bash", "-n"], input=IMPORT_SH.read_bytes(), capture_output=True
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")


def test_fa98_container_scripts_are_checked_out_with_lf():
    attributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes and "*.lua text eol=lf" in attributes
    for path in (IMPORT_SH, STYLE):
        assert b"\r" not in path.read_bytes(), path


# --- (6) zweiter Start lädt nichts neu -----------------------------------------------------


def test_fa98_ensure_with_a_stand_does_not_import():
    script = IMPORT_SH.read_text(encoding="utf-8")
    assert 'if [ "$cmd" = ensure ] && has_stand; then' in script
    assert compose_command("osm-import") == ["ensure"]
    assert compose_command("data-init") == ["ensure"]


def _repo(image: str) -> str:
    """Image ohne Tag, auch wenn der Tag eine ${...}-Variable ist."""
    return re.sub(r":(\$\{GEOFACT_VERSION[^}]*\}|[\w.]+)$", "", image)


def compose_command(service: str):
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"][service][
        "command"
    ]


# --- (7) data-init: Image-Raster, Downloads, Prüfungen ------------------------------------


class FakeNet:
    """Serves bytes per URL and counts downloads."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.calls: list[str] = []

    def __call__(self, url: str, dest: Path) -> None:
        self.calls.append(url)
        dest.write_bytes(self.files[url])


def _zip(member: str, payload: bytes) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(member, payload)
    return buffer.getvalue()


@pytest.fixture
def setup(tmp_path):
    image = tmp_path / "image"
    target = tmp_path / "target"
    image.mkdir()
    (image / ".gitkeep").write_text("")
    (image / "dem_sachsen_glo90.tif").write_bytes(b"dem")
    (image / data_init.GHS_POP_NAME).write_bytes(b"cut")
    csv = b"bus_id\n1\n"
    items = [
        data_init.Remote(
            data_init.GHS_POP_NAME, "http://ghs.zip", member=data_init.GHS_POP_NAME
        ),
        data_init.Remote(
            "europa/buses.csv", "http://buses", md5=hashlib.md5(csv).hexdigest()
        ),
    ]
    net = FakeNet(
        {
            "http://ghs.zip": _zip(data_init.GHS_POP_NAME, b"full raster" * 10),
            "http://buses": csv,
        }
    )
    return image, target, items, net


def test_fa98_data_init_downloads_checks_and_records(setup):
    image, target, items, net = setup
    state = data_init.run("ensure", image, target, net, items)
    assert (target / data_init.GHS_POP_NAME).read_bytes() == b"full raster" * 10
    assert (target / "europa" / "buses.csv").is_file()
    assert (target / "dem_sachsen_glo90.tif").read_bytes() == b"dem"
    assert not (target / ".gitkeep").exists()
    recorded = json.loads((target / "DATENSTAND.json").read_text(encoding="utf-8"))
    assert recorded == state
    assert state["files"][data_init.GHS_POP_NAME]["quelle"] == "http://ghs.zip"
    assert state["files"]["dem_sachsen_glo90.tif"]["quelle"].startswith("Image ")


def test_fa98_data_init_second_ensure_downloads_nothing(setup):
    image, target, items, net = setup
    data_init.run("ensure", image, target, net, items)
    net.calls.clear()
    data_init.run("ensure", image, target, net, items)
    assert net.calls == []


def test_fa98_data_init_update_downloads_again(setup):
    image, target, items, net = setup
    data_init.run("ensure", image, target, net, items)
    net.calls.clear()
    data_init.run("update", image, target, net, items)
    assert sorted(net.calls) == ["http://buses", "http://ghs.zip"]


def test_fa98_image_rasters_are_refreshed_but_never_replace_the_full_raster(setup):
    image, target, items, net = setup
    data_init.run("ensure", image, target, net, items)
    (image / "dem_sachsen_glo90.tif").write_bytes(b"dem v2")
    data_init.run("ensure", image, target, net, items)
    assert (target / "dem_sachsen_glo90.tif").read_bytes() == b"dem v2"
    assert (target / data_init.GHS_POP_NAME).read_bytes() == b"full raster" * 10


def test_fa98_downloaded_files_keep_their_source_over_the_image_copy(setup):
    image, target, items, net = setup
    (image / "europa").mkdir()
    (image / "europa" / "buses.csv").write_bytes(b"bus_id\n1\n")
    state = data_init.run("ensure", image, target, net, items)
    assert state["files"]["europa/buses.csv"]["quelle"] == "http://buses"


def test_fa98_enabling_a_skipped_download_later_fetches_the_full_file(setup):
    image, target, items, net = setup
    data_init.run(
        "ensure", image, target, net, []
    )  # first with "none": the cut copy only
    assert (target / data_init.GHS_POP_NAME).read_bytes() == b"cut"
    state = data_init.run("ensure", image, target, net, items)
    assert (target / data_init.GHS_POP_NAME).read_bytes() == b"full raster" * 10
    assert state["files"][data_init.GHS_POP_NAME]["quelle"] == "http://ghs.zip"


def test_fa98_wrong_md5_is_an_error_and_leaves_no_file(setup):
    image, target, items, net = setup
    net.files["http://buses"] = b"tampered"
    with pytest.raises(
        data_init.DataInitError, match="MD5 von europa/buses.csv passt nicht"
    ):
        data_init.run("ensure", image, target, net, items)
    assert not (target / "europa" / "buses.csv").exists()
    assert not (target / "europa" / "buses.csv.part").exists()
    assert not (target / "DATENSTAND.json").exists()


def test_fa98_broken_archive_is_an_error(setup):
    image, target, items, net = setup
    net.files["http://ghs.zip"] = b"not a zip"
    with pytest.raises(data_init.DataInitError, match="beschädigt"):
        data_init.run("ensure", image, target, net, items)
    assert not (target / data_init.GHS_POP_NAME).exists()


def test_fa98_missing_image_directory_is_an_error(tmp_path):
    with pytest.raises(data_init.DataInitError, match="Image-Daten fehlt"):
        data_init.run("ensure", tmp_path / "nope", tmp_path / "t", FakeNet({}), [])


def test_fa98_skipping_a_download_is_explicit_and_keeps_the_cut_copy(setup, capsys):
    image, target, _, net = setup
    items = data_init.remotes(ghs_pop_url="none", europa="none")
    assert items == []
    assert "GEOFACT_GHS_POP_URL=none" in capsys.readouterr().err
    data_init.run("ensure", image, target, net, items)
    assert (target / data_init.GHS_POP_NAME).read_bytes() == b"cut"


def test_fa98_europa_checksums_match_the_fetch_script():
    spec = importlib.util.spec_from_file_location(
        "fetch_europa_data", REPO_ROOT / "scripts" / "fetch_europa_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert data_init.EUROPA_FILES == module.FILES


# --- (8) Stand abfragen --------------------------------------------------------------------


def test_fa98_status_reports_the_stand(setup):
    image, target, items, net = setup
    assert data_init.run("status", image, target, net, items) == {"files": {}}
    data_init.run("ensure", image, target, net, items)
    net.calls.clear()
    assert (
        data_init.GHS_POP_NAME
        in data_init.run("status", image, target, net, items)["files"]
    )
    assert net.calls == []
    script = IMPORT_SH.read_text(encoding="utf-8")
    assert "ensure|update|status" in script
    for field in ("source_url", "osm_data_timestamp", "imported_at", "pbf_md5"):
        assert field in script


# --- (9) Frontend unter derselben Adresse ---------------------------------------------------


def test_fa98_frontend_image_of_the_package_calls_the_api_on_the_same_host(compose):
    assert _repo(compose["services"]["frontend"]["image"]).endswith("-frontend-proxy")
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = {s.get("id"): s for s in workflow["jobs"]["build"]["steps"] if s.get("id")}
    assert steps["push_frontend_proxy"]["with"]["build-args"] == "NEXT_PUBLIC_API_BASE="
    api = (REPO_ROOT / "frontend" / "lib" / "api.ts").read_text(encoding="utf-8")
    # "??" behält den leeren String (relative Pfade), "||" würde ihn ersetzen
    assert 'process.env.NEXT_PUBLIC_API_BASE ?? "' in api


def test_fa98_workflow_publishes_all_package_images(compose):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "for part in backend frontend osm frontend_proxy" in workflow
    assert "file: Dockerfile.osm" in workflow
    assert "docker-compose.server.yml .env.server.example" in workflow
    images = {_repo(svc["image"]) for svc in compose["services"].values()}
    # Präfix nicht fest: der Abgabe-Zweig nennt die Images ghcr.io/erichsdb/geofact-project
    suffixes = {re.sub(r"^\$\{GEOFACT_IMAGE:-[^}]+\}", "", i) for i in images}
    assert suffixes == {"-osm", "-backend", "-frontend-proxy", "caddy"}


# --- (10) Ereignisströme ungepuffert --------------------------------------------------------


def test_fa98_proxy_routes_api_unbuffered_and_the_rest_to_the_frontend(caddyfile):
    api = caddyfile[caddyfile.index("handle /api/*") : caddyfile.index("handle {")]
    assert "reverse_proxy backend:8000" in api
    assert "flush_interval -1" in api
    assert "encode" not in api
    assert "reverse_proxy frontend:3000" in caddyfile[caddyfile.index("handle {") :]
    # "$$" ist das Compose-Escape für "$": Caddy liest {$GEOFACT_SITE_ADDRESS}
    assert caddyfile.lstrip().startswith("{$$GEOFACT_SITE_ADDRESS}")


# --- (11) keine Secrets ----------------------------------------------------------------------


def test_fa98_secrets_come_from_the_environment(compose):
    env = compose["services"]["backend"]["environment"]
    for key in ("GEOFACT_WEB_USERS", "GEOFACT_WEB_SECRET_KEY", "OPENAI_API_KEY"):
        assert env[key].startswith("${") and env[key].endswith(":-}"), key
    assert "${GEOFACT_PG_PASSWORD:-geofact}" in env["GEOFACT_PG_DSN"]
    text = COMPOSE.read_text(encoding="utf-8") + ENV_EXAMPLE.read_text(encoding="utf-8")
    assert not re.search(r"sk-or-[A-Za-z0-9]{8,}", text)
    assert "C:\\Users" not in text and "/home/" not in text


def test_fa98_backend_image_ships_the_package_scripts():
    dockerfile = (REPO_ROOT / "Dockerfile.backend").read_text(encoding="utf-8")
    assert "COPY docker/data_init.py docker/wait_ready.py ./docker/" in dockerfile
    assert "COPY scripts/add_web_user.py ./scripts/add_web_user.py" in dockerfile
    osm = (REPO_ROOT / "Dockerfile.osm").read_text(encoding="utf-8")
    assert "COPY docker/osm/pipeline.lua /styles/pipeline.lua" in osm
    assert "postgresql-17-postgis-3" in osm and "osm2pgsql" in osm
