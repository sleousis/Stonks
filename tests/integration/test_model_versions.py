"""Roadmap 22.6: model versions under one strategy id, swapped only through
governance (audited, check or override, never for a retired strategy)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date

import pytest

from stonks.registry.store import GovernanceError, StrategyRegistry
from stonks.registry.versions import (
    ModelVersionRegistry,
    SwapRefused,
    book_id,
    parse_book_id,
)
from stonks.store.state import SqliteState
from stonks.strategies.examples.buy_and_hold import BuyAndHold
from tests.fixtures.governance import seed_status

LONG_REASON = "manual swap: the new fit handled the regime change better"


@dataclass(frozen=True)
class _Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class _Report:
    strategy_id: str
    version: int
    checks: list[_Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def as_dict(self) -> dict:
        return {"strategy_id": self.strategy_id, "version": self.version, "passed": self.passed}


@pytest.fixture
def env(tmp_path):
    state = SqliteState(tmp_path / "state.sqlite")
    state.migrate()
    reg = StrategyRegistry(state=state, artifacts_dir=tmp_path / "artifacts")
    sid = reg.register(BuyAndHold({"ticker": "A.US"}), reports=[])
    versions = ModelVersionRegistry.on(reg)
    yield reg, versions, state, sid
    state.close()


def _candidate(versions: ModelVersionRegistry, sid: str) -> int:
    version, rel, folder = versions.next_version(sid)
    BuyAndHold({"ticker": "A.US", "allocation": 0.5}).save(folder)
    versions.add_candidate(
        sid,
        version,
        rel,
        train_start=date(2024, 1, 1),
        train_end=date(2025, 1, 1),
        fit={"n": 3},
        actor="test",
    )
    return version


def test_book_ids_round_trip():
    assert book_id("mom_1", 3) == "mom_1@v3"
    assert parse_book_id("mom_1@v3") == ("mom_1", 3)
    assert parse_book_id("mom_1") is None


def test_baseline_is_version_one_and_live(env):
    _, versions, state, sid = env
    (v1,) = versions.list(sid)
    assert (v1.version, v1.status, v1.created_by) == (1, "live", "system")
    assert v1.artifact_path.name == sid
    (event,) = versions.history(sid)
    assert (event.kind, event.to_status) == ("baseline", "live")
    versions.list(sid)  # idempotent
    assert len(state.sql("SELECT * FROM model_versions")) == 1


def test_unknown_strategy_raises_key_error(env):
    _, versions, _, _ = env
    with pytest.raises(KeyError):
        versions.list("nope")


def test_add_candidate_supersedes_the_older_one(env):
    _, versions, _, sid = env
    first = _candidate(versions, sid)
    second = _candidate(versions, sid)
    by_version = {v.version: v.status for v in versions.list(sid)}
    assert by_version == {1: "live", first: "rejected", second: "candidate"}
    kinds = [e.kind for e in versions.history(sid)]
    assert kinds == ["baseline", "candidate", "supersede", "candidate"]
    assert [c.version for c in versions.candidates()] == [second]


def test_swap_needs_a_check_or_an_override(env):
    _, versions, _, sid = env
    v2 = _candidate(versions, sid)
    with pytest.raises(SwapRefused, match="needs a passing swap check"):
        versions.swap(sid, v2, actor="cli")
    failing = _Report(sid, v2, [_Check("min_days", False, "3 days")])
    with pytest.raises(SwapRefused, match="min_days"):
        versions.swap(sid, v2, actor="cli", check_report=failing)
    with pytest.raises(GovernanceError, match="at least"):
        versions.swap(sid, v2, actor="cli", override=True, reason="short")
    other = _Report(sid, 99, [_Check("min_days", True)])
    with pytest.raises(SwapRefused, match="not"):
        versions.swap(sid, v2, actor="cli", check_report=other)
    assert versions.live(sid).version == 1


def test_swap_with_passing_check_moves_the_artifact(env):
    reg, versions, state, sid = env
    v2 = _candidate(versions, sid)
    event = versions.swap(
        sid, v2, actor="cli", check_report=_Report(sid, v2, [_Check("min_days", True)])
    )
    assert (event.kind, event.to_status, event.check_passed) == ("swap", "live", True)
    assert event.check_report == {"strategy_id": sid, "version": v2, "passed": True}
    statuses = {v.version: v.status for v in versions.list(sid)}
    assert statuses == {1: "archived", v2: "live"}
    handle = next(h for h in reg.list_all() if h.id == sid)
    assert handle.artifact_path == versions.get(sid, v2).artifact_path
    assert reg.load(sid).params["allocation"] == 0.5


def test_override_swap_is_logged(env):
    _, versions, _, sid = env
    v2 = _candidate(versions, sid)
    event = versions.swap(sid, v2, actor="cli", override=True, reason=LONG_REASON)
    assert event.override and event.reason == LONG_REASON and event.check_passed is None


def test_retired_strategy_never_swaps(env):
    reg, versions, _, sid = env
    v2 = _candidate(versions, sid)
    seed_status(reg, sid, "retired")
    with pytest.raises(GovernanceError, match="retired"):
        versions.swap(sid, v2, actor="cli", override=True, reason=LONG_REASON)
    assert versions.candidates() == []


def test_reject_needs_a_reason_and_a_candidate(env):
    _, versions, _, sid = env
    v2 = _candidate(versions, sid)
    with pytest.raises(GovernanceError):
        versions.reject(sid, v2, actor="cli", reason=" ")
    versions.reject(sid, v2, actor="cli", reason="worse fit")
    assert versions.get(sid, v2).status == "rejected"
    with pytest.raises(GovernanceError, match="only a candidate"):
        versions.reject(sid, v2, actor="cli", reason="again")


def test_failed_fit_is_kept(env):
    _, versions, _, sid = env
    version, rel, _ = versions.next_version(sid)
    failed = versions.record_failure(
        sid,
        version,
        rel,
        train_start=date(2024, 1, 1),
        train_end=date(2025, 1, 1),
        error="ValueError: too few trades",
        actor="test",
    )
    assert failed.status == "failed" and failed.error.startswith("ValueError")
    assert versions.candidates() == []


def test_raw_writes_are_refused(env):
    _, versions, state, sid = env
    v2 = _candidate(versions, sid)
    with pytest.raises(sqlite3.DatabaseError, match="audited"):
        state.execute(
            "UPDATE model_versions SET status = 'live' WHERE strategy_id = ? AND version = ?",
            [sid, v2],
        )
    with pytest.raises(sqlite3.DatabaseError, match="swap"):
        state.execute(
            "UPDATE strategies SET artifact_path = ? WHERE id = ?", [f"{sid}/versions/v2", sid]
        )
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        state.execute("DELETE FROM model_version_events")
