"""Expiry payoff of a structure (roadmap 17.6), worked by hand: the curve,
max loss and gain, breakevens, and a structure built from a chain."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from stonks.core.options import OptionContract
from stonks.options.chain import ChainSnapshot
from stonks.options.payoff import (
    PayoffLeg,
    expiry_payoff,
    payoff_structures,
    structure_payoff,
)
from stonks.options.synthetic import SyntheticChainSpec, SyntheticOptionSource

EXP = date(2025, 11, 21)


def call(strike: float) -> OptionContract:
    return OptionContract("X.US", EXP, strike, "call")


def put(strike: float) -> OptionContract:
    return OptionContract("X.US", EXP, strike, "put")


def profit_at(payoff, spot: float) -> float:
    return dict(payoff.points)[spot]


def test_long_call_costs_its_premium_and_gains_without_bound():
    p = expiry_payoff([PayoffLeg(call(100), 1, 2.5)], spot=100.0)
    assert p.cost == pytest.approx(250.0)
    assert p.max_loss == pytest.approx(250.0)
    assert p.max_gain is None
    assert p.breakevens == pytest.approx((102.5,))
    assert profit_at(p, 100.0) == pytest.approx(-250.0)
    spots = [s for s, _ in p.points]
    assert spots == sorted(spots) and 100.0 in spots and 102.5 in spots


def test_bull_call_spread_caps_both_sides():
    p = expiry_payoff([PayoffLeg(call(100), 1, 3.0), PayoffLeg(call(110), -1, 1.0)], spot=104.0)
    assert p.cost == pytest.approx(200.0)
    assert p.max_loss == pytest.approx(200.0)
    assert p.max_gain == pytest.approx(800.0)
    assert p.breakevens == pytest.approx((102.0,))


def test_short_call_loss_is_unbounded_and_credit_is_negative_cost():
    p = expiry_payoff([PayoffLeg(call(100), -1, 2.0)], spot=95.0)
    assert p.cost == pytest.approx(-200.0)
    assert p.max_loss is None
    assert p.max_gain == pytest.approx(200.0)
    assert p.breakevens == pytest.approx((102.0,))


def test_covered_call_counts_the_shares():
    p = expiry_payoff(
        [PayoffLeg(None, 100, 100.0, shares="X.US"), PayoffLeg(call(105), -1, 2.0)],
        spot=100.0,
    )
    assert p.cost == pytest.approx(10_000 - 200)
    assert p.max_gain == pytest.approx(700.0)
    assert p.max_loss == pytest.approx(9_800.0)
    assert p.breakevens == pytest.approx((98.0,))


def test_iron_condor_has_two_breakevens():
    legs = [
        PayoffLeg(put(90), 1, 0.5),
        PayoffLeg(put(95), -1, 1.5),
        PayoffLeg(call(105), -1, 1.5),
        PayoffLeg(call(110), 1, 0.5),
    ]
    p = expiry_payoff(legs, spot=100.0)
    assert p.cost == pytest.approx(-200.0)
    assert p.max_gain == pytest.approx(200.0)
    assert p.max_loss == pytest.approx(300.0)
    assert p.breakevens == pytest.approx((93.0, 107.0))


def test_empty_legs_are_refused():
    with pytest.raises(ValueError):
        expiry_payoff([], spot=100.0)


def test_structures_list_skips_exits_and_names_their_parameters():
    listed = {s.name: s for s in payoff_structures()}
    assert "close_group" not in listed
    assert {"long_call", "bull_call_spread", "iron_condor", "covered_call"} <= set(listed)
    assert "dte" in listed["long_call"].params
    assert {"short_delta", "wing_delta"} <= set(listed["iron_condor"].params)
    assert listed["covered_call"].holds_shares
    assert not listed["iron_condor"].holds_shares


def test_structure_payoff_builds_legs_from_the_chain(chain_snapshot):
    built = structure_payoff("bull_call_spread", chain_snapshot, {"dte": 45})
    assert built is not None
    assert [leg.quantity for leg in built.legs] == [1, -1]
    assert built.legs[0].contract.strike < built.legs[1].contract.strike
    assert built.payoff.max_gain is not None and built.payoff.max_loss is not None
    width = (built.legs[1].contract.strike - built.legs[0].contract.strike) * 100
    assert built.payoff.max_gain + built.payoff.max_loss == pytest.approx(width)

    covered = structure_payoff("covered_call", chain_snapshot, {"dte": 30, "delta": 0.3})
    assert covered is not None
    assert covered.legs[0].shares == "X.US" and covered.legs[0].quantity == 100

    condor = structure_payoff("iron_condor", chain_snapshot, {})
    assert condor is not None and len(condor.payoff.breakevens) == 2


def test_structure_payoff_none_when_the_chain_cannot_supply_legs(chain_snapshot):
    empty = ChainSnapshot("X.US", chain_snapshot.as_of, (), spot=100.0)
    assert structure_payoff("long_call", empty, {}) is None
    near = structure_payoff("long_call", chain_snapshot, {"dte": 14})
    assert near is not None
    assert near.legs[0].contract.expiry - chain_snapshot.as_of <= timedelta(30)


def test_unknown_structure_is_a_value_error(chain_snapshot):
    with pytest.raises(ValueError):
        structure_payoff("close_group", chain_snapshot, {})
    with pytest.raises(ValueError):
        structure_payoff("nope", chain_snapshot, {})


@pytest.fixture
def chain_snapshot() -> ChainSnapshot:
    from stonks.options.chain import OptionQuote, snapshots

    day = date(2025, 1, 2)
    src = SyntheticOptionSource(
        {"X.US": {day: 100.0}},
        SyntheticChainSpec(horizon_days=80),
        fixed_strikes={"X.US": [float(k) for k in range(70, 135, 5)]},
    )
    quotes = [
        OptionQuote(
            contract=OptionContract(
                r.underlying, r.expiry, r.strike, r.right, r.multiplier, r.style, r.settlement
            ),
            as_of=r.as_of,
            bid=r.bid,
            ask=r.ask,
            last=r.last,
            volume=r.volume,
            open_interest=r.open_interest,
            underlying_price=r.underlying_price,
            iv=r.iv,
            delta=r.delta,
        )
        for r in src.fetch_option_quotes("X.US")
    ]
    snap = snapshots(quotes)[("X.US", day)]
    assert not math.isnan(snap.spot or math.nan)
    return snap
