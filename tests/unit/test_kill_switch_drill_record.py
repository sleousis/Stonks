"""Recording kill switch drills in the real state DB (roadmap 19.11).

The drill runs on scratch state, and each run leaves one row in
``kill_switch_drills`` that the live_small and live_scale gates read."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from stonks.production.drills import (
    DrillReport,
    DrillStep,
    latest_drill,
    record_drill,
)
from stonks.store.state import SqliteState

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    with SqliteState(tmp_path / "state.sqlite") as s:
        s.migrate()
        yield s


def _report(ok: bool = True) -> DrillReport:
    return DrillReport(
        broker="simulated",
        portfolio_id="pf_drill",
        client_id="drill-1",
        started_at=T0,
        finished_at=T0,
        steps=(DrillStep("engage_kill", ok, "engaged", 1.0),),
        halt_id=1,
        cancel_seconds=0.1,
    )


def test_a_recorded_drill_keeps_who_when_and_the_report(state):
    rid = record_drill(state, _report(), ran_by="cli:ops", ran_at=T0)
    row = state.sql("SELECT * FROM kill_switch_drills WHERE id = ?", [rid])[0]
    assert row["ran_by"] == "cli:ops"
    assert row["ran_at"] == T0.isoformat()
    assert row["passed"] == 1
    assert row["portfolio_id"] is None
    assert row["broker"] == "simulated"
    assert json.loads(row["report_json"])["passed"] is True


def test_a_failed_drill_is_recorded_as_failed(state):
    rid = record_drill(state, _report(ok=False), ran_by="cli:ops", portfolio_id="pf_a")
    row = state.sql("SELECT passed, portfolio_id FROM kill_switch_drills WHERE id = ?", [rid])[0]
    assert (row["passed"], row["portfolio_id"]) == (0, "pf_a")


def test_the_latest_drill_counts_global_and_own_runs_only(state):
    record_drill(state, _report(), ran_by="a", ran_at=datetime(2026, 9, 1, tzinfo=UTC))
    record_drill(
        state, _report(), ran_by="a", portfolio_id="pf_a", ran_at=datetime(2026, 9, 5, tzinfo=UTC)
    )
    record_drill(
        state, _report(), ran_by="a", portfolio_id="pf_b", ran_at=datetime(2026, 9, 9, tzinfo=UTC)
    )
    latest = latest_drill(state, "pf_a")
    assert latest is not None
    assert (latest.ran_at, latest.passed) == (datetime(2026, 9, 5, tzinfo=UTC), True)
    other = latest_drill(state, "pf_c")
    assert other is not None and other.ran_at == datetime(2026, 9, 1, tzinfo=UTC)


def test_a_later_failed_drill_supersedes_an_earlier_pass(state):
    record_drill(
        state, _report(), ran_by="a", portfolio_id="pf_a", ran_at=datetime(2026, 9, 1, tzinfo=UTC)
    )
    record_drill(state, _report(ok=False), ran_by="a", ran_at=datetime(2026, 9, 10, tzinfo=UTC))
    latest = latest_drill(state, "pf_a")
    assert latest is not None
    assert (latest.ran_at, latest.passed) == (datetime(2026, 9, 10, tzinfo=UTC), False)


def test_no_drill_means_none(state):
    assert latest_drill(state, "pf_a") is None
