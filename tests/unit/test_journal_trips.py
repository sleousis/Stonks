"""Round trips of a paper or live book (roadmap 23.3): pairing, sleeves,
excursions, R multiples and exit efficiency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from stonks.backtest.corporate_actions import CorporateActionRecord
from stonks.journal.trips import Ledger, LedgerFill, LedgerOrder, build_trades

T0 = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
NOW = datetime(2026, 3, 20, tzinfo=UTC)


def _day(i: int) -> datetime:
    return T0 + timedelta(days=i)


def _fill(
    fid, cid, side, qty, price, day, *, ticker="X.US", fee=0.0, strategy="mom", origin="strategy"
):
    return LedgerFill(
        fill_id=fid,
        client_id=cid,
        ticker=ticker,
        side=side,
        quantity=qty,
        price=price,
        fee=fee,
        filled_at=_day(day),
        strategy_id=strategy if origin == "strategy" else None,
        origin=origin,
    )


def _order(
    cid,
    side="buy",
    *,
    order_type="market",
    stop_price=None,
    protective=False,
    context=None,
    day=0,
    origin="strategy",
    ticker="X.US",
):
    return LedgerOrder(
        client_id=cid,
        ticker=ticker,
        side=side,
        order_type=order_type,
        stop_price=stop_price,
        protective=protective,
        context=context or {},
        created_at=_day(day),
        origin=origin,
    )


def _bars(rows):
    """rows: (day index, high, low, close)"""
    return pd.DataFrame(
        [
            {
                "ticker": "X.US",
                "timestamp": datetime(_day(d).year, _day(d).month, _day(d).day),
                "high": h,
                "low": lo,
                "close": c,
            }
            for d, h, lo, c in rows
        ]
    )


def _ledger(fills, orders=(), actions=()):
    by_id = {o.client_id: o for o in orders}
    for f in fills:
        by_id.setdefault(f.client_id, _order(f.client_id, f.side, origin=f.origin))
    return Ledger(fills=tuple(fills), orders=by_id, corporate_actions=tuple(actions))


def test_a_closed_long_trade_has_pnl_holding_time_excursions_and_efficiency():
    fills = [_fill(1, "b1", "buy", 10, 100.0, 0, fee=1.0), _fill(2, "s1", "sell", 10, 110.0, 3)]
    bars = _bars([(0, 101, 95, 100), (1, 104, 97, 103), (2, 115, 102, 112), (3, 112, 108, 110)])
    [t] = build_trades(_ledger(fills), bars=bars, now=NOW)
    assert (t.trade_id, t.leg_id, t.side, t.sleeve, t.origin) == (
        1,
        "1.1",
        "long",
        "mom",
        "strategy",
    )
    assert not t.is_open
    assert t.pnl == pytest.approx(99.0)
    assert t.holding_days == pytest.approx(3.0)
    assert t.mae_pct == pytest.approx(-0.05)
    assert t.mfe_pct == pytest.approx(0.15)
    # exit at +10% inside a range of -5% .. +15%: 15 of 20 points
    assert t.exit_efficiency == pytest.approx(0.75)
    assert t.r_multiple is None and t.stop_price is None
    assert t.entry_client_id == "b1" and t.exit_client_id == "s1"


def test_short_trade_efficiency_counts_the_fall_as_favourable():
    fills = [_fill(1, "s1", "sell", 5, 50.0, 0), _fill(2, "b1", "buy", 5, 45.0, 2)]
    bars = _bars([(0, 52, 49, 50), (1, 51, 44, 46), (2, 47, 44, 45)])
    [t] = build_trades(_ledger(fills), bars=bars, now=NOW)
    assert t.side == "short"
    assert t.pnl == pytest.approx(25.0)
    assert t.mae_pct == pytest.approx(-0.04)
    assert t.mfe_pct == pytest.approx(0.12)
    assert t.exit_efficiency == pytest.approx((0.10 + 0.04) / 0.16)


def test_partial_exits_are_legs_of_one_trade_and_the_rest_stays_open():
    fills = [
        _fill(7, "b1", "buy", 10, 100.0, 0),
        _fill(8, "s1", "sell", 4, 105.0, 1),
    ]
    bars = _bars([(0, 101, 99, 100), (1, 106, 100, 105), (5, 108, 104, 107)])
    closed, still_open = build_trades(_ledger(fills), bars=bars, now=NOW)
    assert (closed.leg_id, still_open.leg_id) == ("7.1", "7.2")
    assert {closed.trade_id, still_open.trade_id} == {7}
    assert still_open.is_open and still_open.quantity == pytest.approx(6)
    assert still_open.exit_price == pytest.approx(107.0)  # the last close
    assert still_open.exit_at is None and still_open.exit_efficiency is None
    assert still_open.pnl == pytest.approx(42.0)


def test_manual_trades_are_their_own_sleeve():
    fills = [
        _fill(1, "m1", "buy", 3, 10.0, 0, origin="manual"),
        _fill(2, "b1", "buy", 2, 10.0, 0),
        _fill(3, "m2", "sell", 3, 12.0, 1, origin="manual"),
        _fill(4, "s1", "sell", 2, 11.0, 1),
    ]
    trades = build_trades(_ledger(fills), bars=None, now=NOW)
    by_sleeve = {t.sleeve: t for t in trades}
    assert set(by_sleeve) == {"manual", "mom"}
    assert by_sleeve["manual"].origin == "manual"
    assert by_sleeve["manual"].pnl == pytest.approx(6.0)
    assert by_sleeve["mom"].pnl == pytest.approx(2.0)
    assert by_sleeve["manual"].exit_trigger == "manual"


def test_r_multiple_from_the_protective_stop_placed_for_the_entry():
    fills = [_fill(1, "b1", "buy", 10, 100.0, 0), _fill(2, "b1:stop", "sell", 10, 95.0, 2)]
    orders = [
        _order("b1"),
        _order(
            "b1:stop",
            "sell",
            order_type="stop",
            stop_price=95.0,
            protective=True,
            context={"trigger": "stop", "entry_client_id": "b1"},
            day=1,
        ),
    ]
    bars = _bars([(0, 101, 99, 100), (1, 100, 96, 97), (2, 97, 94, 95)])
    [t] = build_trades(_ledger(fills, orders), bars=bars, now=NOW)
    assert t.stop_price == pytest.approx(95.0)
    assert t.stop_source == "protective_stop"
    assert t.risk_amount == pytest.approx(50.0)
    assert t.r_multiple == pytest.approx(-1.0)
    assert t.mae_r == pytest.approx(-1.2)
    assert t.exit_trigger == "stop"


def test_r_multiple_from_a_plan_on_the_entry_order():
    fills = [
        _fill(1, "m1", "buy", 10, 100.0, 0, origin="manual"),
        _fill(2, "m2", "sell", 10, 104.0, 1, origin="manual"),
    ]
    orders = [
        _order(
            "m1",
            origin="manual",
            context={"trigger": "manual", "plan": {"stop": 98.0, "target": 106.0}},
        )
    ]
    [t] = build_trades(_ledger(fills, orders), bars=None, now=NOW)
    assert t.stop_source == "order_plan"
    assert t.r_multiple == pytest.approx(2.0)
    assert t.target_price == pytest.approx(106.0)


def test_a_stop_on_the_wrong_side_gives_no_r():
    fills = [
        _fill(1, "m1", "buy", 10, 100.0, 0, origin="manual"),
        _fill(2, "m2", "sell", 10, 104.0, 1, origin="manual"),
    ]
    orders = [_order("m1", origin="manual", context={"plan": {"stop": 101.0}})]
    [t] = build_trades(_ledger(fills, orders), bars=None, now=NOW)
    assert t.stop_price is None and t.r_multiple is None


def test_a_split_rescales_the_lot_and_its_excursions():
    fills = [_fill(1, "b1", "buy", 10, 100.0, 0), _fill(2, "s1", "sell", 20, 55.0, 2)]
    split = CorporateActionRecord(
        timestamp=datetime(2026, 3, 3, tzinfo=UTC),
        ticker="X.US",
        kind="split",
        value=2.0,
        quantity_before=10,
        quantity_after=20,
        cash_delta=0.0,
    )
    bars = _bars([(0, 102, 98, 100), (1, 52, 49, 50), (2, 56, 50, 55)])
    [t] = build_trades(_ledger(fills, actions=[split]), bars=bars, now=NOW)
    assert t.quantity == pytest.approx(20)
    assert t.entry_price == pytest.approx(50.0)
    assert t.pnl == pytest.approx(100.0)
    assert t.mae_pct == pytest.approx(-0.02)


def test_option_contracts_are_left_out():
    fills = [_fill(1, "o1", "buy", 1, 3.0, 0, ticker="AAPL.US:2026-06-19:C:150")]
    assert build_trades(_ledger(fills), bars=None, now=NOW) == []
