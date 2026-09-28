"""The factor tear sheet as a static HTML page (roadmap 22.3). Same
escaping rule and stylesheet as the other reports (``reporting.html``); no
scripts, no external assets."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

from stonks.reporting.charts import line_chart
from stonks.reporting.html import CSS, e, table, tile
from stonks.reporting.signals import _f, _p

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _heat(value: float | None, scale: float) -> str:
    """One heatmap cell: green for a positive IC, red for a negative one."""
    if value is None or not math.isfinite(value):
        return '<td class="muted">-</td>'
    alpha = min(1.0, abs(value) / scale) * 0.6 if scale > 0 else 0.0
    rgb = "21,128,61" if value >= 0 else "220,38,38"
    return f'<td style="background:rgba({rgb},{alpha:.2f})">{e(f"{value:+.3f}")}</td>'


def _monthly_section(sheet: Any) -> str:
    cells = [v for row in sheet.monthly_ic for v in row.months if v is not None]
    scale = max((abs(v) for v in cells), default=0.0)
    head = "".join(f"<th>{m}</th>" for m in ("year", *_MONTHS))
    body = "".join(
        f"<tr><td>{e(row.year)}</td>" + "".join(_heat(v, scale) for v in row.months) + "</tr>"
        for row in sheet.monthly_ic
    )
    if not body:
        return '<h3>Monthly IC</h3><p class="muted">no IC in the window</p>'
    return (
        f"<h3>Monthly IC (h={e(sheet.ic_horizon)})</h3>"
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    )


def _group_tables(sheet: Any) -> str:
    out = []
    titles = {"sector": "sector", "asset_class": "asset class", "size": "size"}
    for key, groups in sheet.ic_by_group.items():
        title: str = titles.get(key) or str(key)
        if key == "size":
            title += f" ({sheet.size_basis.replace('_', ' ')})"
        rows = (
            [
                e(g.group),
                e(g.n_dates),
                e(_f(g.mean_names, "{:.1f}")),
                e(_f(g.mean_ic)),
                e(_f(g.t_stat_hac, "{:+.2f}")),
            ]
            for g in groups
        )
        out.append(
            f"<h3>IC by {e(title)}</h3>"
            + table(["group", "dates", "names", "mean IC", "t (HAC)"], rows, "no groups")
        )
    return "".join(out)


def _curves(sheet: Any) -> str:
    qc = sheet.quantile_curves
    if not qc.dates:
        return ""
    days = [date.fromisoformat(d) for d in qc.dates]
    series = [
        (f"top bucket (Q{len(qc.series)})", list(zip(days, qc.series[-1], strict=True)), "s1"),
        ("bottom bucket (Q1)", list(zip(days, qc.series[0], strict=True)), "neg"),
        ("top minus bottom", list(zip(days, qc.spread, strict=True)), "s2"),
    ]
    chart = line_chart(series, title="Cumulative return per bucket", fmt=lambda v: f"{v:.1%}")
    return f"<h3>Cumulative return, held {e(sheet.every_bars)} bars at a time</h3>{chart}"


def render_factor_section(sheet: Any) -> str:
    """A :class:`~stonks.factors.tearsheet.FactorTearSheet` as one ``<section>``."""
    factor = sheet.factor
    head = (
        f"<h2>Factor · {e(factor['id'])}</h2>"
        f'<p class="muted">{e(factor.get("description") or "")} · direction '
        f"{e('+1' if factor.get('direction', 1) > 0 else '-1')}</p>"
        f'<p class="muted">{e(sheet.window[0])} to {e(sheet.window[1])} · '
        f"{e(sheet.n_tickers)} tickers · {e(sheet.n_dates)} dates sampled every "
        f"{e(sheet.every_bars)} bars · returns from the next open</p>"
    )
    if sheet.status != "ok":
        return f'<section>{head}<p class="muted">{e(sheet.status)}: {e(sheet.note)}</p></section>'
    main = next((h for h in sheet.horizons if h.horizon == sheet.ic_horizon), None)
    ab = sheet.alpha_beta
    tiles = "".join(
        [
            tile(f"Mean IC (h={sheet.ic_horizon})", _f(main.mean_ic if main else None)),
            tile("ICIR", _f(main.icir if main else None, "{:+.3f}")),
            tile("t (HAC)", _f(main.t_stat_hac if main else None, "{:+.2f}")),
            tile("Coverage", _f(sheet.coverage, "{:.0%}")),
            tile("Alpha a year", _p(ab.alpha_annual)),
            tile("Beta", _f(ab.beta, "{:+.2f}")),
            tile("Top-bucket turnover", _f(sheet.top_quantile_turnover, "{:.2f}")),
        ]
    )
    rows = (
        [
            e(h.horizon),
            e(h.n_dates),
            e(_f(h.mean_ic)),
            e(_f(h.icir, "{:+.3f}")),
            e(_f(h.hit_rate, "{:.1%}")),
            e(_f(h.t_stat_hac, "{:+.2f}")),
            e(_p(h.spread_mean)),
            e(_f(h.spread_t_hac, "{:+.2f}")),
        ]
        for h in sheet.horizons
    )
    ic_table = table(
        ["horizon", "dates", "mean IC", "ICIR", "IC > 0", "t (HAC)", "top minus bottom", "t"],
        rows,
        "no horizons",
    )
    n_q = sheet.n_quantiles
    q_rows = ([e(h.horizon), *(e(_p(v)) for v in h.quantile_means)] for h in sheet.horizons)
    q_table = table(["horizon", *(f"Q{i + 1}" for i in range(n_q))], q_rows, "no buckets")
    alpha = (
        f'<p class="muted">Long-short book (turned by the direction) on the equal-weight '
        f"universe: alpha {e(_p(ab.alpha_annual))} a year (t {e(_f(ab.alpha_t, '{:+.2f}'))}), "
        f"beta {e(_f(ab.beta, '{:+.2f}'))}, R squared {e(_f(ab.r_squared, '{:.2f}'))}, "
        f"{e(ab.n_periods)} periods.</p>"
    )
    return (
        f'<section>{head}<div class="tiles">{tiles}</div>'
        f"<h3>IC per horizon</h3>{ic_table}"
        f"<h3>Mean forward return per bucket (Q1 lowest values)</h3>{q_table}"
        f"{_curves(sheet)}{alpha}{_group_tables(sheet)}{_monthly_section(sheet)}</section>"
    )


def render_factor_page(sheet: Any) -> str:
    """A self-contained page with one factor tear sheet."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Factor tear sheet · {e(sheet.factor['id'])}</title><style>{CSS}</style></head>"
        f"<body><header><h1>Factor tear sheet</h1></header><main>"
        f"{render_factor_section(sheet)}</main></body></html>"
    )
