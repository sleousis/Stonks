"""Accounts repositories over a migrated state DB: users, portfolios,
subscriptions (modes and the auto gate), the audit log and tenant isolation."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.accounts import (
    DEFAULT_OWNER_ID,
    DEFAULT_PORTFOLIO_ID,
    MIN_PAPER_DAYS_FOR_AUTO,
    AccountsError,
    AuditLog,
    AutoGateRefused,
    BookSpec,
    Mode,
    NotFound,
    PortfolioRepository,
    Role,
    Scope,
    SubscriptionRepository,
    UserRepository,
    owned_portfolio,
)
from stonks.config import RiskPolicy
from stonks.store.state import SqliteState
from tests.fixtures.paper import link_connection, seed_paper_days

NOW = "2026-01-02T15:00:00+00:00"
SYSTEM = Scope.service("system")


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    for sid, status in [("s_active", "active"), ("s_shadow", "shadow"), ("s_retired", "retired")]:
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
def users(state):
    return UserRepository(state)


@pytest.fixture
def portfolios(state):
    return PortfolioRepository(state)


@pytest.fixture
def subs(state):
    return SubscriptionRepository(state)


@pytest.fixture
def alice(users):
    return Scope.for_user(users.create(display_name="Alice", role=Role.TRADER, actor="t"))


@pytest.fixture
def bob(users):
    return Scope.for_user(users.create(display_name="Bob", role=Role.TRADER, actor="t"))


def _owner(users: UserRepository) -> Scope:
    return Scope.for_user(users.get(DEFAULT_OWNER_ID))


# ---- users -------------------------------------------------------------------


def test_default_owner_is_an_admin(users):
    owner = users.get(DEFAULT_OWNER_ID)
    assert owner.role is Role.ADMIN
    assert owner.role.is_admin and owner.role.can_trade
    assert not Role.VIEWER.can_trade


def test_create_user_and_find_by_email_case_insensitively(users):
    u = users.create(display_name="Carol", role=Role.VIEWER, email="Carol@X.io", actor="t")
    assert u.id.startswith("usr_")
    assert users.get_by_email("carol@x.io") == u
    with pytest.raises(NotFound):
        users.get("usr_missing")


def test_user_creation_is_audited(state, users):
    u = users.create(display_name="Dan", role=Role.TRADER, actor="user:usr_owner")
    (row,) = state.sql("SELECT * FROM audit_log WHERE action = 'user.create'")
    assert row["actor"] == "user:usr_owner"
    assert row["target_id"] == u.id


def test_disabling_a_user_pauses_their_auto_subscriptions(state, users, alice, portfolios, subs):
    pf = portfolios.create(alice, name="Live", kind="broker")
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    state.execute("UPDATE subscriptions SET mode='auto' WHERE id=?", [sub.id])
    users.set_status(alice.user_id, "disabled", actor="user:usr_owner")
    assert subs.get(SYSTEM, sub.id).paused_reason == "user_disabled"
    # A disabled user's scope sees nothing any more.
    with pytest.raises(NotFound):
        owned_portfolio(state, alice, pf.id)


# ---- scope -------------------------------------------------------------------


def test_scope_actor_strings(alice):
    assert alice.actor == f"user:{alice.user_id}"
    assert SYSTEM.actor == "service:system"
    assert Scope.service("scheduler").is_service


def test_scope_kind_and_id_must_agree():
    with pytest.raises(ValueError):
        Scope(user_id="usr_x", role=Role.ADMIN, kind="service")
    with pytest.raises(ValueError):
        Scope(user_id="svc_scheduler", role=Role.ADMIN)


def test_owned_portfolio_returns_own_row(state, alice, portfolios):
    pf = portfolios.create(alice, name="Mine")
    assert owned_portfolio(state, alice, pf.id) == pf


def test_owned_portfolio_hides_other_users_rows_as_not_found(state, alice, bob, portfolios):
    pf = portfolios.create(alice, name="Mine")
    with pytest.raises(NotFound):
        owned_portfolio(state, bob, pf.id)
    with pytest.raises(NotFound):
        owned_portfolio(state, bob, "pf_missing")


def test_admins_do_not_see_other_users_portfolios(state, users, alice, portfolios):
    """Decision 2026-09-26: admins see totals only, never individual books."""
    pf = portfolios.create(alice, name="Mine")
    with pytest.raises(NotFound):
        owned_portfolio(state, _owner(users), pf.id)
    assert pf.id not in {p.id for p in portfolios.list(_owner(users))}


def test_service_scope_reaches_every_portfolio(state, alice, portfolios):
    pf = portfolios.create(alice, name="Mine")
    assert owned_portfolio(state, SYSTEM, pf.id) == pf
    assert {DEFAULT_PORTFOLIO_ID, pf.id} <= {p.id for p in portfolios.list(SYSTEM)}


# ---- portfolios --------------------------------------------------------------


def test_portfolio_defaults(alice, portfolios):
    pf = portfolios.create(alice, name="Book")
    assert pf.owner_id == alice.user_id
    assert pf.kind == "simulated"
    assert pf.allow_short is False
    assert pf.status == "active"
    assert pf.risk_policy == {}
    assert pf.universe is None


def test_list_returns_only_own_portfolios(alice, bob, portfolios):
    a = portfolios.create(alice, name="A")
    portfolios.create(bob, name="B")
    assert [p.id for p in portfolios.list(alice)] == [a.id]


def test_portfolio_risk_policy_is_validated_and_audited(state, alice, portfolios):
    pf = portfolios.create(alice, name="Book")
    with pytest.raises(ValueError):
        portfolios.set_risk_policy(alice, pf.id, {"max_weight_per_ticker": 3})
    updated = portfolios.set_risk_policy(alice, pf.id, {"max_weight_per_ticker": 0.1})
    assert updated.risk_policy == {"max_weight_per_ticker": 0.1}
    actions = [r["action"] for r in state.sql("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-1] == "portfolio.risk_policy"


def test_cannot_change_another_users_portfolio(alice, bob, portfolios):
    pf = portfolios.create(alice, name="Book")
    with pytest.raises(NotFound):
        portfolios.set_risk_policy(bob, pf.id, {"max_weight_per_ticker": 0.1})
    with pytest.raises(NotFound):
        portfolios.set_status(bob, pf.id, "paused")


# ---- subscriptions -----------------------------------------------------------


def test_subscribe_notify_without_portfolio(alice, subs):
    sub = subs.subscribe(alice, strategy_id="s_shadow")
    assert sub.mode is Mode.NOTIFY
    assert sub.portfolio_id is None
    assert subs.list_for_user(alice) == [sub]


def test_paper_needs_an_owned_portfolio(alice, bob, portfolios, subs):
    with pytest.raises(AccountsError):
        subs.subscribe(alice, strategy_id="s_active", mode=Mode.PAPER)
    bobs = portfolios.create(bob, name="Bob book")
    with pytest.raises(NotFound):
        subs.subscribe(alice, strategy_id="s_active", portfolio_id=bobs.id, mode=Mode.PAPER)


def test_retired_or_unknown_strategies_cannot_be_subscribed(alice, subs):
    with pytest.raises(AccountsError):
        subs.subscribe(alice, strategy_id="s_retired")
    with pytest.raises(NotFound):
        subs.subscribe(alice, strategy_id="s_nope")


def test_new_subscription_cannot_start_in_auto(alice, portfolios, subs):
    pf = portfolios.create(alice, name="Live", kind="broker")
    with pytest.raises(AutoGateRefused):
        subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.AUTO)


def test_other_users_subscriptions_are_not_found(alice, bob, subs):
    sub = subs.subscribe(alice, strategy_id="s_shadow")
    with pytest.raises(NotFound):
        subs.get(bob, sub.id)
    with pytest.raises(NotFound):
        subs.set_mode(bob, sub.id, Mode.NOTIFY)
    assert subs.list_for_user(bob) == []


def test_list_for_portfolio_checks_ownership(alice, bob, portfolios, subs):
    pf = portfolios.create(alice, name="Book")
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    assert subs.list_for_portfolio(alice, pf.id) == [sub]
    with pytest.raises(NotFound):
        subs.list_for_portfolio(bob, pf.id)


def _live(state, portfolios, scope, name="Live"):
    pf = portfolios.create(scope, name=name, kind="broker")
    link_connection(state, pf.id)
    return pf


def test_paper_days_come_from_portfolio_runs(state, alice, portfolios, subs):
    pf = portfolios.create(alice, name="Book")
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    seed_paper_days(state, sub.id, 3, portfolio_id=pf.id)
    assert subs.get(alice, sub.id).paper_days_completed == 3
    assert subs.list_for_user(alice)[0].paper_days_completed == 3


def test_a_risk_breach_restarts_the_paper_count(state, alice, portfolios, subs):
    pf = portfolios.create(alice, name="Book")
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    seed_paper_days(state, sub.id, 5, portfolio_id=pf.id)
    # five weekdays from Thu 1 Jan end on Wed 7 Jan; the breach comes after
    seed_paper_days(state, sub.id, 1, start=date(2026, 1, 8), breached=True, portfolio_id=pf.id)
    assert subs.get(alice, sub.id).paper_days_completed == 0


def test_auto_requires_twenty_paper_days(state, alice, portfolios, subs):
    pf = _live(state, portfolios, alice)
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    seed_paper_days(state, sub.id, MIN_PAPER_DAYS_FOR_AUTO - 1)
    with pytest.raises(AutoGateRefused) as err:
        subs.set_mode(alice, sub.id, Mode.AUTO)
    assert any("paper" in r for r in err.value.reasons)
    seed_paper_days(state, sub.id, 1, start=date(2026, 2, 1))
    on = subs.set_mode(alice, sub.id, Mode.AUTO)
    assert on.mode is Mode.AUTO
    assert on.auto_enabled_by == alice.actor
    assert on.auto_enabled_at is not None


def test_auto_gate_lists_every_failed_check(alice, portfolios, subs):
    pf = portfolios.create(alice, name="Sim")  # simulated, not broker
    sub = subs.subscribe(alice, strategy_id="s_shadow", portfolio_id=pf.id, mode=Mode.PAPER)
    reasons = subs.auto_blockers(alice, sub.id)
    assert len(reasons) == 3  # not active, not a broker portfolio, too few paper days
    with pytest.raises(AutoGateRefused):
        subs.set_mode(alice, sub.id, Mode.AUTO)


def test_leaving_paper_for_notify_restarts_the_count(state, alice, portfolios, subs):
    pf = _live(state, portfolios, alice)
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    seed_paper_days(state, sub.id, MIN_PAPER_DAYS_FOR_AUTO)
    subs.set_mode(alice, sub.id, Mode.NOTIFY)
    assert subs.get(alice, sub.id).paper_days_completed == 0
    back = subs.set_mode(alice, sub.id, Mode.PAPER)
    assert back.paper_days_completed == 0
    with pytest.raises(AutoGateRefused):
        subs.set_mode(alice, sub.id, Mode.AUTO)


def test_auto_back_to_paper_clears_auto_fields(state, alice, portfolios, subs):
    pf = _live(state, portfolios, alice)
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    seed_paper_days(state, sub.id, MIN_PAPER_DAYS_FOR_AUTO)
    subs.set_mode(alice, sub.id, Mode.AUTO)
    back = subs.set_mode(alice, sub.id, Mode.PAPER)
    assert back.mode is Mode.PAPER
    assert back.auto_enabled_at is None and back.paused_reason is None
    assert back.paper_days_completed == MIN_PAPER_DAYS_FOR_AUTO


def test_mode_changes_are_audited(state, alice, portfolios, subs):
    pf = portfolios.create(alice, name="Book")
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    subs.set_mode(alice, sub.id, Mode.NOTIFY)
    entries = AuditLog(state).for_portfolio(alice, pf.id)
    mode_rows = [e for e in entries if e.action == "subscription.mode"]
    assert len(mode_rows) == 1
    assert mode_rows[0].details == {"from": "paper", "to": "notify"}
    assert mode_rows[0].actor == alice.actor
    assert mode_rows[0].target_id == sub.id


def test_disable_and_enable_a_subscription_are_audited(state, alice, bob, portfolios, subs):
    pf = portfolios.create(alice, name="Book")
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    assert sub.enabled
    off = subs.disable(alice, sub.id, reason="holiday")
    assert not off.enabled and off.mode is Mode.PAPER
    assert subs.disable(alice, sub.id).enabled is False  # idempotent, no second row
    on = subs.enable(alice, sub.id)
    assert on.enabled
    actions = [
        (e.action, e.details)
        for e in AuditLog(state).for_portfolio(alice, pf.id)
        if e.action.startswith("subscription.")
    ]
    assert ("subscription.disable", {"reason": "holiday"}) in actions
    assert ("subscription.enable", {}) in actions
    assert sum(1 for a, _ in actions if a == "subscription.disable") == 1
    with pytest.raises(NotFound):
        subs.disable(bob, sub.id)


def test_audit_log_for_portfolio_is_scoped(state, alice, bob, portfolios):
    pf = portfolios.create(alice, name="Book")
    with pytest.raises(NotFound):
        AuditLog(state).for_portfolio(bob, pf.id)


def test_backfilled_default_book(state, users, subs, portfolios):
    owner = _owner(users)
    pf = owned_portfolio(state, owner, DEFAULT_PORTFOLIO_ID)
    subs.subscribe(owner, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    book = BookSpec.for_portfolio(
        pf,
        subs.list_for_portfolio(owner, pf.id),
        global_risk=RiskPolicy(max_weight_per_ticker=0.5),
        default_initial_cash=10_000.0,
    )
    assert book.portfolio_id == DEFAULT_PORTFOLIO_ID
    assert dict(book.strategy_weights or {}) == {"s_active": 1.0}
    assert book.initial_cash == 10_000.0
    assert book.risk.max_weight_per_ticker == 0.5


def test_book_for_portfolio_merges_risk_and_skips_non_trading_subscriptions(
    state, alice, portfolios, subs
):
    pf = portfolios.create(alice, name="Book", initial_cash=2_000.0, allow_short=True)
    portfolios.set_risk_policy(alice, pf.id, {"max_weight_per_ticker": 0.3})
    a = subs.subscribe(
        alice,
        strategy_id="s_active",
        portfolio_id=pf.id,
        mode=Mode.PAPER,
        weight=2.0,
        risk_overrides={"max_open_positions": 3, "max_weight_per_ticker": 0.9},
    )
    subs.subscribe(alice, strategy_id="s_shadow", portfolio_id=pf.id, mode=Mode.NOTIFY)
    book = BookSpec.for_portfolio(
        portfolios.get(alice, pf.id),
        subs.list_for_portfolio(alice, pf.id),
        global_risk=RiskPolicy(max_weight_per_ticker=0.5),
        default_initial_cash=10_000.0,
    )
    assert dict(book.strategy_weights or {}) == {"s_active": 2.0}
    assert dict(book.strategy_modes) == {"s_active": Mode.PAPER}
    assert book.risk.max_weight_per_ticker == 0.3
    slice_ = book.risk_overrides[a.strategy_id]
    assert slice_.max_weight_per_ticker == 0.3  # 0.9 can't loosen
    assert slice_.max_open_positions == 3
    assert book.allow_short is True
    assert book.initial_cash == 2_000.0
    assert book.broker == "simulated"


# ---- auto checklist: connection and halts (S6) ------------------------------------


def test_auto_needs_a_healthy_connection_that_can_trade(state, alice, portfolios, subs):
    pf = portfolios.create(alice, name="Live", kind="broker")
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    seed_paper_days(state, sub.id, MIN_PAPER_DAYS_FOR_AUTO)
    assert subs.auto_blockers(alice, sub.id) == [
        "the portfolio is not linked to a broker connection"
    ]
    con = link_connection(state, pf.id, provider="fake", status="error")
    assert subs.auto_blockers(alice, sub.id) == [
        "the broker connection is error",
        "the fake connection cannot place orders",
    ]
    state.execute("UPDATE broker_connections SET status = 'active' WHERE id = ?", [con])
    state.execute("DELETE FROM broker_connections WHERE id = ?", [con])
    assert subs.auto_blockers(alice, sub.id) == ["the portfolio's broker connection is gone"]


def test_auto_is_refused_while_a_halt_is_in_force(state, alice, portfolios, subs):
    from stonks.production.halts import trip_halt

    pf = _live(state, portfolios, alice)
    sub = subs.subscribe(alice, strategy_id="s_active", portfolio_id=pf.id, mode=Mode.PAPER)
    seed_paper_days(state, sub.id, MIN_PAPER_DAYS_FOR_AUTO)
    assert subs.auto_blockers(alice, sub.id) == []
    trip_halt(state, "kill", reason="stop", actor=alice.actor, scope="user", user_id=alice.user_id)
    [blocker] = subs.auto_blockers(alice, sub.id)
    assert blocker.startswith("trading is halted: kill (user ")
    with pytest.raises(AutoGateRefused):
        subs.set_mode(alice, sub.id, Mode.AUTO)


def test_user_risk_limits_are_validated_partial_and_audited(state, users, alice):
    assert users.risk_policy(alice.user_id) == {}
    stored = users.set_risk_policy(alice.user_id, {"max_weight_per_ticker": 0.2}, actor=alice.actor)
    assert stored == {"max_weight_per_ticker": 0.2}
    assert users.risk_policy(alice.user_id) == stored
    with pytest.raises(ValueError):
        users.set_risk_policy(alice.user_id, {"no_such_limit": 1}, actor=alice.actor)
    with pytest.raises(NotFound):
        users.risk_policy("usr_missing")
    [row] = state.sql("SELECT * FROM audit_log WHERE action = 'user.risk_policy'")
    assert row["target_id"] == alice.user_id


def test_paper_accounts_are_created_once_and_hidden_from_lists(state, alice, portfolios):
    from stonks.accounts.paper import ensure_paper_account, paper_account_id

    live = portfolios.create(alice, name="Live", kind="broker", initial_cash=5_000.0)
    portfolios.create(alice, name="Live (paper)")  # the natural name is taken
    paper = ensure_paper_account(state, live)
    assert paper.id == paper_account_id(live.id) and paper.kind == "simulated"
    assert paper.owner_id == alice.user_id and paper.initial_cash == 5_000.0
    assert paper.name == f"Live (paper {live.id})"
    assert ensure_paper_account(state, live) == paper
    assert paper.id not in {p.id for p in portfolios.list(alice)}
    assert owned_portfolio(state, alice, paper.id) == paper
