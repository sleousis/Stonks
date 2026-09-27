"""Leg selection, the structure builders and the strategy helpers
(roadmap 17.5), on small hand-made chains."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from stonks.backtest.options_ledger import OptionLedger, PositionGroup
from stonks.core.options import OptionContract
from stonks.options.analytics import analyze, model_mark, risk_view
from stonks.options.chain import ChainSnapshot, OptionQuote
from stonks.options.pricing import PricingInputs, pricing_model
from stonks.options.selector import LegSelector
from stonks.options.strategies._common import (
    atm_iv,
    buy_shares,
    days_left,
    exit_intent,
    group_value,
    profit_fraction,
    sma,
)
from stonks.options.strategies.cash_secured_put import CashSecuredPut
from stonks.options.strategies.vertical_spread import TrendVerticalSpread
from stonks.options.strategy import OptionDecisionContext, OptionIntent
from stonks.options.structures import BuildRequest, build, register_structure, structures

D = date(2025, 3, 3)
E1 = D + timedelta(days=30)
E2 = D + timedelta(days=60)
BS = pricing_model("black_scholes")


def quote(
    strike: float,
    right: str = "call",
    expiry: date = E1,
    *,
    spot: float = 100.0,
    vol: float = 0.25,
    delta: bool = False,
    iv: bool = True,
    spread: float = 0.05,
    oi: float = 500.0,
) -> OptionQuote:
    c = OptionContract("X.US", expiry, strike, right)  # type: ignore[arg-type]
    x = PricingInputs(right, spot, strike, (expiry - D).days / 365, 0.0, 0.0, vol)  # type: ignore[arg-type]
    price = BS.price(x)
    g = BS.greeks(x)
    return OptionQuote(
        c,
        D,
        bid=max(price - spread, 0.01),
        ask=price + spread,
        last=price,
        open_interest=oi,
        underlying_price=spot,
        iv=vol if iv else None,
        delta=g.delta if delta else None,
    )


def chain(**kw) -> ChainSnapshot:
    qs = [
        quote(k, r, e, **kw)
        for e in (E1, E2)
        for r in ("call", "put")
        for k in (80, 90, 95, 100, 105, 110, 120)
    ]
    return ChainSnapshot("X.US", D, tuple(qs), spot=100.0)


def ctx(
    ch: ChainSnapshot | None = None,
    ledger: OptionLedger | None = None,
    closes: list[float] | None = None,
    equity: float = 100_000.0,
) -> OptionDecisionContext:
    closes = closes or [100.0] * 30
    history = {"X.US": [(D - timedelta(days=len(closes) - 1 - i), c) for i, c in enumerate(closes)]}
    return OptionDecisionContext(
        as_of=D,
        history=history,
        chains={"X.US": ch} if ch is not None else {},
        ledger=ledger or OptionLedger(cash=equity),
        equity=equity,
    )


# ---- selector ---------------------------------------------------------------------


def test_expiry_nearest_target_inside_the_window():
    sel = LegSelector(min_dte=7, max_dte=90)
    ch = chain()
    assert sel.expiry(ch, 35) == E1
    assert sel.expiry(ch, 50) == E2
    assert LegSelector(min_dte=70, max_dte=90).expiry(ch, 35) is None


@pytest.mark.parametrize("kw", [{"delta": True}, {}, {"iv": False}])
def test_by_delta_finds_the_same_strike_from_any_delta_source(kw):
    sel = LegSelector()
    q = sel.by_delta(chain(**kw), "call", 0.30, 30)
    assert q is not None and q.contract.strike == 105
    p = sel.by_delta(chain(**kw), "put", 0.30, 30)
    assert p is not None and p.contract.strike == 95


def test_liquidity_filters():
    wide = LegSelector(max_spread_pct=0.001)
    assert wide.by_delta(chain(), "call", 0.3, 30) is None
    assert LegSelector(min_open_interest=1_000).by_delta(chain(), "call", 0.3, 30) is None
    one_sided = OptionQuote(OptionContract("X.US", E1, 100.0, "call"), D, bid=None, ask=1.0)
    assert not LegSelector().liquid(one_sided)
    assert LegSelector().by_delta(ChainSnapshot("X.US", D, ()), "call", 0.3, 30) is None


def test_delta_needs_a_spot_and_time():
    sel = LegSelector()
    q = quote(100, iv=False)
    bare = OptionQuote(q.contract, D, bid=q.bid, ask=q.ask)
    assert sel.delta(bare, None) is None
    expired = OptionQuote(
        OptionContract("X.US", D, 100.0, "call"), D, bid=1.0, ask=1.1, underlying_price=100.0
    )
    assert sel.delta(expired, None) is None
    itm = quote(80, iv=False).contract
    silly = OptionQuote(itm, D, bid=0.001, ask=0.002, underlying_price=100.0)
    assert sel.delta(silly, None) is None  # no vol prices it that low


def test_by_strike():
    sel = LegSelector()
    assert sel.by_strike(chain(), "put", 97, E1).contract.strike == 95  # type: ignore[union-attr]
    assert sel.by_strike(chain(), "put", 97, date(2030, 1, 1)) is None


# ---- structures -------------------------------------------------------------------


def req(intent: OptionIntent, c: OptionDecisionContext, sel: LegSelector | None = None):
    return BuildRequest(intent, c, sel or LegSelector(), f"t:{intent.structure}", "t")


def test_registry():
    names = set(structures())
    assert {
        "long_call",
        "long_put",
        "covered_call",
        "cash_secured_put",
        "protective_put",
        "bull_call_spread",
        "bear_put_spread",
        "bull_put_spread",
        "bear_call_spread",
        "iron_condor",
        "close_group",
    } <= names
    with pytest.raises(ValueError, match="unknown structure"):
        build(req(OptionIntent("X.US", "strangle"), ctx(chain())))

    def other(r):  # pragma: no cover - never called
        return None

    with pytest.raises(ValueError, match="already registered"):
        register_structure("long_call")(other)


@pytest.mark.parametrize(
    ("structure", "legs"),
    [
        ("long_call", [("buy", "call", 100)]),
        ("long_put", [("buy", "put", 100)]),
        ("bull_call_spread", [("buy", "call", 100), ("sell", "call", 105)]),
        ("bear_put_spread", [("buy", "put", 100), ("sell", "put", 95)]),
        ("bull_put_spread", [("buy", "put", 90), ("sell", "put", 95)]),
        ("bear_call_spread", [("buy", "call", 110), ("sell", "call", 105)]),
        (
            "iron_condor",
            [("buy", "put", 90), ("sell", "put", 95), ("sell", "call", 105), ("buy", "call", 110)],
        ),
    ],
)
def test_builders_pick_the_expected_legs(structure, legs):
    params = (
        {"dte": 30, "short_delta": 0.3, "wing_delta": 0.08}
        if structure == "iron_condor"
        else {"dte": 30}
    )
    if structure in ("bull_put_spread", "bear_call_spread"):
        params["long_delta"] = 0.08
    combo = build(req(OptionIntent("X.US", structure, params), ctx(chain())))
    assert combo is not None
    got = [(leg.side, leg.contract.right, leg.contract.strike) for leg in combo.legs]  # type: ignore[union-attr]
    assert got == legs
    assert combo.group_id == combo.client_id and combo.effect == "open"
    if len(legs) > 1:
        assert combo.net_limit is not None


def test_builders_return_none_without_a_chain_or_legs():
    empty = ctx(None)
    for s in (
        "long_call",
        "covered_call",
        "cash_secured_put",
        "protective_put",
        "bull_call_spread",
        "iron_condor",
    ):
        assert build(req(OptionIntent("X.US", s), empty)) is None
    # only one strike listed: a vertical cannot be made
    one = ChainSnapshot("X.US", D, (quote(100), quote(100, "put")), spot=100.0)
    assert build(req(OptionIntent("X.US", "bull_call_spread", {"dte": 30}), ctx(one))) is None
    assert build(req(OptionIntent("X.US", "iron_condor", {"dte": 30}), ctx(one))) is None
    far = LegSelector(min_dte=100, max_dte=200)
    assert build(req(OptionIntent("X.US", "bull_call_spread"), ctx(chain()), far)) is None
    assert build(req(OptionIntent("X.US", "iron_condor"), ctx(chain()), far)) is None


def test_covered_call_and_cash_secured_put_sizing():
    led = OptionLedger(cash=50_000.0, shares={"X.US": 250})
    cc = build(req(OptionIntent("X.US", "covered_call", {"dte": 30}), ctx(chain(), led)))
    assert cc is not None and cc.quantity == 2
    # with one call already written only one more fits
    led.contracts["X.US:2025-04-02:C:120"] = OptionContract("X.US", E1, 120.0, "call")
    led.options["X.US:2025-04-02:C:120"] = -1
    cc = build(req(OptionIntent("X.US", "covered_call", {"dte": 30}), ctx(chain(), led)))
    assert cc is not None and cc.quantity == 1
    # 50,000 cash secures five 90 puts (9,000 each)
    csp = build(
        req(
            OptionIntent("X.US", "cash_secured_put", {"dte": 30}),
            ctx(chain(), OptionLedger(cash=50_000.0), equity=50_000.0),
        )
    )
    assert csp is not None and csp.quantity == 5
    none = build(
        req(
            OptionIntent("X.US", "cash_secured_put", {"dte": 30}),
            ctx(chain(), OptionLedger(cash=100.0), equity=100.0),
        )
    )
    assert none is None
    pp = build(
        req(
            OptionIntent("X.US", "protective_put", {"dte": 30}),
            ctx(chain(), OptionLedger(cash=0.0, shares={"X.US": 300})),
        )
    )
    assert pp is not None and pp.quantity == 3


def test_close_group():
    led = OptionLedger(cash=0.0, shares={"X.US": 100})
    c = OptionContract("X.US", E1, 110.0, "call")
    led.contracts[c.contract_id] = c
    led.options[c.contract_id] = -2
    led.open_group(PositionGroup("g", "covered_call", {c.contract_id: -2, "X.US": 100}, D))
    combo = build(req(OptionIntent("X.US", "close_group", group_id="g"), ctx(chain(), led)))
    assert combo is not None and combo.effect == "close" and combo.group_id == "g"
    assert [(leg.side, leg.ratio, leg.instrument) for leg in combo.legs] == [
        ("buy", 2, c.contract_id)
    ]
    with_shares = build(
        req(
            OptionIntent("X.US", "close_group", {"include_shares": True}, group_id="g"),
            ctx(chain(), led),
        )
    )
    assert with_shares is not None and len(with_shares.legs) == 2
    assert (
        build(req(OptionIntent("X.US", "close_group", group_id="nope"), ctx(chain(), led))) is None
    )
    led.open_group(PositionGroup("s", "shares", {"X.US": 100}, D))
    assert build(req(OptionIntent("X.US", "close_group", group_id="s"), ctx(chain(), led))) is None


# ---- strategy helpers -------------------------------------------------------------


def test_context_helpers():
    closes = [100 * (1.01 if i % 2 else 0.99) for i in range(30)]
    c = ctx(chain(), closes=closes)
    assert c.spot("X.US") == closes[-1]
    assert c.spot("Y.US") is None
    rv = c.realized_vol("X.US", 21)
    assert rv is not None and rv > 0.2
    assert c.realized_vol("X.US", 50) is None
    assert ctx(chain(), closes=[100.0, 100.0, 100.0]).realized_vol("X.US", 1) is None
    assert sma([1, 2, 3], 2) == 2.5 and sma([1], 2) is None and sma([1], 0) is None


def test_group_value_profit_and_exits():
    ch = chain()
    q110 = ch.by_id()["X.US:2025-04-02:C:110"]
    led = OptionLedger(cash=0.0)
    led.contracts[q110.contract_id] = q110.contract
    led.options[q110.contract_id] = -1
    credit = (q110.mark or 0) * 100 * 2  # sold at twice today's mark
    group = PositionGroup("g", "covered_call", {q110.contract_id: -1}, D, open_cost=-credit)
    led.open_group(group)
    c = ctx(ch, led)
    assert group_value(c, group) == pytest.approx(-(q110.mark or 0) * 100)
    assert profit_fraction(c, group) == pytest.approx(0.5)
    assert days_left(c, group) == 30
    assert exit_intent(c, group, 0.5, 5) is not None
    assert exit_intent(c, group, 0.9, 5) is None
    assert exit_intent(c, group, 0.9, 30).reason == "30 days left"  # type: ignore[union-attr]
    # no quote today: no value, no profit reading
    bare = ctx(ChainSnapshot("X.US", D, ()), led)
    assert group_value(bare, group) is None and profit_fraction(bare, group) is None
    shares_only = PositionGroup("s", "shares", {"X.US": 100}, D)
    assert days_left(c, shares_only) is None and exit_intent(c, shares_only, 0.5, 5) is None


def test_atm_iv_and_buy_shares():
    assert atm_iv(ctx(chain()), "X.US", 30) == pytest.approx(0.25)
    assert atm_iv(ctx(None), "X.US") is None
    short = ChainSnapshot("X.US", D, (quote(100, expiry=D + timedelta(days=3)),), spot=100.0)
    assert atm_iv(ctx(short), "X.US") is None
    puts_only = ChainSnapshot("X.US", D, (quote(100, "put"),), spot=100.0)
    assert atm_iv(ctx(puts_only), "X.US") is None
    order = buy_shares(ctx(chain()), "X.US", 0.5, "s")
    assert order is not None and order.legs[0].ratio == 500
    assert buy_shares(ctx(chain(), equity=5_000.0), "X.US", 1.0, "s") is None
    held = ctx(chain(), OptionLedger(cash=1e6, shares={"X.US": 100}))
    assert buy_shares(held, "X.US", 1.0, "s") is None


def test_cash_secured_put_trend_filter_and_wheel():
    down = [120.0 - i for i in range(30)]
    s = CashSecuredPut({"trend_days": 20})
    assert s.decide(ctx(chain(), closes=down)) == []
    up = [80.0 + i for i in range(30)]
    out = s.decide(ctx(chain(), closes=up))
    assert [d.structure for d in out] == ["cash_secured_put"]  # type: ignore[union-attr]
    wheel = CashSecuredPut().decide(ctx(chain(), OptionLedger(cash=0.0, shares={"X.US": 100})))
    assert [d.structure for d in wheel] == ["covered_call"]  # type: ignore[union-attr]


def test_vertical_spread_flips_with_the_signal():
    ch = chain()
    q = ch.by_id()["X.US:2025-04-02:C:100"]
    led = OptionLedger(cash=0.0)
    led.contracts[q.contract_id] = q.contract
    led.options[q.contract_id] = 1
    led.open_group(PositionGroup("g", "bull_call_spread", {q.contract_id: 1}, D, open_cost=500.0))
    falling = [130.0 - i for i in range(80)]
    out = TrendVerticalSpread({"lookback": 21}).decide(ctx(ch, led, closes=falling))
    assert [(d.structure, d.reason) for d in out] == [("close_group", "signal flipped")]  # type: ignore[union-attr]
    assert TrendVerticalSpread({"lookback": 100}).decide(ctx(ch, closes=falling)) == []


def test_analytics_helpers():
    q = quote(100)
    assert analyze(q, None).iv == pytest.approx(0.25)
    no_spot = OptionQuote(q.contract, D, bid=1.0, ask=1.2)
    assert analyze(no_spot, None).greeks is None
    implied = analyze(OptionQuote(q.contract, D, bid=q.bid, ask=q.ask), 100.0)
    assert implied.iv == pytest.approx(0.25, abs=0.01)
    none = analyze(OptionQuote(q.contract, D, bid=None, ask=None), 100.0)
    assert none.iv is None and none.greeks is None
    assert model_mark(q.contract, D, 120.0, None) == 20.0
    assert model_mark(q.contract, D, 100.0, 0.25) == pytest.approx(
        BS.price(PricingInputs("call", 100.0, 100.0, 30 / 365, 0.0, 0.0, 0.25)), rel=0.01
    )
    held = OptionContract("X.US", E2, 130.0, "call")
    v = risk_view(
        D,
        {"X.US": chain()},
        {"X.US": 100.0},
        contracts={held.contract_id: held},
        ivs={held.contract_id: 0.3},
        only={held.contract_id, q.contract_id},
    )
    assert held.contract_id in v.greeks and q.contract_id in v.greeks
    assert len(v.greeks) == 2
