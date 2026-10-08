"""Implements: FA2 (Zusatz: eindeutige Dateinamen der Ausgaben; Layer-Referenzen in Ausgabeoptionen).

Contract-Tests für ``OutputSpec.target_name`` und die Prüfung in
``Scenario.validate_dag``: zwei Ausgaben mit demselben Dateinamen sind ein
Fehler mit Position (die zweite überschriebe die erste still), die
Default-Namen (``<index>_<source>.<ext>``) kollidieren nie."""

from __future__ import annotations

import pytest

from geofact import api
from geofact.core.errors import ConfigError
from geofact.core.registry import Registry

LAYERS = """
scenario:
  name: Namen
  region: "12.2,51.2,12.5,51.4"
layers:
  - id: a
    source: file
    path: data/a.geojson
    data_type: vector
steps:
  - id: p
    op: buffer
    inputs: {geometry: a}
    params: {radius_km: 1}
output:
"""


def _load(outputs: str, registry: Registry | None = None):
    return api.load_scenario(LAYERS + outputs, registry=registry)


def test_fa02_same_basename_is_rejected_with_position():
    with pytest.raises(ConfigError) as exc:
        _load("""
  - {type: geojson, source: p, path: ergebnis/o.geojson}
  - {type: csv, source: p}
  - {type: geojson, source: a, path: o.geojson}
""")
    assert exc.value.issues == [
        (
            "output -> 2 -> path",
            "Dateiname 'o.geojson' wird auch von output -> 0 geschrieben - path eindeutig wählen",
        )
    ]


def test_fa02_names_differing_only_in_case_collide():
    with pytest.raises(
        ConfigError, match="Dateiname 'O.geojson' wird auch von output -> 0"
    ):
        _load("""
  - {type: geojson, source: p, path: o.geojson}
  - {type: geojson, source: a, path: O.geojson}
""")


def test_fa02_default_names_never_collide():
    doc = _load("""
  - {type: geojson, source: p}
  - {type: geojson, source: p}
  - {type: csv, source: p}
""")
    names = [out.target_name(i, "geojson") for i, out in enumerate(doc.scenario.output)]
    assert names == ["00_p.geojson", "01_p.geojson", "02_p.geojson"]


def test_fa02_path_colliding_with_a_default_name_is_rejected():
    with pytest.raises(
        ConfigError, match="Dateiname '00_p.geojson' wird auch von output -> 0"
    ):
        _load("""
  - {type: geojson, source: p}
  - {type: geojson, source: a, path: 00_p.geojson}
""")


def test_fa02_target_name_uses_path_basename():
    doc = _load("  - {type: geojson, source: p, path: sub/dir/karte.geojson}\n")
    assert doc.scenario.output[0].target_name(0, "geojson") == "karte.geojson"


def test_fa02_output_spec_binds_provenance_and_attribution():
    from geofact.core.contracts import License
    from geofact.core.provenance import NodeAttribution

    spec = _load("  - {type: geojson, source: p}\n").scenario.output[0]
    assert spec.provenance is None and spec.attribution is None
    attribution = NodeAttribution(licenses=(License(name="CC0-1.0"),))
    spec.bind_attribution(attribution)
    spec.bind_provenance({})
    assert spec.attribution is attribution
    assert spec.provenance == {}
    assert (
        "provenance" not in spec.model_dump() and "attribution" not in spec.model_dump()
    )


# ------------------------------------------------- referenced_ids (Duck-Typing)

PLOT_PLUGIN = """
from typing import Optional

from pydantic import BaseModel, ConfigDict

from geofact.plugin_api import register_output


class PlotOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layers: Optional[list[str]] = None

    def referenced_ids(self) -> list[str]:
        return list(self.layers or [])


@register_output("probe_plot", extension="html", options_model=PlotOptions)
def write_plot(data, target, spec):
    target.write_text("plot", encoding="utf-8")
"""


@pytest.fixture
def plot_registry(tmp_path) -> Registry:
    directory = tmp_path / "plugins"
    directory.mkdir()
    (directory / "probe_plot.py").write_text(PLOT_PLUGIN, encoding="utf-8")
    return api.discover(plugin_paths=[directory])


def test_fa02_output_option_references_unknown_layer(plot_registry):
    registry = plot_registry
    with pytest.raises(ConfigError) as exc:
        _load(
            "  - {type: probe_plot, source: p, layers: [a, gibtsnicht]}\n",
            registry=registry,
        )
    assert exc.value.issues == [
        ("output -> 0 -> layers -> 1", "unbekannter Layer 'gibtsnicht'")
    ]
    assert _load("  - {type: probe_plot, source: p, layers: [a]}\n", registry=registry)


@pytest.mark.parametrize("path", [".", "..", "ergebnis/.."])
def test_fa02_output_path_without_a_file_name_is_rejected(path):
    """Nachtreview 02.10. (Runde 2): ``path: '.'`` oder ``'..'`` nennt ein
    Verzeichnis; vorher bestand die Validierung und der Lauf brach mit einem
    rohen PermissionError ab."""
    with pytest.raises(ConfigError) as exc:
        _load(f"  - {{type: geojson, source: p, path: '{path}'}}\n")
    assert [pos for pos, _ in exc.value.issues] == ["output -> 0 -> path"]
    assert "nennt keinen Dateinamen" in exc.value.issues[0][1]


def test_fa02_output_option_referencing_a_never_loaded_layer_is_rejected(plot_registry):
    """Nachtreview 02.10. (Runde 2): ein in ``layers`` genannter Layer, den
    kein Schritt und keine Ausgabe liest, wird nie geladen (Lazy Loading);
    vorher bestand die Validierung, und der Lauf übersprang die Ausgabe."""
    config = LAYERS.replace(
        "steps:",
        """  - id: extra
    source: file
    path: data/extra.geojson
    data_type: vector
steps:""",
    )
    with pytest.raises(ConfigError) as exc:
        api.load_scenario(
            config + "  - {type: probe_plot, source: p, layers: [a, extra]}\n",
            registry=plot_registry,
        )
    [(location, message)] = exc.value.issues
    assert location == "output -> 0 -> layers -> 1"
    assert "Layer 'extra'" in message and "nicht geladen" in message


@pytest.mark.parametrize(
    "node_id", ["a/b", "a\b", "..", ".", "dgm#1", "pop?2", "ndvi%a", " "]
)
def test_fa02_layer_and_step_ids_that_break_paths_or_urls_are_rejected(node_id):
    """Nachtreview 02.10. (Runde 2): literale ids wurden nicht geprüft (nur
    erzeugte); '/' oder '..' führten zu Unterverzeichnissen bzw. einem
    anderen Web-Endpunkt, '#'/'?' schnitten URLs ab."""
    for old in ("id: a\n", "id: p\n"):
        config = LAYERS.replace(old, f"id: {node_id!r}\n").replace(
            "geometry: a}",
            "geometry: " + (repr(node_id) if old == "id: a\n" else "a") + "}",
        )
        with pytest.raises(ConfigError) as exc:
            api.load_scenario(config + "  - {type: geojson, source: a}\n")
        section = "layers" if old == "id: a\n" else "steps"
        assert f"{section} -> 0 -> id" in [pos for pos, _ in exc.value.issues]


def test_fa02_ids_with_spaces_and_umlauts_stay_valid():
    config = LAYERS.replace("id: p\n", "id: 'Puffer Grünfläche'\n")
    assert api.load_scenario(
        config + "  - {type: geojson, source: 'Puffer Grünfläche'}\n"
    )
