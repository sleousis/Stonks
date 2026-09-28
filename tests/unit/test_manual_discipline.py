"""Discipline rules for manual orders (roadmap 23.4): off by default, act
only on a person's own entries, never on an exit (P28)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from stonks.config import RiskPolicy
from stonks.core.types import Order, Portfolio
from stonks.production.rules import RiskContext, registered_rules
from stonks.production.rules._manual import ManualContext
from stonks.production.rules.manual_discipline import (
    ManualDiscipline,
    ManualDisciplineSettings,
)
from stonks.production.rules.settings import RuleSettings, tighter_rule_settings

NOW = datetime(2026, 4, 2, 15, 0, tzinfo=UTC)


def _policy(**kw) -> RiskPolicy:
    return RiskPolicy(
        rules=RuleSettings(manual_discipline=ManualDisciplineSettings(enabled=True, **kw))
    )


def _ctx(policy: RiskPolicy, manual: ManualContext | None, positions=None) -> RiskContext:
    return RiskContext(
        portfolio=Portfolio(cash=10_000.0, positions=positions or {}),
        prices={"UP.US": 100.0},
        asset_classes={"UP.US": "equity"},
        policy=policy,
        manual=manual,
    )


def _buy(qty: float = 10.0) -> Order:
    return Order(client_id="manual:pf:k", ticker="UP.US", side="buy", quantity=qty)


def _sell(qty: float = 10.0) -> Order:
    return Order(client_id="manual:pf:s", ticker="UP.US", side="sell", quantity=qty)


def test_registered_and_off_by_default():
    names = {r.name for r in registered_rules()}
    assert "manual_discipline" in names
    assert not ManualDiscipline().enabled(RiskPolicy())
    assert ManualDiscipline().enabled(_policy())


def test_strategy_orders_are_untouched():
    kept, adj = ManualDiscipline().apply([_buy()], _ctx(_policy(), None))
    assert kept == [_buy()] and adj == []


def test_live_entry_without_stop_is_dropped():
    manual = ManualContext(now=NOW, live=True, has_stop=False)
    kept, adj = ManualDiscipline().apply([_buy()], _ctx(_policy(), manual))
    assert kept == [] and "stop" in adj[0].reason
    ok = ManualContext(now=NOW, live=True, has_stop=True)
    assert ManualDiscipline().apply([_buy()], _ctx(_policy(), ok))[0] == [_buy()]
    paper = ManualContext(now=NOW, live=False, has_stop=False)
    assert ManualDiscipline().apply([_buy()], _ctx(_policy(), paper))[0] == [_buy()]


def test_cooldown_after_a_losing_exit():
    policy = _policy(cooldown_minutes=60)
    recent = ManualContext(now=NOW, last_losing_exit_at=NOW - timedelta(minutes=20))
    kept, adj = ManualDiscipline().apply([_buy()], _ctx(policy, recent))
    assert kept == [] and "15:40" in adj[0].reason
    old = ManualContext(now=NOW, last_losing_exit_at=NOW - timedelta(minutes=90))
    assert ManualDiscipline().apply([_buy()], _ctx(policy, old))[0] == [_buy()]


def test_daily_entry_cap_and_loss_limit():
    capped = ManualContext(now=NOW, entries_today=3)
    kept, adj = ManualDiscipline().apply([_buy()], _ctx(_policy(max_entries_per_day=3), capped))
    assert kept == [] and "3 manual entries" in adj[0].reason
    lost = ManualContext(now=NOW, pnl_today=-600.0)
    kept, adj = ManualDiscipline().apply([_buy()], _ctx(_policy(max_daily_loss=500.0), lost))
    assert kept == [] and "loss" in adj[0].reason


def test_an_exit_always_passes():
    manual = ManualContext(now=NOW, live=True, entries_today=99, pnl_today=-1e6)
    policy = _policy(max_entries_per_day=1, max_daily_loss=1.0)
    kept, adj = ManualDiscipline().apply([_sell()], _ctx(policy, manual, {"UP.US": 10.0}))
    assert kept == [_sell()] and adj == []


def test_overrides_only_tighten():
    base = RuleSettings(
        manual_discipline=ManualDisciplineSettings(enabled=True, cooldown_minutes=30)
    )
    merged = tighter_rule_settings(
        base,
        {
            "manual_discipline": {
                "enabled": False,
                "require_stop_live": False,
                "cooldown_minutes": 10,
                "max_entries_per_day": 5,
            }
        },
    )
    got = merged.manual_discipline
    assert got.enabled and got.require_stop_live
    assert got.cooldown_minutes == 30 and got.max_entries_per_day == 5


@pytest.mark.parametrize("field", ["cooldown_minutes", "max_entries_per_day"])
def test_limits_must_be_positive(field):
    with pytest.raises(ValueError):
        ManualDisciplineSettings(**{field: 0})
