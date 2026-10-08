"""Implements: FA58 (CRS provenance comparison plot, output ``crs_plot``).

Contract tests of builtin/outputs/crs_plot.py: one SVG group per layer with its
CRS labels, a raw panel on one shared linear axis with the origin (degrees and
metres side by side, extents and distances annotated, labels never hidden), an
inset for every layer that collapses to a dot, the harmonised panel draws the
real geometries (polygons, lines, points) on top of each other, the legend names
source CRS -> working CRS for all layers; the output is deterministic with a
fixed size, ``format: svg`` writes a standalone SVG; it is skipped with a reason
when the run has no provenance or a named layer was not loaded (never eager
loading), a layer without geometry still renders; ``layers`` is checked at
validation (referenced_ids)."""

from __future__ import annotations

import re

import geopandas as gpd
import pytest
from shapely.geometry import Point

from geofact import api
from geofact.core.errors import ConfigError, OutputSkipped
from geofact.core.types import LayerProvenance

HEAD = """
scenario:
  name: CRS-Vergleich
  region: "13.0,50.9,13.1,51.0"
layers:
  - {id: wgs, source: file, path: data/a.geojson, data_type: vector}
  - {id: gk, source: file, path: data/b.geojson, data_type: vector}
  - {id: tab, source: file, path: data/c.geojson, data_type: vector}
  - {id: utm, source: file, path: data/d.geojson, data_type: vector}
steps:
  - {id: b, op: buffer, inputs: {geometry: wgs}, params: {radius_km: 1}}
output:
"""

SQUARE = (
    (359000.0, 5640000.0),
    (366000.0, 5640000.0),
    (366000.0, 5651000.0),
    (359000.0, 5651000.0),
    (359000.0, 5640000.0),
)
RAW_SQUARE = (
    (4500000.0, 5640000.0),
    (4507000.0, 5640000.0),
    (4507000.0, 5651000.0),
    (4500000.0, 5651000.0),
    (4500000.0, 5640000.0),
)
TARGET = "EPSG:32633 (WGS 84 / UTM zone 33N)"

WGS = LayerProvenance(
    layer="wgs",
    source="file",
    source_crs="EPSG:4326",
    source_crs_label="EPSG:4326 (WGS 84)",
    crs_override=False,
    target_crs="EPSG:32633",
    target_crs_label=TARGET,
    raw_bounds=(13.0, 50.9, 13.1, 51.0),
    bounds=(359000.0, 5640000.0, 366000.0, 5651000.0),
    feature_count=2,
    raw_sample=[(13.0, 50.9), (13.1, 51.0)],
    sample=[(359000.0, 5640000.0), (366000.0, 5651000.0)],
    raw_shapes=(("line", (((13.0, 50.9), (13.1, 51.0)),)),),
    shapes=(("line", (((359000.0, 5640000.0), (366000.0, 5651000.0)),)),),
)
GK = LayerProvenance(
    layer="gk",
    source="file",
    source_crs="EPSG:31468",
    source_crs_label="EPSG:31468 (DHDN / 3-degree Gauss-Kruger zone 4)",
    crs_override=True,
    target_crs="EPSG:32633",
    target_crs_label=TARGET,
    raw_bounds=(4500000.0, 5640000.0, 4507000.0, 5651000.0),
    bounds=(359000.0, 5640000.0, 366000.0, 5651000.0),
    feature_count=1,
    raw_sample=[(4503000.0, 5645000.0)],
    sample=[(362000.0, 5645500.0)],
    raw_shapes=(("polygon", (RAW_SQUARE,)),),
    shapes=(("polygon", (SQUARE,)),),
)
UTM = LayerProvenance(
    layer="utm",
    source="file",
    source_crs="EPSG:25833",
    source_crs_label="EPSG:25833 (ETRS89 / UTM zone 33N)",
    crs_override=False,
    target_crs="EPSG:32633",
    target_crs_label=TARGET,
    raw_bounds=(359100.0, 5640100.0, 365900.0, 5650900.0),
    bounds=(359100.0, 5640100.0, 365900.0, 5650900.0),
    feature_count=2,
    raw_sample=[(359100.0, 5640100.0), (365900.0, 5650900.0)],
    sample=[(359100.0, 5640100.0), (365900.0, 5650900.0)],
    raw_shapes=(
        ("point", (((359100.0, 5640100.0),),)),
        ("point", (((365900.0, 5650900.0),),)),
    ),
    shapes=(
        ("point", (((359100.0, 5640100.0),),)),
        ("point", (((365900.0, 5650900.0),),)),
    ),
)
TAB = LayerProvenance(
    layer="tab",
    source="file",
    source_crs=None,
    source_crs_label="ohne CRS",
    crs_override=False,
    target_crs="EPSG:32633",
    target_crs_label=TARGET,
    raw_bounds=None,
    bounds=None,
    feature_count=0,
    raw_sample=[],
    sample=[],
)


