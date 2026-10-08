"""Application layer of GeoFACT (FA45): discovery of extensions (``discovery``),
the fixed kernel services region resolution / UTM zone (``region``) and FA6
validation (``validate``), loading one layer (``loading``: connector ->
transform -> FA6), the executor (``executor``, driven by the plan in
``geofact.core.plan``), writing of declared outputs (``outputs``), progress
events, cancellation and warning routing (``events``) and the catalog
introspection (``catalog``).

Knows ``geofact.core`` and ``geofact.support``; reaches the shipped blocks only
through the registry that ``discovery`` builds from the package *name*
``geofact.builtin`` - it never imports a block (checked by
``tests/unit/test_architecture_layers.py``)."""
