"""Calendars and news routes (roadmap 20.7): scoped to the caller's
holdings and watchlists, the earnings check for the order ticket, and the
refresh job with its upcoming-event notifications."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from stonks.api import create_app
from stonks.app.context import AppContext
from stonks.app.services import Services
from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow, EconomicEventRow
from stonks.store.lake import DuckDBLake
from stonks.store.state import SqliteState
from tests.fixtures.calendars import FakeCalendarSource
from tests.integration.app.multiuser.conftest import BASE
from tests.integration.app.test_api import REMOTE

TODAY = datetime.now(UTC).date()


def _day(offset: int) -> date:
    return TODAY + timedelta(days=offset)


@pytest.fixture
def calendar_source() -> FakeCalendarSource:
    return FakeCalendarSource(
        earnings=[
            EarningsEventRow(
                ticker="UP.US",
                period_end=_day(-30),
                report_date=_day(1),
                before_after_market="after",
                eps_estimate=1.2,
            ),
            EarningsEventRow(ticker="DOWN.US", period_end=_day(-30), report_date=_day(5)),
            EarningsEventRow(ticker="FAR.US", period_end=_day(-30), report_date=_day(3)),
        ],
        dividends=[DividendEventRow(ticker="FLAT.US", ex_date=_day(1), amount=0.25)],
        economic=[
            EconomicEventRow(
                country="US",
                event_time=datetime.combine(_day(2), datetime.min.time(), tzinfo=UTC),
                event_type="CPI",
                comparison="yoy",
            ),
            EconomicEventRow(
                country="DE",
                event_time=datetime.combine(_day(2), datetime.min.time(), tzinfo=UTC),
                event_type="ZEW",
            ),
        ],
    )


@pytest.fixture
def app(settings, seeded, calendar_source, auth):
    settings.api.allowed_hosts = ["testserver"]
    svc = Services.create(AppContext(settings, source_factory=lambda: calendar_source))
    application = create_app(settings, services=svc)
    application.state.auth = auth
    return application


@pytest.fixture
def client(app):
    with TestClient(app, client=REMOTE, base_url=BASE) as c:
        yield c


@pytest.fixture
def books(settings, people, client):
    """Alice holds UP.US; Bob watches DOWN.US and FLAT.US."""
    alice, bob = people["alice"], people["bob"]
    made = client.post("/api/portfolios", json={"name": "Main"}, headers=alice["headers"])
    assert made.status_code == 201, made.text
    pid = made.json()["id"]
    with SqliteState(settings.state.path) as state:
        state.execute(
            "INSERT INTO portfolio_snapshots (portfolio_id, as_of, taken_at, cash, positions_json,"
            " total_value) VALUES (?, ?, ?, ?, ?, ?)",
            [
                pid,
                TODAY.isoformat(),
                datetime.now(UTC).isoformat(),
                100.0,
                json.dumps({"UP.US": 5}),
                600.0,
            ],
        )
    wl = client.post(
        "/api/watchlists",
        json={"name": "Watch", "tickers": ["DOWN.US", "FLAT.US"]},
        headers=bob["headers"],
    ).json()
    return {"portfolio_id": pid, "watchlist_id": wl["id"]}


def _refresh(client, headers):
    job = client.post("/api/calendars/refresh", json={}, headers=headers)
    assert job.status_code == 202, job.text
    job_id = job.json()["id"]
    services = client.app.state.services
    final = services.runner.wait(job_id, timeout=30)
    assert final.status == "succeeded", final.error
    return client.get(f"/api/calendars/refresh/{job_id}/result", headers=headers).json()


def test_refresh_needs_an_admin_and_sends_event_alerts(client, people, books, calendar_source):
    assert (
        client.post(
            "/api/calendars/refresh", json={}, headers=people["alice"]["headers"]
        ).status_code
        == 403
    )
    result = _refresh(client, people["ada"]["headers"])
    assert result["status"] == "ok"
    assert result["calendars_ok"] == 3
    # alice and the owner of the default book (seeded with UP.US): UP.US
    # earnings tomorrow; bob: FLAT.US ex-dividend tomorrow
    assert result["alerts"]["sent"] == 3
    assert [c[0] for c in calendar_source.calls] == ["earnings", "dividends", "economic"]
    again = _refresh(client, people["ada"]["headers"])
    assert again["alerts"]["sent"] == 0


def test_calendar_scopes(client, people, books):
    _refresh(client, people["ada"]["headers"])
    alice, bob = people["alice"]["headers"], people["bob"]["headers"]
    mine = client.get("/api/calendars", headers=alice).json()
    assert mine["scope"] == "holdings"
    assert mine["tickers"] == ["UP.US"]
    assert [e["ticker"] for e in mine["earnings"]] == ["UP.US"]
    assert mine["earnings"][0]["name"] == "Up Corp"
    assert mine["dividends"] == []
    assert {e["event_type"] for e in mine["economic"]} == {"CPI", "ZEW"}

    watched = client.get("/api/calendars", params={"scope": "watchlists"}, headers=bob).json()
    assert [e["ticker"] for e in watched["earnings"]] == ["DOWN.US"]
    assert [d["ticker"] for d in watched["dividends"]] == ["FLAT.US"]
    one = client.get(
        "/api/calendars",
        params={"scope": "watchlists", "watchlist_id": books["watchlist_id"]},
        headers=bob,
    )
    assert one.status_code == 200
    # another person's list is a 404
    other = client.get(
        "/api/calendars",
        params={"scope": "watchlists", "watchlist_id": books["watchlist_id"]},
        headers=alice,
    )
    assert other.status_code == 404
    everything = client.get(
        "/api/calendars", params={"scope": "all", "countries": "us"}, headers=alice
    ).json()
    assert [e["ticker"] for e in everything["earnings"]] == ["UP.US", "FAR.US", "DOWN.US"]
    assert [e["country"] for e in everything["economic"]] == ["US"]
    picked = client.get(
        "/api/calendars", params={"scope": "tickers", "tickers": "far.us"}, headers=alice
    ).json()
    assert [e["ticker"] for e in picked["earnings"]] == ["FAR.US"]
    assert (
        client.get("/api/calendars", params={"scope": "tickers"}, headers=alice).status_code == 422
    )
    too_long = client.get(
        "/api/calendars",
        params={"start": TODAY.isoformat(), "end": _day(200).isoformat()},
        headers=alice,
    )
    assert too_long.status_code == 422
    assert (
        client.get(
            "/api/calendars", params={"portfolio_id": books["portfolio_id"]}, headers=bob
        ).status_code
        == 404
    )


def test_news_for_holdings_and_tickers(client, people, books, settings):
    with DuckDBLake(settings.lake.path) as lake:
        lake.upsert_news(
            pd.DataFrame(
                [
                    {
                        "ticker": "UP.US",
                        "published_at": datetime.now(UTC).replace(tzinfo=None),
                        "title": "Up Corp beats",
                        "url": "https://example.com/up",
                        "source_name": "Wire",
                        "content": None,
                        "symbols": ["UP.US"],
                        "tags": ["earnings"],
                        "sentiment": 0.8,
                        "sentiment_pos": None,
                        "sentiment_neg": None,
                        "sentiment_neu": None,
                    }
                ]
            )
        )
        lake.upsert_news_sentiment(
            pd.DataFrame([{"ticker": "UP.US", "date": TODAY, "sentiment": 0.4, "article_count": 2}])
        )
    alice = people["alice"]["headers"]
    news = client.get("/api/calendars/news", headers=alice).json()
    assert news["tickers"] == ["UP.US"]
    assert [n["title"] for n in news["items"]] == ["Up Corp beats"]
    assert news["sentiment"][0]["article_count"] == 2
    none = client.get(
        "/api/calendars/news", params={"scope": "tickers", "tickers": "DOWN.US"}, headers=alice
    ).json()
    assert none["items"] == []
    assert (
        client.get("/api/calendars/news", params={"scope": "all"}, headers=alice).status_code == 422
    )


def test_earnings_warnings_and_alert_kinds(client, people, books):
    _refresh(client, people["ada"]["headers"])
    alice = people["alice"]["headers"]
    res = client.get(
        "/api/calendars/earnings-warnings", params={"tickers": "UP.US,DOWN.US"}, headers=alice
    )
    assert res.status_code == 200
    body = res.json()
    assert body["checked"] == ["UP.US", "DOWN.US"]
    # DOWN.US reports in five days: never before the next open
    assert "DOWN.US" not in [w["ticker"] for w in body["warnings"]]
    kinds = client.get("/api/calendars/alert-kinds", headers=alice).json()
    assert [k["kind"] for k in kinds] == [
        "earnings_upcoming",
        "economic_release",
        "ex_dividend_upcoming",
    ]
    assert [k["topic"] for k in kinds] == ["earnings", "economic", "dividends"]
