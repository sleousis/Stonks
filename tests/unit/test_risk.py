"""Unit tests for the production risk layer (roadmap 2.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stonks.core.types import Order, Portfolio
from stonks.production.risk import RiskPolicy, apply_risk

PRICES = {"A.US": 10.0, "B.US": 20.0, "C.US": 50.0, "BTC-USD.CC": 100.0}
CLASSES = {"A.US": "equity", "B.US": "equity", "C.US": "equity", "BTC-USD.CC": "crypto"}


def _buy(ticker: str, qty: float) -> Order:
    return Order(client_id=f"b:{ticker}", ticker=ticker, side="buy", quantity=qty)


def _sell(ticker: str, qty: float) -> Order:
    return Order(client_id=f"s:{ticker}", ticker=ticker, side="sell", quantity=qty)


def test_default_policy_passes_orders_through_unchanged():
    orders = [_buy("A.US", 100)]
    result = apply_risk(orders, Portfolio(cash=10_000.0), PRICES, CLASSES, RiskPolicy())
    assert result.orders == orders
    assert result.adjustments == []


def test_max_weight_per_ticker_clips_buy():
    policy = RiskPolicy(max_weight_per_ticker=0.25)
    result = apply_risk([_buy("A.US", 1000)], Portfolio(cash=10_000.0), PRICES, CLASSES, policy)
    assert len(result.orders) == 1
    assert result.orders[0].quantity == pytest.approx(250.0)  # 2500 / 10
    [adj] = result.adjustments
    assert adj.rule == "max_weight_per_ticker"
    assert adj.original_quantity == 1000
    assert adj.adjusted_quantity == pytest.approx(250.0)


def test_max_weight_per_ticker_counts_existing_position():
    # equity = 5000 cash + 250*10 = 7500 ; cap 0.5 → 3750 ; already 2500 held → room 1250
    policy = RiskPolicy(max_weight_per_ticker=0.5)
    pf = Portfolio(cash=5_000.0, positions={"A.US": 250.0})
    result = apply_risk([_buy("A.US", 1000)], pf, PRICES, CLASSES, policy)
    assert result.orders[0].quantity == pytest.approx(125.0)


def test_ticker_already_over_cap_drops_buy():
    policy = RiskPolicy(max_weight_per_ticker=0.1)
    pf = Portfolio(cash=5_000.0, positions={"A.US": 500.0})
    result = apply_risk([_buy("A.US", 10)], pf, PRICES, CLASSES, policy)
    assert result.orders == []
    assert result.adjustments[0].adjusted_quantity == 0.0
    assert result.adjustments[0].rule == "max_weight_per_ticker"


def test_max_open_positions_drops_new_positions_but_allows_adding():
    policy = RiskPolicy(max_open_positions=1)
    pf = Portfolio(cash=5_000.0, positions={"A.US": 10.0})
    result = apply_risk([_buy("B.US", 10), _buy("A.US", 10)], pf, PRICES, CLASSES, policy)
    assert [o.ticker for o in result.orders] == ["A.US"]
    assert result.adjustments[0].ticker == "B.US"
    assert result.adjustments[0].rule == "max_open_positions"


def test_sell_frees_a_position_slot_for_a_buy():
    policy = RiskPolicy(max_open_positions=1)
    pf = Portfolio(cash=5_000.0, positions={"A.US": 10.0})
    result = apply_risk([_buy("B.US", 10), _sell("A.US", 10)], pf, PRICES, CLASSES, policy)
    # sells are placed first so their proceeds/slots are real before buys
    assert [(o.side, o.ticker) for o in result.orders] == [("sell", "A.US"), ("buy", "B.US")]
    assert result.adjustments == []


def test_max_weight_per_asset_class_clips_buy():
    policy = RiskPolicy(max_weight_per_asset_class={"crypto": 0.1})
    result = apply_risk(
        [_buy("BTC-USD.CC", 50), _buy("A.US", 10)],
        Portfolio(cash=10_000.0),
        PRICES,
        CLASSES,
        policy,
    )
    by_ticker = {o.ticker: o.quantity for o in result.orders}
    assert by_ticker["BTC-USD.CC"] == pytest.approx(10.0)  # 1000 / 100
    assert by_ticker["A.US"] == 10
    assert result.adjustments[0].rule == "max_weight_per_asset_class"


def test_asset_class_cap_blocks_buy_of_unknown_class():
    policy = RiskPolicy(max_weight_per_asset_class={"crypto": 0.1})
    prices = {**PRICES, "X.US": 10.0}
    result = apply_risk([_buy("X.US", 10)], Portfolio(cash=10_000.0), prices, CLASSES, policy)
    assert result.orders == []
    assert result.adjustments[0].rule == "unknown_asset_class"


def test_asset_class_cap_blocks_buy_when_a_holding_in_the_class_has_no_price():
    # ETH is held but unpriced: its value is unknown, so the class exposure
    # can't be bounded and a crypto buy could breach the cap.
    policy = RiskPolicy(max_weight_per_asset_class={"crypto": 0.5})
    classes = {**CLASSES, "ETH-USD.CC": "crypto"}
    pf = Portfolio(cash=10_000.0, positions={"ETH-USD.CC": 1_000.0})
    result = apply_risk([_buy("BTC-USD.CC", 10)], pf, PRICES, classes, policy)
    assert result.orders == []
    assert result.adjustments[0].rule == "unpriced_holding"


def test_ticker_cap_ignores_unpriced_holdings_in_other_classes():
    policy = RiskPolicy(max_weight_per_asset_class={"crypto": 0.5})
    classes = {**CLASSES, "OLD.US": "equity"}
    pf = Portfolio(cash=10_000.0, positions={"OLD.US": 5.0})
    result = apply_risk([_buy("BTC-USD.CC", 10)], pf, PRICES, classes, policy)
    assert [o.ticker for o in result.orders] == ["BTC-USD.CC"]


def test_cash_buffer_limits_total_buys():
    # equity 10_000, buffer 0.2 → at most 8_000 deployable
    policy = RiskPolicy(cash_buffer_fraction=0.2)
    result = apply_risk(
        [_buy("A.US", 500), _buy("B.US", 500)], Portfolio(cash=10_000.0), PRICES, CLASSES, policy
    )
    by_ticker = {o.ticker: o.quantity for o in result.orders}
    assert by_ticker["A.US"] == 500  # 5000
    assert by_ticker["B.US"] == pytest.approx(150.0)  # 3000 / 20
    assert result.adjustments[0].rule == "cash_buffer"


def test_cash_buffer_accounts_for_fees_and_slippage():
    policy = RiskPolicy(cash_buffer_fraction=0.0)
    result = apply_risk(
        [_buy("A.US", 1000)],
        Portfolio(cash=1_000.0),
        PRICES,
        CLASSES,
        policy,
        slippage_bps=100.0,  # buy at 10.1
        fee_per_trade=10.0,
    )
    # (1000 - 10) / 10.1
    assert result.orders[0].quantity == pytest.approx(990.0 / 10.1)


def test_min_order_notional_drops_small_buys():
    policy = RiskPolicy(min_order_notional=100.0)
    result = apply_risk([_buy("A.US", 5)], Portfolio(cash=10_000.0), PRICES, CLASSES, policy)
    assert result.orders == []
    assert result.adjustments[0].rule == "min_order_notional"


def test_min_order_notional_applies_after_clipping():
    policy = RiskPolicy(max_weight_per_ticker=0.005, min_order_notional=100.0)
    result = apply_risk([_buy("A.US", 1000)], Portfolio(cash=10_000.0), PRICES, CLASSES, policy)
    assert result.orders == []  # clipped to 50 notional, then below min
    assert result.adjustments[-1].rule == "min_order_notional"


def test_sells_are_never_blocked_by_caps_or_min_notional():
    policy = RiskPolicy(
        max_open_positions=0,
        max_weight_per_ticker=0.0,
        max_weight_per_asset_class={"equity": 0.0},
        cash_buffer_fraction=1.0,
        min_order_notional=1_000_000.0,
    )
    pf = Portfolio(cash=0.0, positions={"A.US": 3.0})
    orders = [_sell("A.US", 3.0)]
    result = apply_risk(orders, pf, PRICES, CLASSES, policy)
    assert result.orders == orders
    assert result.adjustments == []


def test_sell_larger_than_position_is_clipped_so_it_cannot_open_a_short():
    pf = Portfolio(cash=0.0, positions={"A.US": 3.0})
    result = apply_risk([_sell("A.US", 10.0)], pf, PRICES, CLASSES, RiskPolicy())
    assert result.orders[0].quantity == 3.0
    assert result.adjustments[0].rule == "sell_exceeds_position"


def test_sell_without_position_is_dropped():
    result = apply_risk([_sell("A.US", 1.0)], Portfolio(cash=0.0), PRICES, CLASSES, RiskPolicy())
    assert result.orders == []
    assert result.adjustments[0].rule == "sell_exceeds_position"


def test_buy_without_price_is_dropped():
    result = apply_risk(
        [_buy("NOPRICE.US", 1.0)], Portfolio(cash=100.0), PRICES, CLASSES, RiskPolicy()
    )
    assert result.orders == []
    assert result.adjustments[0].rule == "no_price"


def test_disabled_policy_is_a_passthrough():
    policy = RiskPolicy(enabled=False, max_weight_per_ticker=0.0)
    orders = [_buy("A.US", 100)]
    result = apply_risk(orders, Portfolio(cash=10_000.0), PRICES, CLASSES, policy)
    assert result.orders == orders


def test_adjustment_serializes_for_tick_summary():
    policy = RiskPolicy(max_weight_per_ticker=0.25)
    result = apply_risk([_buy("A.US", 1000)], Portfolio(cash=10_000.0), PRICES, CLASSES, policy)
    d = result.adjustments[0].as_dict()
    assert d["ticker"] == "A.US"
    assert d["rule"] == "max_weight_per_ticker"
    assert "reason" in d


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_weight_per_ticker": 1.5},
        {"cash_buffer_fraction": -0.1},
        {"max_open_positions": -1},
        {"min_order_notional": -5.0},
        {"max_weight_per_asset_class": {"crypto": 2.0}},
        {"max_weight_per_asset_class": {"stocks": 0.5}},
    ],
)
def test_policy_validates_bounds(kwargs):
    with pytest.raises(ValidationError):
        RiskPolicy(**kwargs)
