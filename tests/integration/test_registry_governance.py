"""BL-24: every status change is audited, and promotion is gated.

``StrategyRegistry.set_status`` is the only writer of ``strategies.status``;
it writes one ``status_changes`` row per change in the same transaction.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

import pytest

from stonks.registry.store import (
    MIN_OVERRIDE_REASON_CHARS,
    GovernanceError,
    PromotionRefused,
    StrategyRegistry,
)
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold

LONG_REASON = "manual override: paper period cut short by the data outage"


@dataclass(frozen=True)
class _Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class _Report:
    """Duck-typed stand-in for ``production.golive.GoLiveReport``."""

    strategy_id: str
    checks: list[_Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


def _passing(sid: str) -> _Report:
    return _Report(sid, [_Check("min_days", True, "30 paper days")])


def _failing(sid: str) -> _Report:
    return _Report(sid, [_Check("min_days", False, "3 paper days, need >= 20")])


@pytest.fixture
def env(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    reg = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = reg.register(BuyAndHold({"ticker": "A.US"}), reports=[])
    yield reg, state, sid
    state.close()


def _status(reg: StrategyRegistry, sid: str) -> str:
    return next(h.status for h in reg.list_all() if h.id == sid)


def _rows(state: SqliteState) -> list[sqlite3.Row]:
    return state.sql("SELECT * FROM status_changes ORDER BY id")


# ---- promotion ---------------------------------------------------------------


def test_promote_with_passing_golive_writes_one_row(env):
    reg, state, sid = env
    change = reg.set_status(sid, "active", actor="cli", golive_report=_passing(sid))
    assert _status(reg, sid) == "active"
    rows = _rows(state)
    assert len(rows) == 1
    row = rows[0]
    assert (row["strategy_id"], row["kind"]) == (sid, "status")
    assert (row["from_status"], row["to_status"]) == ("shadow", "active")
    assert row["actor"] == "cli"
    assert row["reason"]  # defaulted from the go-live pass
    assert row["override"] == 0
    assert row["golive_passed"] == 1
    report = json.loads(row["golive_report_json"])
    assert report["strategy_id"] == sid
    assert report["checks"][0]["name"] == "min_days"
    assert change is not None and change.id == row["id"]


def test_promote_without_golive_report_raises(env):
    reg, state, sid = env
    with pytest.raises(PromotionRefused, match="go-live"):
        reg.set_status(sid, "active", actor="cli", reason=LONG_REASON)
    assert _status(reg, sid) == "shadow"
    assert _rows(state) == []


def test_promote_with_failing_golive_raises_and_names_failures(env):
    reg, state, sid = env
    with pytest.raises(PromotionRefused, match="min_days"):
        reg.set_status(sid, "active", actor="cli", golive_report=_failing(sid))
    assert _status(reg, sid) == "shadow"
    assert _rows(state) == []


def test_report_for_another_strategy_does_not_count(env):
    reg, state, sid = env
    with pytest.raises(PromotionRefused, match="another strategy"):
        reg.set_status(sid, "active", actor="cli", golive_report=_passing("someone_else"))
    assert _status(reg, sid) == "shadow"


def test_override_with_long_reason_promotes_and_records_failing_report(env):
    reg, state, sid = env
    reg.set_status(
        sid,
        "active",
        actor="api",
        reason=LONG_REASON,
        override=True,
        golive_report=_failing(sid),
    )
    assert _status(reg, sid) == "active"
    (row,) = _rows(state)
    assert row["override"] == 1
    assert row["golive_passed"] == 0
    assert row["reason"] == LONG_REASON
    assert json.loads(row["golive_report_json"])["checks"][0]["passed"] is False


def test_override_without_report_is_recorded_with_null_golive(env):
    reg, state, sid = env
    reg.set_status(sid, "active", actor="api", reason=LONG_REASON, override=True)
    (row,) = _rows(state)
    assert row["golive_passed"] is None
    assert row["golive_report_json"] is None


@pytest.mark.parametrize("reason", [None, "", "too short", " " * 40, "x" * 19])
def test_override_with_short_reason_raises(env, reason):
    reg, state, sid = env
    with pytest.raises(GovernanceError, match=str(MIN_OVERRIDE_REASON_CHARS)):
        reg.set_status(sid, "active", actor="api", reason=reason, override=True)
    assert _status(reg, sid) == "shadow"
    assert _rows(state) == []


def test_min_override_reason_is_twenty_chars():
    assert MIN_OVERRIDE_REASON_CHARS == 20


# ---- demotion ---------------------------------------------------------------


@pytest.mark.parametrize("target", ["retired", "shadow"])
def test_demotion_requires_a_reason(env, target):
    reg, state, sid = env
    reg.set_status(sid, "active", actor="t", reason=LONG_REASON, override=True)
    for reason in (None, "", "   "):
        with pytest.raises(GovernanceError, match="reason"):
            reg.set_status(sid, target, actor="t", reason=reason)
    assert _status(reg, sid) == "active"
    assert len(_rows(state)) == 1


@pytest.mark.parametrize("target", ["retired", "shadow"])
def test_demotion_with_reason_is_always_allowed_and_logged(env, target):
    reg, state, sid = env
    reg.set_status(sid, "active", actor="t", reason=LONG_REASON, override=True)
    reg.set_status(sid, target, actor="ops", reason="drawdown past the quit line")
    assert _status(reg, sid) == target
    row = _rows(state)[-1]
    assert (row["from_status"], row["to_status"]) == ("active", target)
    assert row["reason"] == "drawdown past the quit line"
    assert row["actor"] == "ops"
    assert row["override"] == 0


def test_retire_from_shadow_needs_no_golive(env):
    reg, _, sid = env
    reg.set_status(sid, "retired", actor="t", reason="superseded")
    assert _status(reg, sid) == "retired"


# ---- general ----------------------------------------------------------------


def test_actor_is_required(env):
    reg, state, sid = env
    for actor in (None, "", "  "):
        with pytest.raises(GovernanceError, match="actor"):
            reg.set_status(sid, "retired", actor=actor, reason="superseded")
    assert _rows(state) == []


def test_unknown_id_raises_key_error_before_any_rule(env):
    reg, _, _ = env
    with pytest.raises(KeyError):
        reg.set_status("nope", "active")


def test_unknown_status_raises_value_error(env):
    reg, _, sid = env
    with pytest.raises(ValueError, match="status"):
        reg.set_status(sid, "zombie", actor="t", reason="whatever it takes")


def test_same_status_is_a_noop_without_a_row(env):
    reg, state, sid = env
    assert reg.set_status(sid, "shadow") is None
    assert _rows(state) == []


def test_every_change_writes_exactly_one_row_and_history_reads_them(env):
    reg, state, sid = env
    reg.set_status(sid, "active", actor="a", golive_report=_passing(sid))
    reg.set_status(sid, "shadow", actor="b", reason="volatility spike")
    reg.set_status(sid, "active", actor="c", reason=LONG_REASON, override=True)
    reg.set_status(sid, "retired", actor="d", reason="edge decayed")
    history = reg.status_history(sid)
    assert [(h.from_status, h.to_status) for h in history] == [
        ("shadow", "active"),
        ("active", "shadow"),
        ("shadow", "active"),
        ("active", "retired"),
    ]
    assert [h.actor for h in history] == ["a", "b", "c", "d"]
    assert [h.override for h in history] == [False, False, True, False]
    assert history[0].golive_passed is True
    assert history[1].golive_passed is None
    assert history[0].created_at
    assert len(_rows(state)) == 4


def test_audit_row_and_status_update_share_a_transaction(env, monkeypatch):
    """If the audit insert fails, the status must not change."""
    reg, state, sid = env
    state.execute("DROP TABLE status_changes")
    with pytest.raises(sqlite3.OperationalError):
        reg.set_status(sid, "retired", actor="t", reason="superseded")
    assert _status(reg, sid) == "shadow"


def test_status_changes_is_append_only(env):
    reg, state, sid = env
    reg.set_status(sid, "retired", actor="t", reason="superseded")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        state.execute("UPDATE status_changes SET reason = 'rewritten'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        state.execute("DELETE FROM status_changes")
    assert _rows(state)[0]["reason"] == "superseded"


def test_record_intervention_logs_non_status_kinds(env):
    reg, state, sid = env
    reg.record_intervention("risk_reset", actor="ops", reason="breaker re-armed after review")
    reg.record_intervention(
        "manual_order", actor="ops", reason="flatten stuck position", strategy_id=sid
    )
    rows = _rows(state)
    assert [r["kind"] for r in rows] == ["risk_reset", "manual_order"]
    assert rows[0]["strategy_id"] is None
    assert rows[1]["strategy_id"] == sid
    assert rows[0]["from_status"] is None and rows[0]["to_status"] is None
    with pytest.raises(GovernanceError, match="kind"):
        reg.record_intervention("status", actor="ops", reason="sneaky status change")
    with pytest.raises(GovernanceError, match="reason"):
        reg.record_intervention("config", actor="ops", reason="")


def test_stored_golive_report_is_strict_json(env):
    """Go-live checks can carry NaN/inf values; the stored JSON must not."""

    @dataclass(frozen=True)
    class _ValuedCheck:
        name: str
        passed: bool
        value: float | None

    reg, state, sid = env
    report = _Report(sid, [_ValuedCheck("max_drift", True, float("nan"))])  # type: ignore[list-item]
    reg.set_status(sid, "active", actor="t", golive_report=report)
    raw = _rows(state)[0]["golive_report_json"]

    def _reject(token: str) -> None:
        raise AssertionError(f"non-standard JSON constant {token}")

    parsed = json.loads(raw, parse_constant=_reject)
    assert parsed["checks"][0]["value"] is None


# ---- no bypass ----------------------------------------------------------------


def test_raw_sql_status_update_is_refused(env):
    """Defense in depth: a status change that skips set_status (and so has
    no matching audit row) is rejected by the database."""
    reg, state, sid = env
    with pytest.raises(sqlite3.DatabaseError, match="set_status"):
        state.execute(
            "UPDATE strategies SET status = 'active', updated_at = 'x' WHERE id = ?", [sid]
        )
    assert _status(reg, sid) == "shadow"
    # an earlier audited change cannot be replayed either
    change = reg.set_status(sid, "active", actor="t", golive_report=_passing(sid))
    reg.set_status(sid, "shadow", actor="t", reason="back to paper trading")
    with pytest.raises(sqlite3.DatabaseError, match="set_status"):
        state.execute(
            "UPDATE strategies SET status = 'active', updated_at = ? WHERE id = ?",
            [change.created_at, sid],
        )
    assert _status(reg, sid) == "shadow"


def test_other_strategy_columns_stay_writable(env):
    reg, state, sid = env
    state.execute("UPDATE strategies SET class_path = 'x:Y' WHERE id = ?", [sid])
    assert next(h for h in reg.list_all() if h.id == sid).class_path == "x:Y"


def test_stale_report_for_another_status_does_not_count(env):
    @dataclass(frozen=True)
    class _StatusReport(_Report):
        status: str = "retired"

    reg, _, sid = env
    report = _StatusReport(sid, [_Check("min_days", True)], status="retired")
    with pytest.raises(PromotionRefused, match="stale"):
        reg.set_status(sid, "active", actor="t", golive_report=report)
    assert _status(reg, sid) == "shadow"
