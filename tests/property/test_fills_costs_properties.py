"""Fill and cost model properties (BL-30, BL-31, BL-49).

Fills: a fill never exceeds the order, filled plus carried never exceeds
the order, the participation cap holds, and the reference price sits inside
the bar's range. Costs: fees are never negative, a buy never fills below
the reference price and a sell never above it (nor at or below zero), and
the cost per share never falls as the order grows.
"""

from __future__ import annotations

from hypothesis import assume, example, given
from hypothesis import strategies as st

from stonks.backtest.costs import (
    AssetClassCosts,
    CostModelSettings,
    FixedCostModel,
    IStarSettings,
    Trade,
)
from stonks.backtest.fills import BarFillModel, BarQuote, FillModelSettings
from stonks.core.types import Order

positive = st.floats(min_value=0.01, max_value=10_000.0, allow_nan=False)
maybe_positive = st.none() | positive


@st.composite
def quotes(draw):
    low = draw(st.floats(min_value=0.5, max_value=1_000.0))
    high = low * draw(st.floats(min_value=1.0, max_value=1.5))
    open_ = draw(st.floats(min_value=low, max_value=high))
    return BarQuote(
        open=open_,
        high=high,
        low=low,
        volume=draw(st.none() | st.floats(min_value=0.0, max_value=1e7)),
        adv=draw(st.none() | st.floats(min_value=0.0, max_value=1e7)),
        gap_days=draw(st.none() | st.floats(min_value=0.0, max_value=30.0)),
        bar_days=draw(st.sampled_from([None, 1.0, 7.0])),
    )


@st.composite
def orders(draw, quote: BarQuote):
    # a stop order carries its stop level in limit_price (Order has no
    # stop_price field; the fill model reads one when a subclass adds it)
    kind = draw(st.sampled_from(["market", "limit", "stop"]))
    side = draw(st.sampled_from(["buy", "sell"]))
    lo, hi = quote.low or quote.open, quote.high or quote.open
    level = st.floats(min_value=lo * 0.8, max_value=hi * 1.2)
    return Order(
        client_id="o",
        ticker="A.US",
        side=side,
        quantity=draw(st.floats(min_value=0.01, max_value=1e6)),
        order_type=kind,
        limit_price=None if kind == "market" else draw(level),
    )


fill_settings = st.builds(
    FillModelSettings,
    max_participation=st.none() | st.floats(min_value=0.01, max_value=1.0),
    participation_basis=st.sampled_from(["bar_volume", "adv"]),
    carry_unfilled=st.booleans(),
    allow_zero_volume=st.booleans(),
    honour_limits=st.booleans(),
)


@given(st.data(), fill_settings)
def test_a_fill_stays_inside_the_order_and_the_bar(data, settings):
    quote = data.draw(quotes())
    order = data.draw(orders(quote))
    decision = BarFillModel(settings).decide(order, quote)
    assert 0.0 <= decision.quantity <= order.quantity
    assert decision.carry >= 0.0
    assert decision.quantity + decision.carry <= order.quantity * (1 + 1e-12)
    if decision.quantity > 0:
        assert decision.price is not None
        assert quote.low <= decision.price <= quote.high
        if order.order_type == "limit" and settings.honour_limits:
            if order.side == "buy":
                assert decision.price <= order.limit_price
            else:
                assert decision.price >= order.limit_price
        rho = settings.max_participation
        basis = quote.adv if settings.participation_basis == "adv" else None
        if settings.participation_basis == "bar_volume":
            basis = quote.volume if quote.volume and quote.volume > 0 else quote.adv
        if rho is not None and basis is not None:
            assert decision.quantity <= rho * basis * (1 + 1e-12)


cost_settings = st.builds(
    CostModelSettings,
    default=st.builds(
        AssetClassCosts,
        fee_flat=st.floats(min_value=0.0, max_value=10.0),
        fee_bps=st.floats(min_value=0.0, max_value=50.0),
        half_spread_bps=st.floats(min_value=0.0, max_value=100.0),
    ),
    impact_bps=st.floats(min_value=0.0, max_value=500.0),
    max_impact_bps=st.floats(min_value=0.0, max_value=1_000.0),
    impact_model=st.sampled_from(["sqrt", "sqrt_vol", "istar"]),
    impact_gamma=st.floats(min_value=0.0, max_value=3.0),
    istar=st.just(IStarSettings()),
    half_spread_model=st.sampled_from(["class", "corwin_schultz"]),
    max_half_spread_bps=st.floats(min_value=0.0, max_value=200.0),
)


@st.composite
def trades(draw):
    return Trade(
        ticker="A.US",
        side=draw(st.sampled_from(["buy", "sell"])),
        quantity=draw(st.floats(min_value=0.001, max_value=1e6)),
        price=draw(st.floats(min_value=0.01, max_value=1e5)),
        bar_volume=draw(st.none() | st.floats(min_value=0.0, max_value=1e8)),
        adv=draw(st.none() | st.floats(min_value=0.0, max_value=1e8)),
        sigma_daily=draw(st.none() | st.floats(min_value=0.0, max_value=0.2)),
        half_spread_bps=draw(st.none() | st.floats(min_value=0.0, max_value=500.0)),
    )


def _per_share(model, trade: Trade) -> float:
    cost = model.cost(trade)
    slip = cost.fill_price - trade.price if trade.side == "buy" else trade.price - cost.fill_price
    return slip + cost.fee / trade.quantity


#: Found by Hypothesis: an ADV too small to divide by made the impact NaN.
_TINY_ADV = Trade("A.US", "buy", 1.0, 1.0, adv=5e-324, sigma_daily=0.0)


@given(cost_settings, trades())
@example(CostModelSettings(impact_model="sqrt_vol"), _TINY_ADV)
@example(CostModelSettings(impact_model="istar"), _TINY_ADV)
def test_costs_are_never_negative_and_always_adverse(settings, trade):
    cost = settings.build().cost(trade)
    assert cost.fee >= 0.0
    if trade.side == "buy":
        assert cost.fill_price >= trade.price
    else:
        assert 0.0 < cost.fill_price <= trade.price


@given(
    st.floats(min_value=0.0, max_value=500.0),
    st.floats(min_value=0.0, max_value=100.0),
    trades(),
)
def test_the_fixed_model_is_adverse_with_a_flat_fee(slippage_bps, fee, trade):
    cost = FixedCostModel(slippage_bps, fee).cost(trade)
    assert cost.fee == fee
    if trade.side == "buy":
        assert cost.fill_price >= trade.price
    else:
        assert cost.fill_price <= trade.price


@given(cost_settings, trades(), st.floats(min_value=1.0, max_value=100.0))
def test_a_bigger_order_never_costs_less_per_share_in_slippage(settings, trade, factor):
    """Impact grows with size, so the adverse price move per share does too.
    (A flat fee per trade spreads thinner over more shares, so only the
    price part is monotone.)"""
    model = settings.build()
    bigger = Trade(**{**trade.__dict__, "quantity": trade.quantity * factor})
    assume(bigger.quantity < 1e12)
    small, large = model.cost(trade), model.cost(bigger)
    sign = 1.0 if trade.side == "buy" else -1.0
    move_small = sign * (small.fill_price - trade.price)
    move_large = sign * (large.fill_price - trade.price)
    assert move_large >= move_small - 1e-9 * trade.price
