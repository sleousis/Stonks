"""A strategy that turns active gets a pf_default subscription when it has
none (accounts.default_book), and migration 022 backfills the strategies
that were already active."""

from __future__ import annotations

import json
import shutil

import pytest

from stonks.accounts.default_book import default_mode, ensure_default_subscription
from stonks.accounts.models import Mode
from stonks.app.context import AppContext
from stonks.app.strategies import change_status
from stonks.store import state as state_mod
from stonks.store.state import SqliteState

REASON = "owner override for the default book test"


def _subs(state: SqliteState, strategy_id: str) -> list[dict]:
    return state.sql(
        "SELECT id, user_id, mode, weight, enabled FROM subscriptions"
        " WHERE portfolio_id = 'pf_default' AND strategy_id = ?",
        [strategy_id],
    )


def _audit(state: SqliteState, sub_id: str) -> list[dict]:
    return state.sql(
        "SELECT actor, action, details_json FROM audit_log WHERE target_id = ?", [sub_id]
    )


def test_default_mode_follows_the_broker():
    assert default_mode("simulated") is Mode.PAPER
    assert default_mode("alpaca") is Mode.AUTO


def test_promotion_subscribes_the_default_book_in_paper(settings, seeded):
    sid = seeded["shadow_id"]
    change_status(AppContext(settings), sid, "active", actor="t", reason=REASON, override=True)
    with SqliteState(settings.state.path) as state:
        [sub] = _subs(state, sid)
        assert (sub["user_id"], sub["mode"], sub["weight"], sub["enabled"]) == (
            "usr_owner",
            "paper",
            1.0,
            1,
        )
        [row] = _audit(state, sub["id"])
        assert (row["actor"], row["action"]) == ("service:system", "subscription.create")
        assert json.loads(row["details_json"])["reason"] == "default_book"


def test_promotion_on_an_external_broker_subscribes_in_auto(settings, seeded):
    settings.brokers.kind = "alpaca"
    sid = seeded["shadow_id"]
    change_status(AppContext(settings), sid, "active", actor="t", reason=REASON, override=True)
    with SqliteState(settings.state.path) as state:
        assert [s["mode"] for s in _subs(state, sid)] == ["auto"]


def test_an_existing_subscription_is_kept_as_it_is(settings, seeded):
    sid = seeded["shadow_id"]
    with SqliteState(settings.state.path) as state:
        first = ensure_default_subscription(state, sid, Mode.PAPER)
        state.execute("UPDATE subscriptions SET enabled = 0 WHERE id = ?", [first])
    ctx = AppContext(settings)
    change_status(ctx, sid, "active", actor="t", reason=REASON, override=True)
    change_status(ctx, sid, "shadow", actor="t", reason="back to shadow for a while")
    change_status(ctx, sid, "active", actor="t", reason=REASON, override=True)
    with SqliteState(settings.state.path) as state:
        assert [(s["id"], s["enabled"]) for s in _subs(state, sid)] == [(first, 0)]


def test_be18_a_subscription_the_system_ended_follows_a_new_promotion(settings, seeded):
    from stonks.accounts.scope import Scope
    from stonks.accounts.subscriptions import SubscriptionRepository

    sid = seeded["shadow_id"]
    with SqliteState(settings.state.path) as state:
        first = ensure_default_subscription(state, sid, Mode.PAPER)
        # the tick ended it when the strategy retired and went flat
        SubscriptionRepository(state).disable(Scope.service("system"), first, reason="retired")
    change_status(AppContext(settings), sid, "active", actor="t", reason=REASON, override=True)
    with SqliteState(settings.state.path) as state:
        assert [(s["id"], s["enabled"]) for s in _subs(state, sid)] == [(first, 1)]
        actions = [(a["actor"], a["action"]) for a in _audit(state, first)]
    assert actions[-1] == ("service:system", "subscription.enable")


def test_a_refused_promotion_subscribes_nothing(settings, seeded):
    from stonks.app.errors import ConflictError

    sid = seeded["shadow_id"]
    with pytest.raises(ConflictError):
        change_status(AppContext(settings), sid, "active", actor="t")
    with SqliteState(settings.state.path) as state:
        assert _subs(state, sid) == []


def test_notify_is_not_a_default_book_mode(tmp_path):
    with SqliteState(tmp_path / "s.sqlite") as state:
        state.migrate()
        with pytest.raises(ValueError, match="notify"):
            ensure_default_subscription(state, "x", Mode.NOTIFY)


# ---- migration 022 -------------------------------------------------------------


def _state_before_022(tmp_path, monkeypatch) -> SqliteState:
    old = tmp_path / "_mig"
    old.mkdir()
    for path in state_mod.MIGRATIONS_DIR.glob("*.sql"):
        if int(path.stem.split("_", 1)[0]) < 22:
            shutil.copy(path, old / path.name)
    real = state_mod.MIGRATIONS_DIR
    monkeypatch.setattr(state_mod, "MIGRATIONS_DIR", old)
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    monkeypatch.setattr(state_mod, "MIGRATIONS_DIR", real)
    return state


def _strategy(state: SqliteState, sid: str, status: str) -> None:
    now = "2026-01-01T00:00:00+00:00"
    if status != "shadow":
        state.execute(
            "INSERT INTO status_changes (strategy_id, kind, from_status, to_status, actor,"
            " reason, override, created_at) VALUES (?, 'status', 'shadow', ?, 't', ?, 1, ?)",
            [sid, status, REASON, now],
        )
    state.execute(
        "INSERT INTO strategies (id, class_path, params_json, artifact_path, status,"
        " created_at, updated_at) VALUES (?, 'x.Y', '{}', ?, ?, ?, ?)",
        [sid, sid, status, now, now],
    )


def test_migration_022_subscribes_the_default_book_to_active_strategies(tmp_path, monkeypatch):
    state = _state_before_022(tmp_path, monkeypatch)
    try:
        for sid, status in [("on", "active"), ("kept", "active"), ("off", "shadow")]:
            _strategy(state, sid, status)
        state.execute(
            "INSERT INTO subscriptions (id, user_id, strategy_id, portfolio_id, mode, weight,"
            " enabled, created_at, updated_at) VALUES ('sub_kept', 'usr_owner', 'kept',"
            " 'pf_default', 'paper', 0.5, 0, 'now', 'now')"
        )
        state.migrate()
        assert 22 in state.applied_migrations()
        [on] = _subs(state, "on")
        assert (on["mode"], on["weight"], on["enabled"]) == ("paper", 1.0, 1)
        [row] = _audit(state, on["id"])
        assert row["actor"] == "service:system"
        assert json.loads(row["details_json"]) == {
            "mode": "paper",
            "reason": "default_book",
            "strategy_id": "on",
        }
        assert [(s["id"], s["weight"]) for s in _subs(state, "kept")] == [("sub_kept", 0.5)]
        assert _subs(state, "off") == []
    finally:
        state.close()
