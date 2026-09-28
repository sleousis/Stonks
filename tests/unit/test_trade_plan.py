"""Trade plan on the manual ticket (roadmap 23.4): entry, stop and target
checks, and a whole-share size from a chosen risk."""

from __future__ import annotations

import pytest

from stonks.production.trade_plan import (
    PlanError,
    check_plan,
    reward_risk,
    risk_per_share,
    size_from_risk,
)


def test_long_plan_needs_stop_below_and_target_above():
    check_plan("buy", 100.0, stop=95.0, target=110.0)
    with pytest.raises(PlanError, match="below"):
        check_plan("buy", 100.0, stop=101.0, target=None)
    with pytest.raises(PlanError, match="above"):
        check_plan("buy", 100.0, stop=None, target=99.0)


def test_short_plan_mirrors_the_long_one():
    check_plan("sell", 100.0, stop=105.0, target=90.0)
    with pytest.raises(PlanError, match="above"):
        check_plan("sell", 100.0, stop=99.0, target=None)
    with pytest.raises(PlanError, match="below"):
        check_plan("sell", 100.0, stop=None, target=101.0)


def test_risk_per_share_and_reward_risk():
    assert risk_per_share("buy", 100.0, 95.0) == pytest.approx(5.0)
    assert risk_per_share("sell", 100.0, 104.0) == pytest.approx(4.0)
    assert reward_risk("buy", 100.0, 95.0, 115.0) == pytest.approx(3.0)
    assert reward_risk("buy", 100.0, 95.0, None) is None


def test_size_from_percent_of_book_is_whole_shares():
    size = size_from_risk(side="buy", entry=100.0, stop=97.0, equity=50_000.0, risk_percent=1.0)
    # 1% of 50k = 500; 500 / 3 = 166.67 -> 166 whole shares
    assert size.quantity == 166
    assert size.risk_budget == pytest.approx(500.0)
    assert size.risk_amount == pytest.approx(498.0)
    assert size.notional == pytest.approx(16_600.0)
    assert size.capped_by is None


def test_size_from_amount_and_target():
    size = size_from_risk(
        side="sell", entry=50.0, stop=52.0, equity=10_000.0, risk_amount=250.0, target=44.0
    )
    assert size.quantity == 125
    assert size.reward_risk == pytest.approx(3.0)


def test_cash_caps_a_buy():
    size = size_from_risk(
        side="buy", entry=100.0, stop=99.0, equity=100_000.0, risk_percent=2.0, cash=5_050.0
    )
    assert size.quantity == 50
    assert size.capped_by == "cash"


def test_risk_too_small_for_one_share_gives_zero_with_a_note():
    size = size_from_risk(side="buy", entry=100.0, stop=90.0, equity=500.0, risk_percent=1.0)
    assert size.quantity == 0
    assert size.note and "one share" in size.note


def test_size_needs_exactly_one_risk_and_a_stop():
    with pytest.raises(PlanError, match="one of"):
        size_from_risk(side="buy", entry=100.0, stop=95.0, equity=1.0)
    with pytest.raises(PlanError, match="one of"):
        size_from_risk(
            side="buy", entry=100.0, stop=95.0, equity=1.0, risk_percent=1.0, risk_amount=5.0
        )
    with pytest.raises(PlanError, match="below"):
        size_from_risk(side="buy", entry=100.0, stop=100.0, equity=1_000.0, risk_percent=1.0)
