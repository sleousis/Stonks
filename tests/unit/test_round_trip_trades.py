"""Round-trip trade reconstruction from fills (long-only, FIFO lots)."""

from __future__ import annotations

from datetime import datetime

import pytest

from stonks.core.types import Fill
from stonks.lab.survival.runs_test import round_trip_trades


def _fill(side, qty, price, day, ticker="X.US", fee=0.0):
    return Fill(
        order_client_id=f"{side}:{ticker}:{day}:{qty}",
        ticker=ticker,
        quantity=qty,
        price=price,
        fee=fee,
        filled_at=datetime(2025, 1, day),
        side=side,
    )


def test_buy_then_sell_is_one_trade_with_fees_in_the_return():
    trades = round_trip_trades(
        [_fill("buy", 10, 100.0, 1, fee=1.0), _fill("sell", 10, 110.0, 5, fee=2.0)]
    )
    assert len(trades) == 1
    t = trades[0]
    assert t.ticker == "X.US"
    assert t.entry_at == datetime(2025, 1, 1) and t.exit_at == datetime(2025, 1, 5)
    assert t.quantity == 10
    assert t.return_pct == pytest.approx((1100.0 - 2.0) / (1000.0 + 1.0) - 1.0)


def test_partial_exits_accumulate_into_one_trade():
    trades = round_trip_trades(
        [_fill("buy", 10, 100.0, 1), _fill("sell", 4, 90.0, 2), _fill("sell", 6, 120.0, 3)]
    )
    assert len(trades) == 1
    assert trades[0].exit_at == datetime(2025, 1, 3)
    assert trades[0].return_pct == pytest.approx((4 * 90.0 + 6 * 120.0) / 1000.0 - 1.0)


def test_sells_close_lots_first_in_first_out():
    trades = round_trip_trades(
        [
            _fill("buy", 10, 100.0, 1, fee=1.0),
            _fill("sell", 4, 110.0, 2, fee=0.4),
            _fill("buy", 5, 200.0, 3, fee=1.0),
            _fill("sell", 11, 150.0, 4, fee=1.1),
        ]
    )
    assert [t.entry_at.day for t in trades] == [1, 3]
    first, second = trades
    # lot 1: 4 @110 then 6 @150; sell fees pro rata to quantity sold
    assert first.return_pct == pytest.approx((440.0 - 0.4 + 900.0 - 0.6) / 1001.0 - 1.0)
    # lot 2: 5 @150
    assert second.return_pct == pytest.approx((750.0 - 0.5) / 1001.0 - 1.0)


def test_a_sell_spanning_lots_closes_the_older_lot_first():
    trades = round_trip_trades(
        [_fill("buy", 2, 10.0, 1), _fill("buy", 2, 20.0, 2), _fill("sell", 3, 15.0, 3)]
    )
    assert len(trades) == 1  # second lot still has 1 open
    assert trades[0].entry_at.day == 1
    assert trades[0].return_pct == pytest.approx(30.0 / 20.0 - 1.0)


def test_open_lots_and_unmatched_sells_are_not_trades():
    trades = round_trip_trades(
        [
            _fill("sell", 3, 50.0, 1),  # nothing to close (long-only)
            _fill("buy", 5, 50.0, 2),
            _fill("sell", 7, 60.0, 3),  # 2 more than held: ignored
            _fill("buy", 1, 60.0, 4),  # still open at the end
        ]
    )
    assert len(trades) == 1
    assert trades[0].quantity == 5
    assert trades[0].return_pct == pytest.approx(0.2)


def test_tickers_are_matched_independently_and_ordered_by_exit():
    trades = round_trip_trades(
        [
            _fill("buy", 1, 10.0, 1, ticker="A.US"),
            _fill("buy", 1, 10.0, 2, ticker="B.US"),
            _fill("sell", 1, 12.0, 3, ticker="B.US"),
            _fill("sell", 1, 9.0, 4, ticker="A.US"),
        ]
    )
    assert [(t.ticker, t.exit_at.day) for t in trades] == [("B.US", 3), ("A.US", 4)]
    assert trades[0].return_pct == pytest.approx(0.2)
    assert trades[1].return_pct == pytest.approx(-0.1)


def test_float_dust_does_not_leave_a_phantom_open_lot():
    trades = round_trip_trades(
        [_fill("buy", 0.1 + 0.2, 10.0, 1), _fill("sell", 0.3, 11.0, 2), _fill("buy", 1, 10.0, 3)]
    )
    assert len(trades) == 1
    # the next buy opens a new lot rather than topping up a dust remainder
    trades = round_trip_trades(
        [
            _fill("buy", 0.1 + 0.2, 10.0, 1),
            _fill("sell", 0.3, 11.0, 2),
            _fill("buy", 1, 10.0, 3),
            _fill("sell", 1, 12.0, 4),
        ]
    )
    assert [t.entry_at.day for t in trades] == [1, 3]


def test_no_fills_no_trades():
    assert round_trip_trades([]) == []
