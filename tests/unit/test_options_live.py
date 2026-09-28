"""Pure parts of live options (roadmap 17.8): mid pricing, expiry plans,
combo legs as ledger orders, the live policy and the chain source."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.config import RiskPolicy
from stonks.core.clock import FixedClock
from stonks.core.combos import ComboLeg, ComboOrder
from stonks.core.options import OptionContract
from stonks.ingest.sources.base import UnsupportedCapabilityError
from stonks.options.chain import OptionQuote
from stonks.options.live.chain_source import BrokerOptionChainSource
from stonks.options.live.events import OptionEvent
from stonks.options.live.expiry import expiry_watch, plan_expiry, weekday_sessions
from stonks.options.live.orders import combo_from_legs, to_leg_orders
from stonks.options.live.pricing import leg_limit, price_combo
from stonks.options.live.risk import live_policy, live_view
from stonks.options.live.settings import ExpirySettings, OptionsLiveSettings

AS_OF = date(2026, 10, 15)
EXP = date(2026, 10, 16)
PUT = OptionContract("AAPL.US", EXP, 190.0, "put")
CALL = OptionContract("AAPL.US", EXP, 200.0, "call")
CALL_210 = OptionContract("AAPL.US", EXP, 210.0, "call")
NOV_PUTS = [OptionContract("AAPL.US", date(2026, 11, 20), k, "put") for k in (185.0, 190.0, 195.0)]
DEC_PUT = OptionContract("AAPL.US", date(2026, 12, 18), 190.0, "put")


def quote(c: OptionContract, bid: float | None, ask: float | None, **kw) -> OptionQuote:
    return OptionQuote(contract=c, as_of=AS_OF, bid=bid, ask=ask, **kw)


# ---- pricing ----------------------------------------------------------------------------


def test_a_leg_sits_at_the_mid_and_the_collar_moves_it_to_the_touch():
    q = quote(CALL, 1.0, 1.2)
    assert leg_limit(q, "buy", collar_share=0.0, max_spread_pct=1.0)[0] == pytest.approx(1.1)
    assert leg_limit(q, "buy", collar_share=1.0, max_spread_pct=1.0)[0] == pytest.approx(1.2)
    assert leg_limit(q, "sell", collar_share=0.5, max_spread_pct=1.0)[0] == pytest.approx(1.05)


@pytest.mark.parametrize(
    ("q", "why"),
    [
        (None, "no live quote"),
        (quote(CALL, None, 1.2), "two-sided"),
        (quote(CALL, 0.1, 1.0), "spread"),
    ],
)
def test_no_quote_or_a_wide_spread_prices_nothing(q, why):
    price, reason = leg_limit(q, "buy", collar_share=0.0, max_spread_pct=0.5)
    assert price is None and why in (reason or "")


def test_a_combo_net_limit_is_the_signed_sum_of_its_legs():
    combo = ComboOrder(
        "cmb-a", (ComboLeg("buy", 1, contract=CALL), ComboLeg("sell", 1, contract=CALL_210))
    )
    got = price_combo(
        combo,
        {CALL.contract_id: quote(CALL, 5.0, 5.2), CALL_210.contract_id: quote(CALL_210, 1.5, 1.7)},
        collar_share=0.0,
        max_spread_pct=0.5,
    )
    assert got.combo is not None and got.combo.net_limit == pytest.approx(5.1 - 1.6)


def test_a_stock_leg_is_not_priced_at_the_mid():
    combo = ComboOrder(
        "cmb-bw", (ComboLeg("buy", 100, shares="AAPL.US"), ComboLeg("sell", 1, contract=CALL))
    )
    assert price_combo(combo, {}, collar_share=0.0, max_spread_pct=1.0).combo is None


# ---- expiry ------------------------------------------------------------------------------


def test_sessions_left_count_weekdays_after_the_decision_day():
    assert weekday_sessions(date(2026, 10, 15), date(2026, 10, 16)) == 1
    assert weekday_sessions(date(2026, 10, 16), date(2026, 10, 19)) == 1  # over a weekend
    assert weekday_sessions(date(2026, 10, 16), date(2026, 10, 16)) == 0


def test_expiry_plans_close_shorts_and_longs_and_skip_stock_and_far_options():
    positions = {PUT.contract_id: -2.0, CALL.contract_id: 1.0, "AAPL.US": 100.0,
                 NOV_PUTS[0].contract_id: -1.0}  # fmt: skip
    items = plan_expiry(positions, AS_OF, ExpirySettings(), portfolio_id="pf")
    got = sorted(
        (i.combo.legs[0].instrument, i.combo.legs[0].side, i.combo.quantity) for i in items
    )
    assert got == [(CALL.contract_id, "sell", 1.0), (PUT.contract_id, "buy", 2.0)]
    assert all(i.combo.effect == "close" for i in items)
    again = plan_expiry(positions, AS_OF, ExpirySettings(), portfolio_id="pf")
    assert {i.combo.client_id for i in items} == {i.combo.client_id for i in again}


def test_close_longs_off_leaves_long_options_to_expire():
    items = plan_expiry(
        {CALL.contract_id: 1.0}, AS_OF, ExpirySettings(close_longs=False), portfolio_id="pf"
    )
    assert items == []


def test_a_roll_picks_the_expiry_nearest_the_target_and_the_nearest_strike():
    settings = ExpirySettings(action="roll", roll_target_days=35)
    [item] = plan_expiry(
        {PUT.contract_id: -1.0},
        AS_OF,
        settings,
        portfolio_id="pf",
        chain=lambda u: [*NOV_PUTS, DEC_PUT],
    )
    assert item.combo.structure == "expiry_roll"
    assert [leg.instrument for leg in item.combo.legs] == [PUT.contract_id, NOV_PUTS[1].contract_id]


def test_a_roll_with_no_target_falls_back_to_a_close():
    [item] = plan_expiry(
        {PUT.contract_id: -1.0},
        AS_OF,
        ExpirySettings(action="roll"),
        portfolio_id="pf",
        chain=lambda u: [],
    )
    assert item.combo.structure == "expiry_close"


def test_the_watch_reports_shorts_in_or_near_the_money_or_without_a_spot():
    positions = {PUT.contract_id: -1.0, CALL.contract_id: -1.0, CALL_210.contract_id: -1.0}
    warnings = expiry_watch(
        positions, EXP, ExpirySettings(watch_band=0.01), spots={"AAPL.US": 201.5}
    )
    assert [w.contract.strike for w in warnings] == [200.0]
    near = expiry_watch(
        {CALL.contract_id: -1.0}, EXP, ExpirySettings(watch_band=0.01), spots={"AAPL.US": 199.0}
    )
    assert near and not near[0].in_the_money
    blind = expiry_watch({PUT.contract_id: -1.0}, EXP, ExpirySettings(), spots={})
    assert blind and blind[0].spot is None


# ---- ledger legs ----------------------------------------------------------------------


def test_combo_legs_round_trip_through_their_orders():
    combo = ComboOrder(
        "cmb-r",
        (ComboLeg("buy", 1, contract=PUT), ComboLeg("sell", 1, contract=NOV_PUTS[1])),
        quantity=2.0,
        structure="expiry_roll",
        net_limit=-2.1,
    )
    legs = to_leg_orders(
        combo, {PUT.contract_id: -2.0}, {PUT.contract_id: 2.0, NOV_PUTS[1].contract_id: 4.1}
    )
    assert [(o.client_id, o.position_effect, o.limit_price, o.time_in_force) for o in legs] == [
        ("cmb-r:0", "close", 2.0, "day"),
        ("cmb-r:1", "open", 4.1, "day"),
    ]
    back = combo_from_legs(list(reversed(legs)))
    assert (back.client_id, back.net_limit, back.quantity, back.structure) == (
        "cmb-r",
        -2.1,
        2.0,
        "expiry_roll",
    )
    assert [leg.instrument for leg in back.legs] == [leg.instrument for leg in combo.legs]
    with pytest.raises(ValueError, match="1 of 2"):
        combo_from_legs(legs[:1])


# ---- the live policy and view -------------------------------------------------------------


def test_the_live_policy_turns_the_defined_risk_rules_on_and_caps_the_guard():
    base = RiskPolicy.model_validate(
        {
            "rules": {
                "short_option_guard": {"enabled": True, "approval_level": 3},
                "option_max_loss": {"max_loss_per_group": 0.01},
            }
        }
    )
    got = live_policy(base, "covered", OptionsLiveSettings())
    rules = got.rules
    assert rules.short_option_guard.enabled and rules.short_option_guard.approval_level == 2
    assert rules.option_margin.enabled
    assert rules.option_max_loss.max_loss_per_group == 0.01  # the tighter one stays
    assert rules.option_max_loss.max_loss_total == 0.10
    naked = live_policy(RiskPolicy(), "naked", OptionsLiveSettings()).rules.short_option_guard
    assert naked.approval_level == 4


def test_the_live_view_takes_greeks_in_pricing_units():
    q = quote(CALL, 5.0, 5.2, delta=0.5, gamma=0.02, vega=0.25, theta=-0.05, underlying_price=203.0)
    view = live_view(AS_OF, {CALL.contract_id: q}, {})
    g = view.greeks[CALL.contract_id]
    assert (g.vega_per_point, g.theta_per_day) == (pytest.approx(0.25), pytest.approx(-0.05))
    assert view.spots == {"AAPL.US": 203.0} and view.marks[CALL.contract_id] == pytest.approx(5.1)


# ---- the chain source and events ---------------------------------------------------------


class _Chains:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date]] = []

    def option_chain(self, underlying, as_of, *, max_expiry_days, strike_band):
        self.calls.append((underlying, as_of))
        return ["row"]


def test_the_chain_source_serves_only_today():
    from datetime import UTC, datetime

    chains = _Chains()
    source = BrokerOptionChainSource(
        chains, clock=FixedClock(datetime(2026, 10, 15, 21, tzinfo=UTC))
    )  # type: ignore[arg-type]
    assert list(source.fetch_option_quotes("AAPL.US")) == ["row"]
    assert list(source.fetch_option_quotes("AAPL.US", date(2026, 1, 1), date(2026, 1, 2))) == []
    assert chains.calls == [("AAPL.US", AS_OF)]
    with pytest.raises(UnsupportedCapabilityError):
        source.fetch_prices("AAPL.US")


@pytest.mark.parametrize(
    ("contract", "kind", "qty", "shares"),
    [
        (CALL, "assignment", -1.0, -100.0),  # a short call assigned sells
        (PUT, "assignment", -2.0, 200.0),  # a short put assigned buys
        (CALL, "exercise", 1.0, 100.0),  # a long call exercised buys
        (PUT, "exercise", 1.0, -100.0),  # a long put exercised sells
        (PUT, "expiry", -1.0, 0.0),
    ],
)
def test_an_event_delivers_the_right_shares(contract, kind, qty, shares):
    assert OptionEvent("e", kind, contract.contract_id, qty, EXP).shares() == shares
