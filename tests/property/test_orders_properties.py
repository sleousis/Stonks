"""Properties of ``orders_from_targets`` (BL-08, BL-49).

Long-only: sells never exceed the position, buys never spend more than cash
plus this batch's proceeds, every quantity is positive, and trading the
orders then asking again gives no orders (idempotence). Long/short: every
order is split at zero, so a closing leg never exceeds the position.
"""

from __future__ import annotations

from datetime import date

from hypothesis import assume, given
from hypothesis import strategies as st

from stonks.core.types import Fill, Portfolio
from stonks.portfolio.orders import orders_from_targets

AS_OF = date(2025, 6, 30)
TICKERS = ["A.US", "B.US", "C.US", "D.US"]

price = st.floats(min_value=0.5, max_value=5_000.0, allow_nan=False)
quantity = st.floats(min_value=0.0, max_value=1_000.0, allow_nan=False)


@st.composite
def book(draw, *, allow_short: bool = False):
    prices = {t: draw(price) for t in TICKERS}
    held = draw(st.lists(st.sampled_from(TICKERS), unique=True, max_size=4))
    signed = st.floats(min_value=-1_000.0, max_value=1_000.0) if allow_short else quantity
    positions = {t: draw(signed) for t in held}
    positions = {t: q for t, q in positions.items() if abs(q) > 1e-3}
    # Portfolio drops positions under 1e-12 as dust, so stay well above it
    cash = draw(st.one_of(st.just(0.0), st.floats(min_value=1.0, max_value=1_000_000.0)))
    portfolio = Portfolio(cash=cash, positions=positions)
    names = draw(st.lists(st.sampled_from(TICKERS), unique=True, max_size=4))
    if allow_short:
        raw = {t: draw(st.floats(min_value=-1.0, max_value=1.0)) for t in names}
        gross = sum(abs(w) for w in raw.values())
    else:
        raw = {t: draw(st.floats(min_value=0.0, max_value=1.0)) for t in names}
        gross = sum(raw.values())
    cap = draw(st.floats(min_value=0.0, max_value=1.0))
    targets = {t: w * cap / gross for t, w in raw.items()} if gross > 1.0 else raw
    buffer = draw(st.sampled_from([0.0, 0.05, 0.1, 0.25]))
    return portfolio, prices, targets, buffer


def _apply(portfolio: Portfolio, orders, prices) -> Portfolio:
    out = Portfolio(cash=portfolio.cash, positions=dict(portfolio.positions))
    for o in orders:
        out.apply_fill(
            Fill(
                order_client_id=o.client_id,
                ticker=o.ticker,
                quantity=o.quantity,
                price=prices[o.ticker],
                fee=0.0,
                filled_at=AS_OF.isoformat(),
                side=o.side,
            )
        )
    return out


@given(book())
def test_long_only_orders_are_safe_for_a_cash_account(case):
    portfolio, prices, targets, buffer = case
    orders = orders_from_targets(targets, portfolio, prices, buffer, as_of=AS_OF)
    assert all(o.quantity > 0 for o in orders)
    sells = [o for o in orders if o.side == "sell"]
    buys = [o for o in orders if o.side == "buy"]
    assert orders == sells + buys  # sells first
    for o in sells:
        assert o.quantity <= portfolio.positions.get(o.ticker, 0.0) + 1e-9
    proceeds = sum(o.quantity * prices[o.ticker] for o in sells)
    spend = sum(o.quantity * prices[o.ticker] for o in buys)
    assert spend <= portfolio.cash + proceeds + 1e-6 * max(1.0, spend)
    after = _apply(portfolio, orders, prices)
    assert after.cash >= -1e-6 * max(1.0, spend)
    assert all(q >= -1e-9 for q in after.positions.values())


@given(book())
def test_long_only_orders_never_hold_what_the_targets_drop(case):
    portfolio, prices, targets, buffer = case
    orders = orders_from_targets(targets, portfolio, prices, buffer, as_of=AS_OF)
    after = _apply(portfolio, orders, prices)
    for ticker in portfolio.positions:
        if targets.get(ticker, 0.0) == 0.0:
            assert after.positions.get(ticker, 0.0) == 0.0


@given(book())
def test_trading_the_orders_then_asking_again_gives_no_orders(case):
    portfolio, prices, targets, buffer = case
    orders = orders_from_targets(targets, portfolio, prices, buffer, as_of=AS_OF)
    spend = sum(o.quantity * prices[o.ticker] for o in orders if o.side == "buy")
    proceeds = sum(o.quantity * prices[o.ticker] for o in orders if o.side == "sell")
    # buys scaled down for cash legitimately leave room to buy again
    assume(spend < 0.999 * (portfolio.cash + proceeds) or spend == 0.0)
    after = _apply(portfolio, orders, prices)
    assert orders_from_targets(targets, after, prices, buffer, as_of=AS_OF) == []


@given(book())
def test_the_same_inputs_give_the_same_client_ids(case):
    portfolio, prices, targets, buffer = case
    one = orders_from_targets(targets, portfolio, prices, buffer, as_of=AS_OF)
    two = orders_from_targets(targets, portfolio, prices, buffer, as_of=AS_OF)
    assert one == two
    assert len({o.client_id for o in one}) == len(one)


@given(book(allow_short=True))
def test_long_short_orders_split_at_zero(case):
    portfolio, prices, targets, buffer = case
    orders = orders_from_targets(targets, portfolio, prices, buffer, as_of=AS_OF, allow_short=True)
    assert all(o.quantity > 0 for o in orders)
    closing: dict[str, float] = {}
    for o in orders:
        assert o.position_effect in ("open", "close")
        held = portfolio.positions.get(o.ticker, 0.0)
        if o.position_effect == "close":
            # a close reduces the position on its own side, never past zero
            assert (o.side == "sell" and held > 0) or (o.side == "buy" and held < 0)
            closing[o.ticker] = closing.get(o.ticker, 0.0) + o.quantity
            # classify folds an opening remainder within float dust
            # (1e-9 of the order) into the close; the broker sells what is held
            assert closing[o.ticker] <= abs(held) + 1e-9 * max(closing[o.ticker], 1.0) + 1e-12
        else:
            # an open never fights the position: flat, or the same side
            assert held == 0.0 or (held > 0) == (o.side == "buy") or o.ticker in closing
