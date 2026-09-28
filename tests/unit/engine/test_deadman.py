"""The engine dead-man (roadmap 21.3.4): no bar close for N minutes while
the market is open alerts the operator once per silent stretch."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from stonks.engine.deadman import EngineDeadman, silent_engines
from stonks.engine.status import EngineStatus, EngineStatusStore
from stonks.notify import Notification, Notifier
from stonks.scheduling.calendar import MarketCalendar, Session
from stonks.scheduling.runs import RunStore
from stonks.store.state import SqliteState

OPEN = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)
CLOSE = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)


class OneDay(MarketCalendar):
    name = "one-day"

    def session(self, day: date) -> Session | None:
        return Session(day, OPEN, CLOSE) if day == OPEN.date() else None


def calendars(name: str) -> MarketCalendar:
    return OneDay()


class Capture(Notifier):
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def _send(self, notification: Notification) -> None:
        self.sent.append(notification)


def status(
    *,
    started=OPEN - timedelta(minutes=30),
    last=OPEN + timedelta(minutes=10),
    stopped=None,
    engine_id="e1",
) -> EngineStatus:
    return EngineStatus(
        engine_id=engine_id,
        calendar="XNYS",
        state="streaming",
        started_at=started,
        updated_at=last or started,
        stopped_at=stopped,
        last_dispatch_at=last,
    )


def test_silent_in_market_hours():
    now = OPEN + timedelta(minutes=16)
    [s] = silent_engines([status()], now, minutes=5, calendar_for=calendars)
    assert s.engine_id == "e1"
    assert s.since == OPEN + timedelta(minutes=10)
    assert s.silent_seconds == 360


def test_quiet_for_less_than_the_limit_is_fine():
    now = OPEN + timedelta(minutes=14)
    assert silent_engines([status()], now, minutes=5, calendar_for=calendars) == []


def test_closed_market_never_alerts():
    now = CLOSE + timedelta(hours=1)
    assert silent_engines([status()], now, minutes=5, calendar_for=calendars) == []


def test_stopped_engine_never_alerts():
    now = OPEN + timedelta(hours=2)
    stopped = status(stopped=OPEN + timedelta(minutes=11))
    assert silent_engines([stopped], now, minutes=5, calendar_for=calendars) == []


def test_silence_counts_from_the_open_not_yesterday():
    yesterday = OPEN - timedelta(days=1)
    engine = status(started=yesterday - timedelta(hours=1), last=yesterday)
    assert (
        silent_engines([engine], OPEN + timedelta(minutes=4), minutes=5, calendar_for=calendars)
        == []
    )
    [s] = silent_engines([engine], OPEN + timedelta(minutes=6), minutes=5, calendar_for=calendars)
    assert s.since == OPEN


def test_a_fresh_engine_without_a_dispatch_counts_from_its_start():
    engine = status(started=OPEN + timedelta(minutes=60), last=None)
    now = OPEN + timedelta(minutes=66)
    [s] = silent_engines([engine], now, minutes=5, calendar_for=calendars)
    assert s.since == OPEN + timedelta(minutes=60)


def test_an_unknown_calendar_falls_back_to_the_default():
    def broken(name: str) -> MarketCalendar:
        if name == "XNYS":
            raise KeyError(name)
        return OneDay()

    now = OPEN + timedelta(minutes=16)
    [s] = silent_engines([status()], now, minutes=5, calendar_for=broken, fallback_calendar="other")
    assert s.engine_id == "e1"


@pytest.fixture
def state_path(tmp_path):
    path = tmp_path / "state.sqlite"
    with SqliteState(path) as s:
        s.migrate()
    return path


def write(state_path, *, last: datetime, now: datetime) -> None:
    store = EngineStatusStore(state_path)
    store.write(
        {
            "engine_id": "e1",
            "calendar": "XNYS",
            "state": "streaming",
            "last_dispatch_at": last.isoformat(),
        },
        now=now,
    )


def test_watchdog_alerts_once_per_silent_stretch(state_path):
    notifier = Capture()
    dog = EngineDeadman(
        state_path, RunStore(state_path), notifier, minutes=5, calendar_for=calendars
    )
    write(state_path, last=OPEN + timedelta(minutes=10), now=OPEN + timedelta(minutes=10))
    assert dog.check(OPEN + timedelta(minutes=12)) == []
    [alerted] = dog.check(OPEN + timedelta(minutes=16))
    assert alerted.engine_id == "e1"
    assert dog.check(OPEN + timedelta(minutes=30)) == []
    assert len(notifier.sent) == 1
    note = notifier.sent[0]
    assert note.level == "error"
    assert "e1" in note.title
    assert note.fields["engine"] == "e1"
    # it comes back, then goes quiet again: a new stretch, a new alert
    write(state_path, last=OPEN + timedelta(minutes=40), now=OPEN + timedelta(minutes=40))
    assert dog.check(OPEN + timedelta(minutes=42)) == []
    assert len(dog.check(OPEN + timedelta(minutes=46))) == 1
    assert len(notifier.sent) == 2


def test_watchdog_without_the_table_does_nothing(tmp_path):
    notifier = Capture()
    path = tmp_path / "old.sqlite"
    store = RunStore(path)
    store.migrate()
    with SqliteState(path) as s:
        s.execute("DROP TABLE engine_status")
    dog = EngineDeadman(path, store, notifier, minutes=5, calendar_for=calendars)
    assert dog.check(OPEN + timedelta(hours=1)) == []
