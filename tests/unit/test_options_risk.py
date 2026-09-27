"""Option risk analytics (roadmap 17.4) worked by hand: Greeks in money,
max loss of the common structures, Reg T and risk-based margin."""

from __future__ import annotations

import math
from datetime import date

import pytest

from stonks.core.options import OptionContract
from stonks.options.pricing import Greeks
from stonks.options.risk import (
    OptionRiskView,
    book_requirement,
    current_value,
    group_positions,
    is_unbounded,
    max_loss,
    naked_requirement,
    portfolio_greeks,
    position_greeks,
    reg_t_requirement,
    risk_based_requirement,
    worst_expiry_value,
)

AS_OF = date(2025, 10, 1)
EXP = date(2025, 11, 21)


def c(strike: float, right: str = "call", **kw) -> OptionContract:
    return OptionContract("X.US", EXP, strike, right, **kw)  # type: ignore[arg-type]


def view(marks: dict[str, float] | None = None, **kw) -> OptionRiskView:
    return OptionRiskView(as_of=AS_OF, spots={"X.US": 100.0}, marks=marks or {}, **kw)


def test_position_and_portfolio_greeks_in_money():
    call = c(100)
    g = Greeks(delta=0.5, gamma=0.02, vega=10.0, theta=-36.5, rho=5.0)
    v = view(greeks={call.contract_id: g})
    pg = position_greeks(call.contract_id, 2, v)
    assert pg is not None
    assert pg.delta_shares == pytest.approx(100)
    assert pg.dollar_delta == pytest.approx(10_000)
    assert pg.dollar_gamma == pytest.approx(400)
    assert pg.vega == pytest.approx(20)
    assert pg.theta == pytest.approx(-20)
    total, unknown = portfolio_greeks(
        {call.contract_id: 2, "X.US": 50, c(110).contract_id: 1, "Y.US": 3, "Z": 0.0}, v
    )
    assert total.delta_shares == pytest.approx(150)
    assert total.dollar_delta == pytest.approx(15_000)
    assert unknown == ["X.US:2025-11-21:C:110", "Y.US"]


def test_max_loss_of_common_structures():
    long_c, short_c = c(100), c(110)
    debit = {long_c.contract_id: 1, short_c.contract_id: -1}
    assert max_loss(debit, view(), cost=400) == pytest.approx(400)

    credit_put = {c(100, "put").contract_id: -1, c(90, "put").contract_id: 1}
    assert max_loss(credit_put, view(), cost=-300) == pytest.approx(700)

    condor = {
        c(95, "put").contract_id: -1,
        c(90, "put").contract_id: 1,
        c(105).contract_id: -1,
        c(110).contract_id: 1,
    }
    assert max_loss(condor, view(), cost=-200) == pytest.approx(300)

    naked = {short_c.contract_id: -1}
    assert math.isinf(max_loss(naked, view(), cost=-200))
    assert is_unbounded(naked, view())

    covered = {"X.US": 100, short_c.contract_id: -1}
    marks = {short_c.contract_id: 2.0}
    assert current_value(covered, view(marks)) == pytest.approx(9_800)
    assert max_loss(covered, view(marks)) == pytest.approx(9_800)

    long_put = {c(95, "put").contract_id: 1}
    assert max_loss(long_put, view({c(95, "put").contract_id: 3.0})) == pytest.approx(300)
    # no mark and no cost: unknown, so treated as unbounded
    assert math.isinf(max_loss(long_put, view()))


def test_expiry_value_and_worst_value():
    spread = {c(100).contract_id: 1, c(110).contract_id: -1}
    assert worst_expiry_value(spread, view()) == 0.0
    assert worst_expiry_value({c(100, "put").contract_id: -2}, view()) == -20_000


def test_reg_t_strategy_based_requirements():
    v = view({c(110).contract_id: 2.0, c(90).contract_id: 11.0})
    assert reg_t_requirement({c(100, "put").contract_id: -1, c(90, "put").contract_id: 1}, v) == (
        pytest.approx(1_000)
    )
    assert reg_t_requirement({c(100).contract_id: 1, c(110).contract_id: -1}, v) == 0.0
    assert reg_t_requirement({c(100, "put").contract_id: -1}, v) == pytest.approx(10_000)
    assert reg_t_requirement({"X.US": 100, c(110).contract_id: -1}, v) == 0.0
    condor = {
        c(95, "put").contract_id: -1,
        c(90, "put").contract_id: 1,
        c(105).contract_id: -1,
        c(110).contract_id: 1,
    }
    assert reg_t_requirement(condor, v) == pytest.approx(500)
    # naked calls: premium + max(20% x S - OTM, 10% x S)
    assert reg_t_requirement({c(110).contract_id: -1}, v) == pytest.approx(1_200)
    assert reg_t_requirement({c(90).contract_id: -1}, v) == pytest.approx(3_100)
    # 100 shares cover the 100 call (lowest strike first), the 110 is naked
    two = {"X.US": 100, c(100).contract_id: -1, c(110).contract_id: -1}
    assert reg_t_requirement(two, v) == pytest.approx(1_200)
    # unknown spot on a naked call cannot be bounded
    lost = OptionRiskView(as_of=AS_OF)
    assert math.isinf(reg_t_requirement({c(110).contract_id: -1}, lost))


def test_naked_requirement_for_index_options_uses_15_percent():
    idx = c(4000, settlement="cash", style="european")
    assert naked_requirement(idx, 1, 4000.0, 50.0) == pytest.approx((50 + 600) * 100)
    put = c(90, "put")
    # put floor is 10% of the strike
    assert naked_requirement(put, 1, 150.0, 0.1) == pytest.approx((0.1 + 9.0) * 100)


def test_risk_based_requirement():
    short_put = {c(100, "put").contract_id: -1}
    assert risk_based_requirement(short_put, view()) == pytest.approx(1_500)
    # the floor per short contract applies when moves cost nothing
    far = {c(50, "put").contract_id: -1}
    assert risk_based_requirement(far, view()) == pytest.approx(37.5)
    # priced with the model when an iv is known: a long call loses at most its value
    call = c(100)
    v = view(ivs={call.contract_id: 0.3})
    req = risk_based_requirement({call.contract_id: 1}, v)
    assert 0 < req < 700
    idx = c(4000, "put", settlement="cash", style="european")
    iv = OptionRiskView(as_of=AS_OF, spots={"X.US": 4000.0}, contracts={idx.contract_id: idx})
    assert risk_based_requirement({idx.contract_id: -1}, iv) == pytest.approx(0.08 * 4000 * 100)
    assert math.isinf(risk_based_requirement(short_put, OptionRiskView(as_of=AS_OF)))
    assert math.isinf(risk_based_requirement({"Y.US": 1, **short_put}, view()))


def test_group_positions_and_book_requirement():
    book = {"X.US": 100, c(110).contract_id: -1, c(100, "put").contract_id: -1}
    groups = [{"X.US": 100, c(110).contract_id: -1}, {"Q.US": 5}]
    parts = group_positions(book, groups)
    assert parts == [{"X.US": 100, c(110).contract_id: -1}, {c(100, "put").contract_id: -1}]
    v = view({c(110).contract_id: 2.0})
    assert book_requirement(book, v, groups) == pytest.approx(10_000)
    assert book_requirement(book, v, groups, method="risk_based") > 0
    # shares alone need nothing
    assert book_requirement({"X.US": 100}, v) == 0.0
    # a group claims only what the book holds
    assert group_positions({"X.US": 50}, [{"X.US": 100}]) == [{"X.US": 50}]
