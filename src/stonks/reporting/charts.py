"""Inline SVG charts for the static report: no scripts, no external assets.

Every label is escaped; coordinates are computed here and are always finite
(empty, single-point and flat series collapse to a safe range instead of
dividing by zero). Colours come from CSS classes the page styles for light
and dark themes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from datetime import date
from html import escape

Series = tuple[str, Sequence[tuple[date, float]], str]  # (label, points, css class)

_W, _H = 720, 220
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 64, 12, 12, 26


def _css_class(value: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", value.lower()) or "s1"


def _fmt_default(v: float) -> str:
    return f"{v:,.2f}"


def line_chart(
    series: Sequence[Series],
    *,
    title: str = "chart",
    fmt: Callable[[float], str] = _fmt_default,
    area: bool = False,
) -> str:
    """One or more series on shared axes. ``area`` fills down to the top of
    the plot (used for drawdown, whose values are <= 0)."""
    finite = [
        (label, [(d, float(v)) for d, v in pts if v is not None and math.isfinite(v)], cls)
        for label, pts, cls in series
    ]
    finite = [s for s in finite if s[1]]
    if not finite:
        return f'<div class="nodata" role="img" aria-label="{escape(title)}">no data</div>'

    all_days = [d for _, pts, _ in finite for d, _ in pts]
    all_vals = [v for _, pts, _ in finite for _, v in pts]
    d0, d1 = min(all_days), max(all_days)
    lo, hi = min(all_vals), max(all_vals)
    if area:
        hi = max(hi, 0.0)
    if hi == lo:
        pad = abs(hi) * 0.05 or 1.0
        lo, hi = lo - pad, hi + pad
    span_days = (d1 - d0).days or 1
    plot_w = _W - _PAD_L - _PAD_R
    plot_h = _H - _PAD_T - _PAD_B

    def x(d: date) -> float:
        if d1 == d0:
            return _PAD_L + plot_w / 2
        return _PAD_L + plot_w * (d - d0).days / span_days

    def y(v: float) -> float:
        return _PAD_T + plot_h * (hi - v) / (hi - lo)

    parts = [
        f'<svg class="chart" viewBox="0 0 {_W} {_H}" role="img" '
        f'aria-label="{escape(title)}" preserveAspectRatio="none">',
        f"<title>{escape(title)}</title>",
    ]
    for frac in (0.0, 0.5, 1.0):
        gy = _PAD_T + plot_h * frac
        val = hi - (hi - lo) * frac
        parts.append(
            f'<line class="grid" x1="{_PAD_L}" y1="{gy:.1f}" x2="{_W - _PAD_R}" y2="{gy:.1f}"/>'
            f'<text class="axis" x="{_PAD_L - 6}" y="{gy + 4:.1f}" text-anchor="end">'
            f"{escape(fmt(val))}</text>"
        )
    parts.append(
        f'<text class="axis" x="{_PAD_L}" y="{_H - 6}">{escape(d0.isoformat())}</text>'
        f'<text class="axis" x="{_W - _PAD_R}" y="{_H - 6}" text-anchor="end">'
        f"{escape(d1.isoformat())}</text>"
    )
    for label, pts, cls in finite:
        coords = " ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in pts)
        css = _css_class(cls)
        if area:
            top = y(min(max(0.0, lo), hi))
            first_x, last_x = x(pts[0][0]), x(pts[-1][0])
            parts.append(
                f'<polygon class="area {css}" '
                f'points="{first_x:.1f},{top:.1f} {coords} {last_x:.1f},{top:.1f}"/>'
            )
        parts.append(
            f'<polyline class="line {css}" points="{coords}"><title>{escape(label)}</title>'
            "</polyline>"
        )
        if len(pts) == 1:
            d, v = pts[0]
            parts.append(f'<circle class="dot {css}" cx="{x(d):.1f}" cy="{y(v):.1f}" r="3"/>')
    parts.append("</svg>")
    if len(finite) > 1:
        legend = "".join(
            f'<span class="key {_css_class(cls)}">{escape(label)}</span>'
            for label, _, cls in finite
        )
        parts.append(f'<div class="legend">{legend}</div>')
    return "".join(parts)
