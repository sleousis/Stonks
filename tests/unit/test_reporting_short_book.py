"""The tear sheet shows a long/short book's financing, forced orders,
exposure and attribution (roadmap 16.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from stonks.backtest.report import ExposurePoint, ShortBookReport, compute_report
from stonks.backtest.simulated_broker import FinancingEvent
from stonks.core.types import Order
from stonks.reporting.tearsheet import TearSheet, render_tear_sheet


def _report(short_book: ShortBookReport | None):
    days = [datetime(2024, 1, 1) + timedelta(days=i) for i in range(10)]
    report = compute_report("ls", days, [10_000.0 + 10 * i for i in range(10)])
    return replace(report, short_book=short_book)


def _book() -> ShortBookReport:
    stamp = datetime(2024, 1, 3, tzinfo=UTC)
    days = [datetime(2024, 1, 1) + timedelta(days=i) for i in range(10)]
    return ShortBookReport(
        financing=(
            FinancingEvent(stamp, "X", "borrow_fee", -1.25, 1),
            FinancingEvent(stamp, None, "debit_interest", -0.5, 1),
        ),
        forced_orders=(
            Order("margin:1:X:cover", "X", "buy", 5.0, strategy_id="margin"),
            Order("recall:2:X:cover", "X", "buy", 5.0, strategy_id="recall"),
        ),
        exposure=tuple(ExposurePoint(d, 0.5, 0.4) for d in days),
        long_pnl=120.0,
        short_pnl=-30.0,
        n_long_trades=3,
        n_short_trades=2,
    )


def test_short_book_section_hand_checked():
    html = render_tear_sheet(TearSheet(title="t", report=_report(_book())))
    assert "Long and short book" in html
    for text in ("Borrow fees", "1.25", "Debit interest", "0.50", "Margin calls", "Recalls"):
        assert text in html, text
    assert "Short P&amp;L" in html and "-30" in html
    # the exposure chart joins the standard charts
    plain = render_tear_sheet(TearSheet(title="t", report=_report(None)))
    assert html.count("<svg") == plain.count("<svg") + 1


def test_long_only_tear_sheet_has_no_short_section():
    html = render_tear_sheet(TearSheet(title="t", report=_report(None)))
    assert "Long and short book" not in html
