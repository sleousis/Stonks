"""Properties of the intraday risk rules (roadmap 21.3.2, P28).

Over random books, orders, equity marks, bar times and sent orders:

- no intraday rule drops or shrinks a closing order, grows an opening one,
  or adds anything but closes that stay within the position;
- data stamped after the event never changes a result (P12);
- a daily book (no intraday state) is never touched;
- the event halt gate never blocks a close under a ``buys`` halt;
- the whole ``apply_risk`` chain with every intraday rule on keeps every
  close the strategy sent.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from stonks.core.types import Order, Portfolio
from stonks.production.intraday_halts import EventVerdict, gate_event_orders
from stonks.production.risk import apply_risk
from stonks.production.rules import RiskContext
from stonks.production.rules._common import is_opening
from stonks.production.rules._intraday import IntradayContext
from stonks.production.rules.intraday_drawdown import IntradayDrawdown
from stonks.production.rules.intraday_loss import IntradayLossLimit
from stonks.production.rules.intraday_orders import IntradayOrderRate
from stonks.production.rules.intraday_stale import IntradayStaleData
from stonks.production.rules.settings import RuleSettings
from tests.fixtures.risk_rules import Policy

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)
TICKERS = ["A.US", "B.US", "C.US", "D.US", "E.US"]
RULES = [IntradayLossLimit(), IntradayDrawdown(), IntradayOrderRate(), IntradayStaleData()]

seconds = st.integers(min_value=0, max_value=6 * 3600)


@st.composite
def rule_settings(draw) -> RuleSettings:
    max_loss = draw(st.none() | st.floats(0.001, 0.2))
    return RuleSettings.model_validate(
        {
            "intraday_loss_limit": {
                "max_loss": max_loss,
                "hard_loss": draw(st.none() | st.floats(0.001, 0.5)),
                "window_minutes": draw(st.integers(1, 120)),
                "flatten": draw(st.booleans()),
            },
            "intraday_drawdown": {
                "schedule": draw(
                    st.none() | st.just(((0.005, 0.5), (0.02, 0.0))) | st.just(((0.01, 0.75),))
                )
            },
            "intraday_order_rate": {
                "max_orders_per_minute": draw(st.none() | st.integers(1, 6)),
                "max_orders_per_day": draw(st.none() | st.integers(1, 40)),
            },
            "intraday_stale_data": {"max_bar_age_seconds": draw(st.none() | st.integers(1, 600))},
        }
    )


@st.composite
def cases(draw) -> tuple[list[Order], RiskContext]:
    positions = {
        t: float(q)
        for t in TICKERS
        if (q := draw(st.integers(-50, 50))) != 0 and draw(st.booleans())
    }
    prices = {t: draw(st.floats(5.0, 200.0)) for t in TICKERS}
    orders: list[Order] = []
    for i, ticker in enumerate(TICKERS):
        kind = draw(st.sampled_from(["none", "close", "open"]))
        held = positions.get(ticker, 0.0)
        qty = float(draw(st.integers(1, 40)))
        if kind == "close" and held:
            side = "sell" if held > 0 else "buy"
            orders.append(Order(f"c{i}", ticker, side, min(qty, abs(held))))  # type: ignore[arg-type]
        elif kind == "open":
            side = "buy" if held >= 0 else "sell"
            score = draw(st.none() | st.floats(-1.0, 1.0))
            orders.append(
                Order(
                    f"o{i}",
                    ticker,
                    side,  # type: ignore[arg-type]
                    qty,
                    decision_context=None if score is None else {"score": score},
                )
            )
    value = 10_000.0
    marks = tuple(
        sorted(
            (NOW - timedelta(seconds=s), value * draw(st.floats(0.8, 1.25)))
            for s in draw(st.lists(seconds, max_size=12))
        )
    )
    intraday = IntradayContext(
        now=NOW,
        equity_marks=marks,
        last_bar_at={
            t: NOW - timedelta(seconds=draw(st.integers(0, 900)))
            for t in TICKERS
            if draw(st.booleans())
        },
        sent_at=tuple(NOW - timedelta(seconds=s) for s in draw(st.lists(seconds, max_size=30))),
        stream_stale=draw(st.booleans()),
    )
    ctx = RiskContext(
        portfolio=Portfolio(cash=draw(st.floats(0.0, 20_000.0)), positions=positions),
        prices=prices,
        asset_classes=dict.fromkeys(TICKERS, "equity"),
        policy=Policy(rules=draw(rule_settings())),
        as_of=NOW.date(),
        allow_short=True,
        portfolio_id="pf_x",
        intraday=intraday,
    )
    return orders, ctx


def _closes(orders, positions) -> dict[str, float]:
    return {o.client_id: o.quantity for o in orders if not is_opening(o, positions)}


def _opens(orders, positions) -> dict[str, float]:
    return {o.client_id: o.quantity for o in orders if is_opening(o, positions)}


@given(cases())
def test_no_intraday_rule_touches_a_close_or_grows_an_open(case):
    orders, ctx = case
    positions = ctx.portfolio.positions
    for rule in RULES:
        kept, adjustments = rule.apply(orders, ctx)
        closes_before, closes_after = _closes(orders, positions), _closes(kept, positions)
        assert all(closes_after.get(c) == q for c, q in closes_before.items()), rule.name
        opens_before, opens_after = _opens(orders, positions), _opens(kept, positions)
        assert set(opens_after) <= set(opens_before), rule.name
        assert all(opens_after[c] <= opens_before[c] + 1e-9 for c in opens_after), rule.name
        # anything added is a close, and closes never exceed the position
        for ticker, held in positions.items():
            side = "sell" if held > 0 else "buy"
            closing = sum(o.quantity for o in kept if o.ticker == ticker and o.side == side)
            assert closing <= abs(held) + 1e-9, rule.name
        changed = sum(1 for c in opens_before if opens_after.get(c) != opens_before[c])
        assert len(adjustments) >= changed, rule.name
        assert all(a.reason for a in adjustments), rule.name


@given(cases(), st.lists(st.integers(1, 3600), min_size=1, max_size=5))
def test_data_after_the_event_never_changes_a_result(case, later):
    orders, ctx = case
    intraday = ctx.intraday
    assert intraday is not None
    future = [NOW + timedelta(seconds=s) for s in later]
    polluted = replace(
        intraday,
        equity_marks=(*intraday.equity_marks, *((t, 1e9) for t in future)),
        sent_at=(*intraday.sent_at, *future),
    )
    for rule in RULES:
        assert rule.apply(orders, ctx) == rule.apply(orders, replace(ctx, intraday=polluted))


@given(cases())
def test_a_daily_book_is_never_touched(case):
    orders, ctx = case
    daily = replace(ctx, intraday=None)
    for rule in RULES:
        assert rule.apply(orders, daily) == (orders, [])


@given(cases())
def test_a_buys_halt_never_blocks_a_close(case):
    orders, ctx = case
    positions = ctx.portfolio.positions
    kept, blocked = gate_event_orders(orders, EventVerdict(mode="buys"), positions)
    assert _closes(kept, positions) == _closes(orders, positions)
    assert _opens(kept, positions) == {}
    assert len(kept) + len(blocked) == len(orders)


@given(cases())
def test_apply_risk_keeps_every_close(case):
    orders, ctx = case
    positions = ctx.portfolio.positions
    result = apply_risk(
        orders,
        ctx.portfolio,
        ctx.prices,
        ctx.asset_classes,
        ctx.policy,
        context=ctx,
        allow_short=True,
    )
    before = {(o.ticker, o.side): o.quantity for o in orders if not is_opening(o, positions)}
    after: dict[tuple[str, str], float] = {}
    for o in result.orders:
        if not is_opening(o, positions):
            after[(o.ticker, o.side)] = after.get((o.ticker, o.side), 0.0) + o.quantity
    assert all(after.get(k, 0.0) >= q - 1e-9 for k, q in before.items())
