"""Scheduled screen alerts (roadmap 23.17): a saved screen runs on a
schedule and notifies its owner about names that newly match."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from stonks.accounts import Role, UserRepository
from stonks.notify.events import Event
from stonks.screener.alerts import finding_event, is_due, load_alerts, run_screen_alerts
from stonks.screener.spec import ScreenSpec
from stonks.store.state import SqliteState

NOW = datetime(2026, 9, 28, 22, 0, tzinfo=UTC)
MONDAY = date(2026, 9, 28)


@pytest.fixture
def state(tmp_path):
    s = SqliteState(tmp_path / "state.sqlite")
    s.migrate()
    yield s
    s.close()


@pytest.fixture
def owner(state) -> str:
    return (
        UserRepository(state)
        .create(display_name="Ann", role=Role.TRADER, actor="test", email="a@x.io")
        .id
    )


def _screen(state, owner, sid="scr_1", name="Cheap", cadence="daily", weekday=None, enabled=1):
    now = NOW.isoformat()
    state.execute(
        "INSERT INTO screens (id, owner_id, name, spec_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [sid, owner, name, json.dumps({"sort_by": "price"}), now, now],
    )
    state.execute(
        "INSERT INTO screen_alerts (screen_id, owner_id, enabled, cadence, weekday, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [sid, owner, enabled, cadence, weekday, now, now],
    )


class Runner:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[date] = []

    def __call__(self, spec: ScreenSpec, as_of: date) -> list[str]:
        assert isinstance(spec, ScreenSpec)
        self.calls.append(as_of)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_first_run_sets_a_baseline_and_sends_nothing(state, owner):
    _screen(state, owner)
    sent: list[Event] = []
    out = run_screen_alerts(
        state, as_of=MONDAY, run=Runner([["AAA.US", "BBB.US"]]), publish=sent.append, now=NOW
    )
    assert (out.ran, out.baselines, out.fired, out.published) == (1, 1, 0, 0)
    assert sent == []
    rows = state.sql("SELECT ticker FROM screen_alert_matches ORDER BY ticker")
    assert [r["ticker"] for r in rows] == ["AAA.US", "BBB.US"]


def test_new_names_notify_the_owner_once(state, owner):
    _screen(state, owner)
    runner = Runner([["AAA.US"], ["AAA.US", "CCC.US", "DDD.US"], ["AAA.US", "CCC.US"]])
    sent: list[Event] = []
    run_screen_alerts(state, as_of=date(2026, 9, 25), run=runner, publish=sent.append, now=NOW)
    out = run_screen_alerts(state, as_of=MONDAY, run=runner, publish=sent.append, now=NOW)
    assert (out.fired, out.published) == (1, 1)
    assert out.findings[0].new == ("CCC.US", "DDD.US")
    event = sent[0]
    assert event.category == "screen_alert"
    assert event.audience.user_ids == (owner,)
    assert "2 new" in event.title and "Cheap" in event.title
    assert "CCC.US" in event.body and event.deep_link == "/screener?screen=scr_1"
    # The same day again does nothing: the alert already ran on it.
    again = run_screen_alerts(state, as_of=MONDAY, run=runner, publish=sent.append, now=NOW)
    assert again.ran == 0 and len(sent) == 1
    # A name that left and nothing new: no alert, and it leaves the set.
    later = run_screen_alerts(
        state, as_of=date(2026, 9, 29), run=runner, publish=sent.append, now=NOW
    )
    assert later.fired == 0 and len(sent) == 1
    rows = state.sql("SELECT ticker FROM screen_alert_matches ORDER BY ticker")
    assert [r["ticker"] for r in rows] == ["AAA.US", "CCC.US"]
    events = state.sql("SELECT as_of, tickers_json, matched FROM screen_alert_events")
    assert [(e["as_of"], json.loads(e["tickers_json"]), e["matched"]) for e in events] == [
        ("2026-09-28", ["CCC.US", "DDD.US"], 3)
    ]


def test_weekly_alerts_run_on_their_weekday_only(state, owner):
    _screen(state, owner, cadence="weekly", weekday=2)
    [rule] = load_alerts(state)
    assert not is_due(rule, MONDAY)
    assert is_due(rule, date(2026, 9, 30))
    runner = Runner([["AAA.US"]])
    out = run_screen_alerts(state, as_of=MONDAY, run=runner, publish=lambda e: None, now=NOW)
    assert out.ran == 0 and runner.calls == []


def test_switched_off_alerts_and_disabled_people_are_skipped(state, owner):
    _screen(state, owner, enabled=0)
    assert load_alerts(state) == []
    other = (
        UserRepository(state)
        .create(display_name="Bo", role=Role.TRADER, actor="test", email="b@x.io")
        .id
    )
    _screen(state, other, sid="scr_2", name="Other")
    state.execute("UPDATE users SET status = 'disabled' WHERE id = ?", [other])
    assert load_alerts(state) == []


def test_a_failing_screen_records_the_error_and_the_rest_still_run(state, owner):
    _screen(state, owner, sid="scr_1", name="Broken")
    _screen(state, owner, sid="scr_2", name="Fine")
    runner = Runner([ValueError("too many candidates"), ["AAA.US"]])
    out = run_screen_alerts(state, as_of=MONDAY, run=runner, publish=lambda e: None, now=NOW)
    assert out.failed == 1 and out.baselines == 1
    rows = {r["screen_id"]: r for r in state.sql("SELECT * FROM screen_alerts")}
    assert "too many candidates" in rows["scr_1"]["last_error"]
    assert rows["scr_1"]["last_as_of"] is None
    assert rows["scr_2"]["last_error"] is None and rows["scr_2"]["last_as_of"] == "2026-09-28"


def test_a_publish_failure_keeps_the_event(state, owner):
    _screen(state, owner)

    def boom(event: Event) -> None:
        raise RuntimeError("router down")

    runner = Runner([["AAA.US"], ["AAA.US", "BBB.US"]])
    run_screen_alerts(state, as_of=date(2026, 9, 25), run=runner, publish=boom, now=NOW)
    out = run_screen_alerts(state, as_of=MONDAY, run=runner, publish=boom, now=NOW)
    assert (out.fired, out.published) == (1, 0)
    assert len(state.sql("SELECT * FROM screen_alert_events")) == 1


def test_the_event_names_at_most_a_few_tickers():
    from stonks.screener.alerts import ScreenAlertFinding

    finding = ScreenAlertFinding(
        screen_id="scr_1",
        owner_id="usr_a",
        name="Momentum",
        as_of=MONDAY,
        new=tuple(f"T{i}.US" for i in range(9)),
        matched=20,
    )
    event = finding_event(finding)
    assert event.title == "9 new names in Momentum"
    assert event.body.startswith("T0.US, T1.US, T2.US, T3.US, T4.US and 4 more")
    assert event.dedupe_key == "screen:scr_1:2026-09-28"
