"""Scheduler config and job specs (roadmap 12.2)."""

from __future__ import annotations

from datetime import time, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from stonks.scheduling.api_backend import API_ACTIONS
from stonks.scheduling.calendar import UnknownCalendarError
from stonks.scheduling.config import (
    DailyTriggerConfig,
    JobConfig,
    SchedulerConfig,
    SessionTriggerConfig,
    scheduler_config_from,
)
from stonks.scheduling.in_process import IN_PROCESS_ACTIONS
from stonks.scheduling.jobs import JobOutcome, UnknownActionError, build_job_specs
from stonks.scheduling.local import LOCAL_ACTIONS, LocalExecutor, register_action
from stonks.scheduling.triggers import DailyTrigger, IntervalTrigger, SessionTrigger

BUILTIN = {"ingest_prices", "tick", "health", "report"}


def test_default_jobs_build():
    specs = build_job_specs(SchedulerConfig(), env={})
    by_name = {s.name: s for s in specs}
    assert set(by_name) == {
        "ingest_prices",
        "tick",
        "report",
        "health",
        "backup",
        "connections_sync",
    }
    tick = by_name["tick"]
    assert tick.trigger == SessionTrigger("XNYS", "close", timedelta(minutes=45))
    assert tick.deadline == timedelta(minutes=60)
    assert tick.catch_up == "latest"
    assert isinstance(by_name["health"].trigger, IntervalTrigger)
    # the tick runs after the ingest it depends on
    ingest_at = by_name["ingest_prices"].trigger.offset
    assert ingest_at < tick.trigger.offset


@pytest.mark.parametrize("registry", [LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS])
def test_every_backend_runs_the_builtin_actions(registry):
    assert set(registry.names()) >= BUILTIN


def test_default_jobs_validate_against_every_backend():
    for registry in (LOCAL_ACTIONS, API_ACTIONS, IN_PROCESS_ACTIONS):
        build_job_specs(SchedulerConfig(), env={}, actions=registry.names())


def test_duplicate_job_names_rejected():
    job = JobConfig(name="a", action="tick", trigger=SessionTriggerConfig())
    with pytest.raises(ValidationError, match="duplicate"):
        SchedulerConfig(jobs=[job, job])


def test_unknown_action_and_calendar_fail_fast():
    bad_action = JobConfig(name="a", action="nope", trigger=SessionTriggerConfig())
    with pytest.raises(UnknownActionError):
        build_job_specs(
            SchedulerConfig(jobs=[bad_action]), env={}, actions=LocalExecutor().actions()
        )
    bad_cal = JobConfig(name="a", action="tick", trigger=SessionTriggerConfig(calendar="ZZZZ"))
    with pytest.raises(UnknownCalendarError):
        build_job_specs(SchedulerConfig(jobs=[bad_cal]), env={})


def test_trigger_discriminator_and_overrides():
    cfg = SchedulerConfig(
        catch_up="all",
        jobs=[
            {
                "name": "crypto_tick",
                "action": "tick",
                "trigger": {"type": "daily", "at": "00:05", "timezone": "UTC"},
                "catch_up": "none",
            },
            {
                "name": "off",
                "action": "tick",
                "trigger": {"type": "interval", "every_minutes": 5},
                "enabled": False,
            },
            {"name": "h", "action": "health", "trigger": {"type": "interval", "every_minutes": 5}},
        ],
    )
    assert isinstance(cfg.jobs[0].trigger, DailyTriggerConfig)
    specs = build_job_specs(cfg, env={})
    assert [s.name for s in specs] == ["crypto_tick", "h"]
    assert specs[0].trigger == DailyTrigger(time(0, 5), tz="UTC")
    assert specs[0].catch_up == "none"
    assert specs[1].catch_up == "all"


def test_ping_url_from_env_or_config():
    jobs = [
        JobConfig(name="a", action="tick", trigger=SessionTriggerConfig(), ping_url_env="PING_A"),
        JobConfig(name="b", action="tick", trigger=SessionTriggerConfig(), ping_url="https://hc/b"),
        JobConfig(name="c", action="tick", trigger=SessionTriggerConfig(), ping_url_env="MISSING"),
    ]
    specs = build_job_specs(SchedulerConfig(jobs=jobs), env={"PING_A": "https://hc/a"})
    assert [s.ping_url for s in specs] == ["https://hc/a", "https://hc/b", None]


def test_ping_url_is_secret_in_repr():
    job = JobConfig(
        name="a", action="tick", trigger=SessionTriggerConfig(), ping_url="https://x/tok"
    )
    assert "tok" not in repr(job)


def test_register_custom_action():
    @register_action("noop_test")
    def _noop(ctx):
        return JobOutcome("succeeded")

    assert LOCAL_ACTIONS.get("noop_test") is _noop


def test_config_from_toml(tmp_path: Path):
    toml = tmp_path / "c.toml"
    toml.write_text(
        '[scheduler]\ncatch_up = "none"\n\n[[scheduler.jobs]]\nname = "t"\naction = "tick"\n'
        'trigger = { type = "session", calendar = "XLON", offset_minutes = 15 }\n',
        encoding="utf-8",
    )
    cfg = scheduler_config_from(object(), toml)
    assert cfg.catch_up == "none"
    assert [j.name for j in cfg.jobs] == ["t"]
    # absent file -> defaults; a Settings that has the field wins
    assert scheduler_config_from(object(), tmp_path / "missing.toml").catch_up == "latest"

    class _S:
        scheduler = SchedulerConfig(catch_up="all")

    assert scheduler_config_from(_S(), toml).catch_up == "all"


def test_extra_keys_forbidden():
    with pytest.raises(ValidationError):
        SchedulerConfig(catchup="none")
