"""Backtest tear sheet for the static HTML report (BL-22)."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timedelta

import numpy as np
import pytest

from stonks.backtest.benchmark import BenchmarkCurve, with_benchmark
from stonks.backtest.report import compute_report
from stonks.backtest.trades import TradeStats
from stonks.reporting.tearsheet import (
    TearSheet,
    drawdown_periods,
    monthly_returns,
    render_tear_sheet,
    render_tear_sheet_page,
    rolling_sharpe,
)

EVIL = "<script>alert('pwn')</script>"


def _days(n, start=date(2023, 1, 2)):
    return [datetime(start.year, start.month, start.day) + timedelta(days=i) for i in range(n)]


# ---- drawdown table ------------------------------------------------------------------


def test_drawdown_periods_on_a_fixture():
    d = _days(10)
    curve = [100, 110, 99, 88, 110, 120, 108, 114, 121, 115]
    periods = drawdown_periods(d, curve, top=5)
    assert len(periods) == 3
    first, second, third = periods
    assert first.depth == pytest.approx(88 / 110 - 1)
    assert (first.start, first.trough, first.recovery) == (d[1], d[3], d[4])
    assert first.length_bars == 3
    assert second.depth == pytest.approx(108 / 120 - 1)
    assert (second.start, second.trough, second.recovery) == (d[5], d[6], d[8])
    # unrecovered at the end
    assert third.depth == pytest.approx(115 / 121 - 1)
    assert third.recovery is None
    assert third.length_bars == 1


def test_drawdown_periods_keeps_the_deepest_n():
    d = _days(9)
    curve = [100, 90, 100, 80, 100, 95, 100, 70, 100]
    periods = drawdown_periods(d, curve, top=2)
    assert [round(p.depth, 2) for p in periods] == [-0.30, -0.20]


def test_drawdown_periods_of_a_rising_curve_is_empty():
    assert drawdown_periods(_days(3), [1, 2, 3]) == []
    assert drawdown_periods([], []) == []


# ---- monthly grid --------------------------------------------------------------------


def test_monthly_returns_chain_month_ends():
    d = [datetime(2023, 1, 2), datetime(2023, 1, 31), datetime(2023, 2, 28), datetime(2023, 3, 1)]
    grid = monthly_returns(d, [100, 110, 99, 108.9])
    assert grid[2023][1] == pytest.approx(0.10)
    assert grid[2023][2] == pytest.approx(-0.10)
    assert grid[2023][3] == pytest.approx(0.10)


def test_monthly_returns_span_years():
    d = [datetime(2022, 12, 30), datetime(2023, 1, 3)]
    grid = monthly_returns(d, [100, 105])
    assert grid[2022][12] == pytest.approx(0.0)
    assert grid[2023][1] == pytest.approx(0.05)


# ---- rolling Sharpe ------------------------------------------------------------------


def test_rolling_sharpe_starts_after_a_full_window():
    rng = np.random.default_rng(0)
    curve = list(100 * np.cumprod(1 + rng.normal(0.001, 0.01, 200)))
    d = _days(200)
    out = rolling_sharpe(d, curve, window=126, periods_per_year=252)
    assert len(out) == 200 - 126
    assert out[0][0] == d[126]
    assert all(np.isfinite(v) for _, v in out)
    assert rolling_sharpe(d[:50], curve[:50], window=126) == []


# ---- rendering -----------------------------------------------------------------------


def _report(with_bench=True, strategy_id="strat"):
    rng = np.random.default_rng(3)
    d = _days(300)
    market = rng.normal(0.0004, 0.01, 299)
    curve = list(10_000 * np.cumprod(np.r_[1.0, 1 + 1.2 * market + 0.0003]))
    report = compute_report(strategy_id, d, curve)
    report = replace(report, trade_stats=TradeStats(n_trades=7, win_rate=0.57, expectancy=12.5))
    if not with_bench:
        return report
    bench = BenchmarkCurve(
        name="EW",
        spec="auto",
        dates=tuple(d),
        values=tuple(10_000 * np.cumprod(np.r_[1.0, 1 + market])),
        members=("A.US", "B.US"),
        excluded=("LATE.US",),
    )
    return with_benchmark(report, bench)


def test_tear_sheet_shows_every_block():
    html = render_tear_sheet(TearSheet(title="Momentum lab run", report=_report()))
    for text in (
        "Momentum lab run",
        "Strategy vs benchmark",
        "Drawdown",
        "Rolling 126-bar Sharpe",
        "Top drawdowns",
        "Monthly returns",
        "Trade statistics",
        "Benchmark statistics",
        "Information ratio",
        "Beta",
        "LATE.US",
    ):
        assert text in html, text
    assert html.count("<svg") == 3
    assert "Jan" in html and "Dec" in html


def test_tear_sheet_without_benchmark_still_renders():
    html = render_tear_sheet(TearSheet(title="t", report=_report(with_bench=False)))
    assert "no benchmark" in html
    assert "Trade statistics" in html


def test_tear_sheet_escapes_everything():
    report = _report(strategy_id=EVIL)
    bench = replace(report.benchmark.curve, name=EVIL, members=(EVIL,), excluded=(EVIL,))
    report = with_benchmark(report, bench)
    html = render_tear_sheet(TearSheet(title=EVIL, report=report))
    assert "<script" not in html
    assert "&lt;script&gt;" in html


def test_standalone_page_has_no_scripts_or_external_assets():
    page = render_tear_sheet_page(TearSheet(title=EVIL, report=_report()))
    assert page.startswith("<!doctype html>")
    assert "<script" not in page.lower()
    assert not re.search(r"""(src|href)\s*=\s*["']?https?:""", page)
    assert "&lt;script&gt;" in page


def test_main_report_includes_attached_tear_sheets():
    from stonks.reporting import ReportData, render_html

    data = ReportData(
        generated_at=datetime(2024, 1, 1),
        since=None,
        portfolio=[],
        positions=None,
        orders=[],
        fills=[],
        strategies=[],
        tear_sheets=[TearSheet(title="Backtest: momentum", report=_report())],
    )
    html = render_html(data)
    assert "Backtest: momentum" in html
    assert "Benchmark statistics" in html


def test_main_report_without_tear_sheets_has_no_backtest_group():
    from stonks.reporting import ReportData, render_html

    data = ReportData(
        generated_at=datetime(2024, 1, 1),
        since=None,
        portfolio=[],
        positions=None,
        orders=[],
        fills=[],
        strategies=[],
    )
    assert "Benchmark statistics" not in render_html(data)
