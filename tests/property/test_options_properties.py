"""Option money-path invariants (roadmap 17.2 to 17.4, P48).

- European prices obey put-call parity and the no-arbitrage bounds.
- Settling an expiring option never changes the book's value marked at
  intrinsic: exercise, assignment and cash settlement only move value
  between cash, shares and the option.
- Splits leave the book's value unchanged at split-adjusted prices.
- The option risk rules only keep or drop whole proposed combos: nothing
  grows, nothing new appears, and a closing combo is never dropped.
"""

from __future__ import annotations

import math
from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from stonks.backtest.options_ledger import OptionLedger
from stonks.core.options import OptionContract
from stonks.core.types import Portfolio
from stonks.options.orders import ComboLeg, ComboOrder
from stonks.options.pricing import Greeks, PricingInputs, pricing_model
from stonks.options.risk import OptionRiskView
from stonks.production.rules import registered_rules
from stonks.production.rules._options import OptionRiskRule
from stonks.production.rules.settings import RuleSettings
from tests.fixtures.risk_rules import AS_OF, Policy, context

BS = pricing_model("black_scholes")
EXP = date(2025, 7, 18)

spots = st.floats(min_value=5.0, max_value=500.0)
moneyness = st.floats(min_value=0.5, max_value=1.5)
times = st.floats(min_value=0.01, max_value=3.0)
rates = st.floats(min_value=-0.01, max_value=0.1)
vols = st.floats(min_value=0.05, max_value=1.5)


@given(spots, moneyness, times, rates, rates, vols)
def test_put_call_parity_and_bounds(spot, m, t, r, q, vol):
    k = spot * m
    call = BS.price(PricingInputs("call", spot, k, t, r, q, vol))
    put = BS.price(PricingInputs("put", spot, k, t, r, q, vol))
    forward_pv = spot * math.exp(-q * t)
    strike_pv = k * math.exp(-r * t)
    assert math.isclose(call - put, forward_pv - strike_pv, rel_tol=1e-7, abs_tol=1e-7)
    assert max(forward_pv - strike_pv, 0.0) - 1e-9 <= call <= forward_pv + 1e-9
    assert max(strike_pv - forward_pv, 0.0) - 1e-9 <= put <= strike_pv + 1e-9


contracts = st.builds(
    OptionContract,
    underlying=st.just("X.US"),
    expiry=st.just(EXP),
    strike=st.sampled_from([50.0, 90.0, 100.0, 110.0, 150.0]),
    right=st.sampled_from(["call", "put"]),
    multiplier=st.sampled_from([100.0, 150.0, 10.0]),
    settlement=st.sampled_from(["physical", "cash"]),
)


@given(
    st.lists(st.tuples(contracts, st.integers(min_value=-5, max_value=5)), max_size=4),
    st.floats(min_value=1.0, max_value=300.0),
    st.integers(min_value=-500, max_value=500),
)
def test_settlement_preserves_value_at_intrinsic(legs, spot, shares):
    led = OptionLedger(cash=10_000.0, shares={"X.US": float(shares)} if shares else {})
    for contract, qty in legs:
        if qty:
            led.fill_option(contract, float(qty), 1.0, 0.0, EXP)
    marks = {cid: led.contracts[cid].intrinsic(spot) for cid in led.options}
    before = led.value({"X.US": spot}, marks)
    for cid in list(led.options):
        led.settle(cid, spot, EXP)
    assert led.options == {}
    after = led.value({"X.US": spot}, {})
    # at-the-money by less than the exercise threshold may expire unexercised
    slack = sum(abs(q) * c.multiplier * 0.01 for c, q in legs)
    assert math.isclose(before, after, rel_tol=1e-9, abs_tol=slack + 1e-6)


@given(
    st.lists(st.tuples(contracts, st.integers(min_value=-5, max_value=5)), max_size=4),
    st.sampled_from([2.0, 3.0, 1.5, 0.5, 0.1]),
    st.floats(min_value=1.0, max_value=300.0),
)
def test_split_preserves_intrinsic_value(legs, ratio, spot):
    led = OptionLedger(cash=0.0, shares={"X.US": 100.0})
    for contract, qty in legs:
        if qty and contract.settlement == "physical":
            led.fill_option(contract, float(qty), 0.0, 0.0, EXP)
    marks = {cid: led.contracts[cid].intrinsic(spot) for cid in led.options}
    before = led.value({"X.US": spot}, marks)
    led.apply_split("X.US", ratio, EXP)
    new_spot = spot / ratio
    marks = {cid: led.contracts[cid].intrinsic(new_spot) for cid in led.options}
    after = led.value({"X.US": new_spot}, marks)
    assert math.isclose(before, after, rel_tol=1e-6, abs_tol=1e-4)


