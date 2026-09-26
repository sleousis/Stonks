"""W3.1 wiring: ``RiskPolicy.rules`` from config, tighten-only merging of
portfolio overrides, and portfolio-aware forced-sell client ids."""

from __future__ import annotations

from datetime import date

from stonks.accounts.book import tighter_of
from stonks.config import RiskPolicy, Settings
from stonks.core.types import Portfolio
from stonks.production.risk import apply_risk, needs_risk_context
from stonks.production.rules.settings import RuleSettings
from tests.fixtures.risk_rules import AS_OF, bars, context


def test_rules_are_off_by_default_and_read_from_config():
    assert Settings().production.risk.rules == RuleSettings()
    settings = Settings(production={"risk": {"rules": {"max_holding": {"max_holding_bars": 10}}}})
    assert settings.production.risk.rules.max_holding.max_holding_bars == 10


def test_portfolio_overrides_only_tighten_the_rules():
    base = RiskPolicy(rules={"max_holding": {"max_holding_bars": 10}})
    tighter = tighter_of(base, {"rules": {"max_holding": {"max_holding_bars": 5}}})
    assert tighter.rules.max_holding.max_holding_bars == 5
    looser = tighter_of(base, {"rules": {"max_holding": {"max_holding_bars": 50}}})
    assert looser.rules.max_holding.max_holding_bars == 10
    added = tighter_of(RiskPolicy(), {"rules": {"sector_cap": {"max_weight_per_sector": 0.3}}})
    assert added.rules.sector_cap.max_weight_per_sector == 0.3


def test_only_history_rules_need_a_context():
    assert not needs_risk_context(RiskPolicy())
    assert needs_risk_context(RiskPolicy(rules={"max_holding": {"max_holding_bars": 3}}))
    assert not needs_risk_context(
        RiskPolicy(enabled=False, rules={"max_holding": {"max_holding_bars": 3}})
    )


def _forced_sell(portfolio_id: str | None) -> str:
    pol = RiskPolicy(rules={"max_holding": {"max_holding_bars": 2}})
    pf = Portfolio(cash=0.0, positions={"A.US": 5.0})
    ctx = context(
        pf,
        {"A.US": 100.0},
        pol,
        history={"A.US": bars([100.0] * 10)},
        entry_dates={"A.US": date(2025, 6, 1)},
        portfolio_id=portfolio_id,
    )
    result = apply_risk([], pf, {"A.US": 100.0}, {"A.US": "equity"}, pol, context=ctx)
    [order] = result.orders
    assert order.side == "sell" and order.quantity == 5.0
    return order.client_id


def test_forced_sells_carry_a_non_default_portfolio_id():
    day = AS_OF.isoformat()
    assert _forced_sell(None) == f"{day}:risk.max_holding:A.US:sell"
    assert _forced_sell("pf_default") == f"{day}:risk.max_holding:A.US:sell"
    assert _forced_sell("pf_bob") == f"{day}:pf_bob:risk.max_holding:A.US:sell"
