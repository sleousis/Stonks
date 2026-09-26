"""SimulatedBroker with a ``FillModel``, lagged market statistics and T+n
settlement (BL-30, BL-31)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import pytest

from stonks.backtest.costs import CostModelSettings, Trade, TradeCost
from stonks.backtest.fills import (
    ExecutionSettings,
    FillModelSettings,
    MarketStats,
)
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Order, Portfolio

_DAY = date(2026, 4, 1)  # a Wednesday


def _order(cid="o1", side="buy", qty=100.0, kind="market", limit=None, ticker="X.US") -> Order:
    return Order(
        client_id=cid,
        ticker=ticker,
        side=side,
        quantity=qty,
        order_type=kind,
        limit_price=limit,
    )


def _broker(cash=1e6, positions=None, fill=None, **kw) -> SimulatedBroker:
    return SimulatedBroker(
        Portfolio(cash=cash, positions=dict(positions or {})),
        fill_model=(fill or FillModelSettings()).build(),
        **kw,
    )


def _prices(broker, *, open_=100.0, high=105.0, low=95.0, volume=1_000.0, as_of=_DAY, **kw):
    broker.set_prices(
        {"X.US": open_},
        as_of=as_of,
        volumes={} if volume is None else {"X.US": volume},
        highs={"X.US": high},
        lows={"X.US": low},
        **kw,
    )


class _Spy:
    def __init__(self) -> None:
        self.trades: list[Trade] = []

    def cost(self, trade: Trade) -> TradeCost:
        self.trades.append(trade)
        return TradeCost(fill_price=trade.price, fee=0.0)


def test_default_broker_has_no_market_stats_needs_and_no_carry():
    broker = SimulatedBroker(Portfolio(cash=1e6))
    assert broker.market_stats_spec is None
    broker.set_prices({"X.US": 10.0}, as_of=_DAY, volumes={"X.US": 0.0})
    fill = broker.place_order(_order(qty=1_000.0))
    assert fill is not None and fill.quantity == 1_000.0  # legacy: fills on zero volume
    assert broker.unfilled_quantity("o1") == 0.0


def test_participation_cap_partially_fills_and_reports_the_remainder():
    broker = _broker()
    _prices(broker, volume=1_000.0)
    fill = broker.place_order(_order(qty=250.0))
    assert fill.quantity == pytest.approx(100.0)
    assert broker.unfilled_quantity("o1") == pytest.approx(150.0)
    assert broker.fetch_portfolio().positions == {"X.US": pytest.approx(100.0)}


def test_zero_volume_bar_does_not_fill_and_carries():
    broker = _broker()
    _prices(broker, volume=0.0)
    assert broker.place_order(_order(qty=5.0)) is None
    assert broker.unfilled_quantity("o1") == 5.0
    assert broker.fills == ()


def test_untouched_limit_expires_without_carry():
    broker = _broker()
    _prices(broker, low=99.0)
    assert broker.place_order(_order(kind="limit", limit=98.0, qty=1.0)) is None
    assert broker.unfilled_quantity("o1") == 0.0


def test_touched_limit_fills_at_the_limit_which_is_the_reference_price():
    broker = _broker()
    _prices(broker, low=95.0)
    fill = broker.place_order(_order(kind="limit", limit=97.0, qty=1.0))
    assert fill.price == pytest.approx(97.0)
    assert broker.reference_price("o1") == pytest.approx(97.0)


def test_gap_guard_uses_the_decision_time():
    broker = _broker()
    _prices(broker, as_of=datetime(2026, 4, 30, tzinfo=UTC))
    assert broker.place_order(_order(qty=1.0), decided_at=datetime(2026, 4, 1, tzinfo=UTC)) is None
    assert broker.unfilled_quantity("o1") == 0.0
    fill = broker.place_order(_order("o2", qty=1.0), decided_at=datetime(2026, 4, 29, tzinfo=UTC))
    assert fill is not None


def test_sell_is_clipped_by_participation_against_the_held_quantity():
    broker = _broker(positions={"X.US": 500.0})
    _prices(broker, volume=1_000.0)
    fill = broker.place_order(_order(side="sell", qty=500.0))
    assert fill.quantity == pytest.approx(100.0)
    assert broker.unfilled_quantity("o1") == pytest.approx(400.0)
    assert broker.fetch_portfolio().positions["X.US"] == pytest.approx(400.0)


def test_lagged_stats_reach_the_cost_model():
    spy = _Spy()
    broker = SimulatedBroker(Portfolio(cash=1e6), cost_model=spy)
    stats = {"X.US": MarketStats(adv=5_000.0, sigma_daily=0.02, half_spread_bps=7.0)}
    broker.set_prices({"X.US": 10.0}, as_of=_DAY, volumes={"X.US": 900.0}, stats=stats)
    broker.place_order(_order(qty=1.0))
    (trade,) = spy.trades
    assert (trade.adv, trade.sigma_daily, trade.half_spread_bps) == (5_000.0, 0.02, 7.0)
    assert trade.bar_volume == 900.0


def test_adv_feeds_the_participation_cap_when_bar_volume_is_missing():
    broker = _broker()
    _prices(broker, volume=None, stats={"X.US": MarketStats(adv=2_000.0)})
    assert broker.place_order(_order(qty=1_000.0)).quantity == pytest.approx(200.0)


def test_market_stats_spec_merges_fill_and_cost_models():
    broker = _broker(cost_model=CostModelSettings(impact_model="sqrt_vol", vol_window=30).build())
    spec = broker.market_stats_spec
    assert spec.adv_window == 20 and spec.vol_window == 30
    assert _broker().market_stats_spec.adv_window == 20
    spread = SimulatedBroker(
        Portfolio(cash=1.0),
        cost_model=CostModelSettings(half_spread_model="abdi_ranaldo").build(),
    ).market_stats_spec
    assert spread.spread_estimator == "abdi_ranaldo"


def test_from_execution_settings():
    legacy = SimulatedBroker.from_execution(Portfolio(cash=1.0), ExecutionSettings())
    assert legacy.market_stats_spec is None
    bar = SimulatedBroker.from_execution(
        Portfolio(cash=1.0),
        ExecutionSettings(fill=FillModelSettings(), settlement_days=1),
        cost_model=CostModelSettings().build(),
    )
    assert bar.market_stats_spec is not None
    assert bar.settlement_days == 1


def test_a_cash_limited_buy_carries_nothing():
    broker = _broker(cash=500.0)  # 5 shares at 100
    _prices(broker, volume=1_000.0)
    fill = broker.place_order(_order(qty=250.0))  # capped to 100, then to cash
    assert fill.quantity == pytest.approx(5.0, rel=1e-6)
    assert broker.unfilled_quantity("o1") == 0.0
    assert broker.place_order(_order("o2", qty=250.0)) is None  # no cash left
    assert broker.unfilled_quantity("o2") == 0.0


def test_a_rejected_sell_carries_nothing():
    broker = _broker(positions={"X.US": 5.0})
    _prices(broker, volume=1_000.0)
    assert broker.place_order(_order(side="sell", qty=250.0)) is None
    assert broker.unfilled_quantity("o1") == 0.0


def test_idempotency_keeps_the_first_decision():
    broker = _broker()
    _prices(broker, volume=1_000.0)
    first = broker.place_order(_order(qty=250.0))
    again = broker.place_order(_order(qty=250.0))
    assert again is first
    assert broker.unfilled_quantity("o1") == pytest.approx(150.0)


# ---- T+n settlement -------------------------------------------------------------------


def test_t_plus_one_holds_sale_proceeds_until_the_next_business_day():
    broker = SimulatedBroker(Portfolio(cash=0.0, positions={"X.US": 10.0}), settlement_days=1)
    broker.set_prices({"X.US": 100.0, "Y.US": 50.0}, as_of=date(2026, 4, 3))  # Friday
    assert broker.place_order(_order("s", side="sell", qty=10.0)) is not None
    assert broker.fetch_portfolio().cash == pytest.approx(1_000.0)
    assert broker.buying_power == pytest.approx(0.0)
    assert broker.place_order(_order("b1", ticker="Y.US", qty=1.0)) is None
    broker.set_prices({"Y.US": 50.0}, as_of=date(2026, 4, 4))  # Saturday: not yet
    assert broker.buying_power == pytest.approx(0.0)
    broker.set_prices({"Y.US": 50.0}, as_of=date(2026, 4, 6))  # Monday: settled
    assert broker.buying_power == pytest.approx(1_000.0)
    assert broker.place_order(_order("b2", ticker="Y.US", qty=20.0)).quantity == 20.0


def test_unsettled_proceeds_scale_buys_down_to_settled_cash():
    broker = SimulatedBroker(Portfolio(cash=300.0, positions={"X.US": 10.0}), settlement_days=1)
    broker.set_prices({"X.US": 100.0, "Y.US": 10.0}, as_of=_DAY)
    broker.place_order(_order("s", side="sell", qty=10.0))
    fill = broker.place_order(_order("b", ticker="Y.US", qty=100.0))
    assert fill.quantity == pytest.approx(30.0)


def test_instant_settlement_by_default():
    broker = SimulatedBroker(Portfolio(cash=0.0, positions={"X.US": 10.0}))
    broker.set_prices({"X.US": 100.0, "Y.US": 10.0}, as_of=_DAY)
    broker.place_order(_order("s", side="sell", qty=10.0))
    assert broker.buying_power == pytest.approx(1_000.0)


# ---- invariants -----------------------------------------------------------------------


def test_cash_never_goes_negative_under_bar_fills_costs_and_settlement():
    rng = np.random.default_rng(11)
    broker = _broker(
        cash=10_000.0,
        cost_model=CostModelSettings.realistic()
        .model_copy(update={"impact_model": "istar"})
        .build(),
        settlement_days=1,
    )
    day = np.datetime64("2026-01-05")
    for i in range(300):
        as_of = (day + i).astype(object)
        o = float(rng.uniform(20, 200))
        stats = {"X.US": MarketStats(adv=float(rng.uniform(0, 5_000)), sigma_daily=0.02)}
        _prices(
            broker,
            open_=o,
            high=o * 1.02,
            low=o * 0.98,
            volume=float(rng.choice([0.0, 500.0, 5_000.0])),
            as_of=as_of,
            stats=stats,
        )
        side = "buy" if rng.random() < 0.6 else "sell"
        held = broker.fetch_portfolio().positions.get("X.US", 0.0)
        qty = float(rng.uniform(1, 300)) if side == "buy" else max(held, 1.0)
        kind = rng.choice(["market", "limit", "stop"])
        limit = None if kind == "market" else o * float(rng.uniform(0.97, 1.03))
        broker.place_order(_order(f"o{i}", side=side, qty=qty, kind=str(kind), limit=limit))
        assert broker.fetch_portfolio().cash >= -1e-6
        assert broker.fetch_portfolio().positions.get("X.US", 0.0) >= -1e-9
