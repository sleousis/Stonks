"""The options ledger and simulated broker (roadmap 17.3), worked by hand:
multipliers, exercise, assignment, cash settlement, splits, dividends,
all-or-none combo fills and the early assignment model."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.options_broker import (
    ComboFill,
    OptionFillSettings,
    OptionsSimulatedBroker,
    Rejection,
)
from stonks.backtest.options_ledger import OptionLedger, PositionGroup
from stonks.core.options import OptionContract
from stonks.options.assignment import ExtrinsicAssignmentModel, NeverAssign
from stonks.options.chain import OptionQuote
from stonks.options.orders import ComboLeg, ComboOrder, combo_id_of

D = date(2025, 10, 1)
EXP = date(2025, 10, 17)


def c(strike: float, right: str = "call", **kw) -> OptionContract:
    return OptionContract("X.US", EXP, strike, right, **kw)  # type: ignore[arg-type]


def q(contract: OptionContract, bid: float | None, ask: float | None) -> OptionQuote:
    return OptionQuote(contract, D, bid=bid, ask=ask)


# ---- ledger -----------------------------------------------------------------------


def test_option_fill_moves_cash_by_the_multiplier():
    led = OptionLedger(cash=10_000.0)
    delta = led.fill_option(c(100), 2, 3.50, 1.30, D)
    assert delta == pytest.approx(-2 * 3.50 * 100 - 1.30)
    assert led.cash == pytest.approx(10_000 - 701.30)
    assert led.options == {"X.US:2025-10-17:C:100": 2}
    # marked at 4.00 the book is worth cash + 2 x 4 x 100
    assert led.value({"X.US": 105.0}, {"X.US:2025-10-17:C:100": 4.0}) == pytest.approx(
        10_000 - 701.30 + 800
    )


def test_long_call_exercise_by_hand():
    led = OptionLedger(cash=20_000.0)
    led.fill_option(c(100), 1, 5.0, 0.0, D)
    events = led.settle("X.US:2025-10-17:C:100", 108.0, EXP)
    assert [e.kind for e in events] == ["exercise"]
    # pays 100 x 100 for 100 shares worth 108
    assert led.shares == {"X.US": 100}
    assert led.cash == pytest.approx(20_000 - 500 - 10_000)
    assert led.options == {}
    assert led.value({"X.US": 108.0}, {}) == pytest.approx(20_000 - 500 + 800)


def test_short_put_assignment_by_hand():
    led = OptionLedger(cash=10_000.0)
    led.fill_option(c(95, "put"), -1, 2.0, 0.65, D)  # receive 200 - 0.65
    led.settle("X.US:2025-10-17:P:95", 90.0, EXP)
    assert led.shares == {"X.US": 100}
    assert led.cash == pytest.approx(10_000 + 200 - 0.65 - 9_500)
    assert led.events[-1].kind == "assignment"


def test_short_call_assigned_against_shares():
    led = OptionLedger(cash=0.0, shares={"X.US": 100})
    led.fill_option(c(100), -1, 1.0, 0.0, D)
    led.settle("X.US:2025-10-17:C:100", 110.0, EXP)
    assert led.shares == {} and led.cash == pytest.approx(100 + 10_000)


def test_long_put_exercise_and_otm_expiry():
    led = OptionLedger(cash=0.0, shares={"X.US": 100})
    led.fill_option(c(95, "put"), 1, 1.0, 0.0, D)
    led.fill_option(c(120), 1, 0.5, 0.0, D)
    led.settle("X.US:2025-10-17:P:95", 80.0, EXP)
    led.settle("X.US:2025-10-17:C:120", 80.0, EXP)
    assert led.shares == {}
    assert led.cash == pytest.approx(-150 + 9_500)
    assert [e.kind for e in led.events[-2:]] == ["exercise", "expired_worthless"]
    assert led.settle("X.US:2025-10-17:C:120", 80.0, EXP) == []


def test_exercise_by_exception_threshold():
    led = OptionLedger(cash=0.0)
    led.fill_option(c(100), 1, 0.1, 0.0, D)
    led.settle("X.US:2025-10-17:C:100", 100.005, EXP)
    assert led.shares == {} and led.events[-1].kind == "expired_worthless"


def test_cash_settled_index_option():
    idx = OptionContract("SPX.INDX", EXP, 4000.0, "put", style="european", settlement="cash")
    led = OptionLedger(cash=0.0)
    led.fill_option(idx, -1, 10.0, 0.0, D)
    led.settle(idx.contract_id, 3950.0, EXP)
    # short one put, 50 in the money: pays 50 x 100, no shares move
    assert led.cash == pytest.approx(1_000 - 5_000)
    assert led.shares == {} and led.events[-1].kind == "cash_settlement"


def test_split_adjusts_shares_options_and_groups():
    led = OptionLedger(cash=0.0, shares={"X.US": 100})
    led.open_group(PositionGroup("g", "covered_call", {}, D))
    led.fill_option(c(150), -1, 2.0, 0.0, D, group_id="g")
    led.fill_option(c(90, "put"), 1, 1.0, 0.0, D)
    led.open_group(PositionGroup("s", "shares", {"X.US": 100}, D))
    led.apply_split("X.US", 2.0, D)
    assert led.shares == {"X.US": 200}
    assert led.options == {"X.US:2025-10-17:C:75": -2, "X.US:2025-10-17:P:45": 2}
    assert led.groups["g"].legs == {"X.US:2025-10-17:C:75": -2}
    assert led.groups["s"].legs == {"X.US": 200}
    led.apply_split("X.US", 1.5, D)
    assert led.options["X.US:2025-10-17:C:50:150"] == -2
    assert led.shares == {"X.US": 300}


def test_dividends_on_long_and_short_shares():
    led = OptionLedger(cash=0.0, shares={"X.US": 100, "Y.US": -50})
    led.apply_dividend("X.US", 0.5, D, withholding=0.15)
    led.apply_dividend("Y.US", 0.5, D, withholding=0.15)
    assert led.cash == pytest.approx(100 * 0.5 * 0.85 - 50 * 0.5)
    assert led.apply_dividend("Z.US", 1.0, D) is None


def test_portfolio_view_prices_options_per_contract():
    led = OptionLedger(cash=1_000.0, shares={"X.US": 10})
    led.fill_option(c(100), 1, 2.0, 0.0, D)
    pf, prices = led.portfolio({"X.US": 100.0, "Y.US": 5.0}, {"X.US:2025-10-17:C:100": 3.0})
    assert prices["X.US:2025-10-17:C:100"] == 300.0
    assert prices["Y.US"] == 5.0
    assert pf.total_value(prices) == pytest.approx(
        led.value({"X.US": 100.0}, {c(100).contract_id: 3.0})
    )


def test_share_groups_never_claim_more_than_held():
    led = OptionLedger(cash=0.0)
    led.open_group(PositionGroup("s", "shares", {}, D))
    led.fill_shares("X.US", 100, 10.0, 0.0, D, group_id="s")
    led.fill_option(c(10), -1, 0.5, 0.0, D)
    led.settle("X.US:2025-10-17:C:10", 12.0, EXP)  # called away
    assert led.shares == {} and "s" not in led.groups


# ---- broker -----------------------------------------------------------------------


def broker(cash: float = 10_000.0, **kw) -> OptionsSimulatedBroker:
    return OptionsSimulatedBroker(OptionLedger(cash=cash), OptionFillSettings(**kw))


def spread(limit: float | None = None) -> ComboOrder:
    return ComboOrder(
        "cid",
        (ComboLeg("buy", 1, c(100)), ComboLeg("sell", 1, c(110))),
        quantity=2,
        structure="bull_call_spread",
        net_limit=limit,
    )


def test_combo_fills_at_the_touch_with_fees():
    b = broker()
    quotes = {c(100).contract_id: q(c(100), 4.0, 4.4), c(110).contract_id: q(c(110), 1.0, 1.2)}
    fill = b.place(spread(), quotes, {}, D)
    assert isinstance(fill, ComboFill)
    assert fill.net_price == pytest.approx(4.4 - 1.0)
    # 2 units x 100 x 3.40 debit, 4 contracts x 0.65
    assert fill.cash_delta == pytest.approx(-(2 * 100 * 3.4) - 4 * 0.65)
    assert b.ledger.cash == pytest.approx(10_000 + fill.cash_delta)
    assert b.ledger.groups["cid"].legs == {c(100).contract_id: 2, c(110).contract_id: -2}
    assert b.ledger.groups["cid"].open_cost == pytest.approx(-fill.cash_delta)
    # idempotent by client id
    assert b.place(spread(), quotes, {}, D) is fill


def test_spread_fraction_zero_fills_at_mid():
    b = broker(spread_fraction=0.0, fee_per_contract=0.0)
    quotes = {c(100).contract_id: q(c(100), 4.0, 4.4), c(110).contract_id: q(c(110), 1.0, 1.2)}
    fill = b.place(spread(), quotes, {}, D)
    assert isinstance(fill, ComboFill) and fill.net_price == pytest.approx(4.2 - 1.1)


def test_combo_is_all_or_none():
    b = broker()
    quotes = {c(100).contract_id: q(c(100), 4.0, 4.4), c(110).contract_id: q(c(110), None, 0.05)}
    result = b.place(spread(), quotes, {}, D)
    assert isinstance(result, Rejection) and "no two-sided quote" in result.reason
    assert b.ledger.options == {} and b.ledger.cash == 10_000
    assert b.ledger.events[-1].kind == "rejected"


def test_net_limit_and_cash_checks():
    quotes = {c(100).contract_id: q(c(100), 4.0, 4.4), c(110).contract_id: q(c(110), 1.0, 1.2)}
    r = broker().place(spread(limit=3.0), quotes, {}, D)
    assert isinstance(r, Rejection) and "limit" in r.reason
    assert isinstance(broker().place(spread(limit=3.4), quotes, {}, D), ComboFill)
    r = broker(cash=100.0).place(spread(), quotes, {}, D)
    assert isinstance(r, Rejection) and "cash" in r.reason


def test_share_legs_fill_with_slippage():
    b = broker(share_slippage_bps=10.0, fee_per_share=0.01)
    buy_write = ComboOrder(
        "bw",
        (ComboLeg("buy", 100, shares="X.US"), ComboLeg("sell", 1, c(110))),
        structure="buy_write",
    )
    quotes = {c(110).contract_id: q(c(110), 1.0, 1.2)}
    fill = b.place(buy_write, quotes, {"X.US": 100.0}, D)
    assert isinstance(fill, ComboFill)
    assert b.ledger.shares == {"X.US": 100}
    expected = -(100 * 100.1) - 1.0 + 100 * 1.0 - 0.65
    assert fill.cash_delta == pytest.approx(expected)
    r = b.place(ComboOrder("nosp", (ComboLeg("buy", 100, shares="Y.US"),)), {}, {}, D)
    assert isinstance(r, Rejection)


def test_close_combo_empties_the_group():
    b = broker()
    quotes = {c(100).contract_id: q(c(100), 4.0, 4.4), c(110).contract_id: q(c(110), 1.0, 1.2)}
    b.place(spread(), quotes, {}, D)
    close = ComboOrder(
        "close",
        (ComboLeg("sell", 2, c(100)), ComboLeg("buy", 2, c(110))),
        group_id="cid",
        effect="close",
    )
    assert isinstance(b.place(close, quotes, {}, D), ComboFill)
    assert b.ledger.options == {} and "cid" not in b.ledger.groups


# ---- orders -----------------------------------------------------------------------


def test_combo_validation_and_leg_orders():
    with pytest.raises(ValueError):
        ComboLeg("buy", 1)
    with pytest.raises(ValueError):
        ComboLeg("buy", 0, c(100))
    with pytest.raises(ValueError):
        ComboLeg("hold", 1, c(100))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ComboOrder("x", ())
    with pytest.raises(ValueError):
        ComboOrder("x", (ComboLeg("buy", 1, c(100)),), quantity=0)
    with pytest.raises(ValueError):
        ComboOrder("x", (ComboLeg("buy", 1, c(100)),), effect="flip")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ComboOrder("x", (ComboLeg("buy", 1, c(100)), ComboLeg("sell", 1, c(100))))
    combo = spread()
    orders = combo.leg_orders()
    assert [(o.ticker, o.side, o.quantity) for o in orders] == [
        (c(100).contract_id, "buy", 2),
        (c(110).contract_id, "sell", 2),
    ]
    assert {combo_id_of(o) for o in orders} == {"cid"}
    assert combo.signed_quantities() == {c(100).contract_id: 2, c(110).contract_id: -2}
    assert combo.underlyings == {"X.US"}
    assert ComboLeg("buy", 1, c(100)).reversed().side == "sell"


# ---- early assignment -------------------------------------------------------------


def test_short_call_assigned_before_ex_dividend_when_extrinsic_is_small():
    m = ExtrinsicAssignmentModel()
    call = c(90)
    # spot 100: intrinsic 10, mark 10.2 -> extrinsic 0.2 < dividend 0.5
    check = m.check(call, -1, D, 100.0, 10.2, (date(2025, 10, 2), 0.5))
    assert check.assign and check.flag
    # ex-date too far away: only flagged
    far = m.check(call, -1, D, 100.0, 10.2, (date(2025, 10, 10), 0.5))
    assert far.flag and not far.assign
    # extrinsic well above the dividend: no risk
    assert not m.check(call, -1, D, 100.0, 12.0, (date(2025, 10, 2), 0.5)).flag
    # no dividend, a long position, out of the money, European: no risk
    assert not m.check(call, -1, D, 100.0, 10.2, None).flag
    assert not m.check(call, 1, D, 100.0, 10.2, (date(2025, 10, 2), 0.5)).flag
    assert not m.check(c(110), -1, D, 100.0, 0.5, (date(2025, 10, 2), 0.5)).flag
    euro = c(90, style="european")
    assert not m.check(euro, -1, D, 100.0, 10.2, (date(2025, 10, 2), 0.5)).flag
    # an ex-date after expiry does not matter
    assert not m.check(call, -1, D, 100.0, 10.2, (date(2025, 11, 2), 0.5)).flag


def test_deep_itm_short_put_assignment_and_flag_only_mode():
    put = c(120, "put")
    m = ExtrinsicAssignmentModel()
    assert m.check(put, -1, D, 100.0, 20.02, None).assign
    assert m.check(put, -1, D, 100.0, 20.08, None) == m.check(put, -1, D, 100.0, 20.08, None)
    flagged = m.check(put, -1, D, 100.0, 20.08, None)
    assert flagged.flag and not flagged.assign
    assert not m.check(put, -1, D, 100.0, 21.0, None).flag
    quiet = ExtrinsicAssignmentModel(assign=False)
    check = quiet.check(put, -1, D, 100.0, 20.0, None)
    assert check.flag and not check.assign
    assert not NeverAssign().check(put, -1, D, 100.0, 20.0, None).flag


def test_early_assignment_settles_in_the_ledger():
    led = OptionLedger(cash=12_000.0)
    led.fill_option(c(120, "put"), -1, 20.0, 0.0, D)
    events = led.settle("X.US:2025-10-17:P:120", 100.0, D, early=True)
    assert events[-1].kind == "early_assignment"
    assert led.shares == {"X.US": 100}
    assert led.cash == pytest.approx(12_000 + 2_000 - 12_000)
