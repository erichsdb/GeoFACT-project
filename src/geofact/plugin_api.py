"""Implements: FA45 (public facade for building-block authors), FA40, FA55/FA58/FA65/FA59/FA73/FA74/FA75 (type re-exports).

The one surface a building block needs - shipped blocks (``geofact.builtin``)
and external plugins import exactly the same names from here:

- data types      DataType, LayerData, RasterLayer, Graph
- contracts       LayerBase, LayerValidation, Step, ParamsBase, LoadContext,
                  License (FA65)
- provenance      NodeAttribution (FA65), LayerProvenance (FA58),
                  crs_label, CRS_PRESETS (FA55), OutputLicense (FA73),
                  RunLineage, LineageNode (FA74; output formats read them from
                  ``spec.output_license`` / ``spec.lineage``)
- expansion       ParameterSpec, ExpansionReport, NodeOrigin, InstanceInfo
                  (FA59, FA62, FA75; read only, blocks need not know about
                  parameters or modules, NFA4)
- markers         register_source, register_file_format, register_table_format,
                  register_transform, register_operation, register_output
- errors          OutputSkipped, PluginError
- infrastructure  ``http`` (retry/backoff/size limit) and ``snapshot`` (FA7
                  snapshot store) as modules, for connectors that fetch

A block is one module with at most one config model and one function; the
decorator only marks the function, ``geofact.engine.discovery`` collects it.
This module imports only ``geofact.core`` and ``geofact.support`` (checked by
``tests/unit/test_architecture_layers.py``), so a plugin never reaches into
the engine."""

from __future__ import annotations

from geofact.core.contracts import (
    LayerBase,
    LayerValidation,
    License,
    LoadContext,
    ParamsBase,
    Step,
)
from geofact.core.crs import CRS_PRESETS, crs_label
from geofact.core.expansion import (
    ExpansionReport,
    InstanceInfo,
    NodeOrigin,
    ParameterSpec,
)
from geofact.core.errors import OutputSkipped, PluginError
from geofact.core.provenance import (
    LineageNode,
    NodeAttribution,
    OutputLicense,
    RunLineage,
)
from geofact.core.registry import (
    register_file_format,
    register_operation,
    register_output,
    register_source,
    register_table_format,
    register_transform,
)
from geofact.core.types import DataType, Graph, LayerData, LayerProvenance, RasterLayer
from geofact.support import http, snapshot

__all__ = [
    "CRS_PRESETS",
    "DataType",
    "ExpansionReport",
    "Graph",
    "InstanceInfo",
    "LayerBase",
    "LayerData",
    "LayerProvenance",
    "LayerValidation",
    "License",
    "LineageNode",
    "LoadContext",
    "NodeAttribution",
    "NodeOrigin",
    "OutputLicense",
    "OutputSkipped",
    "ParameterSpec",
    "ParamsBase",
    "PluginError",
    "RasterLayer",
    "RunLineage",
    "Step",
    "crs_label",
    "http",
    "register_file_format",
    "register_operation",
    "register_output",
    "register_source",
    "register_table_format",
    "register_transform",
    "snapshot",
]
