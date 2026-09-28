"""Render ``ReportData`` to one self-contained HTML page.

Templating is ``string.Template`` for the page skeleton plus small builders
for repeated fragments. The escaping rule is simple: every value that came
from data goes through :func:`_e` (``html.escape`` with quotes) at the point
it is placed into markup; builders only ever concatenate already-escaped
fragments. The page has no scripts, no external stylesheets, fonts or images,
and themes itself via ``prefers-color-scheme``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from string import Template

from stonks.production.golive import GoLiveCheck
from stonks.reporting.charts import line_chart
from stonks.reporting.data import ReportData, StrategyPanel
from stonks.reporting.html import CSS as _CSS
from stonks.reporting.html import badge as _badge
from stonks.reporting.html import e as _e
from stonks.reporting.html import money as _money
from stonks.reporting.html import num as _num
from stonks.reporting.html import pct as _pct
from stonks.reporting.html import table as _table
from stonks.reporting.html import tile as _tile
from stonks.reporting.tearsheet import render_tear_sheet


def _portfolio_section(data: ReportData) -> str:
    rows = data.portfolio
    if not rows:
        return (
            '<section><h2>Portfolio</h2><p class="muted">no portfolio snapshots yet</p></section>'
        )
    last = rows[-1]
    worst = min(r.drawdown for r in rows)
    tiles = "".join(
        [
            _tile("Value", _money(last.total_value)),
            _tile("Return since inception", _pct(last.cumulative_return)),
            _tile("Max drawdown (shown)", _pct(worst)),
            _tile("Days shown", str(len(rows))),
        ]
    )
    equity = line_chart(
        [("portfolio value", [(r.day, r.total_value) for r in rows], "s1")],
        title="Portfolio equity curve",
    )
    drawdown = line_chart(
        [("drawdown", [(r.day, r.drawdown) for r in rows], "neg")],
        title="Portfolio drawdown",
        fmt=lambda v: f"{v:.1%}",
        area=True,
    )
    return (
        f'<section><h2>Portfolio</h2><div class="tiles">{tiles}</div>'
        f"<h3>Equity curve</h3>{equity}<h3>Drawdown</h3>{drawdown}</section>"
    )


def _positions_section(data: ReportData) -> str:
    pos = data.positions
    if pos is None:
        return (
            '<section><h2>Positions</h2><p class="muted">no portfolio snapshots yet</p></section>'
        )
    table = _table(
        ["ticker", "quantity"],
        ([_e(t), _e(_num(q))] for t, q in sorted(pos.quantities.items())),
        "no open positions",
    )
    return (
        f'<section><h2>Positions</h2><p class="muted">as of {_e(pos.taken_at)} · cash '
        f"{_e(_money(pos.cash))} · value {_e(_money(pos.total_value))}</p>{table}</section>"
    )


def _orders_section(data: ReportData) -> str:
    orders = _table(
        ["created", "strategy", "ticker", "side", "qty", "type", "status"],
        (
            [
                _e(o["created_at"]),
                _e(o["strategy_id"] or "-"),
                _e(o["ticker"]),
                _e(o["side"]),
                _e(_num(o["quantity"])),
                _e(o["order_type"]),
                _e(o["status"]),
            ]
            for o in data.orders
        ),
        "no orders",
    )
    fills = _table(
        ["filled", "strategy", "ticker", "side", "qty", "price", "fee"],
        (
            [
                _e(f["filled_at"]),
                _e(f["strategy_id"] or "-"),
                _e(f["ticker"]),
                _e(f["side"] or "-"),
                _e(_num(f["quantity"])),
                _e(_money(f["price"])),
                _e(_money(f["fee"])),
            ]
            for f in data.fills
        ),
        "no fills",
    )
    return (
        f"<section><h2>Recent orders</h2>{orders}</section>"
        f"<section><h2>Recent fills</h2>{fills}</section>"
    )


def _gate_table(checks: Sequence[GoLiveCheck]) -> str:
    return _table(
        ["check", "verdict", "detail"],
        ([_e(c.name), _badge(c.passed), _e(c.detail)] for c in checks),
        "no checks",
    )


def _strategy_section(panel: StrategyPanel) -> str:
    h, paper = panel.handle, panel.paper
    params = _table(
        ["param", "value"],
        ([_e(k), _e(json.dumps(v, default=str))] for k, v in sorted(h.params.items())),
        "no params",
    )
    reports = _table(
        ["test", "verdict", "metrics", "notes"],
        (
            [
                _e(r.test_id),
                _badge(r.passed),
                _e(", ".join(f"{k}={_num(v)}" for k, v in sorted(r.metrics.items()))),
                _e(r.notes),
            ]
            for r in panel.reports
        ),
        "no survival reports",
    )
    if paper.source == "shadow":
        paper_title = "Test book P&L vs backtest expectation"
    elif paper.source == "portfolio":
        paper_title = "Live vs backtest drift (combined real portfolio)"
    else:
        paper_title = "Paper period"
    series = [("paper", [(r.day, r.total_value) for r in paper.rows], "s1")]
    expected = panel.expected_curve
    if expected:
        series.append(("backtest expectation", expected, "s2"))
    chart = line_chart(series, title=f"{paper_title}: {h.id}")
    tiles = "".join(
        [
            _tile("Paper return", _pct(paper.period_return)),
            _tile("Backtest expectation", _pct(paper.expected_return)),
            _tile("Drift", _pct(paper.drift)),
            _tile(
                "Max drawdown", _pct(None if paper.max_drawdown is None else -paper.max_drawdown)
            ),
            _tile("Days", str(paper.days)),
            _tile("Trades", str(paper.trades)),
        ]
    )
    gate_ok = bool(panel.gate) and all(c.passed for c in panel.gate)
    return (
        f'<section class="strategy" id="strategy-{_e(h.id)}">'
        f"<h2>{_e(h.id)} "
        f'<span class="badge status-{_e(h.status)}">{_e(h.status)}</span></h2>'
        f'<p class="muted">{_e(h.class_path)} · registered {_e(h.created_at)} · updated '
        f"{_e(h.updated_at)}</p>"
        f"<h3>Params</h3>{params}"
        f"<h3>Survival verdicts</h3>{reports}"
        f"<h3>{_e(paper_title)}</h3>"
        f'<div class="tiles">{tiles}</div>{chart}'
        f"<h3>Go-live gate {_badge(gate_ok)}</h3>{_gate_table(panel.gate)}"
        "</section>"
    )


def _strategies_section(data: ReportData) -> str:
    parts = ["<h2 class='group'>Strategies</h2>"]
    if data.unknown_strategy_ids:
        parts.append(
            '<p class="warn">unknown strategy id(s): '
            + _e(", ".join(data.unknown_strategy_ids))
            + "</p>"
        )
    if not data.strategies:
        parts.append('<p class="muted">no strategies</p>')
    parts.extend(_strategy_section(p) for p in data.strategies)
    return "".join(parts)


def _tear_sheets_section(data: ReportData) -> str:
    if not data.tear_sheets:
        return ""
    return "<h2 class='group'>Backtests</h2>" + "".join(
        render_tear_sheet(sheet) for sheet in data.tear_sheets
    )


_PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Stonks report</title>
<style>$css</style>
</head>
<body>
<header><h1>Stonks report</h1><p class="muted">$subtitle</p></header>
<main>
$portfolio
$positions
$orders
$strategies
$tear_sheets
</main>
</body>
</html>
"""
)


def render_html(data: ReportData) -> str:
    subtitle = f"generated {data.generated_at.isoformat(timespec='seconds')}"
    if data.since is not None:
        subtitle += f" · showing from {data.since.isoformat()} (returns from inception)"
    return _PAGE.substitute(
        css=_CSS,
        subtitle=_e(subtitle),
        portfolio=_portfolio_section(data),
        positions=_positions_section(data),
        orders=_orders_section(data),
        strategies=_strategies_section(data),
        tear_sheets=_tear_sheets_section(data),
    )
