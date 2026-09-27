"""Backtest tear sheet (BL-22): strategy against its benchmark.

Reads a ``BacktestReport`` and, when present, its ``benchmark``
(``getattr(report, "benchmark", None)``, a
``stonks.backtest.benchmark.BenchmarkResult``). Shows the equity curve
against the benchmark, both drawdowns, a rolling Sharpe, the top drawdowns,
a monthly returns grid, the trade statistics from the ledger and the
benchmark statistics. A long/short backtest (``report.short_book``, roadmap
16.4) adds its long, short and net exposure over time, the financing it
paid, the orders the engine forced and the P&L of each leg.

The data helpers are pure functions; rendering follows ``reporting.html``'s
rule (every data value is escaped where it is placed into markup) and emits
no scripts or external assets.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import date, datetime, time
from string import Template
from typing import Any

import numpy as np

from stonks.backtest import metrics
from stonks.reporting.charts import line_chart
from stonks.reporting.html import CSS, e, money, num, pct, table, tile

__all__ = [
    "DrawdownPeriod",
    "TearSheet",
    "drawdown_periods",
    "monthly_returns",
    "render_tear_sheet",
    "render_tear_sheet_page",
    "rolling_sharpe",
]

ROLLING_SHARPE_BARS = 126
TOP_DRAWDOWNS = 5
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class TearSheet:
    """One backtest to show: a title and its report (a ``BacktestReport``,
    ideally a ``BenchmarkedReport`` from ``stonks.lab.backtesting``)."""

    title: str
    report: Any
    #: Extra HTML sections shown after the tear sheet, already escaped
    #: (e.g. :func:`stonks.reporting.forecast_weights.strategy_report_sections`).
    sections: tuple[str, ...] = ()


# ---- data --------------------------------------------------------------------------


@dataclass(frozen=True)
class DrawdownPeriod:
    #: Trough over the prior peak, minus 1 (<= 0).
    depth: float
    #: Date of the peak the drawdown starts from.
    start: date
    trough: date
    #: First date back at or above the peak; ``None`` if never recovered.
    recovery: date | None
    #: Bars from the peak to the recovery (or to the last bar).
    length_bars: int


def drawdown_periods(
    dates: Sequence[date], curve: Sequence[float], top: int = TOP_DRAWDOWNS
) -> list[DrawdownPeriod]:
    """The ``top`` deepest peak-to-recovery episodes, deepest first."""
    periods: list[DrawdownPeriod] = []
    peak_i = 0
    trough_i: int | None = None
    for i in range(1, len(curve)):
        if curve[i] >= curve[peak_i]:
            if trough_i is not None:
                periods.append(_period(dates, curve, peak_i, trough_i, i))
                trough_i = None
            peak_i = i
        elif trough_i is None or curve[i] < curve[trough_i]:
            trough_i = i
    if trough_i is not None:
        periods.append(_period(dates, curve, peak_i, trough_i, None))
    periods.sort(key=lambda p: p.depth)
    return periods[:top]


def _period(
    dates: Sequence[date], curve: Sequence[float], peak: int, trough: int, rec: int | None
) -> DrawdownPeriod:
    depth = curve[trough] / curve[peak] - 1.0 if curve[peak] > 0 else 0.0
    end = rec if rec is not None else len(curve) - 1
    return DrawdownPeriod(
        depth=depth,
        start=dates[peak],
        trough=dates[trough],
        recovery=dates[rec] if rec is not None else None,
        length_bars=end - peak,
    )


def monthly_returns(dates: Sequence[date], curve: Sequence[float]) -> dict[int, dict[int, float]]:
    """``{year: {month: return}}`` chaining month-end values; the first
    month is measured from the first value."""
    month_end: dict[tuple[int, int], float] = {}
    for d, v in zip(dates, curve, strict=True):
        month_end[(d.year, d.month)] = float(v)
    out: dict[int, dict[int, float]] = {}
    prev = float(curve[0]) if len(curve) else 0.0
    for (year, month), value in month_end.items():
        out.setdefault(year, {})[month] = value / prev - 1.0 if prev > 0 else 0.0
        prev = value
    return out


def _yearly(grid: dict[int, dict[int, float]]) -> dict[int, float]:
    return {y: math.prod(1.0 + r for r in months.values()) - 1.0 for y, months in grid.items()}


def rolling_sharpe(
    dates: Sequence[date],
    curve: Sequence[float],
    window: int = ROLLING_SHARPE_BARS,
    periods_per_year: float = 252.0,
) -> list[tuple[date, float]]:
    """Annualized Sharpe of the trailing ``window`` bar returns, dated at
    each window's last bar."""
    values = np.asarray(curve, dtype=float)
    if values.size <= window:
        return []
    with np.errstate(divide="ignore", invalid="ignore"):
        rets = np.where(values[:-1] > 0, values[1:] / values[:-1] - 1.0, 0.0)
    return [
        (dates[i + 1], metrics.sharpe(rets[i + 1 - window : i + 1], periods_per_year))
        for i in range(window - 1, rets.size)
    ]


