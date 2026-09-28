"""Maintenance margin monitoring (roadmap 19.13): the level of a margin
account from the broker's cushion, the check row, the alert, and the
margin call rule reducing before the broker liquidates."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.core.clock import FixedClock
from stonks.core.types import Order, Portfolio
from stonks.execution.brokers.base import LiveAccountState
from stonks.production.live.context import LiveContext
from stonks.production.live.margin import (
    check_margin,
    latest_check,
    margin_level,
)
from stonks.production.rules import RiskContext
from stonks.production.rules.margin_call import MarginCall, MarginCallSettings
from stonks.production.rules.settings import RuleSettings

NOW = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)
SETTINGS = MarginCallSettings(enabled=True, warn_cushion=0.15, reduce_cushion=0.10,
                              restore_cushion=0.20)  # fmt: skip


def account(equity=100_000.0, excess=50_000.0, **kw) -> LiveAccountState:
    base: dict[str, object] = {
        "equity": equity,
        "cash": 0.0,
        "settled_cash": 0.0,
        "available_funds": excess,
        "buying_power": excess * 4,
        "currency": "USD",
        "account_type": "margin",
        "reported_type": "margin",
        "excess_liquidity": excess,
        "maintenance_margin": equity - excess,
        "initial_margin": equity - excess,
    }
    base.update(kw)
    return LiveAccountState(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("excess", "level"),
    [
        (50_000.0, "ok"),
        (15_000.0, "ok"),
        (14_000.0, "warn"),
        (9_000.0, "reduce"),
        (-1.0, "call"),
    ],  # fmt: skip
)
def test_margin_level_follows_the_cushion(excess, level):
    assert margin_level(account(excess=excess), SETTINGS) == level


def test_no_cushion_means_no_level():
    a = account(excess_liquidity=None, maintenance_margin=0.0)
    assert margin_level(a, SETTINGS) is None


def test_a_reduce_above_warn_still_warns_first():
    odd = MarginCallSettings(warn_cushion=0.05, reduce_cushion=0.10)
    assert margin_level(account(excess=8_000.0), odd) == "reduce"
    assert margin_level(account(excess=12_000.0), odd) == "ok"


def test_check_records_a_row_and_alerts_once_per_level_and_day(state):
    sent = []
    clock = FixedClock(NOW)
    check = check_margin(state, "pf_default", account(excess=12_000.0), SETTINGS,
                         source="monitor", publish=sent.append, clock=clock)  # fmt: skip
    assert check is not None and check.level == "warn"
    got = latest_check(state, "pf_default")
    assert got is not None and got.level == "warn" and got.cushion == pytest.approx(0.12)
    assert len(sent) == 1
    assert sent[0].dedupe_key == "margin:pf_default:warn:2026-09-28"
    assert sent[0].urgency == "normal"
    assert "pf_default" not in sent[0].body or "12" in sent[0].body
    check_margin(state, "pf_default", account(excess=5_000.0), SETTINGS,
                 source="tick", publish=sent.append, clock=clock)  # fmt: skip
    assert sent[-1].urgency == "high" and sent[-1].level == "error"
    assert "reduce" in sent[-1].body.lower() or "close" in sent[-1].body.lower()


def test_an_ok_check_is_recorded_without_an_alert(state):
    sent = []
    check_margin(state, "pf_default", account(), SETTINGS, source="monitor",
                 publish=sent.append, clock=FixedClock(NOW))  # fmt: skip
    assert sent == []
    got = latest_check(state, "pf_default")
    assert got is not None and got.level == "ok" and got.source == "monitor"


def test_a_cash_account_is_not_checked(state):
    a = account(account_type="cash", reported_type="cash")
    assert check_margin(state, "pf_default", a, SETTINGS, source="tick") is None
    assert latest_check(state, "pf_default") is None


# ---- the margin call rule reduces before the broker does -----------------------------------


def ctx_with(acct: LiveAccountState, positions: dict[str, float]) -> RiskContext:
    policy = RuleSettings(margin_call=SETTINGS)

    class _Policy:
        rules = policy

    return RiskContext(
        portfolio=Portfolio(cash=-20_000.0, positions=positions),
        prices={"AAPL.US": 100.0, "MSFT.US": 400.0},
        asset_classes={},
        policy=_Policy(),
        portfolio_id="pf_live",
        as_of=date(2026, 9, 28),
        live=LiveContext(portfolio_id="pf_live", account=acct),
    )


def test_a_reduce_level_forces_closes_and_drops_opens():
    ctx = ctx_with(account(excess=5_000.0), {"AAPL.US": 500.0, "MSFT.US": 200.0})
    buy = Order(client_id="b", ticker="AAPL.US", side="buy", quantity=10.0)
    kept, adjustments = MarginCall().apply([buy], ctx)
    assert buy not in kept
    forced = [o for o in kept if o.decision_context and o.decision_context.get("forced")]
    assert forced and all(o.side == "sell" and o.position_effect == "close" for o in forced)
    # 20% of 100,000 minus 5,000: 15,000 of maintenance to free
    assert forced[0].decision_context["deficit"] == pytest.approx(15_000.0)
    assert any("cushion" in a.reason for a in adjustments)


def test_a_healthy_live_margin_account_is_left_to_the_what_if():
    ctx = ctx_with(account(excess=60_000.0), {"AAPL.US": 500.0})
    buy = Order(client_id="b", ticker="AAPL.US", side="buy", quantity=1_000_000.0)
    kept, adjustments = MarginCall().apply([buy], ctx)
    assert kept == [buy] and adjustments == []


def test_a_warn_level_changes_no_order():
    ctx = ctx_with(account(excess=12_000.0), {"AAPL.US": 500.0})
    buy = Order(client_id="b", ticker="AAPL.US", side="buy", quantity=1.0)
    kept, adjustments = MarginCall().apply([buy], ctx)
    assert kept == [buy] and adjustments == []
