"""Implements: FA2 (Datentypen im Pipeline-Graphen), FA40 (Fehlertypen des Kerns).

Contract-Tests für geofact/core: DataType, RasterLayer, Graph, LayerStore,
data_type_of() und die gemeinsamen Fehlertypen. geofact.core bleibt rein: es
importiert weder networkx noch rasterio zur Laufzeit (FA45).
"""

from __future__ import annotations

import subprocess
import sys

import geopandas as gpd
import numpy as np
import pytest
from affine import Affine
from pyproj import CRS
from shapely.geometry import Point

from geofact.core.errors import (
    ConfigError,
    LayerLoadError,
    OutputSkipped,
    PluginError,
    RunCancelled,
    StepExecutionError,
)
from geofact.core.store import LayerStore
from geofact.core.types import DataType, Graph, LayerData, RasterLayer, data_type_of


def _raster() -> RasterLayer:
    return RasterLayer(
        data=np.zeros((2, 3)), transform=Affine.identity(), crs=CRS.from_epsg(32633)
    )


def test_fa02_data_type_of_maps_the_three_runtime_types():
    frame = gpd.GeoDataFrame({"a": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    assert data_type_of(frame) is DataType.VECTOR
    assert data_type_of(_raster()) is DataType.RASTER
    assert data_type_of(Graph(nx_graph=object(), crs=None)) is DataType.GRAPH


def test_fa02_data_type_of_unknown_object_raises_type_error():
    """Vorher galt jedes unbekannte Objekt stillschweigend als Graph; eine
    falsche Operation fiele erst beim Schreiben der Ausgabe auf."""
    with pytest.raises(TypeError, match="Unbekannter Datentyp builtins.str"):
        data_type_of("kein Layer")
    with pytest.raises(TypeError, match="Unbekannter Datentyp"):
        data_type_of(None)


def test_fa02_data_type_of_plain_dataframe_is_unknown():
    import pandas as pd

    with pytest.raises(TypeError):
        data_type_of(pd.DataFrame({"a": [1]}))


def test_fa02_raster_layer_shape_uses_the_last_two_axes():
    layer = RasterLayer(
        data=np.zeros((4, 5, 6)), transform=Affine.identity(), crs=CRS.from_epsg(4326)
    )
    assert layer.shape == (5, 6)
    assert _raster().shape == (2, 3)


def test_fa09_layer_store_roundtrip_and_explicit_missing_error():
    store = LayerStore()
    raster = _raster()
    store["r"] = raster
    assert "r" in store and len(store) == 1
    assert store["r"] is raster
    assert store.get("fehlt") is None
    with pytest.raises(KeyError, match="Layer 'fehlt' ist nicht geladen"):
        store["fehlt"]


def test_fa02_layer_data_covers_all_three_types():
    assert {t.__name__ for t in LayerData.__args__} == {
        "GeoDataFrame",
        "RasterLayer",
        "Graph",
    }


def test_fa40_config_error_lists_position_and_message():
    error = ConfigError([("steps -> 3 -> params -> radius_km", "muss > 0 sein")])
    assert error.issues == [("steps -> 3 -> params -> radius_km", "muss > 0 sein")]
    assert "[steps -> 3 -> params -> radius_km] muss > 0 sein" in str(error)


def test_fa40_step_execution_error_names_step_operation_origin_and_cause():
    cause = ValueError("kaputt")
    error = StepExecutionError(
        "puffer", "buffer", cause, origin="geofact.builtin.operations.buffer"
    )
    assert error.step_id == "puffer" and error.op == "buffer" and error.cause is cause
    assert (
        "Schritt 'puffer' (buffer, geofact.builtin.operations.buffer): ValueError: kaputt"
        in str(error)
    )


def test_fa40_layer_load_error_names_layer_and_source():
    error = LayerLoadError("hospitals", "osm", TimeoutError("503"))
    assert "Layer 'hospitals' (source: osm): TimeoutError: 503" in str(error)


def test_fa40_core_error_types():
    assert issubclass(PluginError, RuntimeError)
    assert issubclass(OutputSkipped, Exception)
    assert issubclass(RunCancelled, Exception)


def test_fa45_core_types_do_not_import_networkx_or_rasterio():
    script = (
        "import sys\n"
        "import geofact.core.types, geofact.core.store, geofact.core.errors\n"
        "assert 'networkx' not in sys.modules\n"
        "assert 'rasterio' not in sys.modules\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert result.returncode == 0 and "ok" in result.stdout, result.stderr
