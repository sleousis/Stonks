"""Upcoming-event alerts: each person hears about earnings and ex-dividend
dates of what they hold or watch, once per event."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from stonks.accounts import PortfolioRepository, Role, Scope, UserRepository
from stonks.calendars.alerts import alert_kinds, check_event_alerts, event_hits
from stonks.calendars.store import CalendarStore
from stonks.calendars.tracking import tracked_tickers
from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow
from stonks.notify.prefs import EVENT_ALERT_TOPICS, EventAlertPrefStore
from stonks.notify.router import NotificationRouter

TODAY = date(2026, 10, 28)


@pytest.fixture
def people(state):
    repo = UserRepository(state)
    alice = repo.create(display_name="Alice", role=Role.TRADER, actor="service:system")
    bob = repo.create(display_name="Bob", role=Role.TRADER, actor="service:system")
    pf = PortfolioRepository(state).create(Scope.for_user(alice), name="Main")
    state.execute(
        "INSERT INTO portfolio_snapshots (portfolio_id, as_of, taken_at, cash, positions_json,"
        " total_value) VALUES (?, ?, ?, ?, ?, ?)",
        [
            pf.id,
            "2026-10-27",
            "2026-10-27T21:00:00",
            100.0,
            json.dumps({"AAPL.US": 10, "OLD.US": 0}),
            1000.0,
        ],
    )
    state.execute(
        "INSERT INTO watchlists (id, owner_id, name, tickers_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ["wl_1", bob.id, "Income", json.dumps(["KO.US", "AAPL.US"]), "2026-10-01", "2026-10-01"],
    )
    return {"alice": alice, "bob": bob}


@pytest.fixture
def events(lake):
    store = CalendarStore(lake)
    store.upsert_earnings(
        [
            EarningsEventRow(
                ticker="AAPL.US",
                period_end=date(2026, 9, 30),
                report_date=date(2026, 10, 29),
                before_after_market="after",
            ),
            EarningsEventRow(
                ticker="FAR.US", period_end=date(2026, 9, 30), report_date=date(2026, 10, 29)
            ),
            EarningsEventRow(
                ticker="KO.US", period_end=date(2026, 9, 30), report_date=date(2026, 11, 20)
            ),
        ],
        source="fake",
    )
    store.upsert_dividends(
        [DividendEventRow(ticker="KO.US", ex_date=date(2026, 10, 29), amount=0.51)], source="fake"
    )
    return store


def test_kinds_are_discovered():
    assert [k.kind for k in alert_kinds()] == [
        "earnings_upcoming",
        "economic_release",
        "ex_dividend_upcoming",
    ]


def test_every_kind_names_a_switch():
    assert {k.kind: k.topic for k in alert_kinds()} == {
        "earnings_upcoming": "earnings",
        "economic_release": "economic",
        "ex_dividend_upcoming": "dividends",
    }
    assert all(k.topic in EVENT_ALERT_TOPICS for k in alert_kinds())


def test_event_hits_for_any_tickers(events):
    [hit] = event_hits("earnings_upcoming", events, ["AAPL.US"], TODAY)
    assert hit.title == "AAPL.US: earnings tomorrow"
    assert hit.body == "AAPL.US reports tomorrow, after the close."
    assert hit.dedupe_key == "event:earnings_upcoming:AAPL.US:2026-10-29"
    assert hit.deep_link == "/calendar?ticker=AAPL.US&date=2026-10-29"
    [div] = event_hits("ex_dividend_upcoming", events, ["KO.US"], TODAY)
    assert div.title == "KO.US: ex-dividend tomorrow"
    assert event_hits("earnings_upcoming", events, ["KO.US"], TODAY) == []
    assert len(event_hits("earnings_upcoming", events, ["KO.US"], TODAY, days_ahead=30)) == 1
    with pytest.raises(KeyError):
        event_hits("nope", events, ["KO.US"], TODAY)


def test_tracked_tickers_are_held_then_watched(state, people):
    assert tracked_tickers(state, people["alice"].id) == ["AAPL.US"]
    assert tracked_tickers(state, people["bob"].id) == ["KO.US", "AAPL.US"]


def test_check_sends_each_event_once(state, lake, people, events):
    router = NotificationRouter(state, channels={})
    report = check_event_alerts(lake, router, TODAY)
    # alice: AAPL earnings; bob: AAPL earnings and KO ex-dividend
    assert (report.people, report.hits, report.sent, report.repeats) == (2, 3, 3, 0)
    assert report.by_kind == {"earnings_upcoming": 2, "ex_dividend_upcoming": 1}
    again = check_event_alerts(lake, router, TODAY)
    assert (again.sent, again.repeats) == (0, 3)
    rows = state.sql(
        "SELECT user_id, category, title, deep_link FROM notification_outbox ORDER BY id"
    )
    assert {r["title"] for r in rows} == {
        "AAPL.US: earnings tomorrow",
        "KO.US: ex-dividend tomorrow",
    }
    assert all(r["category"] == "event_alert" for r in rows)


def test_a_kind_can_be_turned_off(state, lake, people, events):
    router = NotificationRouter(state, channels={})
    report = check_event_alerts(lake, router, TODAY, days_ahead={"ex_dividend_upcoming": 0})
    assert report.by_kind == {"earnings_upcoming": 2}


def test_a_person_can_turn_a_kind_off_for_themselves(state, lake, people, events):
    EventAlertPrefStore(state).set(people["bob"].id, {"earnings": False}, now=datetime.now(UTC))
    router = NotificationRouter(state, channels={})
    report = check_event_alerts(lake, router, TODAY)
    # alice still hears about AAPL earnings; bob only about the KO ex-dividend date
    assert (report.sent, report.muted) == (2, 1)
    rows = state.sql("SELECT user_id, title FROM notification_outbox ORDER BY id")
    assert {(r["user_id"], r["title"]) for r in rows} == {
        (people["alice"].id, "AAPL.US: earnings tomorrow"),
        (people["bob"].id, "KO.US: ex-dividend tomorrow"),
    }
