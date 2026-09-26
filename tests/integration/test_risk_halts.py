"""The halt store (BL-28, 12.6): trips, expiry, latching, logged resets,
the operational halt from health, and the health check for open halts."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from stonks.production.halts import (
    HaltError,
    active_halts,
    clear_halt,
    halt_health_check,
    list_halts,
    sync_operational_halt,
    trip_halt,
)
from stonks.production.health import HealthCheck, HealthReport
from stonks.store.state import SqliteState

DAY = date(2025, 6, 10)


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def test_a_trip_is_idempotent_while_open(state):
    first, created = trip_halt(
        state,
        "month_loss",
        reason="6% down",
        actor="system",
        portfolio_id="pf_default",
        expires_on=date(2025, 7, 1),
        on=DAY,
    )
    again, created_again = trip_halt(
        state,
        "month_loss",
        reason="7% down",
        actor="system",
        portfolio_id="pf_default",
        expires_on=date(2025, 7, 1),
        on=DAY,
    )
    assert created and not created_again and again.id == first.id
    assert [h.kind for h in active_halts(state, DAY, portfolio_id="pf_default")] == ["month_loss"]


def test_a_month_halt_expires_and_a_new_trip_opens_a_new_row(state):
    old, _ = trip_halt(
        state,
        "month_loss",
        reason="r",
        actor="system",
        portfolio_id="pf_default",
        expires_on=date(2025, 7, 1),
        on=DAY,
    )
    assert active_halts(state, date(2025, 7, 1), portfolio_id="pf_default") == []
    new, created = trip_halt(
        state,
        "month_loss",
        reason="r",
        actor="system",
        portfolio_id="pf_default",
        expires_on=date(2025, 8, 1),
        on=date(2025, 7, 15),
    )
    assert created and new.id != old.id
    [expired] = [h for h in list_halts(state, include_cleared=True) if h.id == old.id]
    assert expired.cleared_by == "system" and expired.clear_reason == "expired"


def test_the_latched_drawdown_halt_survives_a_new_month_until_cleared(state):
    halt, _ = trip_halt(
        state, "drawdown", reason="20% drawdown", actor="system", portfolio_id="pf_default", on=DAY
    )
    assert active_halts(state, date(2026, 1, 5), portfolio_id="pf_default") == [halt]
    cleared = clear_halt(state, halt.id, actor="user:usr_owner", reason="reviewed the book")
    assert cleared.cleared_by == "user:usr_owner"
    assert active_halts(state, date(2026, 1, 5), portfolio_id="pf_default") == []


def test_clearing_writes_a_risk_reset_row(state):
    halt, _ = trip_halt(state, "drawdown", reason="dd", actor="system", portfolio_id="pf_a", on=DAY)
    clear_halt(state, halt.id, actor="user:usr_owner", reason="checked")
    [row] = state.sql("SELECT kind, actor, reason FROM status_changes WHERE kind = 'risk_reset'")
    assert row["actor"] == "user:usr_owner"
    assert "drawdown" in row["reason"] and "checked" in row["reason"]


def test_clearing_needs_a_reason_and_an_open_halt(state):
    halt, _ = trip_halt(state, "drawdown", reason="dd", actor="system", portfolio_id="pf_a", on=DAY)
    with pytest.raises(HaltError, match="reason"):
        clear_halt(state, halt.id, actor="user:u", reason="  ")
    clear_halt(state, halt.id, actor="user:u", reason="ok")
    with pytest.raises(HaltError, match="already cleared"):
        clear_halt(state, halt.id, actor="user:u", reason="again")
    with pytest.raises(HaltError, match="no halt"):
        clear_halt(state, 999, actor="user:u", reason="x")


def test_halt_rows_are_append_only_once_cleared(state):
    halt, _ = trip_halt(state, "drawdown", reason="dd", actor="system", portfolio_id="pf_a", on=DAY)
    with pytest.raises(sqlite3.DatabaseError):
        state.execute("DELETE FROM risk_halts")
    with pytest.raises(sqlite3.DatabaseError):
        state.execute("UPDATE risk_halts SET kind = 'kill'")
    clear_halt(state, halt.id, actor="user:u", reason="ok")
    with pytest.raises(sqlite3.DatabaseError):
        state.execute("UPDATE risk_halts SET cleared_by = 'someone else'")


def test_active_halts_cover_global_user_and_portfolio_scope(state):
    trip_halt(state, "kill", reason="g", actor="user:admin", scope="global", halt="all", on=DAY)
    trip_halt(state, "kill", reason="u", actor="user:usr_a", scope="user", user_id="usr_a", on=DAY)
    trip_halt(state, "drawdown", reason="p", actor="system", portfolio_id="pf_a", on=DAY)
    trip_halt(state, "drawdown", reason="other", actor="system", portfolio_id="pf_b", on=DAY)
    got = active_halts(state, DAY, portfolio_id="pf_a", user_id="usr_a")
    assert sorted(h.reason for h in got) == ["g", "p", "u"]
    assert [h.reason for h in active_halts(state, DAY, portfolio_id="pf_c", user_id="usr_c")] == [
        "g"
    ]


def test_scope_needs_its_target(state):
    with pytest.raises(HaltError):
        trip_halt(state, "kill", reason="r", actor="a", scope="user", on=DAY)
    with pytest.raises(HaltError):
        trip_halt(state, "kill", reason="r", actor="a", scope="portfolio", on=DAY)


def _report(*checks: tuple[str, bool]) -> HealthReport:
    return HealthReport(
        checks=[HealthCheck(name=n, ok=ok, detail="d") for n, ok in checks],
        checked_at=datetime(2025, 6, 10, 12, tzinfo=UTC),
    )


def test_stale_data_opens_the_operational_halt_and_health_clears_it(state):
    halt = sync_operational_halt(state, _report(("freshness:AAPL.US", False), ("x", True)))
    assert halt is not None and halt.scope == "global" and halt.kind == "operational"
    assert [h.kind for h in active_halts(state, DAY, portfolio_id="pf_a")] == ["operational"]
    # an ingest failure alone is not operational
    assert sync_operational_halt(state, _report(("ingest_failures", False))) is None
    assert active_halts(state, DAY, portfolio_id="pf_a") == []
    assert state.sql("SELECT COUNT(*) FROM status_changes WHERE kind='risk_reset'")[0][0] == 1


def test_the_health_check_reports_open_halts(state):
    ok = halt_health_check(state, DAY)
    assert ok.ok and ok.name == "risk_halts"
    trip_halt(state, "drawdown", reason="dd", actor="system", portfolio_id="pf_a", on=DAY)
    bad = halt_health_check(state, DAY)
    assert not bad.ok and "drawdown" in bad.detail and "pf_a" in bad.detail


def test_a_trip_notifies_the_portfolio_owner(state):
    from stonks.production.halts import notify_trip

    halt, _ = trip_halt(
        state, "drawdown", reason="dd", actor="system", portfolio_id="pf_default", on=DAY
    )
    notify_trip(state, halt)
    [row] = state.sql("SELECT user_id, category, level, urgency, title FROM notification_outbox")
    assert (row["user_id"], row["category"], row["level"], row["urgency"]) == (
        "usr_owner",
        "risk",
        "error",
        "high",
    )
    assert "drawdown" in row["title"]


def test_a_failing_notification_never_raises(state):
    from stonks.production.halts import notify_trip

    halt, _ = trip_halt(state, "kill", reason="k", actor="a", scope="global", on=DAY)

    def boom(event):
        raise RuntimeError("down")

    notify_trip(state, halt, publish=boom)