# Weitere Ausgaben, die gk, tab und utm lesen: ein Layer, den crs_plot nennt, muss
# im Lauf geladen werden (Validierung, Nachtreview 02.10. Runde 2).
READERS = """  - {type: geojson, source: gk, path: gk.geojson}
  - {type: geojson, source: tab, path: tab.geojson}
  - {type: geojson, source: utm, path: utm.geojson}
"""


def _spec(output: str):
    return api.load_scenario(HEAD + output + READERS).scenario.output[0]


def _data() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"n": [1]}, geometry=[Point(360000, 5641000)], crs="EPSG:32633"
    )


def _render(spec, tmp_path, name: str = "plot.html") -> str:
    target = tmp_path / name
    api.registry().output("crs_plot").write(_data(), target, spec)
    return target.read_text(encoding="utf-8")


def _panel(html: str, name: str) -> str:
    match = re.search(rf'<svg[^>]*data-panel="{name}".*?</svg>', html, re.S)
    assert match, f"panel {name} missing"
    return match.group(0)


def test_fa58_crs_plot_draws_one_group_per_layer_with_crs_labels(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs}\n")
    spec.bind_provenance({"wgs": WGS, "gk": GK})
    html = _render(spec, tmp_path)
    for panel in ("raw", "harmonized"):
        svg = _panel(html, panel)
        assert svg.count('class="layer"') == 2
        assert 'data-layer="wgs"' in svg and 'data-layer="gk"' in svg
    assert f"EPSG:4326 (WGS 84) → {TARGET}" in html
    assert "EPSG:31468" in html and "erzwungen" in html  # crs_override marked


def test_fa58_crs_plot_raw_panel_shares_one_axis_for_degrees_and_metres(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs, title: Drei CRS}\n")
    spec.bind_provenance({"wgs": WGS, "gk": GK})
    html = _render(spec, tmp_path)
    raw = _panel(html, "raw")
    # linear axis from the origin, ticks as grouped plain numbers (no log scale)
    ticks = sorted(
        {
            float(t.replace(" ", ""))
            for t in re.findall(r'class="tick"[^>]*>([^<]+)<', raw)
        }
    )
    assert 0.0 in ticks and max(ticks) >= 4_000_000
    # every layer's raw extent is annotated in its own unit, plus the distance
    assert "x 13.000 … 13.100" in raw and "x 4 500 000 … 4 507 000" in raw
    assert re.search(r"Abstand wgs–gk ≈ [0-9 ]+<", raw)
    assert "(0, 0)" in raw
    assert "Drei CRS" in html


def test_fa58_crs_plot_raw_axis_ticks_are_equidistant(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs}\n")
    spec.bind_provenance({"wgs": WGS, "gk": GK})
    raw = _panel(_render(spec, tmp_path), "raw")
    xs = [
        float(t.replace(" ", ""))
        for t in re.findall(
            r'class="tick" x="[0-9.]+" y="[0-9.]+" font-size="10" text-anchor="middle"'
            r"[^>]*>([^<]+)<",
            raw,
        )
    ]
    steps = {round(b - a) for a, b in zip(xs, xs[1:])}
    assert len(xs) >= 3 and len(steps) == 1  # linear


def test_fa58_crs_plot_labels_are_never_hidden_even_at_the_same_spot(tmp_path):
    twin = LayerProvenance(**{**GK.__dict__, "layer": "tab"})  # same raw extent as gk
    spec = _spec("  - {type: crs_plot, source: wgs, layers: [wgs, gk, tab]}\n")
    spec.bind_provenance({"wgs": WGS, "gk": GK, "tab": twin})
    raw = _panel(_render(spec, tmp_path), "raw")
    labels = re.findall(
        r'<g class="label" data-layer="([^"]+)">(?:<rect[^>]*/>)?<text[^>]* y="([0-9.]+)"',
        raw,
    )
    assert sorted(layer for layer, _ in labels) == ["gk", "tab", "wgs"]
    ys = {layer: float(y) for layer, y in labels}
    assert abs(ys["gk"] - ys["tab"]) >= 14  # stacked, not on top of each other


def test_fa58_crs_plot_collapsed_layers_get_an_inset_with_their_raw_extent(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs}\n")
    spec.bind_provenance({"wgs": WGS, "gk": GK})
    html = _render(spec, tmp_path)
    insets = re.findall(r'<svg data-panel="inset" data-layer="([^"]+)"', html)
    assert insets == ["wgs", "gk"]  # both shrink to a dot on the shared axis
    wgs_inset = re.search(
        r'<svg data-panel="inset" data-layer="wgs".*?</svg>', html, re.S
    ).group(0)
    assert "Ausdehnung 0.100 × 0.100" in wgs_inset
    assert 'class="line"' in wgs_inset  # the raw geometry, in its own scale


def test_fa58_crs_plot_harmonized_panel_overlays_real_geometries(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs, layers: [utm, wgs, gk]}\n")
    spec.bind_provenance({"wgs": WGS, "gk": GK, "utm": UTM})
    harmonized = _panel(_render(spec, tmp_path), "harmonized")
    order = re.findall(r'<g class="layer" data-layer="([^"]+)"', harmonized)
    assert order == ["gk", "wgs", "utm"]  # polygons below lines below points
    assert harmonized.count('class="polygon"') == 1
    assert harmonized.count('class="line"') == 1
    assert harmonized.count('class="point"') == 2
    assert f"Harmonisiert: {TARGET}" in harmonized
    # congruent overlay: the polygon of gk and the line of wgs start at the same pixel
    poly = re.search(r'class="polygon" d="M([0-9.]+) ([0-9.]+)', harmonized).groups()
    line = re.search(r'class="line" d="M([0-9.]+) ([0-9.]+)', harmonized).groups()
    assert poly == line


