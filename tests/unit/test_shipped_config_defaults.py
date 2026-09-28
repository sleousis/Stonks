"""The shipped config (``config/default.toml``, what a new install runs) turns
on the protective defaults (complexity audit F55, F67) and keeps a test book
for approved strategies (F28). The pydantic defaults stay permissive for
backtests and library use; this file is the install's policy."""

from __future__ import annotations

from stonks.accounts.book import tighter_of
from stonks.config import DEFAULT_CONFIG_PATH, RiskPolicy, load_settings


def _shipped():
    return load_settings(DEFAULT_CONFIG_PATH)


def test_one_ticker_cannot_take_the_whole_portfolio():
    risk = _shipped().production.risk
    assert risk.enabled
    assert risk.max_weight_per_ticker == 0.25


def test_the_circuit_breaker_is_on_with_the_recommended_limits():
    breaker = _shipped().production.risk.rules.circuit_breaker
    assert breaker.active
    assert breaker.max_month_loss == 0.06
    assert breaker.max_week_loss == 0.04
    assert breaker.max_drawdown_halt == 0.20


def test_the_drawdown_scaler_is_on():
    schedule = _shipped().production.risk.rules.drawdown_scaling.schedule
    assert schedule == ((0.10, 0.5), (0.20, 0.0))


def test_overrides_still_only_tighten_the_shipped_policy():
    base = _shipped().production.risk
    looser = tighter_of(
        base,
        {"max_weight_per_ticker": 1.0, "rules": {"circuit_breaker": {"max_month_loss": 0.5}}},
    )
    assert looser.max_weight_per_ticker == 0.25
    assert looser.rules.circuit_breaker.max_month_loss == 0.06
    tighter = tighter_of(base, {"max_weight_per_ticker": 0.1})
    assert tighter.max_weight_per_ticker == 0.1


def test_approved_strategies_keep_a_test_book():
    assert _shipped().production.model_books == "all"


def test_the_library_defaults_stay_permissive_for_backtests():
    policy = RiskPolicy()
    assert policy.max_weight_per_ticker == 1.0
    assert not policy.rules.circuit_breaker.active
    assert policy.rules.drawdown_scaling.schedule is None