OPTION_RULES = [r for r in registered_rules() if isinstance(r, OptionRiskRule)]
LEG_CONTRACTS = [
    OptionContract("X.US", EXP, k, r)  # type: ignore[arg-type]
    for k in (90.0, 100.0, 110.0)
    for r in ("call", "put")
]
GREEKS = Greeks(0.4, 0.02, 10.0, -15.0, 3.0)


@st.composite
def cases(draw):
    view = OptionRiskView(
        as_of=AS_OF,
        contracts={c.contract_id: c for c in LEG_CONTRACTS},
        greeks={c.contract_id: GREEKS for c in LEG_CONTRACTS},
        marks={c.contract_id: draw(st.sampled_from([0.5, 2.0, 8.0])) for c in LEG_CONTRACTS},
        spots={"X.US": 100.0},
    )
    held = {
        c.contract_id: float(draw(st.integers(min_value=-3, max_value=3)))
        for c in draw(st.lists(st.sampled_from(LEG_CONTRACTS), unique=True, max_size=3))
    }
    held = {k: v for k, v in held.items() if v}
    shares = draw(st.sampled_from([0.0, 100.0, 300.0]))
    if shares:
        held["X.US"] = shares
    orders = []
    for i in range(draw(st.integers(min_value=0, max_value=4))):
        chosen = draw(st.lists(st.sampled_from(LEG_CONTRACTS), unique=True, min_size=1, max_size=3))
        legs = tuple(ComboLeg(draw(st.sampled_from(["buy", "sell"])), 1, c) for c in chosen)
        orders.extend(
            ComboOrder(f"c{i}", legs, quantity=draw(st.sampled_from([1, 2, 5]))).leg_orders()
        )
    for cid, qty in held.items():
        if cid != "X.US" and draw(st.booleans()):
            orders.extend(
                ComboOrder(
                    f"close:{cid}",
                    (ComboLeg("sell" if qty > 0 else "buy", abs(qty), view.contracts[cid]),),
                    effect="close",
                ).leg_orders()
            )
    rules = RuleSettings.model_validate(
        {
            "option_greek_limits": {"max_dollar_delta": draw(st.sampled_from([0.1, 1.0]))},
            "option_max_loss": {"max_loss_per_group": draw(st.sampled_from([0.01, 0.5]))},
            "option_margin": {
                "enabled": True,
                "method": draw(st.sampled_from(["reg_t", "risk_based"])),
            },
            "short_option_guard": {"enabled": True, "approval_level": draw(st.integers(1, 4))},
        }
    )
    prices = {"X.US": 100.0} | {cid: view.marks[cid] * 100 for cid in held if cid != "X.US"}
    ctx = context(
        Portfolio(cash=draw(st.sampled_from([0.0, 5_000.0, 50_000.0])), positions=held),
        prices,
        Policy(rules=rules),
        asset_classes={},
        options=view,
    )
    return orders, ctx


@given(cases())
def test_option_rules_keep_or_drop_whole_combos(case):
    orders, ctx = case
    proposed = {o.client_id: o for o in orders}
    for rule in OPTION_RULES:
        kept, adjustments = rule.apply(orders, ctx)
        kept_ids = {o.client_id for o in kept}
        assert kept_ids <= set(proposed)
        assert all(o == proposed[o.client_id] for o in kept)
        by_combo: dict[str, set[str]] = {}
        for o in orders:
            by_combo.setdefault(o.client_id.rsplit(":", 1)[0], set()).add(o.client_id)
        for ids in by_combo.values():
            assert ids <= kept_ids or not (ids & kept_ids)
        closes = {o.client_id for o in orders if o.client_id.startswith("close:")}
        assert closes <= kept_ids
        assert len(adjustments) == len(set(proposed) - kept_ids)
