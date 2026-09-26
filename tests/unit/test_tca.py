"""Implementation shortfall by hand (BL-32, P22): decision vs arrival vs
fill, split into delay, impact and fees, plus the opportunity cost of what
did not fill and the gap to the backtest's fill convention."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings, FixedCostModel, Trade
from stonks.core.types import Fill, Order
from stonks.production.tca import (
    FillLeg,
    OrderTca,
    annotate_orders,
    backtest_shortfalls,
    compute_shortfall,
    expected_cost_bps,
    summarize,
)


def test_buy_shortfall_by_hand():
    # decided at 100, market at 101 when it arrived, filled at 101.5, fee 2
    s = compute_shortfall(
        "buy", 100.0, 10.0, [FillLeg(quantity=10.0, price=101.5, fee=2.0, arrival_price=101.0)]
    )
    # notional at decision: 10 * 100 = 1000
    assert s.delay_bps == pytest.approx(100.0)  # (101 - 100) * 10 / 1000
    assert s.impact_bps == pytest.approx(50.0)  # (101.5 - 101) * 10 / 1000
    assert s.fee_bps == pytest.approx(20.0)  # 2 / 1000
    assert s.is_bps == pytest.approx(170.0)
    assert s.is_cost == pytest.approx(17.0)
    assert s.opportunity_cost == 0.0
    assert s.total_bps == pytest.approx(170.0)


def test_sell_shortfall_by_hand():
    # a sell decided at 50 that arrived at 49 and filled at 48.9 lost on both legs
    s = compute_shortfall(
        "sell", 50.0, 4.0, [FillLeg(quantity=4.0, price=48.9, fee=0.0, arrival_price=49.0)]
    )
    assert s.delay_bps == pytest.approx(200.0)  # (50 - 49) * 4 / 200
    assert s.impact_bps == pytest.approx(20.0)  # (49 - 48.9) * 4 / 200
    assert s.is_bps == pytest.approx(220.0)


def test_a_favourable_move_is_a_negative_cost():
    s = compute_shortfall("buy", 100.0, 1.0, [FillLeg(1.0, 99.0, 0.0, 99.0)])
    assert s.delay_bps == pytest.approx(-100.0)
    assert s.is_bps == pytest.approx(-100.0)


def test_unknown_arrival_keeps_the_total_but_not_the_split():
    s = compute_shortfall("buy", 100.0, 10.0, [FillLeg(10.0, 101.0, 0.0)])
    assert s.arrival_price is None
    assert s.delay_bps is None and s.impact_bps is None
    assert s.is_bps == pytest.approx(100.0)


def test_order_level_arrival_fills_in_for_the_legs():
    s = compute_shortfall("buy", 100.0, 10.0, [FillLeg(10.0, 101.0, 0.0)], arrival_price=100.5)
    assert s.delay_bps == pytest.approx(50.0)
    assert s.impact_bps == pytest.approx(50.0)


def test_partial_fills_use_the_volume_weighted_price():
    s = compute_shortfall(
        "buy",
        100.0,
        10.0,
        [FillLeg(4.0, 101.0, 1.0, 100.0), FillLeg(6.0, 102.0, 1.0, 100.0)],
    )
    assert s.filled_quantity == 10.0
    assert s.fill_price == pytest.approx(101.6)
    assert s.impact_bps == pytest.approx(160.0)
    assert s.fee_bps == pytest.approx(20.0)


def test_opportunity_cost_of_a_rejected_order():
    # nothing filled; the price then ran from 100 to 105 before the next close
    s = compute_shortfall("buy", 100.0, 10.0, [], post_close_price=105.0)
    assert s.filled_quantity == 0.0
    assert s.is_bps is None
    assert s.opportunity_cost == pytest.approx(50.0)
    assert s.opportunity_bps == pytest.approx(500.0)
    assert s.total_bps == pytest.approx(500.0)


def test_opportunity_cost_waits_for_the_next_close():
    s = compute_shortfall("sell", 100.0, 10.0, [FillLeg(5.0, 100.0, 0.0, 100.0)])
    assert s.opportunity_cost is None
    assert s.total_bps is None


def test_convention_gap_against_the_backtests_next_open():
    # the tick filled at the close (arrival 100); a backtest would have
    # entered at the next open (99): live paid 100 bps more on the entry
    s = compute_shortfall(
        "buy", 100.0, 1.0, [FillLeg(1.0, 100.0, 0.0, 100.0)], benchmark_price=99.0
    )
    assert s.convention_bps == pytest.approx(100.0)


def test_expected_cost_from_the_cost_model():
    model = CostModelSettings(default=AssetClassCosts(half_spread_bps=5.0, fee_bps=1.0)).build()
    trade = Trade(ticker="X", side="buy", quantity=10.0, price=100.0)
    # 5 bps spread + 1 bps fee on the fill notional (100.05 * 10)
    assert expected_cost_bps(model, trade) == pytest.approx(5.0 + 1.0 * 100.05 / 100.0)
    fixed = FixedCostModel(slippage_bps=10.0, fee_per_trade=1.0)
    assert expected_cost_bps(fixed, trade) == pytest.approx(10.0 + 1.0 / 1000.0 * 1e4)


def _row(sid, ticker, portfolio, day, is_price, expected=None, qty=10.0):
    shortfall = compute_shortfall(
        "buy", 100.0, qty, [FillLeg(qty, is_price, 0.0, 100.0)], expected_bps=expected
    )
    return OrderTca(
        client_id=f"{day}:{sid}:{ticker}",
        portfolio_id=portfolio,
        strategy_id=sid,
        ticker=ticker,
        side="buy",
        status="filled",
        decided_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        shortfall=shortfall,
    )


def test_summaries_weight_by_notional_and_group():
    rows = [
        _row("a", "X", "pf", date(2026, 3, 2), 101.0, expected=40.0, qty=10.0),
        _row("a", "Y", "pf", date(2026, 3, 3), 100.5, expected=20.0, qty=30.0),
        _row("b", "X", "pf", date(2026, 4, 1), 100.0, qty=10.0),
    ]
    [total] = summarize(rows, "all")
    assert total.orders == 3
    # (100 bps * 1000 + 50 bps * 3000 + 0 * 1000) / 5000
    assert total.is_bps == pytest.approx(50.0)
    by_strategy = {g.key: g for g in summarize(rows, "strategy")}
    assert by_strategy["a"].is_bps == pytest.approx(62.5)
    assert by_strategy["a"].expected_bps == pytest.approx(25.0)
    assert by_strategy["a"].model_gap_bps == pytest.approx(62.5 - 25.0)
    assert by_strategy["b"].expected_bps is None
    assert [g.key for g in summarize(rows, "month")] == ["2026-03", "2026-04"]
    assert {g.key for g in summarize(rows, "ticker")} == {"X", "Y"}
    assert [g.key for g in summarize(rows, "week")] == ["2026-W10", "2026-W14"]


def test_annotate_orders_records_the_decision():
    orders = [
        Order(client_id="c1", ticker="X", side="buy", quantity=10.0, strategy_id="mom"),
        Order(client_id="c2", ticker="Y", side="sell", quantity=5.0),
    ]
    decided = datetime(2026, 3, 20, tzinfo=UTC)
    out = annotate_orders(
        orders,
        decided_at=decided,
        prices={"X": 100.0, "Y": 50.0},
        signals={"mom": {"X": 0.03, "Z": 0.05}},
        cost_model=FixedCostModel(slippage_bps=10.0),
        constructor="single_winner",
        exit_only=False,
        target_weights={"X": 0.5},
    )
    x, y = out
    assert x.decision_price == 100.0 and x.decided_at == decided
    assert x.expected_cost_bps == pytest.approx(10.0)
    assert x.decision_context == {
        "trigger": "signal",
        "strategy_id": "mom",
        "score": 0.03,
        "rank": 2,
        "candidates": 2,
        "constructor": "single_winner",
        "target_weight": 0.5,
    }
    # an order no strategy made came from a risk rule
    assert y.decision_context["trigger"] == "risk_rule"
    assert y.decision_price == 50.0


def test_annotate_marks_exits_without_picks():
    [o] = annotate_orders(
        [Order(client_id="c", ticker="X", side="sell", quantity=1.0, strategy_id="mom")],
        decided_at=datetime(2026, 3, 20, tzinfo=UTC),
        prices={"X": 10.0},
        signals={},
        exit_only=True,
    )
    assert o.decision_context["trigger"] == "exit_no_pick"
    assert o.expected_cost_bps is None


def test_backtest_shortfalls_use_the_same_math():
    ts = datetime(2026, 3, 21, tzinfo=UTC)
    fills = [
        Fill("o1", "X", 10.0, 101.5, 2.0, ts, "buy"),
        Fill("o1~2", "X", 5.0, 102.0, 1.0, ts, "buy"),
        Fill("o2", "X", 10.0, 99.0, 0.0, ts, "sell"),
    ]
    reference = {"o1": 101.0, "o1~2": 101.0, "o2": 99.5}
    rows = backtest_shortfalls(fills, {"o1": 100.0, "o2": 100.0}, reference.get)
    assert [r.client_id for r in rows] == ["o1", "o2"]
    live = compute_shortfall(
        "buy", 100.0, 15.0, [FillLeg(10.0, 101.5, 2.0, 101.0), FillLeg(5.0, 102.0, 1.0, 101.0)]
    )
    assert rows[0].shortfall == live
    assert rows[1].shortfall.delay_bps == pytest.approx(50.0)
