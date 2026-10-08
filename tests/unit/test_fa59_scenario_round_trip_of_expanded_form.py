"""Implements: FA59 (Nachbedingung 4: ``model_dump`` ist die ausgedehnte Form und laedt verlustfrei wieder, FA2).

A scenario with parameters, a module instance (FA75), a selector and when is loaded through the
Scenario hook; its dump contains no expansion keys, reloads to the same dump,
and the original text stays in ``source_text``. Offline.
"""

from __future__ import annotations

import yaml

from geofact import api

EXPANSION_KEYS = {
    "parameters",
    "modules",
    "instances",
    "foreach",
    "when",
    "with",
    "key",
    "exports",
}

TEXT = """
parameters:
  cities: {type: list, default: [leipzig, dresden]}
  radius: {type: float, default: 1.5}
  with_map: {type: boolean, default: false}
modules:
  stadt:
    layers:
      - id: punkte
        source: points
        features: [{name: a, lon: 12.9, lat: 50.82}]
    steps:
      - id: puffer
        op: buffer
        inputs: {geometry: punkte}
        params: {radius_km: "${radius}"}
    exports: [puffer]
scenario:
  name: Rundreise
  region: "12.85,50.78,12.98,50.87"
instances:
  - {id: s, module: stadt, foreach: {var: c, in: "${cities}"}}
steps:
  - {id: alle, op: collect, inputs: "s[*].puffer"}
output:
  - {type: geojson, source: alle}
  - {type: map, source: alle, when: "${with_map}"}
"""


def _keys(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            found |= _keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= _keys(item)
    return found


def test_fa59_scenario_round_trip_of_expanded_form():
    document = api.load_scenario(TEXT)
    dump = document.scenario.model_dump(mode="json")
    assert not (_keys(dump) & EXPANSION_KEYS)
    assert [step["id"] for step in dump["steps"]] == [
        "s[leipzig].puffer",
        "s[dresden].puffer",
        "alle",
    ]
    assert len(dump["output"]) == 1
    reloaded = api.load_scenario(
        yaml.safe_dump(dump, allow_unicode=True, sort_keys=False)
    )
    assert reloaded.scenario.model_dump(mode="json") == dump
    assert document.source_text == TEXT