def test_fa58_crs_plot_legend_names_three_source_crs_and_the_working_crs(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs, layers: [gk, utm, wgs]}\n")
    spec.bind_provenance({"wgs": WGS, "gk": GK, "utm": UTM})
    html = _render(spec, tmp_path)
    legend = re.search(r'<g class="legend".*?</g>', html, re.S).group(0)
    rows = re.findall(r'<text class="legend-row" data-layer="([^"]+)"', legend)
    assert rows == ["gk", "utm", "wgs"]
    for source in (
        "EPSG:31468 (DHDN / 3-degree Gauss-Kruger zone 4)",
        "EPSG:25833 (ETRS89 / UTM zone 33N)",
        "EPSG:4326 (WGS 84)",
    ):
        assert f"{source} → {TARGET}" in legend
    assert legend.count("erzwungen (crs_override)") == 1  # only gk


def test_fa58_crs_plot_output_is_deterministic_with_a_fixed_size(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs}\n")
    spec.bind_provenance({"wgs": WGS, "gk": GK})
    first = _render(spec, tmp_path, "a.html")
    second = _render(spec, tmp_path, "b.html")
    assert first == second
    assert re.search(
        r'<svg xmlns="http://www.w3.org/2000/svg" class="crs-plot" width="1200" '
        r'height="\d+"',
        first,
    )


def test_fa58_crs_plot_format_svg_writes_a_standalone_svg(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs, format: svg}\n")
    assert spec.options.extension == "svg"
    spec.bind_provenance({"wgs": WGS, "gk": GK})
    page = _render(spec, tmp_path, "plot.svg")
    assert page.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns=')
    assert "<html" not in page and 'data-panel="harmonized"' in page
    with pytest.raises(ConfigError, match="format"):
        _spec("  - {type: crs_plot, source: wgs, format: pdf}\n")


def test_fa58_crs_plot_with_only_empty_layers_says_so(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs, layers: [tab]}\n")
    spec.bind_provenance({"tab": TAB})
    html = _render(spec, tmp_path)
    for panel in ("raw", "harmonized"):
        svg = _panel(html, panel)
        assert "keine Koordinaten" in svg and 'data-layer="tab"' in svg
    assert "keine Geometrie" in html and 'data-panel="inset"' not in html


def test_fa58_crs_plot_without_provenance_is_skipped_with_reason(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs}\n")
    with pytest.raises(OutputSkipped, match="Provenienz"):
        _render(spec, tmp_path)
    assert not (tmp_path / "plot.html").exists()


def test_fa58_crs_plot_with_a_layer_not_loaded_is_skipped(tmp_path):
    spec = _spec("  - {type: crs_plot, source: wgs, layers: [wgs, gk]}\n")
    spec.bind_provenance({"wgs": WGS})
    with pytest.raises(OutputSkipped, match="'gk'.*nicht geladen"):
        _render(spec, tmp_path)


def test_fa58_crs_plot_layer_without_geometry_still_renders_raw_panel(tmp_path):
    spec = _spec(
        "  - {type: crs_plot, source: wgs, layers: [tab, wgs], max_points: 10}\n"
    )
    spec.bind_provenance({"wgs": WGS, "tab": TAB})
    html = _render(spec, tmp_path)
    raw = _panel(html, "raw")
    assert 'data-layer="tab"' in raw and "keine Geometrie" in html
    assert raw.count('class="pt"') == 2  # only the two raw points of wgs


def test_fa58_crs_plot_validates_named_layers_and_max_points():
    with pytest.raises(ConfigError) as exc:
        _spec("  - {type: crs_plot, source: wgs, layers: [wgs, gibtsnicht]}\n")
    assert exc.value.issues == [
        ("output -> 0 -> layers -> 1", "unbekannter Layer 'gibtsnicht'")
    ]
    with pytest.raises(ConfigError, match="max_points"):
        _spec("  - {type: crs_plot, source: wgs, max_points: 5}\n")


def test_fa58_crs_plot_thins_samples_to_max_points(tmp_path):
    many = [(13.0 + i * 0.001, 50.9) for i in range(50)]
    dense = LayerProvenance(**{**WGS.__dict__, "raw_sample": many, "sample": many})
    spec = _spec("  - {type: crs_plot, source: wgs, max_points: 10}\n")
    spec.bind_provenance({"wgs": dense})
    raw = _panel(_render(spec, tmp_path), "raw")
    assert raw.count('class="pt"') == 10


def test_fa58_crs_plot_is_in_the_catalog():
    names = {entry["name"] for entry in api.extension_catalog()["outputs"]}
    assert "crs_plot" in names
