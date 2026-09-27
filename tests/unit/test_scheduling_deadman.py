"""Dead-man pings and the deadline watchdog (roadmap 12.3). No HTTP."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import requests

from stonks.notify import Notification, Notifier
from stonks.scheduling.deadman import (
    DeadlineWatchdog,
    HttpPinger,
    missed_deadlines,
    ping_target,
)
from stonks.scheduling.jobs import JobSpec
from stonks.scheduling.runs import RunStore
from stonks.scheduling.triggers import SessionTrigger

SECRET = "https://hc-ping.example/0f3c-secret-uuid"


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class Recorder(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def _send(self, n: Notification) -> None:
        self.sent.append(n)


class _Resp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url: {SECRET}")


class _Session:
    def __init__(self, resp=None, exc=None):
        self.calls = []
        self.resp, self.exc = resp or _Resp(), exc

    def post(self, url, params=None, data=None, timeout=None):
        self.calls.append((url, params, data, timeout))
        if self.exc:
            raise self.exc
        return self.resp


# ---- pinger ---------------------------------------------------------------------------


def test_ping_targets_follow_healthchecks_convention():
    assert ping_target("https://hc/x/", "start") == "https://hc/x/start"
    assert ping_target("https://hc/x", "success") == "https://hc/x"
    assert ping_target("https://hc/x", "fail") == "https://hc/x/fail"


def test_http_pinger_posts_with_run_id():
    session = _Session()
    HttpPinger(timeout_seconds=3, session=session).ping(SECRET, "fail", run_id="r1", body="boom")
    assert session.calls == [(f"{SECRET}/fail", {"rid": "r1"}, b"boom", 3)]


@pytest.mark.parametrize(
    "session", [_Session(resp=_Resp(500)), _Session(exc=requests.ConnectionError(SECRET))]
)
def test_http_pinger_never_raises_or_logs_the_url(session, capsys):
    HttpPinger(session=session).ping(SECRET, "start", run_id="r1")
    out = capsys.readouterr().out
    assert "deadman.ping_failed" in out
    assert "secret-uuid" not in out


# ---- watchdog -------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> RunStore:
    s = RunStore(tmp_path / "state.sqlite")
    s.migrate()
    s.register_instance("i1", host="h", pid=1, now=_utc(2026, 9, 25))
    return s


def _tick(deadline_minutes=60) -> JobSpec:
    return JobSpec(
        "tick",
        "tick",
        SessionTrigger("XNYS", offset=timedelta(minutes=45)),
        deadline=timedelta(minutes=deadline_minutes),
    )


def test_missing_run_after_deadline_alerts_once(store):
    notifier = Recorder()
    dog = DeadlineWatchdog([_tick()], store, notifier)
    # Friday 2026-09-25: fire 20:45 UTC, deadline 21:45.
    assert dog.check(_utc(2026, 9, 25, 21, 44)) == []
    [miss] = dog.check(_utc(2026, 9, 25, 21, 46))
    assert miss.reason == "did not run" and miss.fire.key == "2026-09-25"
    assert dog.check(_utc(2026, 9, 25, 22)) == []  # deduped
    assert len(notifier.sent) == 1
    assert "tick" in notifier.sent[0].title
    # a fresh watchdog (restart) doesn't re-alert either
    assert DeadlineWatchdog([_tick()], store, notifier).check(_utc(2026, 9, 26)) == []


def test_succeeded_or_skipped_run_is_on_time(store):
    spec = _tick()
    fire = spec.trigger.next_fire(_utc(2026, 9, 25))
    run_id = store.claim(spec, fire, instance_id="i1", now=fire.scheduled_for, catch_up=False)
    store.finish(run_id, "skipped", now=fire.scheduled_for)
    assert missed_deadlines([spec], store, _utc(2026, 9, 26)) == []


def test_running_or_failed_runs_are_reported(store):
    spec = _tick()
    fire = spec.trigger.next_fire(_utc(2026, 9, 25))
    run_id = store.claim(spec, fire, instance_id="i1", now=fire.scheduled_for, catch_up=False)
    [miss] = missed_deadlines([spec], store, _utc(2026, 9, 26))
    assert miss.reason == "still running"
    store.finish(run_id, "failed", now=fire.scheduled_for, error="x")
    [miss] = missed_deadlines([spec], store, _utc(2026, 9, 26))
    assert miss.reason == "failed"


def test_fires_before_first_start_are_ignored(store):
    assert missed_deadlines([_tick()], store, _utc(2026, 9, 26), not_before=_utc(2026, 9, 26)) == []
    # the watchdog reads not_before from the first registered instance
    late = RunStore(store._path)
    late.register_instance("i0", host="h", pid=1, now=_utc(2026, 8, 1))
    assert late.first_started_at() == _utc(2026, 8, 1)


def test_weekend_checks_fridays_run(store):
    [miss] = missed_deadlines([_tick()], store, _utc(2026, 9, 27, 12))  # Sunday
    assert miss.fire.key == "2026-09-25"


def test_jobs_without_deadline_are_not_watched(store):
    spec = JobSpec("h", "health", SessionTrigger("XNYS"))
    assert missed_deadlines([spec], store, _utc(2026, 9, 26)) == []


def test_extra_checks_run_with_each_check_and_a_failure_is_contained(store):
    seen = []

    class Boom:
        def check(self, now):
            raise RuntimeError("engine table locked")

    class Seen:
        def check(self, now):
            seen.append(now)

    dog = DeadlineWatchdog([], store, Recorder(), extra_checks=[Boom(), Seen()])
    now = _utc(2026, 9, 25, 15)
    assert dog.check(now) == []
    assert seen == [now]


def test_watchdog_includes_the_engine_deadman_from_settings(tmp_path):
    from stonks.config import Settings
    from stonks.engine.deadman import EngineDeadman, engine_deadman_from_settings

    settings = Settings()
    settings.state.path = tmp_path / "state.sqlite"
    store = RunStore(settings.state.path)
    store.migrate()
    dead = engine_deadman_from_settings(settings, store, Recorder())
    assert isinstance(dead, EngineDeadman)
    assert dead.minutes == settings.streaming.monitor.deadman_minutes
