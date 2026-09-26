"""Unit tests for the round-trip trade ledger and trade statistics (BL-02)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from stonks.backtest.corporate_actions import CorporateActionRecord
from stonks.backtest.report import compute_report
from stonks.backtest.trades import (
    RoundTrip,
    TradeStats,
    build_round_trips,
    compute_trade_stats,
    with_trades,
)
from stonks.core.types import Fill

T0 = datetime(2025, 1, 1, tzinfo=UTC)
DAYS = [T0 + timedelta(days=i) for i in range(10)]


def _fill(side, qty, price, day, *, ticker="X.US", fee=0.0, strategy="0", tag=""):
    return Fill(
        order_client_id=f"{strategy}:{side}:{ticker}:{day}{tag}",
        ticker=ticker,
        quantity=qty,
        price=price,
        fee=fee,
        filled_at=DAYS[day],
        side=side,
    )


def _closed(trades):
    return [t for t in trades if not t.is_open]


# ---- pairing -----------------------------------------------------------------


def test_buy_then_sell_is_one_round_trip_with_fees_in_pnl():
    trades = build_round_trips(
        [_fill("buy", 10, 100.0, 1, fee=1.0), _fill("sell", 10, 110.0, 5, fee=2.0)],
        timeline=DAYS,
    )
    assert len(trades) == 1
    t = trades[0]
    assert (t.ticker, t.strategy_key) == ("X.US", "0")
    assert (t.entry_ts, t.exit_ts) == (DAYS[1], DAYS[5])
    assert (t.qty, t.entry_px, t.exit_px) == (10, 100.0, 110.0)
    assert t.fees == pytest.approx(3.0)
    assert t.pnl == pytest.approx(100.0 - 3.0)
    assert t.return_pct == pytest.approx(97.0 / 1001.0)
    assert t.bars_held == 4
    assert t.is_open is False
    assert t.mae_pct is None and t.mfe_pct is None


def test_fifo_with_scale_in_and_partial_exit():
    fills = [
        _fill("buy", 10, 100.0, 0, fee=1.0),
        _fill("buy", 10, 120.0, 1, fee=1.0),
        _fill("sell", 15, 130.0, 3, fee=3.0),
        _fill("sell", 5, 90.0, 4),
    ]
    trades = build_round_trips(fills, timeline=DAYS)
    assert [(t.entry_px, t.exit_px, t.qty) for t in trades] == [
        (100.0, 130.0, 10),
        (120.0, 130.0, 5),
        (120.0, 90.0, 5),
    ]
    first, second, third = trades
    # the 15-share sell fee splits 10/15 and 5/15; the second lot's buy fee halves
    assert first.fees == pytest.approx(1.0 + 2.0)
    assert first.pnl == pytest.approx(300.0 - 3.0)
    assert second.fees == pytest.approx(0.5 + 1.0)
    assert second.pnl == pytest.approx(50.0 - 1.5)
    assert third.fees == pytest.approx(0.5)
    assert third.pnl == pytest.approx(-150.0 - 0.5)
    assert [t.bars_held for t in trades] == [3, 2, 3]


def test_open_lot_is_flagged_and_marked_at_the_last_close():
    bars = pd.DataFrame(
        {
            "ticker": "X.US",
            "timestamp": DAYS[:4],
            "high": [101.0, 104.0, 106.0, 108.0],
            "low": [99.0, 95.0, 100.0, 104.0],
            "close": [100.0, 100.0, 105.0, 107.0],
        }
    )
    trades = build_round_trips([_fill("buy", 10, 100.0, 1)], timeline=DAYS[:4], bars=bars)
    (t,) = trades
    assert t.is_open is True
    assert t.exit_ts == DAYS[3]
    assert t.exit_px == 107.0
    assert t.pnl == pytest.approx(70.0)
    assert t.bars_held == 3  # held at the close of bars 1, 2 and 3
    assert t.mae_pct == pytest.approx(-0.05)
    assert t.mfe_pct == pytest.approx(0.08)


def test_open_lot_without_bars_is_marked_at_the_given_marks():
    (t,) = build_round_trips([_fill("buy", 10, 100.0, 1)], timeline=DAYS[:3], marks={"X.US": 90.0})
    assert t.is_open and t.exit_px == 90.0 and t.pnl == pytest.approx(-100.0)
    assert t.exit_ts == DAYS[2]


def test_mae_and_mfe_are_bounded_by_zero():
    bars = pd.DataFrame(
        {
            "ticker": "X.US",
            "timestamp": DAYS[:3],
            "high": [100.0, 100.0, 100.0],
            "low": [100.0, 100.0, 100.0],
            "close": [100.0, 100.0, 100.0],
        }
    )
    (t,) = build_round_trips(
        [_fill("buy", 1, 100.0, 0), _fill("sell", 1, 100.0, 2)], timeline=DAYS, bars=bars
    )
    assert t.mae_pct == 0.0 and t.mfe_pct == 0.0


def test_two_strategy_instances_keep_separate_ledgers():
    fills = [
        _fill("buy", 10, 100.0, 0, strategy="0"),
        _fill("buy", 5, 101.0, 1, strategy="1"),
        _fill("sell", 5, 110.0, 2, strategy="1"),
        _fill("sell", 10, 90.0, 3, strategy="0"),
    ]
    trades = build_round_trips(fills, timeline=DAYS)
    by_key = {t.strategy_key: t for t in trades}
    assert by_key["0"].entry_px == 100.0 and by_key["0"].exit_px == 90.0
    assert by_key["1"].entry_px == 101.0 and by_key["1"].exit_px == 110.0


def test_client_ids_without_a_prefix_share_one_key():
    fill = Fill("abc", "X.US", 1.0, 10.0, 0.0, DAYS[0], "buy")
    (t,) = build_round_trips([fill], timeline=DAYS[:1], marks={"X.US": 10.0})
    assert t.strategy_key == ""


def test_slippage_cost_uses_the_reference_price():
    fills = [_fill("buy", 10, 101.0, 0), _fill("sell", 10, 108.0, 2)]
    refs = {fills[0].order_client_id: 100.0, fills[1].order_client_id: 110.0}
    (t,) = build_round_trips(fills, timeline=DAYS, reference_price=refs.get)
    assert t.slippage_cost == pytest.approx(10.0 + 20.0)
    assert t.pnl == pytest.approx(70.0)  # already inside the fill prices


def test_split_rescales_the_open_lot():
    split = CorporateActionRecord(DAYS[2], "X.US", "split", 4.0, 10.0, 40.0, 0.0)
    fills = [_fill("buy", 10, 400.0, 0, fee=4.0), _fill("sell", 40, 110.0, 3, fee=1.0)]
    (t,) = build_round_trips(fills, timeline=DAYS, corporate_actions=[split])
    assert t.qty == pytest.approx(40.0)
    assert t.entry_px == pytest.approx(100.0)
    assert t.pnl == pytest.approx(400.0 - 5.0)
    assert t.return_pct == pytest.approx(395.0 / 4004.0)


def test_split_adjusts_mae_and_mfe_bars():
    split = CorporateActionRecord(DAYS[2], "X.US", "split", 4.0, 10.0, 40.0, 0.0)
    bars = pd.DataFrame(
        {
            "ticker": "X.US",
            "timestamp": DAYS[:4],
            "high": [400.0, 440.0, 105.0, 110.0],
            "low": [400.0, 380.0, 95.0, 100.0],
            "close": [400.0, 400.0, 100.0, 105.0],
        }
    )
    fills = [_fill("buy", 10, 400.0, 0), _fill("sell", 40, 105.0, 3)]
    (t,) = build_round_trips(fills, timeline=DAYS, corporate_actions=[split], bars=bars)
    assert t.mae_pct == pytest.approx(-0.05)
    assert t.mfe_pct == pytest.approx(0.10)


def test_dividends_are_credited_to_open_lots_per_share():
    div = CorporateActionRecord(DAYS[2], "X.US", "dividend", 2.0, 15.0, 15.0, 30.0)
    fills = [
        _fill("buy", 10, 100.0, 0, strategy="0"),
        _fill("buy", 5, 100.0, 1, strategy="1"),
        _fill("sell", 10, 100.0, 3, strategy="0"),
        _fill("sell", 5, 100.0, 3, strategy="1"),
    ]
    trades = build_round_trips(fills, timeline=DAYS, corporate_actions=[div])
    by_key = {t.strategy_key: t for t in trades}
    assert by_key["0"].dividends == pytest.approx(20.0)
    assert by_key["0"].pnl == pytest.approx(20.0)
    assert by_key["1"].dividends == pytest.approx(10.0)


def test_corporate_action_on_a_fill_bar_applies_before_the_fill():
    # a buy at the ex-date open gets no dividend
    div = CorporateActionRecord(DAYS[1], "X.US", "dividend", 2.0, 5.0, 5.0, 10.0)
    fills = [
        _fill("buy", 5, 100.0, 0, tag="a"),
        _fill("buy", 5, 100.0, 1, tag="b"),
        _fill("sell", 10, 100.0, 2),
    ]
    first, second = build_round_trips(fills, timeline=DAYS, corporate_actions=[div])
    assert first.dividends == pytest.approx(10.0)
    assert second.dividends == 0.0


def test_naive_and_aware_timestamps_mix():
    naive_days = [d.replace(tzinfo=None) for d in DAYS]
    (t,) = build_round_trips(
        [_fill("buy", 1, 10.0, 0), _fill("sell", 1, 11.0, 2)], timeline=naive_days
    )
    assert t.bars_held == 2


# ---- statistics ----------------------------------------------------------------


def _trip(pnl, bars=1, is_open=False):
    return RoundTrip(
        ticker="X.US",
        strategy_key="0",
        entry_ts=DAYS[0],
        exit_ts=DAYS[bars],
        qty=1.0,
        entry_px=100.0,
        exit_px=100.0 + pnl,
        pnl=pnl,
        return_pct=pnl / 100.0,
        bars_held=bars,
        fees=0.0,
        slippage_cost=0.0,
        mae_pct=None,
        mfe_pct=None,
        is_open=is_open,
    )


def test_hand_computed_expectancy_and_profit_factor():
    trades = [_trip(30.0, 2), _trip(10.0, 4), _trip(-20.0, 1), _trip(99.0, 1, is_open=True)]
    stats = compute_trade_stats(trades)
    assert stats.n_trades == 3
    assert stats.n_open == 1
    assert stats.win_rate == pytest.approx(2 / 3)
    assert stats.avg_win == pytest.approx(20.0)
    assert stats.avg_loss == pytest.approx(-20.0)
    assert stats.payoff_ratio == pytest.approx(1.0)
    assert stats.expectancy == pytest.approx(2 / 3 * 20.0 + 1 / 3 * -20.0)
    assert stats.trade_profit_factor == pytest.approx(40.0 / 20.0)
    assert stats.avg_bars_held == pytest.approx(7 / 3)


def test_zero_trades_give_zero_stats_without_division_errors():
    stats = compute_trade_stats([])
    assert stats == TradeStats()
    assert stats.n_trades == 0 and stats.win_rate == 0.0 and stats.expectancy == 0.0


def test_all_winners_have_infinite_payoff_and_profit_factor():
    stats = compute_trade_stats([_trip(5.0), _trip(15.0)])
    assert stats.payoff_ratio == math.inf
    assert stats.trade_profit_factor == math.inf
    assert stats.avg_loss == 0.0


def test_turnover_costs_and_exposure_on_a_two_trade_fixture():
    # one year of 5 daily marks; equity 1000 flat; two round trips of 500 notional
    dates = [T0 + timedelta(days=365.25 * i / 4) for i in range(5)]
    curve = [1000.0] * 5
    fills = [
        Fill("0:a", "X.US", 5.0, 100.0, 1.0, dates[0], "buy"),
        Fill("0:b", "X.US", 5.0, 100.0, 1.0, dates[1], "sell"),
        Fill("0:c", "X.US", 5.0, 100.0, 1.0, dates[2], "buy"),
        Fill("0:d", "X.US", 5.0, 100.0, 1.0, dates[3], "sell"),
    ]
    refs = {"0:a": 99.0, "0:b": 101.0, "0:c": 100.0, "0:d": 100.0}
    report = with_trades(compute_report("s", dates, curve), fills, reference_price=refs.get)
    stats = report.trade_stats
    assert stats.n_trades == 2
    assert stats.turnover_annual == pytest.approx(2000.0 / 1000.0 / 1.0)
    assert stats.costs_paid == pytest.approx(4.0 + 5.0 + 5.0)
    assert stats.cost_drag_annual == pytest.approx(14.0 / 1000.0)
    assert stats.exposure == pytest.approx(2 / 5)
    assert report.fitness == pytest.approx(0.0)  # flat curve: sharpe 0
    assert len(report.trades) == 2


def test_with_trades_computes_fitness_from_turnover():
    dates = [T0 + timedelta(days=i) for i in range(6)]
    curve = [100.0, 101.0, 100.5, 102.0, 101.0, 103.0]
    base = compute_report("s", dates, curve)
    fills = [Fill("0:a", "X.US", 1.0, 100.0, 0.0, dates[0], "buy")]
    report = with_trades(base, fills, marks={"X.US": 103.0})
    turnover_daily = report.trade_stats.turnover_annual / 252
    expected = base.sharpe * math.sqrt(abs(base.cagr) / max(turnover_daily, 0.125))
    assert report.fitness == pytest.approx(expected)
    assert report.trade_stats.exposure == 1.0


def test_with_trades_on_no_fills_keeps_zero_stats():
    dates = [T0 + timedelta(days=i) for i in range(3)]
    report = with_trades(compute_report("s", dates, [1.0, 1.0, 1.0]), [])
    assert report.trades == ()
    assert report.trade_stats.n_trades == 0
    assert report.trade_stats.turnover_annual == 0.0
    assert report.fitness == 0.0
