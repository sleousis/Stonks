"""Phase 16.1: dividends and splits on short positions, in the backtest and
in the tick's ledger path."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from stonks.backtest.corporate_actions import apply_to_portfolio, dividend_cash
from stonks.core.corporate_actions import CorporateActions, Dividend, Split
from stonks.core.types import Portfolio
from stonks.production.corporate_actions import (
    CorporateActionPlan,
    PlannedAction,
    _base_quantity,
    apply_plan,
)

EX = date(2026, 3, 18)
AS_OF = datetime(2026, 3, 18)


def test_a_short_pays_the_dividend_in_full_despite_withholding() -> None:
    # Short 10, dividend 1.00, 30 % withholding: the short still pays 10.00.
    p = Portfolio(cash=100.0, positions={"X": -10.0})
    rec = apply_to_portfolio(p, Dividend("X", EX, 1.0), AS_OF, withholding_rate=0.3)
    assert rec is not None and rec.cash_delta == pytest.approx(-10.0)
    assert p.cash == pytest.approx(90.0)


def test_a_long_is_credited_net_of_withholding() -> None:
    assert dividend_cash(10.0, 1.0, 0.3) == pytest.approx(7.0)
    assert dividend_cash(-10.0, 1.0, 0.3) == pytest.approx(-10.0)


def test_a_split_scales_a_short() -> None:
    # Short 10 at 2:1 becomes short 20; the price halves, the value holds.
    p = Portfolio(cash=1_000.0, positions={"X": -10.0})
    rec = apply_to_portfolio(p, Split("X", EX, 2.0), AS_OF)
    assert p.positions == {"X": -20.0}
    assert rec is not None and (rec.quantity_before, rec.quantity_after) == (-10.0, -20.0)
    assert p.total_value({"X": 50.0}) == Portfolio(1_000.0, {"X": -10.0}).total_value({"X": 100.0})


# ---- the tick's ledger path ------------------------------------------------------------


def _plan(event, base: float) -> CorporateActionPlan:
    return CorporateActionPlan(due=[PlannedAction(event, base)])


def test_tick_dividend_on_a_short_is_debited_in_full() -> None:
    p = Portfolio(cash=100.0, positions={"X": -10.0})
    [rec] = apply_plan(p, _plan(Dividend("X", EX, 0.5), -10.0), withholding_rate=0.3)
    assert rec.cash_delta == pytest.approx(-5.0)
    assert p.cash == pytest.approx(95.0)


def test_tick_split_on_a_short() -> None:
    p = Portfolio(cash=100.0, positions={"X": -10.0})
    [rec] = apply_plan(p, _plan(Split("X", EX, 3.0), -10.0))
    assert p.positions == {"X": -30.0}
    assert rec.quantity_after == -30.0


def test_tick_split_never_flips_the_sign() -> None:
    # Held -2 now, but the base was -10 (covered 8 since): a reverse split
    # 1:10 would take -2 + (0.1 - 1) x -10 = +7; it stops at flat.
    p = Portfolio(cash=0.0, positions={"X": -2.0})
    apply_plan(p, _plan(Split("X", EX, 0.1), -10.0))
    assert p.positions == {}
    q = Portfolio(cash=0.0, positions={"X": 2.0})
    apply_plan(q, _plan(Split("X", EX, 0.1), 10.0))
    assert q.positions == {}


def test_tick_zero_base_is_skipped() -> None:
    p = Portfolio(cash=1.0, positions={"X": -1.0})
    assert apply_plan(p, _plan(Dividend("X", EX, 1.0), 0.0)) == []


def test_base_quantity_is_signed_for_shorts() -> None:
    actions = CorporateActions.from_events([Split("X", date(2026, 3, 16), 2.0)])
    history = [(date(2026, 3, 13), {"X": -5.0}), (date(2026, 3, 17), {"X": -10.0})]
    # The latest snapshot before the ex-date is -10 (already post-split).
    assert _base_quantity(history, Dividend("X", EX, 1.0), actions) == -10.0
    # From the 13th: -5 scaled by the 2:1 split on the 16th.
    early = [(date(2026, 3, 13), {"X": -5.0})]
    assert _base_quantity(early, Dividend("X", EX, 1.0), actions) == -10.0


def test_base_quantity_is_zero_after_a_flip() -> None:
    history = [
        (date(2026, 3, 13), {"X": -5.0}),
        (date(2026, 3, 20), {"X": 5.0}),  # flipped long after the ex-date
    ]
    assert _base_quantity(history, Dividend("X", EX, 1.0), CorporateActions()) == 0.0
    assert (
        _base_quantity([(date(2026, 3, 13), {})], Dividend("X", EX, 1.0), CorporateActions()) == 0.0
    )
