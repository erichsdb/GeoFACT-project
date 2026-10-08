"""Implements: FA58 (Vergleichsplot der CRS-Provenienz, Ausgabeformat ``crs_plot``).

Zeichnet aus der CRS-Provenienz des Laufs (``spec.provenance``, je geladenem
Layer ein ``LayerProvenance``) eine SVG-Abbildung fester Größe; ``format: html``
(Default) bettet sie in eine HTML-Seite ein, ``format: svg`` schreibt sie
eigenständig. Panels (je Layer eine Gruppe ``<g class="layer" data-layer="id">``):

- ``raw``: alle Layer auf einer gemeinsamen linearen Achse, je in der Einheit des
  Quell-CRS (Grad und Meter gemischt - der Fehler ohne Harmonisierung), mit
  Etiketten und Abständen. Etiketten werden versetzt, nie weggelassen.
- ``inset``: Layer, die auf dieser Achse zu einem Punkt schrumpfen, in eigenem Maßstab.
- ``harmonized``: die vereinfachten Geometrien aller Layer im Arbeits-CRS.
- Legende: ``Quell-CRS → Arbeits-CRS``, ``crs_override`` als erzwungen markiert.

Optionen: ``layers`` (Default: alle geladenen), ``title``, ``max_points``, ``format``.
Ohne Lauf-Provenienz oder mit einem in diesem Lauf nicht geladenen Layer wird die
Ausgabe mit Grund übersprungen (``OutputSkipped``); der Plot lädt nie selbst
(Lazy Loading); ``source`` ist nur der Anker im DAG. Die Ausgabe ist deterministisch und ohne neue Abhängigkeit (reines SVG).
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from geofact.plugin_api import DataType, LayerProvenance, OutputSkipped, register_output

_PALETTE = [
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#ff7f0e",
    "#9467bd",
    "#17becf",
    "#e377c2",
    "#8c564b",
]
_FONT = "DejaVu Sans, Arial, Helvetica, sans-serif"
_WIDTH = 1200
_MARGIN = 20
_PANEL_W, _PANEL_H = 570, 480
_PANEL_TOP = 56
_INSET_H = 200
_LEGEND_ROW = 20
# Rand des Zeichenbereichs innerhalb eines Panels (links Platz für y-Marken)
_PL, _PR, _PT, _PB = 78, 14, 30, 44
_COLLAPSED_PX = 12.0  # kleiner als das (beide Seiten) = "zum Punkt geschrumpft"
_LABEL_GAP = 28.0  # Zeilenabstand gestapelter Etiketten (zwei Zeilen)

Bounds = tuple[float, float, float, float]
Shape = tuple[str, tuple[tuple[tuple[float, float], ...], ...]]


class CrsPlotOptions(BaseModel):
    """Formatspezifische Felder einer crs_plot-Ausgabe."""

    model_config = ConfigDict(extra="forbid")

    layers: Optional[list[str]] = Field(
        None,
        description="Layer-ids, die gezeichnet werden (Default: alle geladenen Layer)",
    )
    title: Optional[str] = Field(None, description="Überschrift der Abbildung")
    max_points: int = Field(
        200,
        ge=10,
        le=2000,
        description="Höchstzahl Stichprobenpunkte je Layer und Panel",
    )
    format: Literal["html", "svg"] = Field(
        "html",
        description="html (Seite mit eingebetteter SVG) oder svg (eigenständige Vektorgrafik)",
    )

    @property
    def extension(self) -> str:
        """Default-Endung des Dateinamens (``engine/outputs.py``, FA2)."""
        return self.format

    def referenced_ids(self) -> list[str]:
        """Layer, die die Ausgabe namentlich nennt (Prüfung in ``validate_dag``)."""
        return list(self.layers or [])


# --------------------------------------------------------------------- Zahlen


def _num(value: float) -> str:
    """Kompakte Zahl: ``13.0``, ``51.25``, ``4.5e6``, ``5.65e6``."""
    if value == 0:
        return "0.0"
    if abs(value) >= 1e5 or abs(value) < 1e-3:
        mantissa, exponent = f"{value:.4e}".split("e")
        mantissa = mantissa.rstrip("0").rstrip(".")
        return f"{mantissa}e{int(exponent)}"
    text = f"{value:.6g}"
    return text if "." in text or "e" in text else f"{text}.0"


def _decimals(span: float) -> int:
    """Nachkommastellen, mit denen eine Spanne ``span`` lesbar aufgelöst wird."""
    if span <= 0 or not math.isfinite(span):
        return 2
    return max(0, min(6, 2 - math.floor(math.log10(span))))


def _fmt(value: float, decimals: int = 0) -> str:
    """Zahl mit Tausendertrennung (Leerzeichen): ``4 565 000``, ``12.93``."""
    text = f"{value:,.{decimals}f}".replace(",", " ")
    return "0" if text in ("-0", "-0.0") else text


def _nice_step(span: float, target: int = 5) -> float:
    raw = span / max(target, 1)
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * power:
            return factor * power
    return 10 * power


def _step_decimals(step: float) -> int:
    """Nachkommastellen, die Marken im Abstand ``step`` exakt zeigen (0.25 -> 2)."""
    for decimals in range(0, 10):
        if abs(round(step, decimals) - step) <= step * 1e-6:
            return decimals
    return 10


def _nice_ticks(lo: float, hi: float, target: int = 5) -> list[float]:
    step = _nice_step(hi - lo, target)
    first = math.ceil(lo / step - 1e-9)
    ticks = []
    k = first
    while k * step <= hi + step * 1e-9:
        ticks.append(round(k * step, 10))
        k += 1
    return ticks


def _p(value: float) -> str:
    """Pixelkoordinate mit fester Rundung (deterministische Ausgabe)."""
    text = f"{value:.1f}"
    return "0.0" if text == "-0.0" else text


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _thin(
    sample: list[tuple[float, float]], max_points: int
) -> list[tuple[float, float]]:
    finite = [(x, y) for x, y in sample if math.isfinite(x) and math.isfinite(y)]
    if len(finite) <= max_points:
        return finite
    step = math.ceil(len(finite) / max_points)
    return finite[::step][:max_points]


def _union(boxes: list[Bounds | None]) -> Bounds | None:
    present = [b for b in boxes if b is not None and all(math.isfinite(v) for v in b)]
    if not present:
        return None
    return (
        min(b[0] for b in present),
        min(b[1] for b in present),
        max(b[2] for b in present),
        max(b[3] for b in present),
    )


def _points_box(points: list[tuple[float, float]]) -> Bounds | None:
    if not points:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _shapes_box(shapes: tuple[Shape, ...]) -> Bounds | None:
    return _points_box([pt for _, rings in shapes for ring in rings for pt in ring])


def _extent_text(bounds: Bounds | None) -> str:
    if bounds is None:
        return "keine Ausdehnung"
    span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    d = _decimals(span)
    return (
        f"x {_fmt(bounds[0], d)} … {_fmt(bounds[2], d)} · "
        f"y {_fmt(bounds[1], d)} … {_fmt(bounds[3], d)}"
    )


# ------------------------------------------------------------------ Maßstab


@dataclass(frozen=True)
class _Frame:
    """Gleichmassige Abbildung Weltkoordinaten -> Pixel in einem Rechteck."""

    left: float
    top: float
    width: float
    height: float
    extent: Bounds

    @property
    def scale(self) -> float:
        xmin, ymin, xmax, ymax = self.extent
        dx = (xmax - xmin) or max(abs(xmin) * 0.01, 1.0)
        dy = (ymax - ymin) or max(abs(ymin) * 0.01, 1.0)
        return min(self.width / (dx * 1.08), self.height / (dy * 1.08))

    def sx(self, x: float) -> float:
        xmin, _, xmax, _ = self.extent
        return self.left + self.width / 2 + (x - (xmin + xmax) / 2) * self.scale

    def sy(self, y: float) -> float:
        _, ymin, _, ymax = self.extent
        return self.top + self.height / 2 - (y - (ymin + ymax) / 2) * self.scale

    def visible(self) -> Bounds:
        """Sichtbarer Weltausschnitt des ganzen Rechtecks."""
        s = self.scale
        xmin, ymin, xmax, ymax = self.extent
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        return (
            cx - self.width / 2 / s,
            cy - self.height / 2 / s,
            cx + self.width / 2 / s,
            cy + self.height / 2 / s,
        )


def _text(
    x: float,
    y: float,
    content: str,
    *,
    size: float = 11,
    anchor: str = "start",
    color: str = "#333",
    weight: str = "normal",
    cls: str = "",
    halo: bool = False,
) -> str:
    klass = f' class="{cls}"' if cls else ""
    bold = f' font-weight="{weight}"' if weight != "normal" else ""
    backdrop = ""
    if halo:
        # Weisser Hintergrund statt Kontur-Halo: ein Kontur-Halo erscheint in
        # PDF-Exporten als doppelter Text (Textebene), ein Rechteck nicht.
        width = _text_width(content, size) + 4
        x0 = (
            x - 2
            if anchor == "start"
            else x - width + 2
            if anchor == "end"
            else x - width / 2
        )
        backdrop = (
            f'<rect class="halo" x="{_p(x0)}" y="{_p(y - size * 0.85)}" width="{_p(width)}" '
            f'height="{_p(size * 1.15)}" fill="#ffffff" fill-opacity="0.85"/>'
        )
    return (
        f'{backdrop}<text{klass} x="{_p(x)}" y="{_p(y)}" font-size="{size}" text-anchor="{anchor}" '
        f'fill="{color}"{bold}>{_esc(content)}</text>'
    )


def _axes(frame: _Frame, *, grouped: bool) -> list[str]:
    """Rahmen, Gitter und lineare Achsenmarken eines Zeichenbereichs."""
    left, top, width, height = frame.left, frame.top, frame.width, frame.height
    vx0, vy0, vx1, vy1 = frame.visible()
    parts = [
        f'<rect x="{_p(left)}" y="{_p(top)}" width="{_p(width)}" height="{_p(height)}" '
        'fill="#ffffff" stroke="#9a9a9a"/>'
    ]
    xt = _nice_ticks(vx0, vx1, 5)
    yt = _nice_ticks(vy0, vy1, 5)
    dx, dy = (
        _step_decimals(_nice_step(vx1 - vx0)),
        _step_decimals(_nice_step(vy1 - vy0)),
    )
    base = top + height
    for x in xt:
        px = frame.sx(x)
        parts.append(
            f'<line class="grid" x1="{_p(px)}" y1="{_p(top)}" x2="{_p(px)}" y2="{_p(base)}" '
            'stroke="#e6e6e6" stroke-width="0.8"/>'
        )
        label = _fmt(x, dx) if grouped else _num(x)
        parts.append(_text(px, base + 15, label, size=10, anchor="middle", cls="tick"))
    for y in yt:
        py = frame.sy(y)
        parts.append(
            f'<line class="grid" x1="{_p(left)}" y1="{_p(py)}" x2="{_p(left + width)}" '
            f'y2="{_p(py)}" stroke="#e6e6e6" stroke-width="0.8"/>'
        )
        label = _fmt(y, dy) if grouped else _num(y)
        parts.append(
            _text(left - 5, py + 3.5, label, size=10, anchor="end", cls="tick")
        )
    return parts


def _clip(name: str, frame: _Frame) -> tuple[str, str]:
    """Clip-Pfad, damit Geometrien nicht über den Rahmen ragen."""
    defs = (
        f'<clipPath id="{name}"><rect x="{_p(frame.left)}" y="{_p(frame.top)}" '
        f'width="{_p(frame.width)}" height="{_p(frame.height)}"/></clipPath>'
    )
    return defs, f'clip-path="url(#{name})"'


# --------------------------------------------------------------- Geometrien


def _shape_marks(shapes: tuple[Shape, ...], frame: _Frame, color: str) -> list[str]:
    """Flächen als ein Pfad (evenodd), Linien als ein Pfad, Punkte als Kreise."""
    sx, sy = frame.sx, frame.sy
    polygons = [rings for kind, rings in shapes if kind == "polygon"]
    lines = [rings[0] for kind, rings in shapes if kind == "line" and rings]
    points = [
        rings[0][0] for kind, rings in shapes if kind == "point" and rings and rings[0]
    ]
    parts: list[str] = []
    if polygons:
        d = " ".join(
            "M" + " L".join(f"{_p(sx(x))} {_p(sy(y))}" for x, y in ring) + " Z"
            for rings in polygons
            for ring in rings
            if ring
        )
        parts.append(
            f'<path class="polygon" d="{d}" fill="{color}" fill-opacity="0.16" '
            f'fill-rule="evenodd" stroke="{color}" stroke-width="1.4" stroke-linejoin="round"/>'
        )
    if lines:
        d = " ".join(
            "M" + " L".join(f"{_p(sx(x))} {_p(sy(y))}" for x, y in ring)
            for ring in lines
        )
        parts.append(
            f'<path class="line" d="{d}" fill="none" stroke="{color}" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"/>'
        )
    for x, y in points:
        parts.append(
            f'<circle class="point" cx="{_p(sx(x))}" cy="{_p(sy(y))}" r="3.4" fill="{color}" '
            'stroke="#ffffff" stroke-width="0.9"/>'
        )
    return parts


def _box_and_sample(
    bounds: Bounds | None,
    sample: list[tuple[float, float]],
    frame: _Frame,
    color: str,
    *,
    min_px: float = 1.0,
) -> list[str]:
    parts: list[str] = []
    if bounds is not None:
        x0, y0, x1, y1 = bounds
        w = max((x1 - x0) * frame.scale, min_px)
        h = max((y1 - y0) * frame.scale, min_px)
        cx, cy = (frame.sx(x0) + frame.sx(x1)) / 2, (frame.sy(y0) + frame.sy(y1)) / 2
        parts.append(
            f'<rect class="extent" x="{_p(cx - w / 2)}" y="{_p(cy - h / 2)}" width="{_p(w)}" '
            f'height="{_p(h)}" fill="{color}" fill-opacity="0.12" stroke="{color}" '
            'stroke-width="1.4"/>'
        )
    for x, y in sample:
        parts.append(
            f'<circle class="pt" cx="{_p(frame.sx(x))}" cy="{_p(frame.sy(y))}" r="2" '
            f'fill="{color}"/>'
        )
    return parts


def _dominant_kind(shapes: tuple[Shape, ...]) -> str | None:
    """Häufigste Geometrieart eines Layers (bei Gleichstand Fläche vor Linie vor Punkt)."""
    counts = {kind: 0 for kind in ("polygon", "line", "point")}
    for kind, _ in shapes:
        if kind in counts:
            counts[kind] += 1
    best = max(counts.values())
    if best == 0:
        return None
    return next(kind for kind in ("polygon", "line", "point") if counts[kind] == best)


def _kind_rank(prov: LayerProvenance) -> int:
    """Zeichenreihenfolge: Flächen unten, Linien, Punkte oben (ohne Geometrien:
    Ausdehnungsrechteck wie eine Fläche)."""
    return {"polygon": 0, None: 0, "line": 1, "point": 2}[_dominant_kind(prov.shapes)]


def _group(prov: LayerProvenance, crs: str, body: list[str]) -> str:
    return (
        f'<g class="layer" data-layer="{_esc(prov.layer)}" data-crs="{_esc(crs)}">'
        f"<title>{_esc(prov.layer)}: {_esc(crs)}</title>{''.join(body)}</g>"
    )


# ------------------------------------------------------------------- Panels


@dataclass(frozen=True)
class _Entry:
    prov: LayerProvenance
    color: str
    raw_box: Bounds | None
    raw_sample: list[tuple[float, float]]
    sample: list[tuple[float, float]]


def _raw_box(prov: LayerProvenance, sample: list[tuple[float, float]]) -> Bounds | None:
    return _union([prov.raw_bounds, _points_box(sample), _shapes_box(prov.raw_shapes)])


def _panel_open(
    name: str, x: float, y: float, w: float, h: float, heading: str
) -> list[str]:
    return [
        f'<svg data-panel="{name}" x="{_p(x)}" y="{_p(y)}" width="{_p(w)}" height="{_p(h)}" '
        f'viewBox="0 0 {_p(w)} {_p(h)}" overflow="hidden">',
        _text(0, 16, heading, size=13, weight="bold", color="#222", cls="caption"),
    ]


Box = tuple[float, float, float, float]


class _Placer:
    """Legt Beschriftungen ohne Überlappung in einen Zeichenbereich: je
    Beschriftung werden Kandidaten (x, y, Ausrichtung) der Reihe nach probiert,
    die erste freie gewinnt. Ist keine frei, gilt der erste Kandidat - eine
    Beschriftung wird nie weggelassen (lieber überlappt sie)."""

    def __init__(self, area: Box) -> None:
        self.area = area
        self.taken: list[Box] = []

    def block(self, box: Box) -> None:
        self.taken.append(box)

    @staticmethod
    def _box(
        x: float, y: float, anchor: str, width: float, above: float, below: float
    ) -> Box:
        x0 = x if anchor == "start" else x - width if anchor == "end" else x - width / 2
        return (x0, y - above, x0 + width, y + below)

    def _free(self, box: Box) -> bool:
        ax0, ay0, ax1, ay1 = self.area
        if box[0] < ax0 or box[1] < ay0 or box[2] > ax1 or box[3] > ay1:
            return False
        return all(
            box[2] <= t[0] or box[0] >= t[2] or box[3] <= t[1] or box[1] >= t[3]
            for t in self.taken
        )

    def place(
        self,
        candidates: list[tuple[float, float, str]],
        width: float,
        above: float,
        below: float,
    ) -> tuple[float, float, str]:
        for x, y, anchor in candidates:
            box = self._box(x, y, anchor, width, above, below)
            if self._free(box):
                self.taken.append(box)
                return x, y, anchor
        x, y, anchor = candidates[0]
        self.taken.append(self._box(x, y, anchor, width, above, below))
        return x, y, anchor


def _text_width(text: str, size: float) -> float:
    """Geschätzte Textbreite (mittlere Zeichenbreite ~0.58 em)."""
    return len(text) * size * 0.58


def _raw_panel(entries: list[_Entry], x: float, y: float) -> tuple[str, list[_Entry]]:
    """Gemeinsame Achse aller Rohkoordinaten (mit Ursprung). Liefert das SVG und
    die Layer, die auf dieser Achse zu einem Punkt schrumpfen (Ausschnitte)."""
    parts = _panel_open(
        "raw",
        x,
        y,
        _PANEL_W,
        _PANEL_H,
        "Rohkoordinaten: gemeinsame Achse, je Einheit des Quell-CRS",
    )
    extent = _union([e.raw_box for e in entries])
    plot = (_PL, _PT, _PANEL_W - _PL - _PR, _PANEL_H - _PT - _PB)
    if extent is None:
        parts.append(
            _text(
                plot[0] + plot[2] / 2,
                plot[1] + plot[3] / 2,
                "keine Koordinaten",
                anchor="middle",
                color="#777",
                cls="note",
            )
        )
        for e in entries:
            parts.append(_group(e.prov, e.prov.source_crs_label, []))
        parts.append("</svg>")
        return "".join(parts), []
    extent = _union([extent, (0.0, 0.0, 0.0, 0.0)])  # Ursprung immer sichtbar
    frame = _Frame(*plot, extent=extent)  # type: ignore[misc]
    parts += _axes(frame, grouped=True)
    ox, oy = frame.sx(0.0), frame.sy(0.0)
    parts.append(
        f'<path class="origin" d="M{_p(ox - 6)} {_p(oy)} L{_p(ox + 6)} {_p(oy)} '
        f'M{_p(ox)} {_p(oy - 6)} L{_p(ox)} {_p(oy + 6)}" stroke="#555" stroke-width="1"/>'
    )

    centres: list[tuple[float, float] | None] = []
    collapsed: list[_Entry] = []
    for e in entries:
        body: list[str] = []
        if e.raw_box is not None:
            body += _box_and_sample(e.raw_box, e.raw_sample, frame, e.color, min_px=2.0)
            cx = (e.raw_box[0] + e.raw_box[2]) / 2
            cy = (e.raw_box[1] + e.raw_box[3]) / 2
            centres.append((cx, cy))
            body.append(
                f'<circle class="marker" cx="{_p(frame.sx(cx))}" cy="{_p(frame.sy(cy))}" r="6" '
                f'fill="none" stroke="{e.color}" stroke-width="2"/>'
            )
            w = (e.raw_box[2] - e.raw_box[0]) * frame.scale
            h = (e.raw_box[3] - e.raw_box[1]) * frame.scale
            if w < _COLLAPSED_PX and h < _COLLAPSED_PX:
                collapsed.append(e)
        else:
            centres.append(None)
        parts.append(_group(e.prov, e.prov.source_crs_label, body))

    plot_left, plot_top, plot_w, plot_h = plot
    placer = _Placer(
        (plot_left + 2, plot_top + 2, plot_left + plot_w - 2, plot_top + plot_h - 2)
    )
    located = [(e, c) for e, c in zip(entries, centres) if c is not None]
    pixels = [(frame.sx(c[0]), frame.sy(c[1])) for _, c in located]
    for px, py in pixels:
        placer.block((px - 8, py - 8, px + 8, py + 8))

    pairs = list(zip(zip(located, pixels), zip(located[1:], pixels[1:])))
    for (_, (x1, y1)), (_, (x2, y2)) in pairs:  # Linien unter den Etiketten
        parts.append(
            f'<line class="distance" x1="{_p(x1)}" y1="{_p(y1)}" x2="{_p(x2)}" y2="{_p(y2)}" '
            'stroke="#666" stroke-width="1" stroke-dasharray="4 3"/>'
        )

    # Etiketten: Name, Quell-CRS, Rohausdehnung - nie verborgen, sondern versetzt
    steps = [0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 8, -8, 10, -10, 12, -12]
    for (e, _), (px, py) in zip(located, pixels):
        head = f"{e.prov.layer} · {e.prov.source_crs or e.prov.source_crs_label}"
        extent_line = _extent_text(e.raw_box)
        width = max(_text_width(head, 11), _text_width(extent_line, 9.5))
        right_first = px < plot_left + plot_w * 0.55
        sides = [(px + 14, "start"), (px - 14, "end")]
        if not right_first:
            sides.reverse()
        candidates = [
            (lx, py + 3 + k * _LABEL_GAP / 2, anchor)
            for k in steps
            for lx, anchor in sides
        ]
        lx, ly, anchor = placer.place(candidates, width, 11, 16)
        edge = lx - 2 if anchor == "start" else lx + 2
        parts.append(
            f'<line class="leader" x1="{_p(px)}" y1="{_p(py)}" x2="{_p(edge)}" '
            f'y2="{_p(ly - 4)}" stroke="{e.color}" stroke-width="0.8"/>'
        )
        parts.append(
            f'<g class="label" data-layer="{_esc(e.prov.layer)}">'
            + _text(
                lx,
                ly,
                head,
                size=11,
                anchor=anchor,
                color=e.color,
                weight="bold",
                halo=True,
            )
            + _text(
                lx,
                ly + 12,
                extent_line,
                size=9.5,
                anchor=anchor,
                color="#333",
                cls="extent-label",
                halo=True,
            )
            + "</g>"
        )

    # Abstände aufeinanderfolgender Layer (reine Zahlenwerte, Einheiten gemischt)
    for ((a, ca), (x1, y1)), ((b, cb), (x2, y2)) in pairs:
        distance = math.hypot(cb[0] - ca[0], cb[1] - ca[1])
        label = f"Abstand {a.prov.layer}–{b.prov.layer} ≈ {_fmt(distance)}"
        candidates = []
        for t in (0.5, 0.4, 0.6, 0.3, 0.7, 0.2, 0.8):
            mx, my = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
            for dy in (14, -6, 26, -18, 38, -30, 50, -42, 62, 74, 86, 98):
                candidates.append((mx, my + dy, "middle"))
        lx, ly, _ = placer.place(candidates, _text_width(label, 10), 9, 3)
        parts.append(
            _text(
                lx,
                ly,
                label,
                size=10,
                anchor="middle",
                color="#444",
                cls="distance-label",
                halo=True,
            )
        )

    ox_label = placer.place(
        [
            (ox + 8, oy + 14, "start"),
            (ox + 8, oy - 8, "start"),
            (ox - 8, oy + 14, "end"),
            (ox - 8, oy - 8, "end"),
        ],
        _text_width("(0, 0)", 9),
        8,
        2,
    )
    parts.append(
        _text(
            ox_label[0],
            ox_label[1],
            "(0, 0)",
            size=9,
            anchor=ox_label[2],
            color="#555",
            cls="origin-label",
            halo=True,
        )
    )
    parts.append(
        _text(
            plot_left + plot_w / 2,
            _PANEL_H - 10,
            "x, y: Zahlenwerte wie in der Datei (Grad bzw. Meter, keine Umrechnung)",
            size=10,
            anchor="middle",
            color="#555",
            cls="axis",
        )
    )
    parts.append("</svg>")
    return "".join(parts), collapsed


def _inset(e: _Entry, x: float, y: float, w: float, h: float, index: int) -> str:
    """Ausschnitt eines zum Punkt geschrumpften Layers in eigenem Maßstab."""
    prov = e.prov
    parts = [
        f'<svg data-panel="inset" data-layer="{_esc(prov.layer)}" x="{_p(x)}" y="{_p(y)}" '
        f'width="{_p(w)}" height="{_p(h)}" viewBox="0 0 {_p(w)} {_p(h)}" overflow="hidden">',
        _text(
            0,
            14,
            f"{prov.layer}: Rohkoordinaten in eigenem Maßstab",
            size=11.5,
            weight="bold",
            color=e.color,
            cls="caption",
        ),
        _text(0, 28, prov.source_crs_label, size=9.5, color="#444"),
    ]
    box = e.raw_box
    assert box is not None
    plot = (64.0, 38.0, w - 74.0, h - 38.0 - 34.0)
    frame = _Frame(*plot, extent=box)
    parts += _axes(frame, grouped=True)
    defs, clip = _clip(f"inset-clip-{index}", frame)
    parts.append(f"<defs>{defs}</defs>")
    if prov.raw_shapes:
        body = _shape_marks(prov.raw_shapes, frame, e.color)
    else:
        body = _box_and_sample(prov.raw_bounds, e.raw_sample, frame, e.color)
    parts.append(f"<g {clip}>{_group(prov, prov.source_crs_label, body)}</g>")
    span_x, span_y = box[2] - box[0], box[3] - box[1]
    d = _decimals(max(span_x, span_y))
    parts.append(
        _text(
            plot[0] + plot[2] / 2,
            h - 6,
            f"Ausdehnung {_fmt(span_x, d)} × {_fmt(span_y, d)}",
            size=9.5,
            anchor="middle",
            color="#444",
            cls="extent-label",
        )
    )
    parts.append("</svg>")
    return "".join(parts)


def _harmonized_panel(entries: list[_Entry], x: float, y: float) -> str:
    targets = sorted({e.prov.target_crs_label for e in entries})
    target = targets[0] if len(targets) == 1 else " / ".join(targets)
    parts = _panel_open(
        "harmonized", x, y, _PANEL_W, _PANEL_H, f"Harmonisiert: {target}"
    )
    extent = _union(
        [
            _union([e.prov.bounds, _points_box(e.sample), _shapes_box(e.prov.shapes)])
            for e in entries
        ]
    )
    plot = (_PL, _PT, _PANEL_W - _PL - _PR, _PANEL_H - _PT - _PB)
    if extent is None:
        parts.append(
            _text(
                plot[0] + plot[2] / 2,
                plot[1] + plot[3] / 2,
                "keine Koordinaten",
                anchor="middle",
                color="#777",
                cls="note",
            )
        )
        for e in entries:
            parts.append(_group(e.prov, e.prov.target_crs_label, []))
        parts.append("</svg>")
        return "".join(parts)
    frame = _Frame(*plot, extent=extent)  # type: ignore[misc]
    parts += _axes(frame, grouped=True)
    defs, clip = _clip("harmonized-clip", frame)
    parts.append(f"<defs>{defs}</defs><g {clip}>")
    ordered = sorted(
        enumerate(entries), key=lambda item: (_kind_rank(item[1].prov), item[0])
    )
    for _, e in ordered:
        if e.prov.shapes:
            body = _shape_marks(e.prov.shapes, frame, e.color)
        else:
            body = _box_and_sample(e.prov.bounds, e.sample, frame, e.color)
        parts.append(_group(e.prov, e.prov.target_crs_label, body))
    parts.append("</g>")
    parts.append(
        _text(
            plot[0] + plot[2] / 2,
            _PANEL_H - 10,
            "x, y in der Einheit des Arbeits-CRS: alle Layer deckungsgleich",
            size=10,
            anchor="middle",
            color="#555",
            cls="axis",
        )
    )
    parts.append("</svg>")
    return "".join(parts)


def _legend(entries: list[_Entry], top: float) -> str:
    parts = [
        f'<g class="legend" transform="translate({_MARGIN} {_p(top)})">',
        _text(0, 0, "Quell-CRS → Arbeits-CRS", size=12.5, weight="bold", color="#222"),
    ]
    for i, e in enumerate(entries):
        prov = e.prov
        y = 20 + i * _LEGEND_ROW
        kind = _dominant_kind(prov.shapes or prov.raw_shapes)
        if kind in ("polygon", None):
            parts.append(
                f'<rect x="0" y="{_p(y - 10)}" width="14" height="11" fill="{e.color}" '
                f'fill-opacity="0.25" stroke="{e.color}" stroke-width="1.4"/>'
            )
        elif kind == "line":
            parts.append(
                f'<line x1="0" y1="{_p(y - 4)}" x2="14" y2="{_p(y - 4)}" stroke="{e.color}" '
                'stroke-width="2.2"/>'
            )
        else:
            parts.append(f'<circle cx="7" cy="{_p(y - 4)}" r="4" fill="{e.color}"/>')
        if prov.raw_bounds is None and not prov.raw_sample and not prov.raw_shapes:
            count = " · keine Geometrie"
        elif prov.feature_count is not None:
            count = f" · {prov.feature_count} Objekte"
        else:
            count = ""
        override = (
            '<tspan fill="#b00000" font-weight="bold"> · erzwungen (crs_override)</tspan>'
            if prov.crs_override
            else ""
        )
        parts.append(
            f'<text class="legend-row" data-layer="{_esc(prov.layer)}" x="22" y="{_p(y)}" '
            f'font-size="11.5" fill="#222"><tspan font-weight="bold">{_esc(prov.layer)}</tspan>'
            f" ({_esc(prov.source)}): {_esc(prov.source_crs_label)} → {_esc(prov.target_crs_label)}"
            f"{override}{_esc(count)}</text>"
        )
    parts.append("</g>")
    return "".join(parts)


def render_crs_svg(
    provenance: dict[str, LayerProvenance],
    layers: list[str],
    title: str,
    max_points: int,
) -> str:
    """Die Abbildung als eigenständige SVG (feste Größe, deterministisch) für
    ``layers`` (alle in ``provenance`` vorhanden)."""
    entries = []
    for i, layer in enumerate(layers):
        prov = provenance[layer]
        raw_sample = _thin(prov.raw_sample, max_points)
        entries.append(
            _Entry(
                prov,
                _PALETTE[i % len(_PALETTE)],
                _raw_box(prov, raw_sample),
                raw_sample,
                _thin(prov.sample, max_points),
            )
        )
    raw, collapsed = _raw_panel(entries, _MARGIN, _PANEL_TOP)
    harmonized = _harmonized_panel(entries, _WIDTH - _MARGIN - _PANEL_W, _PANEL_TOP)
    body = [raw, harmonized]
    cursor = _PANEL_TOP + _PANEL_H + 12
    if collapsed:
        body.append(
            _text(
                _MARGIN,
                cursor + 14,
                "Ausschnitte: Layer, die auf der gemeinsamen Achse zu einem Punkt schrumpfen",
                size=12.5,
                weight="bold",
                color="#222",
                cls="caption",
            )
        )
        n = len(collapsed)
        gap = 16.0
        inner = _WIDTH - 2 * _MARGIN
        w = min(380.0, (inner - (n - 1) * gap) / n)
        for i, e in enumerate(collapsed):
            body.append(_inset(e, _MARGIN + i * (w + gap), cursor + 24, w, _INSET_H, i))
        cursor += 24 + _INSET_H + 12
    legend_top = cursor + 16
    body.append(_legend(entries, legend_top))
    height = int(legend_top + 20 + len(entries) * _LEGEND_ROW + 8)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="crs-plot" width="{_WIDTH}" height="{height}" '
        f'viewBox="0 0 {_WIDTH} {height}" font-family="{_FONT}" role="img" aria-label="{_esc(title)}">'
        f"<title>{_esc(title)}</title>"
        f'<rect width="{_WIDTH}" height="{height}" fill="#ffffff"/>'
        + _text(_MARGIN, 32, title, size=18, weight="bold", color="#111", cls="title")
        + "".join(body)
        + "</svg>"
    )


def render_crs_plot(
    provenance: dict[str, LayerProvenance],
    layers: list[str],
    title: str,
    max_points: int,
) -> str:
    """HTML-Seite, die die Abbildung (``render_crs_svg``) einbettet."""
    svg = render_crs_svg(provenance, layers, title, max_points)
    return (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        "<style>body{margin:16px;background:#fff;color:#222;font-family:system-ui,sans-serif}"
        "svg.crs-plot{max-width:100%;height:auto;display:block}</style></head><body>"
        f"{svg}</body></html>"
    )


_RENDERERS: dict[
    str, Callable[[dict[str, LayerProvenance], list[str], str, int], str]
] = {
    "html": render_crs_plot,
    "svg": lambda provenance, layers, title, max_points: (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + render_crs_svg(provenance, layers, title, max_points)
        + "\n"
    ),
}


@register_output(
    "crs_plot",
    extension="html",
    accepts=(DataType.VECTOR, DataType.RASTER, DataType.GRAPH),
    options_model=CrsPlotOptions,
    description="Vergleichsplot der CRS-Provenienz: Rohkoordinaten je Quell-CRS auf einer "
    "gemeinsamen Achse neben der harmonisierten Überlagerung der Geometrien "
    "(HTML oder eigenständige SVG)",
)
def _write_crs_plot(data, target: Path, spec) -> None:
    options: CrsPlotOptions = (
        spec.options if spec.options is not None else CrsPlotOptions()
    )
    provenance = getattr(spec, "provenance", None)
    if not provenance:
        raise OutputSkipped(
            "crs_plot: keine CRS-Provenienz in diesem Lauf - der Plot entsteht nur bei einem "
            "Lauf, der Layer lädt (geofact run)"
        )
    layers = list(options.layers) if options.layers else list(provenance)
    missing = [layer for layer in layers if layer not in provenance]
    if missing:
        names = ", ".join(f"'{layer}'" for layer in missing)
        raise OutputSkipped(
            f"crs_plot: Layer {names} wurde in diesem Lauf nicht geladen (Lazy Loading: nur "
            "benötigte Layer haben Provenienz) - Layer von einem Schritt oder einer Ausgabe "
            "lesen lassen oder aus layers entfernen"
        )
    title = options.title or "CRS-Vergleich: Quell-CRS → Arbeits-CRS"
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = _RENDERERS[options.format](provenance, layers, title, options.max_points)
    target.write_text(content, encoding="utf-8", newline="\n")
