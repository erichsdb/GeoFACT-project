"""Implements: FA40, FA45 (shipped building blocks).

Every non-underscore module below this package is an extension module: it
declares its contract (layer model, step class, options) and its function and
marks the function with a ``register_*`` decorator from ``geofact.plugin_api``.
``geofact.engine.discovery`` scans exactly this package by name; modules with a
leading underscore (``_tabular.py``, ``_rest_client.py``, ``_geometry.py``) are
helpers and are never scanned. Shipped blocks import only ``geofact.core``,
``geofact.support`` and ``geofact.plugin_api`` - never ``geofact.engine``."""
