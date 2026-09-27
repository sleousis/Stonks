"""The ``approve`` subscription mode (roadmap 19.8): an optional mode between
paper and auto. The tick decides and every order waits for a person.
Switching to it passes the same checklist as auto. From approve, auto needs
nothing more."""

from __future__ import annotations

import pytest

from stonks.accounts import (
    MIN_PAPER_DAYS_FOR_AUTO,
    AutoGateRefused,
    BookSpec,
    Mode,
    PortfolioRepository,
    Role,
    Scope,
    SubscriptionRepository,
    UserRepository,
)
from stonks.config import RiskPolicy
from stonks.store.state import SqliteState
from tests.fixtures.paper import link_connection, seed_paper_days

NOW = "2026-01-02T15:00:00+00:00"


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    for sid, status in [("s_active", "active"), ("s_shadow", "shadow")]:
        s.execute(
            "INSERT INTO strategies (id, class_path, params_json, status, created_at, updated_at)"
            " VALUES (?, 'm:C', '{}', 'shadow', ?, ?)",
            [sid, NOW, NOW],
        )
        if status != "shadow":
            s.execute(
                "INSERT INTO status_changes (strategy_id, from_status, to_status, actor, reason,"
                " override, created_at) VALUES (?, 'shadow', ?, 't', 'seed', 1, ?)",
                [sid, status, NOW],
            )
            s.execute("UPDATE strategies SET status=? WHERE id=?", [status, sid])
    yield s
    s.close()


@pytest.fixture
def subs(state):
    return SubscriptionRepository(state)


@pytest.fixture
def alice(state):
    users = UserRepository(state)
    return Scope.for_user(users.create(display_name="Alice", role=Role.TRADER, actor="t"))


def _paper_on_live(state, alice, subs, days: int = MIN_PAPER_DAYS_FOR_AUTO):
    pf = PortfolioRepository(state).create(alice, name="Live", kind="broker")
    link_connection(state, pf.id)
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    seed_paper_days(state, sub.id, days, portfolio_id=pf.id)
    return pf, sub


def test_the_mode_ladder():
    assert [m.value for m in Mode] == ["notify", "paper", "approve", "auto"]
    assert Mode.APPROVE.places_orders and Mode.APPROVE.needs_portfolio
    assert Mode.APPROVE.trades_live and Mode.AUTO.trades_live
    assert not Mode.PAPER.trades_live and not Mode.NOTIFY.trades_live
    assert Mode.APPROVE.needs_approval and not Mode.AUTO.needs_approval


def test_a_new_subscription_cannot_start_in_approve(state, alice, subs):
    pf = PortfolioRepository(state).create(alice, name="Live", kind="broker")
    with pytest.raises(AutoGateRefused):
        subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.APPROVE)


def test_approve_passes_the_auto_checklist(state, alice, subs):
    _, sub = _paper_on_live(state, alice, subs, days=MIN_PAPER_DAYS_FOR_AUTO - 1)
    with pytest.raises(AutoGateRefused) as err:
        subs.set_mode(alice, sub.id, Mode.APPROVE)
    assert any("paper" in r for r in err.value.reasons)
    assert subs.get(alice, sub.id).mode is Mode.PAPER


def test_approve_on_a_simulated_portfolio_is_refused(state, alice, subs):
    pf = PortfolioRepository(state).create(alice, name="Sim")
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    with pytest.raises(AutoGateRefused) as err:
        subs.set_mode(alice, sub.id, Mode.APPROVE)
    assert "approve mode needs a broker portfolio" in err.value.reasons


def test_shadow_strategies_cannot_use_approve(state, alice, subs):
    pf = PortfolioRepository(state).create(alice, name="Live", kind="broker")
    link_connection(state, pf.id)
    sub = subs.subscribe(alice, strategy_id="s_shadow", portfolio_id=pf.id, mode=Mode.PAPER)
    with pytest.raises(AutoGateRefused) as err:
        subs.set_mode(alice, sub.id, Mode.APPROVE)
    assert "the strategy is not active" in err.value.reasons


def test_paper_to_approve_to_auto_and_back(state, alice, subs):
    _, sub = _paper_on_live(state, alice, subs)
    on = subs.set_mode(alice, sub.id, Mode.APPROVE)
    assert on.mode is Mode.APPROVE
    assert on.auto_enabled_by == alice.actor and on.auto_enabled_at is not None
    assert on.paper_days_completed == MIN_PAPER_DAYS_FOR_AUTO
    auto = subs.set_mode(alice, sub.id, Mode.AUTO)
    assert auto.mode is Mode.AUTO
    back = subs.set_mode(alice, sub.id, Mode.APPROVE)
    assert back.mode is Mode.APPROVE and back.paused_reason is None
    paper = subs.set_mode(alice, sub.id, Mode.PAPER)
    assert paper.auto_enabled_at is None
    modes = [
        r["details_json"]
        for r in state.sql(
            "SELECT details_json FROM audit_log WHERE action = 'subscription.mode' ORDER BY id"
        )
    ]
    assert len(modes) == 4


def test_the_book_trades_approve_subscriptions(state, alice, subs):
    pf, sub = _paper_on_live(state, alice, subs)
    subs.set_mode(alice, sub.id, Mode.APPROVE)
    portfolio = PortfolioRepository(state).get(alice, pf.id)
    spec = BookSpec.for_portfolio(
        portfolio,
        subs.list_for_portfolio(alice, pf.id),
        global_risk=RiskPolicy(),
        global_construction={},
        default_initial_cash=1000.0,
    )
    assert spec.strategy_modes == {"s_active": Mode.APPROVE}
