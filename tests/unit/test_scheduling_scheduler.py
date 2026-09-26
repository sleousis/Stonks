"""The scheduler loop (roadmap 12.2): claims, catch-up, coalescing, locks,
alerts and pings. Time is injected; nothing sleeps."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.notify import Notification, Notifier
from stonks.scheduling.config import SchedulerConfig
from stonks.scheduling.deadman import Pinger
from stonks.scheduling.jobs import JobOutcome, JobSpec
from stonks.scheduling.local import register_action
from stonks.scheduling.runs import RunStore
from stonks.scheduling.scheduler import InstanceLock, Scheduler, SchedulerAlreadyRunningError
from stonks.scheduling.triggers import IntervalTrigger, SessionTrigger


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.t = now

    def now(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)


class Recorder(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def _send(self, n: Notification) -> None:
        self.sent.append(n)


class FakePinger(Pinger):
    def __init__(self) -> None:
        self.pings: list[tuple[str, str, str]] = []

    def _send(self, url, event, *, run_id, body):
        self.pings.append((url, event, run_id))


calls: list[tuple[str, date]] = []
behaviour: dict[str, object] = {}


@register_action("test_record")
def _record(ctx):
    calls.append((ctx.spec.name, ctx.fire.as_of))
    b = behaviour.get(ctx.spec.name)
    if isinstance(b, Exception):
        raise b
    if callable(b):
        return b(ctx)
    if isinstance(b, JobOutcome):
        return b
    return JobOutcome("succeeded", {"n": len(calls)})


@pytest.fixture(autouse=True)
def _reset():
    calls.clear()
    behaviour.clear()


@pytest.fixture
def store(tmp_path) -> RunStore:
    s = RunStore(tmp_path / "state.sqlite")
    s.migrate()
    return s


def _close_job(name="ingest", minutes=30, catch_up="latest", **kw) -> JobSpec:
    return JobSpec(
        name=name,
        action="test_record",
        trigger=SessionTrigger("XNYS", offset=timedelta(minutes=minutes)),
        catch_up=catch_up,
        **kw,
    )


def _sched(store, specs, clock, notifier=None, pinger=None, **cfg) -> Scheduler:
    return Scheduler(
        specs,
        store,
        settings=None,
        notifier=notifier or Recorder(),
        config=SchedulerConfig(**cfg),
        pinger=pinger,
        clock=clock,
    )


# ---- basic firing -------------------------------------------------------------------


def test_fires_after_close_once(store):
    clock = FakeClock(_utc(2026, 9, 25, 20))  # Friday, before the close
    s = _sched(store, [_close_job()], clock, catch_up="none")
    s.start()
    assert s.run_pending() == []
    clock.advance(minutes=29)  # 20:29
    assert s.run_pending() == []
    clock.advance(minutes=2)  # 20:31
    [result] = s.run_pending()
    assert (result.status, result.fire.as_of, result.catch_up) == (
        "succeeded",
        date(2026, 9, 25),
        False,
    )
    clock.advance(minutes=5)
    assert s.run_pending() == []
    assert calls == [("ingest", date(2026, 9, 25))]
    run = store.get("ingest", "2026-09-25")
    assert run.status == "succeeded" and run.detail == {"n": 1}


def test_jobs_run_in_fire_order(store):
    clock = FakeClock(_utc(2026, 9, 25, 20))
    specs = [_close_job("tick", 45), _close_job("ingest", 30)]
    s = _sched(store, specs, clock, catch_up="none")
    s.start()
    clock.advance(hours=1)
    assert [r.job_name for r in s.run_pending()] == ["ingest", "tick"]


def test_weekend_and_holiday_do_not_fire(store):
    clock = FakeClock(_utc(2026, 11, 26, 0))  # Thanksgiving
    s = _sched(store, [_close_job()], clock, catch_up="none")
    s.start()
    clock.advance(hours=23)
    assert s.run_pending() == []


# ---- catch-up -----------------------------------------------------------------------


def _seed_run(store, spec, fire_day: date, clock_time: datetime):
    fire = spec.trigger.last_fire_at_or_before(clock_time, timedelta(days=5))
    assert fire.as_of == fire_day
    run_id = store.claim(spec, fire, instance_id="old", now=clock_time, catch_up=False)
    store.finish(run_id, "succeeded", now=clock_time)


def test_catch_up_latest_runs_only_most_recent_missed(store):
    spec = _close_job()
    _seed_run(store, spec, date(2026, 9, 21), _utc(2026, 9, 21, 21))
    clock = FakeClock(_utc(2026, 9, 24, 12))  # Thursday morning; Tue+Wed missed
    s = _sched(store, [spec], clock)
    s.start()
    [r] = s.run_pending()
    assert (r.fire.as_of, r.catch_up) == (date(2026, 9, 23), True)
    assert store.get("ingest", "2026-09-22") is None
    assert s.run_pending() == []


def test_catch_up_all_is_capped_and_oldest_first(store):
    spec = _close_job(catch_up="all")
    _seed_run(store, spec, date(2026, 9, 21), _utc(2026, 9, 21, 21))
    clock = FakeClock(_utc(2026, 9, 25, 12))  # Tue, Wed, Thu missed
    s = _sched(store, [spec], clock, max_catch_up_runs=2)
    s.start()
    assert [r.fire.as_of for r in s.run_pending()] == [date(2026, 9, 23), date(2026, 9, 24)]


def test_first_start_has_nothing_to_catch_up(store):
    clock = FakeClock(_utc(2026, 9, 25, 12))
    s = _sched(store, [_close_job(catch_up="all")], clock)
    s.start()
    assert s.run_pending() == []


def test_catch_up_none(store):
    spec = _close_job(catch_up="none")
    _seed_run(store, spec, date(2026, 9, 21), _utc(2026, 9, 21, 21))
    clock = FakeClock(_utc(2026, 9, 25, 12))
    s = _sched(store, [spec], clock)
    s.start()
    assert s.run_pending() == []


def test_catch_up_window_limits_history(store):
    # Last ran long ago; only fires inside the window count.
    spec = _close_job(catch_up="all")
    _seed_run(store, spec, date(2026, 9, 14), _utc(2026, 9, 14, 21))
    clock = FakeClock(_utc(2026, 9, 25, 12))
    s = _sched(store, [spec], clock, catch_up_window_hours=36, max_catch_up_runs=10)
    s.start()
    assert [r.fire.as_of for r in s.run_pending()] == [date(2026, 9, 24)]


def test_catch_up_skips_fires_that_already_ran(store):
    spec = _close_job()
    _seed_run(store, spec, date(2026, 9, 24), _utc(2026, 9, 24, 21))
    clock = FakeClock(_utc(2026, 9, 25, 12))
    s = _sched(store, [spec], clock)
    s.start()
    assert s.run_pending() == []


def test_restart_does_not_rerun_a_finished_fire(store):
    spec = _close_job()
    clock = FakeClock(_utc(2026, 9, 25, 20))
    first = _sched(store, [spec], clock)
    first.start()
    clock.advance(hours=1)
    assert len(first.run_pending()) == 1
    second = _sched(store, [spec], clock)
    second.start()
    assert second.run_pending() == []
    assert len(calls) == 1


# ---- coalescing / double runs ------------------------------------------------------


def test_suspended_loop_coalesces_to_latest_fire(store):
    spec = JobSpec("health", "test_record", IntervalTrigger(timedelta(hours=1)), catch_up="none")
    clock = FakeClock(_utc(2026, 9, 25, 0, 30))
    s = _sched(store, [spec], clock)
    s.start()
    clock.advance(hours=5)  # machine slept through five fires
    [r] = s.run_pending()
    assert r.fire.scheduled_for == _utc(2026, 9, 25, 5)


def test_two_schedulers_never_run_the_same_fire(store):
    spec = _close_job()
    clock = FakeClock(_utc(2026, 9, 25, 20))
    a, b = _sched(store, [spec], clock), _sched(store, [spec], clock)
    a.start()
    b.start()
    clock.advance(hours=1)
    ra, rb = a.run_pending(), b.run_pending()
    assert [r.status for r in ra + rb] == ["succeeded", "already_claimed"]
    assert len(calls) == 1


def test_instance_lock_is_exclusive(tmp_path):
    path = tmp_path / "scheduler.lock"
    with InstanceLock(path), pytest.raises(SchedulerAlreadyRunningError):
        InstanceLock(path).acquire()
    # released: a new scheduler can take it
    lock = InstanceLock(path)
    lock.acquire()
    lock.release()


def test_start_marks_interrupted_runs_failed(store):
    spec = _close_job()
    fire = spec.trigger.next_fire(_utc(2026, 9, 25))
    store.claim(spec, fire, instance_id="crashed", now=_utc(2026, 9, 25, 21), catch_up=False)
    s = _sched(store, [spec], FakeClock(_utc(2026, 9, 25, 22)))
    s.start()
    run = store.get("ingest", fire.key)
    assert run.status == "failed" and "interrupted" in run.error
    # the interrupted fire has a row, so catch-up doesn't blindly rerun it
    assert s.run_pending() == []


# ---- failures, alerts, pings --------------------------------------------------------


def test_exception_fails_the_run_and_alerts(store):
    notifier, pinger = Recorder(), FakePinger()
    spec = _close_job(ping_url="https://hc.example/abc")
    behaviour["ingest"] = RuntimeError("vendor down")
    clock = FakeClock(_utc(2026, 9, 25, 20))
    s = _sched(store, [spec], clock, notifier=notifier, pinger=pinger)
    s.start()
    clock.advance(hours=1)
    [r] = s.run_pending()
    assert r.status == "failed"
    assert store.get("ingest", "2026-09-25").error == "RuntimeError: vendor down"
    [alert] = notifier.sent
    assert alert.level == "error" and "ingest" in alert.title
    assert [p[1] for p in pinger.pings] == ["start", "fail"]
    assert pinger.pings[0][2] == pinger.pings[1][2] == r.run_id


def test_action_that_alerted_itself_is_not_alerted_twice(store):
    notifier = Recorder()
    behaviour["ingest"] = JobOutcome("failed", {"error": "tick error"}, alerted=True)
    clock = FakeClock(_utc(2026, 9, 25, 20))
    s = _sched(store, [_close_job()], clock, notifier=notifier)
    s.start()
    clock.advance(hours=1)
    assert s.run_pending()[0].status == "failed"
    assert notifier.sent == []


def test_success_and_skip_ping_success(store):
    pinger = FakePinger()
    behaviour["ingest"] = JobOutcome("skipped", {"reason": "market_closed"})
    clock = FakeClock(_utc(2026, 9, 25, 20))
    s = _sched(store, [_close_job(ping_url="https://hc.example/abc")], clock, pinger=pinger)
    s.start()
    clock.advance(hours=1)
    assert s.run_pending()[0].status == "skipped"
    assert [p[1] for p in pinger.pings] == ["start", "success"]


# ---- shutdown and the loop --------------------------------------------------------


def test_stop_request_finishes_current_job_then_stops(store):
    clock = FakeClock(_utc(2026, 9, 25, 20))
    specs = [_close_job("ingest", 30), _close_job("tick", 45)]
    s = _sched(store, specs, clock)

    def stop_during(ctx):
        s.request_stop()
        return JobOutcome("succeeded")

    behaviour["ingest"] = stop_during
    s.start()
    clock.advance(hours=1)
    assert [r.job_name for r in s.run_pending()] == ["ingest"]
    assert store.get("tick", "2026-09-25") is None


def test_run_forever_with_injected_wait(store):
    clock = FakeClock(_utc(2026, 9, 25, 20))
    s = _sched(store, [_close_job()], clock, poll_seconds=3600)
    waits: list[float] = []

    def fake_wait(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds=seconds)
        if len(waits) >= 3:
            s.request_stop()

    s.run_forever(wait=fake_wait)
    # first wake lands exactly on the 20:30 fire, then polls
    assert waits[:2] == [1800, 3600]
    assert calls == [("ingest", date(2026, 9, 25))]
    inst = store.latest_instance()
    assert inst["id"] == s.instance_id and inst["stopped_at"] is not None


def test_request_stop_from_another_thread_wakes_the_loop(store):
    clock = FakeClock(_utc(2026, 9, 26, 12))  # Saturday: nothing due
    s = _sched(store, [_close_job()], clock, poll_seconds=3600)
    t = threading.Thread(target=s.run_forever)
    t.start()
    s.request_stop()
    t.join(timeout=5)
    assert not t.is_alive()


def test_next_runs(store):
    clock = FakeClock(_utc(2026, 9, 25, 22))
    s = _sched(store, [_close_job()], clock)
    [n] = s.next_runs()
    assert n.next_fire.scheduled_for == _utc(2026, 9, 28, 20, 30)
    assert "XNYS" in n.trigger


def test_heartbeat_and_watchdog_run_while_a_long_job_blocks_the_loop(store):
    clock = FakeClock(_utc(2026, 9, 25, 20))
    s = _sched(store, [_close_job()], clock, watchdog_seconds=0.01)
    checked = threading.Event()

    class Dog:
        def check(self, now):
            checked.set()
            return []

    def long_job(ctx):
        assert checked.wait(5), "watchdog never ran while the job was running"
        s.request_stop()
        return JobOutcome("succeeded")

    behaviour["ingest"] = long_job
    s.run_forever(wait=lambda seconds: clock.advance(hours=1), watchdog=Dog())
    assert store.get("ingest", "2026-09-25").status == "succeeded"


# ---- self-review regressions -------------------------------------------------------


def test_manual_run_does_not_hide_a_missed_fire_from_catch_up(store):
    from stonks.scheduling.triggers import Fire

    spec = _close_job()
    _seed_run(store, spec, date(2026, 9, 22), _utc(2026, 9, 22, 21))
    # Thursday 09:00: a manual run for another day; Wednesday's fire was missed.
    manual = Fire(_utc(2026, 9, 24, 9), date(2026, 9, 21), "manual:2026-09-24T09:00:00+00:00")
    run_id = store.claim(spec, manual, instance_id="cli", now=manual.scheduled_for, catch_up=False)
    store.finish(run_id, "succeeded", now=manual.scheduled_for)
    s = _sched(store, [spec], FakeClock(_utc(2026, 9, 24, 12)))
    s.start()
    assert [r.fire.as_of for r in s.run_pending()] == [date(2026, 9, 23)]


def test_recovery_leaves_a_fresh_manual_run_alone(store):
    from stonks.scheduling.triggers import Fire

    spec = _close_job()
    now = _utc(2026, 9, 25, 12)
    manual = Fire(now, date(2026, 9, 25), f"manual:{now.isoformat()}")
    store.claim(spec, manual, instance_id="cli", now=now, catch_up=False)
    _sched(store, [spec], FakeClock(now + timedelta(minutes=5))).start()
    assert store.get("ingest", manual.key).status == "running"
    _sched(store, [spec], FakeClock(now + timedelta(days=2))).start()
    assert store.get("ingest", manual.key).status == "failed"


def test_a_store_error_mid_batch_is_retried_not_lost(store, monkeypatch):
    clock = FakeClock(_utc(2026, 9, 25, 20))
    s = _sched(store, [_close_job("ingest", 30), _close_job("tick", 45)], clock)
    s.start()
    clock.advance(hours=1)
    real_claim = store.claim
    failures = iter([True])

    def flaky(spec, fire, **kw):
        if spec.name == "tick" and next(failures, False):
            raise RuntimeError("database is locked")
        return real_claim(spec, fire, **kw)

    monkeypatch.setattr(store, "claim", flaky)
    with pytest.raises(RuntimeError):
        s.run_pending()
    assert store.get("ingest", "2026-09-25").status == "succeeded"
    clock.advance(minutes=1)
    assert [r.job_name for r in s.run_pending()] == ["tick"]


def test_loop_survives_a_failing_iteration(store, monkeypatch):
    clock = FakeClock(_utc(2026, 9, 25, 20))
    s = _sched(store, [_close_job()], clock, poll_seconds=3600)
    boom = iter([True])

    real = s.run_pending

    def flaky():
        if next(boom, False):
            raise RuntimeError("database is locked")
        return real()

    monkeypatch.setattr(s, "run_pending", flaky)
    waits = []

    def fake_wait(seconds):
        waits.append(seconds)
        clock.advance(seconds=seconds)
        if len(waits) >= 2:
            s.request_stop()

    s.run_forever(wait=fake_wait)
    assert calls == [("ingest", date(2026, 9, 25))]
