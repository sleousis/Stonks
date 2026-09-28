"""Parameter heatmaps (22.5) for the tear sheet and the terminal.

:func:`heatmap_html` draws a :class:`~stonks.lab.heatmap.ParameterHeatmap`
as an HTML table: one coloured cell per parameter pair (a diverging scale
around zero, grey for a failed cell), the tuned set outlined, the plateau
test's neighbourhood marked with a dashed border and its verdict above the
map. :func:`heatmap_text` prints the same grid for ``stonks lab run``.
Values are escaped where they are placed into markup (``reporting.html``).
"""

from __future__ import annotations

import math
from typing import Any

from stonks.lab.heatmap import FAST_METRIC, ParameterHeatmap
from stonks.reporting.html import badge, e

__all__ = ["heatmap_html", "heatmap_text"]


def _label(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _within(value: Any, bounds: tuple[float, float] | None) -> bool:
    if bounds is None or not isinstance(value, int | float) or isinstance(value, bool):
        return False
    return bounds[0] - 1e-12 <= float(value) <= bounds[1] + 1e-12


def _colour(value: float, scale: float) -> str:
    """Green for positive, red for negative, stronger with ``|value|``."""
    if not math.isfinite(value):
        return "background:var(--grid)"
    strength = min(1.0, abs(value) / scale) if scale > 0 else 0.0
    hue = 142 if value >= 0 else 0
    alpha = 0.12 + 0.68 * strength
    return f"background:hsla({hue},65%,42%,{alpha:.2f})"


def _metric_name(heatmap: ParameterHeatmap) -> str:
    if heatmap.metric == FAST_METRIC:
        return "fast Sharpe (approximate, vectorised)"
    return heatmap.metric


def _verdict_html(heatmap: ParameterHeatmap) -> str:
    plateau = heatmap.plateau
    if plateau is None:
        return '<p class="muted">The plateau test did not run in this suite.</p>'
    return (
        f"<p>Plateau test {badge(plateau.passed)} {e(plateau.notes)}. "
        f"The dashed cells are within {e(f'{plateau.step:.0%}')} of each range, "
        "where the test nudges the tuned set.</p>"
    )


def heatmap_html(heatmap: ParameterHeatmap) -> str:
    """One ``<section>`` with the map, its legend and the plateau verdict."""
    finite = [abs(v) for row in heatmap.scores for v in row if math.isfinite(v)]
    scale = max(finite) if finite else 0.0
    plateau = heatmap.plateau
    best_x, best_y = heatmap.best.get(heatmap.x), heatmap.best.get(heatmap.y)
    head = "".join(f'<th scope="col">{e(_label(x))}</th>' for x in heatmap.x_values)
    rows = []
    for y, row in zip(heatmap.y_values, heatmap.scores, strict=True):
        cells = []
        for x, value in zip(heatmap.x_values, row, strict=True):
            style = [_colour(value, scale), "text-align:right"]
            near = plateau is not None and (
                (plateau.x_range is None or _within(x, plateau.x_range))
                and (plateau.y_range is None or _within(y, plateau.y_range))
                and (plateau.x_range is not None or plateau.y_range is not None)
            )
            if near:
                style.append("border:2px dashed var(--muted)")
            is_best = x == best_x and y == best_y
            if is_best:
                style.append("outline:3px solid var(--fg);outline-offset:-3px;font-weight:700")
            text = f"{value:.3g}" if math.isfinite(value) else "n/a"
            title = f"{heatmap.x}={_label(x)}, {heatmap.y}={_label(y)}: {text}"
            mark = " (tuned)" if is_best else ""
            cells.append(f'<td style="{";".join(style)}" title="{e(title)}">{e(text + mark)}</td>')
        rows.append(f'<tr><th scope="row">{e(_label(y))}</th>{"".join(cells)}</tr>')
    table = (
        '<div class="scroll"><table class="heatmap">'
        f"<caption>{e(_metric_name(heatmap))} by {e(heatmap.x)} (across) and "
        f"{e(heatmap.y)} (down)</caption>"
        f'<thead><tr><th scope="col">{e(heatmap.y)} \\ {e(heatmap.x)}</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    n_cells = sum(len(r) for r in heatmap.scores)
    fixed = ", ".join(f"{k}={_label(v)}" for k, v in sorted(heatmap.fixed.items()))
    return (
        '<section class="heatmap-section">'
        "<h3>Parameter heatmap</h3>"
        f"{_verdict_html(heatmap)}"
        f"{table}"
        f'<p class="muted">{e(n_cells)} cells, each counted as a trial. '
        f"Other parameters held at: {e(fixed or 'none')}.</p>"
        "</section>"
    )


def heatmap_text(heatmap: ParameterHeatmap) -> str:
    """The grid as plain text, the tuned cell starred."""
    best_x, best_y = heatmap.best.get(heatmap.x), heatmap.best.get(heatmap.y)
    width = max(9, *(len(_label(x)) + 1 for x in heatmap.x_values))
    lines = [f"heatmap: {_metric_name(heatmap)}, {heatmap.y} (rows) by {heatmap.x} (columns)"]
    lines.append(" " * width + "".join(f"{_label(x):>{width}}" for x in heatmap.x_values))
    for y, row in zip(heatmap.y_values, heatmap.scores, strict=True):
        cells = []
        for x, value in zip(heatmap.x_values, row, strict=True):
            text = f"{value:.3g}" if math.isfinite(value) else "n/a"
            if x == best_x and y == best_y:
                text += "*"
            cells.append(f"{text:>{width}}")
        lines.append(f"{_label(y):>{width}}" + "".join(cells))
    if heatmap.plateau is not None:
        verdict = "pass" if heatmap.plateau.passed else "fail"
        lines.append(f"plateau test: {verdict} ({heatmap.plateau.notes})")
    lines.append("* the tuned set; every cell is a counted trial")
    return "\n".join(lines)
