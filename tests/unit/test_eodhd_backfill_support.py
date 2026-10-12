"""What the backfill needs from the EODHD adapter: request costs and a
meter, quota and rate-limit answers, the account's usage, and company data
asked for part by part from one shared /fundamentals response. Hermetic:
every request goes through a fake session."""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from stonks.ingest.sources import eodhd
from stonks.ingest.sources.eodhd import EodhdDataSource, EodhdQuotaExceededError, request_cost

SECRET = "SuPeRsEcReTkEy123"


class _Response:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self._body if isinstance(self._body, str) else json.dumps(self._body)

    def json(self) -> Any:
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)  # type: ignore[arg-type]


class _Session:
    """Answers by path (the part after ``/api``); records each request. A
    list is a script of answers (the last repeats), so a JSON list body
    goes in a :class:`_Response`."""

    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url, params=None, timeout=None):
        path = url.split("/api", 1)[1]
        self.requests.append((path, dict(params or {})))
        item = self.routes[path]
        if isinstance(item, list):
            item = item.pop(0) if len(item) > 1 else item[0]
        return item if isinstance(item, _Response) else _Response(200, item)


class _Meter:
    def __init__(self) -> None:
        self.seen: list[tuple[str, int]] = []

    def before_request(self, endpoint: str, cost: int) -> None:
        self.seen.append((endpoint, cost))


@pytest.fixture
def sleeps(monkeypatch):
    out: list[float] = []
    monkeypatch.setattr(eodhd.time, "sleep", lambda s: out.append(s))
    return out


def _source(session: _Session, **kwargs: Any) -> EodhdDataSource:
    return EodhdDataSource(api_key=SECRET, session=session, **kwargs)  # type: ignore[arg-type]


# ---- costs and the meter ---------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "cost"),
    [
        ("/eod/AAPL.US", 1),
        ("/div/AAPL.US", 1),
        ("/splits/AAPL.US", 1),
        ("/exchange-symbol-list/US", 1),
        ("/calendar/earnings", 1),
        ("/economic-events", 1),
        ("/intraday/AAPL.US", 5),
        ("/news", 5),
        ("/sentiments", 5),
        ("/fundamentals/AAPL.US", 10),
        ("/fundamentals/GSPC.INDX", 10),
        ("/exchange-details/US", 1),
        ("/ust/bill-rates", 1),
        ("/ust/yield-rates", 1),
        ("/calendar/ipos", 1),
        ("/calendar/splits", 1),
        ("/id-mapping", 1),
        ("/historical-market-cap/AAPL.US", 10),
        ("/insider-transactions", 10),
        ("/macro-indicator/USA", 10),
        ("/eod-bulk-last-day/US", 100),
        ("/user", 0),
    ],
)
def test_request_cost_follows_the_measured_prices(path, cost):
    assert request_cost(path) == cost


def test_meter_hears_every_request_with_its_cost_and_never_the_key(sleeps):
    session = _Session({"/eod/AAPL.US": [_Response(503, "busy"), []]})
    meter = _Meter()
    source = _source(session)
    source.set_request_meter(meter)
    assert list(source.fetch_prices("AAPL.US")) == []
    # the retry is a second request, so it is metered too
    assert meter.seen == [("/eod/AAPL.US", 1), ("/eod/AAPL.US", 1)]
    assert all(SECRET not in endpoint for endpoint, _ in meter.seen)


def test_a_meter_that_raises_stops_the_request_before_it_leaves():
    class _Stop(Exception):
        pass

    class _Refuse:
        def before_request(self, endpoint: str, cost: int) -> None:
            raise _Stop(endpoint)

    session = _Session({"/eod/AAPL.US": []})
    source = _source(session)
    source.set_request_meter(_Refuse())
    with pytest.raises(_Stop):
        source.fetch_prices("AAPL.US")
    assert session.requests == []


# ---- quota and rate limits ---------------------------------------------------------


def test_402_means_the_daily_allowance_is_used_up(sleeps):
    session = _Session({"/eod/AAPL.US": _Response(402, "limit reached")})
    with pytest.raises(EodhdQuotaExceededError):
        _source(session).fetch_prices("AAPL.US")
    assert len(session.requests) == 1
    assert sleeps == []


def test_429_waits_as_long_as_retry_after_says(sleeps):
    session = _Session({"/eod/AAPL.US": [_Response(429, "slow down", {"Retry-After": "7"}), []]})
    assert list(_source(session).fetch_prices("AAPL.US")) == []
    assert sleeps == [7.0]


# ---- usage -------------------------------------------------------------------------


def test_fetch_usage_reads_the_user_endpoint():
    session = _Session(
        {
            "/user": {
                "apiRequests": 345,
                "apiRequestsDate": "2026-10-12",
                "dailyRateLimit": 100000,
                "extraLimit": 489,
            }
        }
    )
    usage = _source(session).fetch_usage()
    assert usage is not None
    assert usage.used_today == 345
    assert usage.daily_limit == 100000
    assert usage.day is not None and usage.day.isoformat() == "2026-10-12"


# ---- company data ---------------------------------------------------------------------

_FUNDAMENTALS = {
    "General": {"Code": "AAPL", "Type": "Common Stock", "Name": "Apple Inc", "CurrencyCode": "USD"},
    "Financials": {
        "Income_Statement": {
            "currency_symbol": "USD",
            "yearly": {"2024-09-30": {"date": "2024-09-30", "totalRevenue": "391035000000"}},
        }
    },
}


def test_profile_statements_and_holdings_share_one_fundamentals_call():
    session = _Session({"/fundamentals/AAPL.US": _FUNDAMENTALS})
    data = _source(session).fetch_company_data(
        "AAPL.US", ["profile", "statements", "fund_holdings"]
    )
    assert [p for p, _ in session.requests] == ["/fundamentals/AAPL.US"]
    assert data.metadata.profile is not None
    assert data.metadata.profile.name == "Apple Inc"
    assert data.statements is not None
    assert len(data.statements.income) == 1
    assert data.fund_holdings == ()


def test_only_the_parts_asked_for_are_fetched():
    session = _Session(
        {
            "/div/AAPL.US": _Response(200, [{"date": "2024-02-09", "value": 0.24}]),
            "/splits/AAPL.US": _Response(
                200, [{"date": "2020-08-31", "split": "4.000000/1.000000"}]
            ),
        }
    )
    data = _source(session).fetch_company_data("AAPL.US", ["dividends", "splits"])
    assert sorted(p for p, _ in session.requests) == ["/div/AAPL.US", "/splits/AAPL.US"]
    assert len(data.metadata.dividends) == 1
    assert len(data.metadata.splits) == 1
    assert data.statements is None
    assert data.metadata.profile is None


def test_news_asks_for_the_configured_page_size():
    session = _Session({"/news": _Response(200, []), "/sentiments": {}})
    _source(session, news_limit=1000).fetch_company_data("AAPL.US", ["news", "sentiment"])
    params = dict(session.requests)["/news"]
    assert params["limit"] == "1000"
    assert params["s"] == "AAPL.US"


def test_a_failed_part_fails_the_whole_call(sleeps):
    session = _Session(
        {
            "/div/AAPL.US": _Response(200, [{"date": "2024-02-09", "value": 0.24}]),
            "/splits/AAPL.US": _Response(500, "x"),
        }
    )
    with pytest.raises(requests.HTTPError):
        _source(session, max_retries=1).fetch_company_data("AAPL.US", ["dividends", "splits"])


def test_metadata_workers_bound_the_metadata_pool():
    source = _source(_Session({}), metadata_workers=2)
    assert source._metadata_executor()._max_workers == 2
    source.close()
