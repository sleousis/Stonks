"""Economic release alerts (roadmap 20.9): each person hears about the
releases of the countries they follow, at the importance they chose, once
per release, through the router (dedupe, quiet hours)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from stonks.accounts import PortfolioRepository, Scope
from stonks.calendars.alerts import alert_kinds, check_event_alerts, event_hits
from stonks.calendars.store import CalendarStore
from stonks.ingest.calendar_schemas import EconomicEventRow
from stonks.ingest.pipeline import IngestPipeline
from stonks.notify.prefs import (
    EconomicAlertPrefStore,
    EventAlertPrefStore,
    PreferenceStore,
    default_countries,
)
from stonks.notify.router import NotificationRouter
from stonks.notify.worker import DeliveryWorker
from tests.fixtures.calendars import FakeCalendarSource

from .fakes import FakePush, add_device

TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)


def _row(country, hour, event_type, comparison="none", day=14):
    return EconomicEventRow(
        country=country,
        event_time=datetime(2026, 10, day, hour, 30, tzinfo=UTC),
        event_type=event_type,
        comparison=comparison,
    )


RELEASES = (
    _row("US", 12, "CPI", "mom"),
    _row("US", 12, "CPI", "yoy"),  # the same release, another comparison
    _row("US", 12, "Retail Sales", "mom"),  # medium
    _row("US", 14, "Crude Oil Inventories"),  # low
    _row("EU", 9, "ECB Interest Rate Decision"),
    _row("GB", 6, "GDP Growth Rate", "qoq"),
    _row("US", 12, "Non Farm Payrolls", day=20),  # beyond the look-ahead
)


@pytest.fixture
def releases(lake):
    """The fake calendar source, through the real calendar pipeline."""
    source = FakeCalendarSource(earnings=(), dividends=(), economic=RELEASES)
    result = IngestPipeline(source=source, lake=lake).run_calendars(TODAY, date(2026, 10, 31))
    assert result.status == "ok"
    return CalendarStore(lake)


@pytest.fixture
def router(state, clock):
    clock.now = NOW
    return NotificationRouter(state, channels={}, clock=clock)


def _mine(state, user_id):
    return [
        r["title"]
        for r in state.sql(
            "SELECT title FROM notification_outbox WHERE user_id = ? ORDER BY id", [user_id]
        )
    ]


def test_the_kind_is_discovered_and_follows_the_economic_switch():
    kinds = {k.kind: k for k in alert_kinds()}
    assert kinds["economic_release"].topic == "economic"
    assert kinds["economic_release"].default_days_ahead == 1


def test_releases_carry_an_importance(releases):
    got = {(e.event_type, e.importance) for e in releases.economic(TODAY, date(2026, 10, 20))}
    assert ("CPI", "high") in got
    assert ("Retail Sales", "medium") in got
    assert ("Crude Oil Inventories", "low") in got


def test_event_hits_default_to_us_high(releases):
    hits = event_hits("economic_release", releases, [], TODAY)
    assert [h.title for h in hits] == ["US: CPI tomorrow"]
    [hit] = hits
    assert hit.body == "United States CPI is due tomorrow at 12:30 UTC. High importance."
    assert hit.dedupe_key == "event:economic_release:US:2026-10-14:CPI@12:30"
    assert hit.deep_link == "/calendar?country=US&date=2026-10-14"


def test_default_countries_follow_portfolio_currencies(state, users):
    alice = users["alice"]
    assert default_countries(state, alice.id) == ("US",)  # no portfolio: US
    repo = PortfolioRepository(state)
    repo.create(Scope.for_user(alice), name="Euro", base_currency="EUR")
    repo.create(Scope.for_user(alice), name="Pounds", base_currency="GBP")
    repo.create(Scope.for_user(alice), name="Euro 2", base_currency="EUR")
    assert default_countries(state, alice.id) == ("EU", "GB")
    prefs = EconomicAlertPrefStore(state).get(alice.id)
    assert (prefs.countries, prefs.countries_default, prefs.min_importance) == (
        ("EU", "GB"),
        True,
        "high",
    )


def test_store_round_trip_and_validation(state, users):
    store = EconomicAlertPrefStore(state)
    uid = users["bob"].id
    store.set(uid, countries=["us", "de", "US"], now=NOW)
    got = store.get(uid)
    assert (got.countries, got.countries_default, got.min_importance) == (
        ("US", "DE"),
        False,
        "high",
    )
    store.set(uid, min_importance="medium", now=NOW)  # countries stay
    assert store.get(uid).countries == ("US", "DE")
    assert store.get(uid).min_importance == "medium"
    store.set(uid, default_countries=True, now=NOW)  # threshold stays
    got = store.get(uid)
    assert (got.countries, got.countries_default, got.min_importance) == (
        ("US",),
        True,
        "medium",
    )
    for bad in (
        {"countries": []},
        {"countries": ["USA1"]},
        {"countries": ["U"]},
        {"min_importance": "urgent"},
        {"countries": ["US"], "default_countries": True},
    ):
        with pytest.raises(ValueError):
            store.set(uid, now=NOW, **bad)


def test_each_person_gets_their_countries_and_threshold_once(state, lake, users, releases, router):
    alice, bob, admin = users["alice"].id, users["bob"].id, users["admin"].id
    PortfolioRepository(state).create(Scope.for_user(users["alice"]), name="Eu", base_currency="EUR")
    prefs = EconomicAlertPrefStore(state)
    prefs.set(bob, countries=["US", "GB"], min_importance="medium", now=NOW)
    prefs.set(admin, min_importance="low", now=NOW)

    report = check_event_alerts(lake, router, TODAY)
    assert _mine(state, alice) == ["EU: ECB Interest Rate Decision tomorrow"]
    assert _mine(state, bob) == [
        "GB: GDP Growth Rate tomorrow",
        "US: CPI tomorrow",
        "US: Retail Sales tomorrow",
    ]
    assert _mine(state, admin) == [
        "US: CPI tomorrow",
        "US: Retail Sales tomorrow",
        "US: Crude Oil Inventories tomorrow",
    ]
    assert report.by_kind["economic_release"] >= 7
    again = check_event_alerts(lake, router, TODAY)
    assert again.sent == 0 and again.repeats == report.sent
    rows = state.sql("SELECT category, urgency FROM notification_outbox WHERE user_id = ?", [bob])
    assert {(r["category"], r["urgency"]) for r in rows} == {("event_alert", "low")}


def test_the_economic_switch_mutes_them(state, lake, users, releases, router):
    bob = users["bob"].id
    EventAlertPrefStore(state).set(bob, {"economic": False}, now=NOW)
    report = check_event_alerts(lake, router, TODAY)
    assert _mine(state, bob) == []
    assert report.muted >= 1


def test_the_look_ahead_can_be_changed_or_turned_off(state, lake, users, releases, router):
    alice = users["alice"].id
    check_event_alerts(lake, router, TODAY, days_ahead={"economic_release": 0})
    assert _mine(state, alice) == []
    check_event_alerts(lake, router, TODAY, days_ahead={"economic_release": 7})
    assert _mine(state, alice) == ["US: CPI tomorrow", "US: Non Farm Payrolls on 2026-10-20"]


def test_quiet_hours_hold_them_back(state, lake, users, releases, clock):
    alice = users["alice"].id
    push = FakePush()
    add_device(state, alice, "psh_1")
    state.execute("UPDATE users SET timezone = 'America/New_York' WHERE id = ?", [alice])
    PreferenceStore(state).set_quiet_hours(alice, "22:00", "07:00", now=NOW)
    clock.now = NOW  # 02:00 in New York
    router = NotificationRouter(state, {"webpush": push}, clock=clock)
    check_event_alerts(lake, router, TODAY)
    worker = DeliveryWorker(state, {"webpush": push}, clock=clock)
    assert worker.run_once().deferred == 1 and push.sent == []
    clock.now = datetime(2026, 10, 13, 11, 0, tzinfo=UTC)  # 07:00 in New York
    assert worker.run_once().sent == 1
    assert push.sent[0][0].title == "US: CPI tomorrow"
