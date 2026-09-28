"""The rebalancing planner (roadmap 23.16): whole shares, sells first, buys
cut to the cash, costs, turnover and a tax preview."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings, FixedCostModel
from stonks.portfolio.planner import PlanError, plan_rebalance, tax_preview
from stonks.tax.lots import OpenLot

DAY = date(2026, 9, 28)


def lot(qty: float, per_share: float, acquired: date) -> OpenLot:
    return OpenLot(ticker="A", kind="long", quantity=qty, acquired=acquired, per_share=per_share,
                   wash_sale_adjustment=0.0, currency="USD", open_fill_id=1)  # fmt: skip


def test_from_cash_to_targets_in_whole_shares():
    plan = plan_rebalance(as_of=DAY, cash=10_000, positions={}, prices={"A": 33.0, "B": 70.0},
                          targets={"A": 0.5, "B": 0.5})  # fmt: skip
    trades = {line.ticker: (line.side, line.quantity) for line in plan.trades}
    assert trades == {"A": ("buy", 151.0), "B": ("buy", 71.0)}
    assert plan.turnover == pytest.approx((151 * 33 + 71 * 70) / 10_000)
    assert plan.cash_after == pytest.approx(10_000 - 151 * 33 - 71 * 70)
    assert plan.total_cost == 0
    assert plan.max_drift_after < 0.01


def test_a_held_ticker_not_targeted_is_sold_in_full_and_sells_come_first():
    plan = plan_rebalance(as_of=DAY, cash=0, positions={"A": 10.5, "B": 5},
                          prices={"A": 100.0, "B": 100.0}, targets={"B": 1.0})  # fmt: skip
    first, second = plan.trades
    assert (first.ticker, first.side, first.quantity) == ("A", "sell", 10.5)
    assert (second.ticker, second.side, second.quantity) == ("B", "buy", 10.0)


def test_buys_are_cut_to_fit_the_cash_after_costs():
    costs = FixedCostModel(fee_per_trade=5.0)
    plan = plan_rebalance(as_of=DAY, cash=1_000, positions={}, prices={"A": 100.0},
                          targets={"A": 1.0}, cost_model=costs)  # fmt: skip
    [line] = plan.trades
    assert line.quantity == 9  # 10 shares plus the fee do not fit
    assert line.cost == pytest.approx(5.0)
    assert line.cost_bps == pytest.approx(5.0 / 900 * 10_000)
    assert plan.cash_after == pytest.approx(1_000 - 900 - 5)
    assert "cut" in plan.notes[0]


def test_small_trades_and_dear_shares_are_skipped_with_a_reason():
    plan = plan_rebalance(as_of=DAY, cash=1_000, positions={"A": 5}, prices={"A": 100.0, "B": 2_000.0},
                          targets={"A": 0.52, "B": 0.4}, min_trade_value=250)  # fmt: skip
    by = {line.ticker: line for line in plan.lines}
    assert by["A"].skipped == "below_min_trade"
    assert by["B"].skipped == "below_one_share"
    assert plan.trades == ()


def test_a_ticker_without_a_price_is_left_alone():
    plan = plan_rebalance(as_of=DAY, cash=1_000, positions={"X": 3}, prices={},
                          targets={})  # fmt: skip
    [line] = plan.lines
    assert line.skipped == "no_price" and not line.trades
    assert "no price for X" in plan.notes[0]


def test_asset_class_costs_are_in_the_plan():
    model = CostModelSettings(default=AssetClassCosts(half_spread_bps=10.0)).build()
    plan = plan_rebalance(as_of=DAY, cash=10_000, positions={}, prices={"A": 100.0},
                          targets={"A": 0.5}, cost_model=model)  # fmt: skip
    [line] = plan.trades
    assert line.cost_bps == pytest.approx(10.0)


@pytest.mark.parametrize(
    "targets",
    [{"A": -0.1}, {"A": 0.7, "B": 0.5}, {"A": float("nan")}],
)
def test_bad_targets_are_refused(targets):
    with pytest.raises(PlanError):
        plan_rebalance(as_of=DAY, cash=1_000, positions={}, prices={"A": 1.0, "B": 1.0},
                       targets=targets)  # fmt: skip


def test_short_books_and_empty_books_are_refused():
    with pytest.raises(PlanError, match="short"):
        plan_rebalance(as_of=DAY, cash=1_000, positions={"A": -1}, prices={"A": 1.0}, targets={})
    with pytest.raises(PlanError, match="no value"):
        plan_rebalance(as_of=DAY, cash=0, positions={}, prices={}, targets={})


def test_tax_preview_closes_the_oldest_lots_first():
    lots = [lot(5, 50.0, date(2025, 1, 2)), lot(10, 90.0, date(2026, 6, 1))]
    preview = tax_preview(lots, 8, 100.0, DAY, {"short": 0.3, "long": 0.15})
    assert [s.quantity for s in preview.lots] == [5, 3]
    assert [s.holding_period for s in preview.lots] == ["long", "short"]
    assert preview.long_term_gain == pytest.approx(250.0)
    assert preview.short_term_gain == pytest.approx(30.0)
    assert preview.estimated_tax == pytest.approx(250 * 0.15 + 30 * 0.3)
    assert tax_preview(lots, 1, 100.0, DAY).estimated_tax is None


def test_a_sell_line_carries_its_tax_preview():
    plan = plan_rebalance(as_of=DAY, cash=0, positions={"A": 10}, prices={"A": 100.0},
                          targets={}, lots={"A": [lot(10, 80.0, date(2026, 1, 5))]},
                          tax_rates={"short": 0.25, "long": 0.1})  # fmt: skip
    [line] = plan.trades
    assert line.tax is not None and line.tax.gain == pytest.approx(200.0)
    assert plan.tax_total == pytest.approx(50.0)
