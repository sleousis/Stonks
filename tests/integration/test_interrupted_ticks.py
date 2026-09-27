"""TO-06: a tick killed mid-run (container stop, OOM, reboot) leaves its
``tick_runs`` row at ``running``. The next start of the process that runs
ticks closes it as ``error`` (interrupted), so the stuck-tick health check
passes again and the operational buy halt clears."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from stonks.config import HealthConfig
from stonks.production.halts import list_halts, run_health
from stonks.production.tick import INTERRUPTED_ERROR, recover_interrupted_ticks
from stonks.store.state import SqliteState

NOW = datetime(2026, 4, 3, 21, 0, tzinfo=UTC)


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


def _running(state, tick_id: str, started: datetime) -> None:
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status) VALUES (?, ?, 'running')",
        [tick_id, started.isoformat(timespec="seconds")],
    )


def _row(state, tick_id):
    return state.sql("SELECT * FROM tick_runs WHERE id = ?", [tick_id])[0]


def test_running_rows_are_closed_as_interrupted(state):
    _running(state, "tick_a", NOW - timedelta(hours=3))
    state.execute(
        "INSERT INTO tick_runs (id, started_at, finished_at, status) VALUES"
        " ('tick_done', ?, ?, 'ok')",
        [NOW.isoformat(), NOW.isoformat()],
    )
    assert recover_interrupted_ticks(state, now=NOW) == ["tick_a"]
    row = _row(state, "tick_a")
    assert row["status"] == "error" and row["finished_at"]
    assert json.loads(row["summary_json"])["error"] == INTERRUPTED_ERROR
    assert _row(state, "tick_done")["status"] == "ok"
    # idempotent
    assert recover_interrupted_ticks(state, now=NOW) == []


def _owned(state, tick_id: str, started: datetime, host: str, pid: int) -> None:
    state.execute(
        "INSERT INTO tick_runs (id, started_at, status, summary_json) VALUES (?, ?, 'running', ?)",
        [
            tick_id,
            started.isoformat(timespec="seconds"),
            json.dumps({"owner": {"host": host, "pid": pid}}),
        ],
    )


def test_be44_a_tick_whose_process_is_alive_survives_recovery(state):
    import os
    import socket

    here = socket.gethostname()
    _owned(state, "tick_live", NOW - timedelta(minutes=5), here, os.getpid())
    _owned(state, "tick_dead", NOW - timedelta(minutes=5), here, 2**30)
    _owned(state, "tick_elsewhere", NOW - timedelta(hours=1), "other-host", 1)
    _owned(state, "tick_abandoned", NOW - timedelta(days=1), "other-host", 1)
    assert sorted(recover_interrupted_ticks(state, now=NOW)) == ["tick_abandoned", "tick_dead"]
    assert _row(state, "tick_live")["status"] == "running"
    assert _row(state, "tick_elsewhere")["status"] == "running"


def test_be44_a_running_tick_records_its_owner(state, lake_trending, tmp_path):
    import os

    from stonks.production.tick import tick_owner

    owner = tick_owner()
    assert owner["pid"] == os.getpid() and owner["host"]


def test_after_recovery_the_next_health_run_clears_the_buy_halt(state, lake_trending):
    _running(state, "tick_a", NOW - timedelta(hours=3))
    run_health(state, lake_trending, [], HealthConfig(), now=NOW)
    [halt] = list_halts(state, on=NOW.date())
    assert halt.kind == "operational" and "stuck_ticks" in halt.reason

    recover_interrupted_ticks(state, now=NOW)
    report = run_health(state, lake_trending, [], HealthConfig(), now=NOW)

    assert {c.name: c.ok for c in report.checks}["stuck_ticks"] is True
    assert list_halts(state, on=NOW.date()) == []


def test_the_local_scheduler_start_recovers_interrupted_ticks(tmp_path):
    from stonks.config import Settings
    from stonks.scheduling.jobs import JobSpec
    from stonks.scheduling.local import LocalExecutor
    from stonks.scheduling.runs import RunStore
    from stonks.scheduling.scheduler import Scheduler
    from stonks.scheduling.triggers import SessionTrigger

    settings = Settings(
        lake={"path": tmp_path / "lake.duckdb"},
        state={"path": tmp_path / "state.sqlite"},
        registry={"artifacts_dir": tmp_path / "artifacts"},
        notify={"backends": []},
    )
    with SqliteState(settings.state.path) as s:
        s.migrate()
        _running(s, "tick_dead", NOW)
    store = RunStore(settings.state.path)
    store.migrate()
    spec = JobSpec("tick", "tick", SessionTrigger("XNYS"))
    from stonks.notify import LogNotifier

    Scheduler(
        [spec], store, settings=settings, notifier=LogNotifier(), executor=LocalExecutor()
    ).start()
    with SqliteState(settings.state.path) as s:
        assert _row(s, "tick_dead")["status"] == "error"
