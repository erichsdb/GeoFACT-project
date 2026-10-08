"""Implements: FA58 (Web-Teil: CRS-Herkunft je Layer im Laufstatus), FA55/FA56 (Web-Teil: Region, Arbeits-CRS, Regionswarnungen), FA44 (Vorab-Check im Web).

Contract-Tests der Web-Schicht: ``RunStatus.layer_provenance`` spiegelt
``layer_loaded.detail.provenance``, ``RunStatus.region`` das Ereignis
``region_ready``; ``region_warning`` landet in ``RunStatus.warnings`` (Lauf-
Ebene), der Schweregrad einer Warnung (``detail.severity``) in
``warning_details``; ein fehlender Dateipfad scheitert vor jedem Ereignis als
Konfigurationsfehler mit Position.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from geofact import api  # noqa: E402
from geofact.api import events as ev  # noqa: E402
from geofact_web.run_manager import RunManager  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
BBOX_REGION = "13.60,51.01,13.94,51.15"


def _document(path: Path | None = None) -> api.ScenarioDocument:
    return api.load_scenario(
        {
            "scenario": {"name": "provenance", "region": BBOX_REGION},
            "layers": [
                {
                    "id": "substations",
                    "source": "file",
                    "data_type": "vector",
                    "path": str(path or FIXTURES_DIR / "substations.geojson"),
                },
            ],
            "steps": [
                {
                    "id": "catchment",
                    "op": "buffer",
                    "inputs": {"geometry": "substations"},
                    "params": {"radius_km": 1},
                },
            ],
            "output": [
                {"type": "geojson", "source": "catchment", "path": "catchment.geojson"}
            ],
        }
    )


def _run_sync(document: api.ScenarioDocument):
    manager = RunManager(max_retained_runs=5)
    state = manager.create(document, refresh_snapshots=False)
    manager._run(state)
    return state


def test_fa58_web_status_exposes_layer_provenance_and_region():
    state = _run_sync(_document())
    assert state.status == "done", state.error
    status = state.to_status()

    provenance = status.layer_provenance["substations"]
    assert provenance.source == "file"
    assert provenance.source_crs == "EPSG:4326"
    assert provenance.target_crs == "EPSG:32633"
    assert provenance.feature_count and provenance.feature_count > 0
    assert provenance.raw_bounds and 13.0 < provenance.raw_bounds[0] < 14.5
    assert provenance.bounds and provenance.bounds[0] > 1000  # Meter im UTM-Arbeits-CRS

    assert status.region is not None
    assert status.region.crs == "EPSG:32633"
    assert status.region.crs_mode == "auto"
    assert status.region.bbox == pytest.approx([13.60, 51.01, 13.94, 51.15])
    # nur benötigte Layer haben Provenienz (Lazy Loading)
    assert set(status.layer_provenance) == {"substations"}


def test_fa56_web_region_warning_lands_on_run_level():
    state = _run_sync(_document())
    RunManager._apply(
        state,
        ev.ProgressEvent(
            type=ev.REGION_WARNING,
            message="Region breiter als eine UTM-Zone",
            detail={"category": "RegionWarning", "severity": "warning"},
        ),
    )
    warning = state.to_status().warnings[-1]
    assert warning.message == "Region breiter als eine UTM-Zone"
    assert warning.category == "RegionWarning"
    assert warning.severity == "warning"
    assert warning.step is None


def test_fa56_web_region_ready_event_sets_region():
    state = _run_sync(_document())
    state.region = None
    RunManager._apply(
        state,
        ev.ProgressEvent(
            type=ev.REGION_READY,
            detail={
                "crs": "EPSG:3035",
                "crs_label": "EPSG:3035 (ETRS89-extended / LAEA Europe)",
                "crs_mode": "declared",
                "crs_spec": "equal_area",
                "bbox": [5.0, 47.0, 15.0, 55.0],
                "area_km2": 1.0,
                "display_name": None,
                "osm_class": None,
                "osm_type": None,
                "warnings": [],
            },
        ),
    )
    assert state.to_status().region.crs_mode == "declared"


def test_fa58_web_warning_severity_is_kept_next_to_the_message():
    state = _run_sync(_document())
    RunManager._apply(
        state,
        ev.ProgressEvent(
            type=ev.STEP_WARNING,
            step="catchment",
            message="nur ein Hinweis",
            detail={"category": "SnapshotNotice", "severity": "info"},
        ),
    )
    step = state.to_status().steps["catchment"]
    assert step.warnings[-1] == "nur ein Hinweis"
    assert step.warning_details[-1].severity == "info"
    assert step.warning_details[-1].category == "SnapshotNotice"


def test_fa44_web_missing_file_fails_before_any_event_with_position(tmp_path):
    state = _run_sync(_document(tmp_path / "gibt_es_nicht.geojson"))
    assert state.status == "error"
    assert "layers -> 0 (substations) -> path" in (state.error or "")
    # Vorab-Check: weder Plan noch Region noch ein Ladevorgang hat stattgefunden.
    types = [event["type"] for event in state.events]
    assert types == [ev.RUN_ERROR]
    assert state.to_status().region is None
    assert state.to_status().steps["catchment"].status == "pending"
