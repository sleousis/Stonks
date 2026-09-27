"""The option risk rules (roadmap 17.4): combos are kept or dropped whole,
closing units always pass, and each limit is checked by hand."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.core.options import OptionContract
from stonks.core.types import Order, Portfolio
from stonks.options.orders import ComboLeg, ComboOrder
from stonks.options.pricing import Greeks
from stonks.options.risk import OptionRiskView
from stonks.production.risk import apply_risk
from stonks.production.rules import registered_rules
from stonks.production.rules._options import closes_only, units
from stonks.production.rules.option_greek_limits import OptionGreekLimits
from stonks.production.rules.option_margin import OptionMargin
from stonks.production.rules.option_max_loss import OptionMaxLoss, free_cover
from stonks.production.rules.short_option_guard import ShortOptionGuard, level_needed
from tests.fixtures.risk_rules import AS_OF, context, policy

EXP = date(2025, 7, 18)
SPOT = 100.0


def c(strike: float, right: str = "call", expiry: date = EXP) -> OptionContract:
    return OptionContract("X.US", expiry, strike, right)  # type: ignore[arg-type]


G = {
    "call": Greeks(delta=0.5, gamma=0.03, vega=12.0, theta=-20.0, rho=4.0),
    "put": Greeks(delta=-0.4, gamma=0.03, vega=12.0, theta=-18.0, rho=-4.0),
}


def view(*contracts: OptionContract, marks: dict[str, float] | None = None, groups=()):
    return OptionRiskView(
        as_of=AS_OF,
        contracts={k.contract_id: k for k in contracts},
        greeks={k.contract_id: G[k.right] for k in contracts},
        marks=marks if marks is not None else {k.contract_id: 2.0 for k in contracts},
        spots={"X.US": SPOT},
        groups=tuple(groups),
    )


def combo(cid: str, *legs: ComboLeg, qty: float = 1.0, effect: str = "open") -> list[Order]:
    return ComboOrder(cid, tuple(legs), quantity=qty, effect=effect).leg_orders()  # type: ignore[arg-type]


def ctx_for(rules: dict, v: OptionRiskView | None, cash: float = 100_000.0, positions=None):
    positions = positions or {}
    prices = {"X.US": SPOT}
    if v is not None:
        for cid in positions:
            if cid in v.marks:
                prices[cid] = v.marks[cid] * 100
    return context(
        Portfolio(cash=cash, positions=dict(positions)), prices, policy(**rules), options=v
    )


def ids(orders: list[Order]) -> list[str]:
    return [o.client_id for o in orders]


# ---- shared behaviour -------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [OptionGreekLimits(), OptionMaxLoss(), OptionMargin(), ShortOptionGuard()],
    ids=lambda r: r.name,
)
def test_off_by_default_and_equity_orders_pass(rule):
    assert not rule.enabled(policy())
    eq = [Order("e", "X.US", "buy", 5)]
    kept, adj = rule.apply(eq, ctx_for({}, None))
    assert kept == eq and adj == []


def test_all_four_rules_are_registered_at_order_nine():
    names = {r.name: r.order for r in registered_rules()}
    for name in ("option_greek_limits", "option_max_loss", "option_margin", "short_option_guard"):
        assert names[name] == 9


def test_units_and_closes_only():
    a = combo("a", ComboLeg("buy", 1, c(100)), ComboLeg("sell", 1, c(110)))
    b = [Order("solo", c(90, "put").contract_id, "sell", 1)]
    assert [ids(u) for u in units(a + b)] == [["a:0", "a:1"], ["solo"]]
    book = {c(100).contract_id: 2.0, c(110).contract_id: -2.0}
    close = combo("z", ComboLeg("sell", 1, c(100)), ComboLeg("buy", 1, c(110)), effect="close")
    assert closes_only(close, book)
    assert not closes_only(close, {c(100).contract_id: 2.0})
    assert not closes_only(combo("z", ComboLeg("sell", 3, c(100))), book)


def test_opening_units_without_market_data_are_dropped_closing_ones_kept():
    rules = {"option_max_loss": {"max_loss_per_group": 1.0}}
    spread = combo("a", ComboLeg("buy", 1, c(100)), ComboLeg("sell", 1, c(110)))
    kept, adj = OptionMaxLoss().apply(spread, ctx_for(rules, None))
    assert kept == [] and len(adj) == 2 and "no option market data" in adj[0].reason
    book = {c(100).contract_id: 1.0}
    close = [Order("x", c(100).contract_id, "sell", 1)]
    kept, _ = OptionMaxLoss().apply(close, ctx_for(rules, None, positions=book))
    assert kept == close


# ---- greek limits -----------------------------------------------------------------


def test_book_dollar_delta_limit():
    v = view(c(100))
    buy10 = combo("a", ComboLeg("buy", 10, c(100)))
    # 10 calls x 0.5 x 100 x 100 = 50,000 dollar delta on 100,000 equity
    kept, adj = OptionGreekLimits().apply(
        buy10, ctx_for({"option_greek_limits": {"max_dollar_delta": 0.3}}, v)
    )
    assert kept == [] and adj[0].rule == "option_greek_limits" and "dollar_delta" in adj[0].reason
    kept, _ = OptionGreekLimits().apply(
        buy10, ctx_for({"option_greek_limits": {"max_dollar_delta": 0.6}}, v)
    )
    assert kept == buy10


def test_a_unit_that_shrinks_a_breach_may_trade():
    v = view(c(95, "put"))
    hedge = combo("h", ComboLeg("buy", 5, c(95, "put")))
    ctx = ctx_for(
        {"option_greek_limits": {"max_dollar_delta": 0.5}}, v, cash=0.0, positions={"X.US": 2000}
    )
    kept, _ = OptionGreekLimits().apply(hedge, ctx)
    assert kept == hedge


def test_other_book_greeks_and_unknown_greeks():
    v = view(c(100))
    buy = combo("a", ComboLeg("buy", 10, c(100)))
    for limit, word in (
        ("max_vega", "vega"),
        ("max_theta", "theta"),
        ("max_dollar_gamma", "gamma"),
    ):
        kept, adj = OptionGreekLimits().apply(
            buy, ctx_for({"option_greek_limits": {limit: 0.0001}}, v)
        )
        assert kept == [] and word in adj[0].reason
    unknown = combo("u", ComboLeg("buy", 1, c(120)))
    kept, adj = OptionGreekLimits().apply(
        unknown, ctx_for({"option_greek_limits": {"max_vega": 1.0}}, v)
    )
    assert kept == [] and "unknown" in adj[0].reason


def test_per_position_limits():
    v = view(c(100))
    buy = combo("a", ComboLeg("buy", 10, c(100)))
    kept, adj = OptionGreekLimits().apply(
        buy, ctx_for({"option_greek_limits": {"max_position_vega": 0.001}}, v)
    )
    # 10 x 0.12 x 100 = 120 dollars per vol point > 100
    assert kept == [] and "vega" in adj[0].reason
    kept, _ = OptionGreekLimits().apply(
        buy, ctx_for({"option_greek_limits": {"max_position_dollar_delta": 0.6}}, v)
    )
    assert kept == buy


# ---- max loss ---------------------------------------------------------------------


def test_max_loss_per_group_by_hand():
    v = view(
        c(100, "put"),
        c(90, "put"),
        marks={c(100, "put").contract_id: 4.0, c(90, "put").contract_id: 1.0},
    )
    credit = combo("p", ComboLeg("sell", 1, c(100, "put")), ComboLeg("buy", 1, c(90, "put")))
    # credit 3.00: max loss (10 - 3) x 100 = 700
    kept, adj = OptionMaxLoss().apply(
        credit, ctx_for({"option_max_loss": {"max_loss_per_group": 0.006}}, v)
    )
    assert kept == [] and "700.00" in adj[0].reason
    kept, _ = OptionMaxLoss().apply(
        credit, ctx_for({"option_max_loss": {"max_loss_per_group": 0.008}}, v)
    )
    assert kept == credit


def test_naked_call_is_unbounded_and_a_covered_call_adds_nothing():
    v = view(c(110))
    naked = combo("n", ComboLeg("sell", 1, c(110)))
    rules = {"option_max_loss": {"max_loss_per_group": 0.0001}}
    kept, adj = OptionMaxLoss().apply(naked, ctx_for(rules, v))
    assert kept == [] and "unbounded" in adj[0].reason
    kept, _ = OptionMaxLoss().apply(naked, ctx_for(rules, v, positions={"X.US": 100}))
    assert kept == naked
    # the shares already cover another call: the second one is naked again
    book = {"X.US": 100, c(120).contract_id: -1}
    v2 = view(c(110), c(120))
    assert free_cover(book, v2, "X.US") == 0
    kept, _ = OptionMaxLoss().apply(naked, ctx_for(rules, v2, positions=book))
    assert kept == []


def test_max_loss_total_counts_the_book_groups():
    long_c = c(100)
    group = {long_c.contract_id: 1.0}
    v = view(
        long_c, c(105), marks={long_c.contract_id: 5.0, c(105).contract_id: 3.0}, groups=[group]
    )
    buy = combo("b", ComboLeg("buy", 1, c(105)))
    rules = {"option_max_loss": {"max_loss_total": 0.007}}
    kept, adj = OptionMaxLoss().apply(buy, ctx_for(rules, v, positions=group))
    # 500 held + 300 new = 800 > 700
    assert kept == [] and "book max loss" in adj[0].reason
    rules = {"option_max_loss": {"max_loss_total": 0.009}}
    kept, _ = OptionMaxLoss().apply(buy, ctx_for(rules, v, positions=group))
    assert kept == buy


# ---- margin -----------------------------------------------------------------------


def test_cash_secured_put_needs_the_strike_in_cash():
    v = view(c(100, "put"))
    csp = combo("p", ComboLeg("sell", 1, c(100, "put")))
    rules = {"option_margin": {"enabled": True}}
    kept, adj = OptionMargin().apply(csp, ctx_for(rules, v, cash=5_000.0))
    assert kept == [] and "reg_t requirement 10000.00" in adj[0].reason
    kept, _ = OptionMargin().apply(csp, ctx_for(rules, v, cash=20_000.0))
    assert kept == csp
    # a cash buffer of 20% of equity leaves too little room
    rules = {"option_margin": {"enabled": True, "cash_buffer": 0.6}}
    kept, _ = OptionMargin().apply(csp, ctx_for(rules, v, cash=20_000.0))
    assert kept == []


def test_margin_risk_based_premium_and_missing_marks():
    v = view(c(100))
    buy = combo("b", ComboLeg("buy", 1, c(100)))
    rules = {"option_margin": {"enabled": True, "method": "risk_based"}}
    kept, adj = OptionMargin().apply(buy, ctx_for(rules, v, cash=100.0))
    assert kept == [] and "premium" in adj[0].reason
    kept, _ = OptionMargin().apply(buy, ctx_for(rules, v, cash=1_000.0))
    assert kept == buy
    nomark = view(c(100), marks={})
    kept, adj = OptionMargin().apply(buy, ctx_for(rules, nomark))
    assert kept == [] and "no mark" in adj[0].reason


def test_margin_counts_units_kept_earlier_in_the_pass():
    v = view(c(100, "put"), c(95, "put"))
    first = combo("p1", ComboLeg("sell", 1, c(100, "put")))
    second = combo("p2", ComboLeg("sell", 1, c(95, "put")))
    rules = {"option_margin": {"enabled": True}}
    kept, adj = OptionMargin().apply(first + second, ctx_for(rules, v, cash=15_000.0))
    assert ids(kept) == ["p1:0"] and adj[0].ticker == c(95, "put").contract_id


# ---- short option guard -----------------------------------------------------------


def test_guard_refuses_naked_calls_at_every_level():
    v = view(c(110))
    naked = combo("n", ComboLeg("sell", 1, c(110)))
    for level in (1, 2, 3, 4):
        rules = {"short_option_guard": {"enabled": True, "approval_level": level}}
        kept, adj = ShortOptionGuard().apply(naked, ctx_for(rules, v))
        assert kept == [] and "naked short call" in adj[0].reason


def test_guard_levels():
    v = view(c(100), c(110), c(95, "put"))
    covered = combo("cc", ComboLeg("sell", 1, c(110)))
    long_call = combo("lc", ComboLeg("buy", 1, c(100)))
    spread = combo("sp", ComboLeg("buy", 1, c(100)), ComboLeg("sell", 1, c(110)))
    lvl = lambda n: {"short_option_guard": {"enabled": True, "approval_level": n}}  # noqa: E731
    kept, _ = ShortOptionGuard().apply(covered, ctx_for(lvl(1), v, positions={"X.US": 100}))
    assert kept == covered
    kept, adj = ShortOptionGuard().apply(long_call, ctx_for(lvl(1), v))
    assert kept == [] and "level 2" in adj[0].reason
    kept, _ = ShortOptionGuard().apply(long_call, ctx_for(lvl(2), v))
    assert kept == long_call
    kept, adj = ShortOptionGuard().apply(spread, ctx_for(lvl(2), v))
    assert kept == [] and "level 3" in adj[0].reason
    kept, _ = ShortOptionGuard().apply(spread, ctx_for(lvl(3), v))
    assert kept == spread


def test_guard_cash_secured_puts_min_dte_and_max_contracts():
    v = view(c(100, "put"), c(90, "put"), c(100, "put", date(2025, 7, 2)))
    csp = combo("p", ComboLeg("sell", 1, c(100, "put")))
    on = {"short_option_guard": {"enabled": True}}
    kept, adj = ShortOptionGuard().apply(csp, ctx_for(on, v, cash=5_000.0))
    assert kept == [] and "cash secured" in adj[0].reason
    kept, _ = ShortOptionGuard().apply(csp, ctx_for(on, v, cash=20_000.0))
    assert kept == csp
    # a put spread is covered by its long leg (level 3)
    spread = combo("s", ComboLeg("sell", 1, c(100, "put")), ComboLeg("buy", 1, c(90, "put")))
    rules = {"short_option_guard": {"enabled": True, "approval_level": 3}}
    kept, _ = ShortOptionGuard().apply(spread, ctx_for(rules, v, cash=100.0))
    assert kept == spread
    near = combo("near", ComboLeg("sell", 1, c(100, "put", date(2025, 7, 2))))
    rules = {"short_option_guard": {"enabled": True, "min_dte": 5}}
    kept, adj = ShortOptionGuard().apply(near, ctx_for(rules, v))
    assert kept == [] and "expires within 5" in adj[0].reason
    rules = {"short_option_guard": {"enabled": True, "max_short_contracts": 2}}
    book = {c(90, "put").contract_id: -2}
    kept, adj = ShortOptionGuard().apply(csp, ctx_for(rules, v, positions=book))
    assert kept == [] and "short contracts" in adj[0].reason


def test_level_needed():
    call, put = c(100), c(100, "put")
    assert level_needed([(call, 1.0)], True) == 2
    assert level_needed([(call, -1.0)], True) == 1
    assert level_needed([(call, -1.0)], False) == 2
    assert level_needed([(put, -1.0)], True) == 2
    assert level_needed([(put, -1.0), (c(90, "put"), 1.0)], True) == 3


def test_apply_risk_runs_the_option_rules_with_a_context():
    v = view(c(110))
    naked = combo("n", ComboLeg("sell", 1, c(110)))
    pol = policy(short_option_guard={"enabled": True})
    ctx = ctx_for({"short_option_guard": {"enabled": True}}, v)
    prices = {"X.US": SPOT, c(110).contract_id: 200.0}
    result = apply_risk(naked, Portfolio(cash=100_000.0), prices, {}, pol, context=ctx)
    assert result.orders == []
    assert {a.rule for a in result.adjustments} == {"short_option_guard"}


def test_guard_counts_cash_already_promised_to_puts():
    v = view(c(100, "put"), c(90, "put"))
    csp = combo("p", ComboLeg("sell", 1, c(100, "put")))
    on = {"short_option_guard": {"enabled": True}}
    book = {c(90, "put").contract_id: -1}
    # 15,000 cash + 200 credit - 9,000 promised = 6,200 free < 10,000
    kept, adj = ShortOptionGuard().apply(csp, ctx_for(on, v, cash=15_000.0, positions=book))
    assert kept == [] and "6200.00 free" in adj[0].reason


def test_max_loss_edges():
    v = view(c(110))
    naked = combo("n", ComboLeg("sell", 1, c(110)))
    rules = {"option_max_loss": {"max_loss_total": 1.0}}
    # the covering shares have no price: the loss cannot be bounded
    unpriced = OptionRiskView(
        as_of=AS_OF, contracts=v.contracts, greeks=v.greeks, marks=v.marks, spots={}
    )
    kept, adj = OptionMaxLoss().apply(naked, ctx_for(rules, unpriced, positions={"X.US": 100}))
    assert kept == [] and "unbounded" in adj[0].reason
    # a shares-only group does not count toward the total
    v2 = view(c(110), groups=[{"X.US": 100}])
    kept, _ = OptionMaxLoss().apply(naked, ctx_for(rules, v2, positions={"X.US": 100}))
    assert kept == naked


def test_margin_pass_with_share_orders_in_it():
    v = view(c(100, "put"))
    shares = [Order("sh", "X.US", "buy", 10)]
    csp = combo("p", ComboLeg("sell", 1, c(100, "put")))
    rules = {"option_margin": {"enabled": True}}
    kept, adj = OptionMargin().apply(shares + csp, ctx_for(rules, v, cash=10_500.0))
    # 10 shares take 1,000 of the cash first, so 9,700 is left for a 10,000 put
    assert ids(kept) == ["sh"] and adj