# ---- rendering ----------------------------------------------------------------------


def _signed(x: float) -> str:
    cls = "up" if x > 0 else "down" if x < 0 else "muted"
    return f'<span class="{cls}">{e(pct(x))}</span>'


def _fmt(x: Any) -> str:
    if isinstance(x, float) and not math.isfinite(x):
        return "inf" if x > 0 else "-inf" if x < 0 else "-"
    return num(x)


def _grid_table(report: Any, bench: Any) -> str:
    dates, curve = report.equity_dates, report.equity_curve
    if len(curve) < 2:
        return '<p class="muted">not enough bars</p>'
    grid = monthly_returns(dates, curve)
    years = _yearly(grid)
    bench_years = _yearly(monthly_returns(dates, bench.curve.values)) if bench else {}
    head = ["Year", *_MONTHS, "Year", *(["Bench", "Excess"] if bench else [])]
    rows = []
    for year in sorted(grid):
        cells = [e(year)]
        cells += [_signed(grid[year][m]) if m in grid[year] else "" for m in range(1, 13)]
        cells.append(f"<strong>{_signed(years[year])}</strong>")
        if bench:
            b = bench_years.get(year, 0.0)
            cells += [_signed(b), _signed(years[year] - b)]
        rows.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    header = "".join(f"<th>{e(h)}</th>" for h in head)
    return (
        '<div class="scroll"><table class="grid"><thead><tr>'
        f"{header}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _drawdown_table(report: Any) -> str:
    return table(
        ["depth", "peak", "trough", "recovery", "bars"],
        (
            [
                _signed(p.depth),
                e(_day(p.start)),
                e(_day(p.trough)),
                e(_day(p.recovery) if p.recovery else "not recovered"),
                e(p.length_bars),
            ]
            for p in drawdown_periods(report.equity_dates, report.equity_curve)
        ),
        "no drawdowns",
    )


def _day(d: date) -> str:
    if isinstance(d, datetime) and d.time() == time(0):
        return d.date().isoformat()
    return d.isoformat()


def _plot_dates(dates: Sequence[date]) -> list[date]:
    """Plain dates for daily curves (midnight stamps), so axes read cleanly."""
    if all(isinstance(d, datetime) and d.time() == time(0) for d in dates):
        return [d.date() for d in dates]
    return list(dates)


_TRADE_LABELS = {
    "n_trades": "Closed trades",
    "n_open": "Open lots",
    "win_rate": "Win rate",
    "avg_win": "Average win",
    "avg_loss": "Average loss",
    "payoff_ratio": "Payoff ratio",
    "expectancy": "Expectancy",
    "trade_profit_factor": "Profit factor",
    "avg_bars_held": "Average bars held",
    "exposure": "Exposure",
    "turnover_annual": "Annual turnover",
    "costs_paid": "Costs paid",
    "cost_drag_annual": "Annual cost drag",
}
_PCT_TRADE = {"win_rate", "exposure", "cost_drag_annual"}


def _trade_table(report: Any) -> str:
    stats = getattr(report, "trade_stats", None)
    if stats is None:
        return '<p class="muted">no trade ledger</p>'
    rows = []
    for f in fields(stats):
        value = getattr(stats, f.name)
        shown = pct(value) if f.name in _PCT_TRADE else _fmt(value)
        rows.append([e(_TRADE_LABELS.get(f.name, f.name)), e(shown)])
    return table(["statistic", "value"], rows, "no trades")


_BENCH_LABELS = {
    "n_obs": ("Paired bars", False),
    "benchmark_cagr": ("Benchmark CAGR", True),
    "excess_cagr": ("Excess CAGR", True),
    "benchmark_sharpe": ("Benchmark Sharpe", False),
    "benchmark_max_dd": ("Benchmark max drawdown", True),
    "beta": ("Beta", False),
    "alpha_annual": ("Alpha (annual)", True),
    "alpha_tstat": ("Alpha t-stat (HAC)", False),
    "r2": ("R squared", False),
    "residual_sharpe": ("Residual Sharpe", False),
    "tracking_error": ("Tracking error", True),
    "information_ratio": ("Information ratio", False),
    "up_capture": ("Up capture", False),
    "down_capture": ("Down capture", False),
    "correlation": ("Correlation", False),
}


def _bench_table(bench: Any) -> str:
    stats = bench.stats
    rows = [[e("Benchmark"), e(bench.curve.name)]]
    if bench.curve.members and bench.curve.members != (bench.curve.name,):
        rows.append([e("Holdings"), e(", ".join(bench.curve.members))])
    if bench.curve.excluded:
        rows.append([e("Left out (not priced at the start)"), e(", ".join(bench.curve.excluded))])
    for f in fields(stats):
        if f.name == "name":
            continue
        label, is_pct = _BENCH_LABELS.get(f.name, (f.name, False))
        value = getattr(stats, f.name)
        rows.append([e(label), e(pct(value) if is_pct else _fmt(value))])
    return table(["statistic", "value"], rows, "no benchmark")


def _short_book_html(book: Any, title: str) -> str:
    """The long/short section: exposure chart plus a statistics table."""
    points = list(book.exposure)
    dates = _plot_dates([p.timestamp for p in points])
    chart = line_chart(
        [
            ("long", list(zip(dates, [p.long for p in points], strict=True)), "s1"),
            ("short", list(zip(dates, [-p.short for p in points], strict=True)), "neg"),
            ("net", list(zip(dates, [p.net for p in points], strict=True)), "s2"),
        ],
        title=f"Exposure: {title}",
        fmt=lambda v: f"{v:.0%}",
    )
    rows = [
        ["Borrow fees", money(book.borrow_fees)],
        ["Debit interest", money(book.debit_interest)],
        ["Financing paid", money(book.financing_total)],
        ["Margin calls", str(book.n_margin_calls)],
        ["Recalls", str(book.n_recalls)],
        ["Largest gross", pct(book.max_gross)],
        ["Largest short", pct(book.max_short)],
        ["Average net", pct(book.mean_net)],
        ["Long trades", str(book.n_long_trades)],
        ["Long P&L", money(book.long_pnl)],
        ["Short trades", str(book.n_short_trades)],
        ["Short P&L", money(book.short_pnl)],
    ]
    stats = table(["statistic", "value"], ([e(k), e(v)] for k, v in rows), "no short book")
    return f"<h3>Long and short book</h3>{chart}{stats}"


def render_tear_sheet(sheet: TearSheet) -> str:
    """One ``<section>`` for ``sheet``."""
    report = sheet.report
    bench = getattr(report, "benchmark", None)
    dates, curve = _plot_dates(report.equity_dates), list(report.equity_curve)
    tiles = [
        tile("CAGR", pct(report.cagr)),
        tile("Sharpe", _fmt(report.sharpe)),
        tile("Max drawdown", pct(report.max_drawdown)),
        tile("Sortino", _fmt(getattr(report, "sortino", 0.0))),
        tile("Calmar", _fmt(getattr(report, "calmar", 0.0))),
    ]
    series = [("strategy", list(zip(dates, curve, strict=True)), "s1")]
    dd_series = [("strategy", list(zip(dates, metrics.drawdowns(curve), strict=True)), "neg")]
    if bench is not None:
        s = bench.stats
        tiles += [
            tile(f"{bench.curve.name} CAGR", pct(s.benchmark_cagr)),
            tile("Excess CAGR", pct(s.excess_cagr)),
            tile("Information ratio", _fmt(s.information_ratio)),
            tile("Beta", _fmt(s.beta)),
            tile("Alpha (t)", f"{pct(s.alpha_annual)} ({_fmt(s.alpha_tstat)})"),
        ]
        bvals = list(bench.curve.values)
        series.append((bench.curve.name, list(zip(dates, bvals, strict=True)), "s2"))
        dd_series.append(
            (bench.curve.name, list(zip(dates, metrics.drawdowns(bvals), strict=True)), "s2")
        )
        bench_html = _bench_table(bench)
    else:
        bench_html = '<p class="muted">no benchmark for this backtest</p>'
    title = f"Strategy vs benchmark: {sheet.title}"
    equity = line_chart(series, title=title)
    drawdown = line_chart(
        dd_series, title=f"Drawdown: {sheet.title}", fmt=lambda v: f"{v:.1%}", area=True
    )
    ppy = float(getattr(report, "periods_per_year", 252.0))
    roll = line_chart(
        [("rolling Sharpe", rolling_sharpe(dates, curve, periods_per_year=ppy), "s1")],
        title=f"Rolling {ROLLING_SHARPE_BARS}-bar Sharpe: {sheet.title}",
    )
    book = getattr(report, "short_book", None)
    short_html = _short_book_html(book, sheet.title) if book is not None else ""
    return (
        '<section class="tearsheet">'
        f"<h2>{e(sheet.title)}</h2>"
        f'<p class="muted">{e(report.strategy_id)} · {e(len(curve))} bars</p>'
        f'<div class="tiles">{"".join(tiles)}</div>'
        f"<h3>Strategy vs benchmark</h3>{equity}"
        f"<h3>Drawdown</h3>{drawdown}"
        f"<h3>Rolling {ROLLING_SHARPE_BARS}-bar Sharpe</h3>{roll}"
        f"<h3>Top drawdowns</h3>{_drawdown_table(report)}"
        f"<h3>Monthly returns</h3>{_grid_table(report, bench)}"
        f"<h3>Trade statistics</h3>{_trade_table(report)}"
        f"<h3>Benchmark statistics</h3>{bench_html}"
        f"{short_html}"
        "</section>"
        f"{''.join(sheet.sections)}"
    )


_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>$title</title>
<style>$css</style>
</head>
<body>
<header><h1>$title</h1></header>
<main>
$body
</main>
</body>
</html>
"""
)


def render_tear_sheet_page(sheet: TearSheet) -> str:
    """A self-contained HTML page with one tear sheet."""
    return _PAGE.substitute(
        title=e(f"Tear sheet: {sheet.title}"), css=CSS, body=render_tear_sheet(sheet)
    )
