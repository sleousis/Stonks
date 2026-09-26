"""The runs test's trade sequence: closed round trips from the backtest's
trade ledger (``BacktestReport.trades``, BL-02), ordered by exit."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from stonks.backtest.report import compute_report
from stonks.backtest.trades import with_trades
from stonks.core.types import Fill
from stonks.lab.survival.runs_test import round_trip_trades


def _fill(side, qty, price, day, ticker="X.US", fee=0.0):
    return Fill(
        order_client_id=f"0:{side}:{ticker}:{day}:{qty}",  # engine-style "<index>:" prefix
        ticker=ticker,
        quantity=qty,
        price=price,
        fee=fee,
        filled_at=datetime(2025, 1, day),
        side=side,
    )


def _trades(fills):
    days = [date(2025, 1, d) for d in range(1, 11)]
    report = compute_report("s", days, [10_000.0] * len(days))
    return round_trip_trades(with_trades(report, fills))


def test_buy_then_sell_is_one_trade_with_fees_in_the_return():
    trades = _trades([_fill("buy", 10, 100.0, 1, fee=1.0), _fill("sell", 10, 110.0, 5, fee=2.0)])
    assert len(trades) == 1
    t = trades[0]
    assert t.ticker == "X.US"
    assert t.entry_ts.day == 1 and t.exit_ts.day == 5
    assert t.qty == 10
    assert t.return_pct == pytest.approx((1100.0 - 2.0) / (1000.0 + 1.0) - 1.0)


def test_partial_exits_are_separate_trades():
    """Ledger semantics: each (lot, sell) pair is a round trip, so a lot
    sold in two steps is two trades (the former private pairing merged
    them into one)."""
    trades = _trades(
        [_fill("buy", 10, 100.0, 1), _fill("sell", 4, 90.0, 2), _fill("sell", 6, 120.0, 3)]
    )
    assert [(t.qty, t.exit_ts.day) for t in trades] == [(4, 2), (6, 3)]
    assert [t.return_pct for t in trades] == pytest.approx([-0.1, 0.2])


def test_a_sell_spanning_lots_closes_the_older_lot_first():
    trades = _trades(
        [_fill("buy", 2, 10.0, 1), _fill("buy", 2, 20.0, 2), _fill("sell", 3, 15.0, 3)]
    )
    assert [(t.entry_ts.day, t.qty) for t in trades] == [(1, 2), (2, 1)]
    assert trades[0].return_pct == pytest.approx(0.5)
    assert trades[1].return_pct == pytest.approx(-0.25)


def test_open_lots_are_not_trades():
    trades = _trades(
        [_fill("buy", 5, 50.0, 2), _fill("sell", 5, 60.0, 3), _fill("buy", 1, 60.0, 4)]
    )
    assert len(trades) == 1
    assert trades[0].qty == 5
    assert trades[0].return_pct == pytest.approx(0.2)


def test_tickers_are_matched_independently_and_ordered_by_exit():
    trades = _trades(
        [
            _fill("buy", 1, 10.0, 1, ticker="A.US"),
            _fill("buy", 1, 10.0, 2, ticker="B.US"),
            _fill("sell", 1, 12.0, 3, ticker="B.US"),
            _fill("sell", 1, 9.0, 4, ticker="A.US"),
        ]
    )
    assert [(t.ticker, t.exit_ts.day) for t in trades] == [("B.US", 3), ("A.US", 4)]
    assert trades[0].return_pct == pytest.approx(0.2)
    assert trades[1].return_pct == pytest.approx(-0.1)


def test_no_fills_no_trades():
    assert _trades([]) == []
