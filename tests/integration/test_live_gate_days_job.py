"""The ``live_gate_days`` scheduler action (roadmap 19.9) on every backend:
it skips while no portfolio is past ``sim_paper``, records the session
for the others, and a dirty week only alerts the owner."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.config import Settings
from stonks.production.live.gates import gate_days
from stonks.production.live.stages import change_stage, get_stage
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, SessionTrigger
from stonks.store.state import SqliteState

SESSION = date(2026, 3, 18)
AT = datetime(2026, 3, 18, 21, 15, tzinfo=UTC)


def _settings(tmp_path) -> Settings:
    s = Settings(state={"path": tmp_path / "state.sqlite"}, notify={"backends": []})
    with SqliteState(s.state.path) as state:
        state.migrate()
    return s


def _ctx(settings) -> RunContext:
    return RunContext(
        spec=JobSpec(
            "live_gate_days", "live_gate_days", SessionTrigger("XNYS", "close", timedelta(0))
        ),
        fire=Fire(AT, SESSION, "k"),
        run_id="srun_t",
        now=AT,
        settings=settings,
        notifier=None,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("registry", [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS])
def test_the_job_skips_without_live_portfolios(tmp_path, registry):
    out = registry.get("live_gate_days")(_ctx(_settings(tmp_path)))
    assert out.status == "skipped" and out.detail["reason"] == "no_live_portfolios"


def test_the_job_records_the_session_and_alerts_a_dirty_week(tmp_path):
    settings = _settings(tmp_path)
    with SqliteState(settings.state.path) as state:
        change_stage(
            state, "pf_default", "broker_paper", actor="t", reason="soak",
            gate_report={"target": "broker_paper", "passed": True},
        )  # fmt: skip
        for i in range(5):
            day = SESSION - timedelta(days=i)
            state.execute(
                "INSERT INTO orders (client_id, ticker, side, quantity, order_type, status,"
                " state, portfolio_id, created_at, updated_at)"
                " VALUES (?, 'A.US', 'buy', 1, 'limit', 'pending', 'submitted', 'pf_default',"
                " ?, 'x')",
                [f"stuck{i}", f"{day.isoformat()}T14:00:00+00:00"],
            )
    for i in range(4, -1, -1):
        ctx = _ctx(settings)
        day = SESSION - timedelta(days=i)
        ctx = RunContext(
            spec=ctx.spec, fire=Fire(AT, day, f"k{i}"), run_id="r", now=AT, settings=settings,
            notifier=None,  # type: ignore[arg-type]
        )  # fmt: skip
        out = LOCAL_ACTIONS.get("live_gate_days")(ctx)
    assert out.status == "succeeded"
    assert out.detail == {
        "session": SESSION.isoformat(),
        "recorded": 1,
        "clean": 0,
        "dirty_weeks": ["pf_default"],
    }
    with SqliteState(settings.state.path) as state:
        assert len(gate_days(state, "pf_default")) == 5
        assert get_stage(state, "pf_default") == "broker_paper"  # an alert, not a demotion
        [push] = state.sql("SELECT * FROM notification_outbox WHERE category = 'risk'")
    assert "were not clean" in push["body"] and "A.US" not in push["body"]
