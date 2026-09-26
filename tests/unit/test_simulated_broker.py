"""Unit tests for SimulatedBroker — the in-memory broker used by both backtest
and dry-run production tick.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.backtest.costs import AssetClassCosts, CostModelSettings, Trade, TradeCost
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Order, Portfolio


@pytest.fixture
def broker():
    portfolio = Portfolio(cash=10_000.0, positions={})
    return SimulatedBroker(portfolio=portfolio, slippage_bps=0.0, fee_per_trade=0.0)


def _order(client_id: str, ticker="AAPL.US", side="buy", qty=10.0, order_type="market"):
    return Order(
        client_id=client_id,
        ticker=ticker,
        side=side,
        quantity=qty,
        order_type=order_type,
    )


def test_fetch_portfolio_returns_seeded(broker):
    p = broker.fetch_portfolio()
    assert p.cash == 10_000.0
    assert p.positions == {}


def test_place_order_market_fills_at_current_price(broker):
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("o1"))
    assert fill is not None
    assert fill.ticker == "AAPL.US"
    assert fill.quantity == 10.0
    assert fill.price == 200.0
    # portfolio updated
    p = broker.fetch_portfolio()
    assert p.cash == 10_000.0 - 10 * 200
    assert p.positions == {"AAPL.US": 10.0}


def test_place_order_is_idempotent_on_client_id(broker):
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    f1 = broker.place_order(_order("dup"))
    f2 = broker.place_order(_order("dup"))  # re-submission
    # same fill returned, no duplicate impact
    assert f1 == f2
    p = broker.fetch_portfolio()
    assert p.positions == {"AAPL.US": 10.0}
    assert p.cash == 10_000.0 - 10 * 200


def test_place_order_scales_oversized_buy_to_affordable_quantity(broker):
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("too_big", qty=1_000.0))  # 200k > 10k cash
    assert fill is not None
    assert fill.quantity == pytest.approx(50.0)
    p = broker.fetch_portfolio()
    assert p.positions["AAPL.US"] == pytest.approx(50.0)
    assert p.cash == pytest.approx(0.0, abs=1e-6)


def test_place_order_all_in_buy_with_fee_and_slippage_is_scaled_not_rejected():
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=10_000.0, positions={}),
        slippage_bps=10.0,
        fee_per_trade=1.0,
    )
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    # strategy sizes as cash / price, ignoring fee and slippage
    fill = broker.place_order(_order("all_in", qty=10_000.0 / 200.0))
    assert fill is not None
    fill_price = 200.0 * 1.001
    assert fill.quantity == pytest.approx((10_000.0 - 1.0) / fill_price)
    p = broker.fetch_portfolio()
    assert p.cash == pytest.approx(0.0, abs=1e-6)
    assert p.cash >= -1e-9


def test_place_order_rejects_buy_when_fee_exceeds_cash():
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=0.5, positions={}),
        fee_per_trade=1.0,
    )
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    assert broker.place_order(_order("broke", qty=1.0)) is None
    p = broker.fetch_portfolio()
    assert p.positions == {}
    assert p.cash == 0.5


def test_fill_time_uses_simulated_datetime(broker):
    as_of = datetime(2026, 4, 1, 14, 35, tzinfo=UTC)
    broker.set_prices({"AAPL.US": 200.0}, as_of=as_of)
    fill = broker.place_order(_order("t1"))
    assert fill.filled_at == as_of


def test_fill_time_converts_plain_date_to_utc_midnight(broker):
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("t2"))
    assert fill.filled_at == datetime(2026, 4, 1, tzinfo=UTC)


def test_fill_time_treats_naive_datetime_as_utc(broker):
    broker.set_prices({"AAPL.US": 200.0}, as_of=datetime(2026, 4, 1, 15, 0))
    fill = broker.place_order(_order("t3"))
    assert fill.filled_at == datetime(2026, 4, 1, 15, 0, tzinfo=UTC)


def test_place_order_slippage_increases_buy_price(broker):
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=10_000.0, positions={}),
        slippage_bps=50.0,  # 0.5%
        fee_per_trade=0.0,
    )
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("buy1", side="buy", qty=10.0))
    assert fill.price == pytest.approx(200.0 * 1.005)


def test_place_order_slippage_decreases_sell_price(broker):
    broker = SimulatedBroker(
        portfolio=Portfolio(cash=0.0, positions={"AAPL.US": 10.0}),
        slippage_bps=50.0,
        fee_per_trade=0.0,
    )
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("sell1", side="sell", qty=5.0))
    assert fill.price == pytest.approx(200.0 * 0.995)


def test_reconcile_returns_all_recorded_fills(broker):
    broker.set_prices({"AAPL.US": 200.0, "MSFT.US": 100.0}, as_of=date(2026, 4, 1))
    broker.place_order(_order("o1", ticker="AAPL.US", qty=10.0))
    broker.place_order(_order("o2", ticker="MSFT.US", qty=20.0))
    fills = broker.reconcile()
    assert len(fills) == 2
    assert {f.ticker for f in fills} == {"AAPL.US", "MSFT.US"}


# ---- CostModel seam ---------------------------------------------------------


class _RecordingModel:
    """Charges a flat 2.0 fee and 1% adverse price; records every trade."""

    def __init__(self) -> None:
        self.trades: list[Trade] = []

    def cost(self, trade: Trade) -> TradeCost:
        self.trades.append(trade)
        mult = 1.01 if trade.side == "buy" else 0.99
        return TradeCost(fill_price=trade.price * mult, fee=2.0)


def test_cost_model_sets_fill_price_and_fee():
    model = _RecordingModel()
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}), cost_model=model)
    broker.set_prices({"AAPL.US": 100.0}, as_of=date(2026, 4, 1))
    fill = broker.place_order(_order("c1", qty=10.0))
    assert fill.price == pytest.approx(101.0)
    assert fill.fee == pytest.approx(2.0)
    assert broker.fetch_portfolio().cash == pytest.approx(10_000.0 - 1_010.0 - 2.0)


def test_cost_model_receives_bar_volume_and_asset_class():
    model = _RecordingModel()
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}), cost_model=model)
    broker.set_asset_classes({"BTC-USD.CC": "crypto"})
    broker.set_prices(
        {"BTC-USD.CC": 100.0, "AAPL.US": 100.0},
        as_of=date(2026, 4, 1),
        volumes={"BTC-USD.CC": 5_000.0},
    )
    broker.place_order(_order("c1", ticker="BTC-USD.CC", qty=1.0))
    broker.place_order(_order("c2", ticker="AAPL.US", qty=1.0))
    btc, aapl = model.trades
    assert (btc.asset_class, btc.bar_volume, btc.price, btc.quantity) == (
        "crypto",
        5_000.0,
        100.0,
        1.0,
    )
    # no asset-class mapping -> equity; no volume given -> unknown
    assert (aapl.asset_class, aapl.bar_volume) == ("equity", None)


def test_set_prices_without_volumes_clears_previous_volumes():
    model = _RecordingModel()
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}), cost_model=model)
    broker.set_prices({"AAPL.US": 100.0}, as_of=date(2026, 4, 1), volumes={"AAPL.US": 9.0})
    broker.set_prices({"AAPL.US": 100.0}, as_of=date(2026, 4, 2))
    broker.place_order(_order("c1", qty=1.0))
    assert model.trades[-1].bar_volume is None


def test_cost_model_and_legacy_cost_args_are_mutually_exclusive():
    with pytest.raises(ValueError):
        SimulatedBroker(
            Portfolio(cash=1.0, positions={}), slippage_bps=5.0, cost_model=_RecordingModel()
        )


def test_oversized_buy_with_bps_fee_and_impact_is_scaled_to_affordable():
    settings = CostModelSettings(
        default=AssetClassCosts(fee_flat=1.0, fee_bps=10.0, half_spread_bps=5.0),
        impact_bps=100.0,
    )
    broker = SimulatedBroker(Portfolio(cash=10_000.0, positions={}), cost_model=settings.build())
    broker.set_prices({"AAPL.US": 100.0}, as_of=date(2026, 4, 1), volumes={"AAPL.US": 1_000.0})
    fill = broker.place_order(_order("big", qty=1_000.0))  # 10x the cash
    assert fill is not None
    cash = broker.fetch_portfolio().cash
    assert cash >= -1e-6
    # close to the maximum affordable size: little cash left over
    assert cash < 1.0
    # the recorded fill is self-consistent with the model at the filled size
    expected = settings.build().cost(
        Trade(
            ticker="AAPL.US",
            side="buy",
            quantity=fill.quantity,
            price=100.0,
            bar_volume=1_000.0,
        )
    )
    assert fill.price == pytest.approx(expected.fill_price)
    assert fill.fee == pytest.approx(expected.fee)


class _VolumeDiscountModel:
    """Breaks the CostModel contract: per-unit price falls with size."""

    def cost(self, trade: Trade) -> TradeCost:
        return TradeCost(fill_price=trade.price * (1 + 1 / (1 + trade.quantity)), fee=0.0)


def test_scaling_never_overdraws_cash_even_if_the_model_breaks_the_contract():
    broker = SimulatedBroker(
        Portfolio(cash=1_000.0, positions={}), cost_model=_VolumeDiscountModel()
    )
    broker.set_prices({"AAPL.US": 100.0}, as_of=date(2026, 4, 1))
    broker.place_order(_order("big", qty=1_000.0))
    assert broker.fetch_portfolio().cash >= -1e-9


def test_buy_rejected_when_even_the_flat_fee_is_unaffordable_under_cost_model():
    model = CostModelSettings(default=AssetClassCosts(fee_flat=5.0)).build()
    broker = SimulatedBroker(Portfolio(cash=4.0, positions={}), cost_model=model)
    broker.set_prices({"AAPL.US": 100.0}, as_of=date(2026, 4, 1))
    assert broker.place_order(_order("broke", qty=1.0)) is None
    assert broker.fetch_portfolio().cash == 4.0


def test_fills_is_a_read_only_tuple_in_fill_order(broker):
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    broker.place_order(_order("a"))
    broker.place_order(_order("a"))  # idempotent resubmission adds nothing
    broker.place_order(_order("b", side="sell", qty=4.0))
    fills = broker.fills
    assert isinstance(fills, tuple)
    assert [f.order_client_id for f in fills] == ["a", "b"]


def test_reference_price_is_the_pre_cost_price():
    broker = SimulatedBroker(Portfolio(cash=10_000.0), slippage_bps=100.0, fee_per_trade=1.0)
    broker.set_prices({"AAPL.US": 200.0}, as_of=date(2026, 4, 1))
    broker.place_order(_order("a"))
    assert broker.fills[0].price == pytest.approx(202.0)
    assert broker.reference_price("a") == 200.0
    assert broker.reference_price("missing") is None


def test_rejected_orders_have_no_reference_price(broker):
    broker.set_prices({}, as_of=date(2026, 4, 1))
    assert broker.place_order(_order("a")) is None
    assert broker.reference_price("a") is None
