"""The ``broker_health`` and ``ibkr_reauth_reminder`` scheduler actions
(roadmap 19.4) on every backend: they skip without gateways and probe the
gateway socket in the scheduler's own process otherwise."""

from __future__ import annotations

import socket
from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.config import Settings
from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobSpec, RunContext
from stonks.scheduling.local import LOCAL_ACTIONS
from stonks.scheduling.triggers import Fire, IntervalTrigger
from stonks.store.state import SqliteState

AT = datetime(2026, 9, 27, 22, tzinfo=UTC)  # a Sunday


def _settings(tmp_path, gateways=None) -> Settings:
    s = Settings(
        state={"path": tmp_path / "state.sqlite"},
        notify={"backends": []},
        brokers={"ibkr": {"gateways": gateways or {}}},
    )
    with SqliteState(s.state.path) as state:
        state.migrate()
    return s


def _ctx(settings, action) -> RunContext:
    return RunContext(
        spec=JobSpec(action, action, IntervalTrigger(timedelta(minutes=5))),
        fire=Fire(AT, date(2026, 9, 27), "k"),
        run_id="srun_t",
        now=AT,
        settings=settings,
        notifier=None,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("registry", [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS])
@pytest.mark.parametrize("action", ["broker_health", "ibkr_reauth_reminder"])
def test_the_jobs_skip_without_gateways(tmp_path, registry, action):
    out = registry.get(action)(_ctx(_settings(tmp_path), action))
    assert out.status == "skipped" and out.detail["reason"] == "no_gateways"


def test_broker_health_probes_a_listening_gateway(tmp_path):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        gw = {"paper": {"host": "127.0.0.1", "port": port, "mode": "paper"}}
        settings = _settings(tmp_path, gw)
        out = LOCAL_ACTIONS.get("broker_health")(_ctx(settings, "broker_health"))
    finally:
        server.close()
    assert out.status == "succeeded" and out.detail["down"] == []
    with SqliteState(settings.state.path) as state:
        row = state.sql("SELECT connected FROM broker_gateway_status WHERE gateway = 'paper'")
    assert row[0]["connected"] == 1


def test_broker_health_reports_a_down_gateway(tmp_path):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens there now
    gw = {"paper": {"host": "127.0.0.1", "port": port, "mode": "paper"}}
    settings = _settings(tmp_path, gw)
    settings.brokers.ibkr.health.probe_timeout_seconds = 0.5
    out = LOCAL_ACTIONS.get("broker_health")(_ctx(settings, "broker_health"))
    assert out.status == "failed" and out.detail["down"] == ["paper"] and out.alerted


def test_the_reminder_is_sent(tmp_path):
    gw = {"paper": {"host": "h", "port": 4004, "mode": "paper", "portfolios": ["pf_default"]}}
    settings = _settings(tmp_path, gw)
    out = API_ACTIONS.get("ibkr_reauth_reminder")(_ctx(settings, "ibkr_reauth_reminder"))
    assert out.status == "succeeded" and out.detail["sent"] == 1
    with SqliteState(settings.state.path) as state:
        rows = state.sql("SELECT dedupe_key FROM notification_outbox")
    assert [r["dedupe_key"] for r in rows] == ["ibkr_reauth:2026-W39:pf:pf_default"]
