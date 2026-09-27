"""Ledger properties of ``Portfolio`` and ``SimulatedBroker`` (BL-49).

Cash plus positions marked at a price equals the book's equity at that
price, and every fill moves equity (marked at the fill's reference price)
by exactly its cost: the fee plus the adverse slippage. A cash account
never goes below zero cash or holds a short.
"""

from __future__ import annotations

from datetime import date, timedelta

from hypothesis import given
from hypothesis import strategies as st

from stonks.backtest.costs import CostModelSettings
from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.core.types import Fill, Order, Portfolio

TICKERS = ["A.US", "B.US", "C.US"]
DAY = date(2025, 1, 2)

price = st.floats(min_value=0.5, max_value=2_000.0, allow_nan=False)


@st.composite
def steps(draw):
    """A few bars: prices per ticker plus orders to place on each."""
    out = []
    for _ in range(draw(st.integers(min_value=1, max_value=8))):
        prices = {t: draw(price) for t in TICKERS}
        orders = draw(
            st.lists(
                st.tuples(
                    st.sampled_from(TICKERS),
                    st.sampled_from(["buy", "sell"]),
                    st.floats(min_value=0.01, max_value=500.0),
                ),
                max_size=5,
            )
        )
        out.append((prices, orders))
    return out


costs = st.sampled_from(
    [
        CostModelSettings(),
        CostModelSettings.realistic(),
        CostModelSettings(impact_model="sqrt_vol", impact_bps=50.0),
    ]
)


@given(
    st.floats(min_value=0.0, max_value=1e6),
    st.dictionaries(st.sampled_from(TICKERS), st.floats(min_value=-1e3, max_value=1e3)),
    st.dictionaries(st.sampled_from(TICKERS), price, min_size=3),
)
def test_equity_is_cash_plus_marked_positions(cash, positions, prices):
    book = Portfolio(cash=cash, positions=dict(positions))
    marked = sum(q * prices[t] for t, q in positions.items())
    assert book.total_value(prices) == cash + marked


@given(
    st.floats(min_value=0.0, max_value=1e6),
    st.sampled_from(["buy", "sell"]),
    st.floats(min_value=0.01, max_value=1e3),
    price,
    st.floats(min_value=0.0, max_value=50.0),
)
def test_a_fill_moves_equity_by_its_fee_only_at_the_fill_price(cash, side, qty, px, fee):
    book = Portfolio(cash=cash, positions={"A.US": 10.0})
    before = book.total_value({"A.US": px})
    fill = Fill(
        order_client_id="x",
        ticker="A.US",
        quantity=qty,
        price=px,
        fee=fee,
        filled_at=DAY.isoformat(),
        side=side,
    )
    book.apply_fill(fill)
    after = book.total_value({"A.US": px})
    assert abs((before - after) - fee) <= 1e-9 * max(1.0, abs(before))


@given(st.floats(min_value=100.0, max_value=1e6), steps(), costs)
def test_a_cash_account_never_goes_short_or_below_zero_cash(cash, bars, cost_settings):
    broker = SimulatedBroker(Portfolio(cash=cash), cost_model=cost_settings.build())
    for i, (prices, orders) in enumerate(bars):
        broker.set_prices(prices, DAY + timedelta(days=i), volumes=dict.fromkeys(prices, 1e6))
        for j, (ticker, side, qty) in enumerate(orders):
            book = broker.fetch_portfolio()
            before = book.total_value(prices)
            fill = broker.place_order(
                Order(client_id=f"{i}:{j}", ticker=ticker, side=side, quantity=qty)
            )
            after = book.total_value(prices)
            assert book.cash >= -1e-6
            assert all(q > 0 for q in book.positions.values())
            if fill is None:
                assert after == before
                continue
            ref = prices[ticker]
            assert fill.fee >= 0
            slip = (fill.price - ref) if side == "buy" else (ref - fill.price)
            assert slip >= -1e-9 * ref
            # equity marked at the reference price drops by exactly the cost
            cost = fill.fee + slip * fill.quantity
            assert abs((before - after) - cost) <= 1e-6 * max(1.0, before)


@given(st.floats(min_value=1_000.0, max_value=1e6), price, st.floats(min_value=0.1, max_value=50.0))
def test_a_round_trip_at_one_price_never_makes_money(cash, px, qty):
    broker = SimulatedBroker(Portfolio(cash=cash), cost_model=CostModelSettings.realistic().build())
    broker.set_prices({"A.US": px}, DAY, volumes={"A.US": 1e6})
    bought = broker.place_order(Order(client_id="b", ticker="A.US", side="buy", quantity=qty))
    if bought is None:
        return
    broker.place_order(Order(client_id="s", ticker="A.US", side="sell", quantity=bought.quantity))
    book = broker.fetch_portfolio()
    assert book.positions == {}
    assert book.cash <= cash + 1e-9
