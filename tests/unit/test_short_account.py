"""BE-30: a paper short book uses the configured margin model and borrow
lists, for its broker and for its risk context."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.backtest.simulated_broker import SimulatedBroker
from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.execution.margin import RegTMargin
from stonks.production.financing import short_account
from stonks.production.rules.settings import RuleSettings

FRI, MON = date(2026, 3, 20), date(2026, 3, 23)


def _policy(**rules) -> RiskPolicy:
    return RiskPolicy(rules=RuleSettings.model_validate(rules))


def test_be30_the_borrow_lists_come_from_the_rules():
    borrow = {"hard": ["X"], "hard_fee_rate_annual": 0.36, "none": ["Z"]}
    margin, source = short_account(_policy(borrow_check={"enabled": True, "borrow": borrow}))
    assert isinstance(margin, RegTMargin)
    assert source is not None
    assert source.quote("X", FRI, "equity").fee_rate_annual == pytest.approx(0.36)
    assert not source.quote("Z", FRI, "equity").shortable


def test_be30_the_squeeze_guard_borrow_is_used_when_the_check_has_none():
    borrow = {"hard": ["X"], "hard_fee_rate_annual": 0.18}
    _, source = short_account(_policy(squeeze_guard={"max_adverse_pct": 0.2, "borrow": borrow}))
    assert source is not None
    assert source.quote("X", FRI, "equity").fee_rate_annual == pytest.approx(0.18)


def test_be30_no_borrow_settings_keep_the_default_source():
    _, source = short_account(_policy())
    assert source is None


def test_be30_a_hard_to_borrow_short_accrues_at_the_hard_fee():
    # 10 x 100 short at 36 %/yr: 1,000 x 0.36 / 360 = 1.00 a night; 3 nights.
    margin, source = short_account(
        _policy(
            borrow_check={"enabled": True, "borrow": {"hard": ["X"], "hard_fee_rate_annual": 0.36}}
        )
    )
    broker = SimulatedBroker(Portfolio(cash=10_000.0))
    broker.enable_shorts(margin, source)
    broker.set_prices({"X": 100.0}, as_of=FRI)
    assert broker.place_order(Order("s", "X", "sell", 10.0)) is not None
    broker.accrue(FRI)
    events = broker.accrue(MON)
    assert sum(e.amount for e in events) == pytest.approx(-3.0)
