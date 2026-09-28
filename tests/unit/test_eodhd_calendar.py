"""EODHD calendar adapter (roadmap 20.7): parsers map vendor JSON into the
vendor-neutral calendar rows, and the client sends the documented URLs and
parameters, paging where the endpoint pages."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from stonks.ingest.sources.base import DataSource, UnsupportedCapabilityError
from stonks.ingest.sources.eodhd import EodhdDataSource, EodhdFreeTierError
from stonks.ingest.sources.eodhd_calendar import (
    parse_dividend_calendar,
    parse_earnings_calendar,
    parse_economic_events,
)

EARNINGS = {
    "type": "Earnings",
    "earnings": [
        {
            "code": "AAPL.US",
            "report_date": "2025-01-30",
            "date": "2024-12-31",
            "before_after_market": "AfterMarket",
            "currency": "USD",
            "actual": 2.4,
            "estimate": 2.34,
            "difference": 0.06,
            "percent": 2.5641,
        },
        {
            "code": "MSFT.US",
            "report_date": "2025-01-29",
            "date": "2024-12-31",
            "before_after_market": "BeforeMarket",
            "currency": "USD",
            "actual": None,
            "estimate": "3.11",
            "difference": None,
            "percent": None,
        },
        {"code": "BAD.US", "report_date": "not a date", "date": "2024-12-31"},
        {"code": "", "report_date": "2025-01-29", "date": "2024-12-31"},
        "junk",
    ],
}


def test_parse_earnings_calendar_maps_fields_and_drops_bad_rows():
    rows = list(parse_earnings_calendar(EARNINGS))
    assert [r.ticker for r in rows] == ["AAPL.US", "MSFT.US"]
    aapl, msft = rows
    assert aapl.report_date == date(2025, 1, 30)
    assert aapl.period_end == date(2024, 12, 31)
    assert aapl.before_after_market == "after"
    assert aapl.eps_actual == 2.4
    assert aapl.eps_estimate == 2.34
    assert aapl.eps_difference == 0.06
    assert aapl.surprise_percent == 2.5641
    assert msft.before_after_market == "before"
    assert msft.eps_estimate == 3.11
    assert msft.eps_actual is None


def test_parse_earnings_calendar_unknown_timing_is_none():
    payload = {
        "earnings": [
            {
                "code": "X.US",
                "report_date": "2025-01-30",
                "date": "2024-12-31",
                "before_after_market": "Whenever",
            }
        ]
    }
    [row] = parse_earnings_calendar(payload)
    assert row.before_after_market is None


def test_parse_earnings_calendar_without_period_uses_report_date():
    payload = {"earnings": [{"code": "X.US", "report_date": "2025-01-30"}]}
    [row] = parse_earnings_calendar(payload)
    assert row.period_end == date(2025, 1, 30)


def test_parse_earnings_calendar_free_tier_text_raises():
    with pytest.raises(EodhdFreeTierError):
        list(parse_earnings_calendar("This API endpoint is paid only"))


def test_parse_earnings_calendar_non_dict_is_empty():
    assert list(parse_earnings_calendar([])) == []
    assert list(parse_earnings_calendar({"earnings": "nope"})) == []


def test_parse_dividend_calendar_reads_dates_and_optional_amounts():
    payload = {
        "meta": {"total": 3},
        "data": [
            {"date": "2025-11-10", "symbol": "AAPL.US"},
            {
                "date": "2025-11-12",
                "symbol": "KO.US",
                "value": 0.51,
                "currency": "USD",
                "paymentDate": "2025-12-01",
                "recordDate": "2025-11-13",
                "declarationDate": "2025-10-20",
            },
            {"date": "bad", "symbol": "X.US"},
            {"date": "2025-11-12"},
        ],
        "links": {"next": None},
    }
    rows = list(parse_dividend_calendar(payload))
    assert [(r.ticker, r.ex_date) for r in rows] == [
        ("AAPL.US", date(2025, 11, 10)),
        ("KO.US", date(2025, 11, 12)),
    ]
    assert rows[0].amount is None
    assert rows[1].amount == 0.51
    assert rows[1].pay_date == date(2025, 12, 1)
    assert rows[1].record_date == date(2025, 11, 13)
    assert rows[1].declaration_date == date(2025, 10, 20)


def test_parse_dividend_calendar_drops_negative_amount():
    payload = {"data": [{"date": "2025-11-10", "symbol": "A.US", "value": -1}]}
    [row] = parse_dividend_calendar(payload)
    assert row.amount is None


def test_parse_economic_events_normalizes():
    payload = [
        {
            "type": "Capital Expenditure",
            "comparison": "yoy",
            "period": "Q4",
            "country": "jp",
            "date": "2025-03-03 23:50:00",
            "actual": -0.2,
            "previous": 8.1,
            "estimate": 4.9,
            "change": -8.3,
            "change_percentage": -102.469,
        },
        {
            "type": "Fed Interest Rate Decision",
            "comparison": None,
            "period": None,
            "country": "US",
            "date": "2025-03-19 18:00:00",
            "actual": None,
        },
        {"type": "Odd", "comparison": "wow", "country": "US", "date": "2025-03-19 18:00:00"},
        {"type": "", "country": "US", "date": "2025-03-19 18:00:00"},
        {"type": "No date", "country": "US"},
        {"type": "No country", "date": "2025-03-19 18:00:00"},
    ]
    rows = list(parse_economic_events(payload))
    assert [r.event_type for r in rows] == [
        "Capital Expenditure",
        "Fed Interest Rate Decision",
        "Odd",
    ]
    capex = rows[0]
    assert capex.country == "JP"
    assert capex.comparison == "yoy"
    assert capex.event_time == datetime(2025, 3, 3, 23, 50, tzinfo=UTC)
    assert capex.change_pct == -102.469
    assert rows[1].comparison == "none"
    assert rows[2].comparison == "none"


# ---- the DataSource seam -------------------------------------------------------


class _Bare(DataSource):
    source_id = "bare"

    def list_tickers(self, exchange):
        return []

    def fetch_prices(self, ticker, since=None, until=None):
        return []

    def fetch_fundamentals(self, ticker):
        raise NotImplementedError


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.fetch_earnings_calendar(date(2025, 1, 1), date(2025, 1, 7)),
        lambda s: s.fetch_dividend_calendar(date(2025, 1, 1), date(2025, 1, 7)),
        lambda s: s.fetch_economic_events(date(2025, 1, 1), date(2025, 1, 7)),
    ],
)
def test_sources_without_calendars_raise_unsupported(call):
    with pytest.raises(UnsupportedCapabilityError):
        call(_Bare())


# ---- HTTP client -----------------------------------------------------------------


class _Resp:
    def __init__(self, body: Any) -> None:
        self.status_code = 200
        self._body = body
        self.text = ""

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, bodies: list[Any]) -> None:
        self.bodies = list(bodies)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str], timeout: int) -> _Resp:
        self.calls.append((url, dict(params)))
        return _Resp(self.bodies.pop(0))


def _src(session: _Session) -> EodhdDataSource:
    return EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]


def test_fetch_earnings_calendar_url_and_symbol_batches():
    tickers = [f"T{i}.US" for i in range(60)]
    session = _Session([EARNINGS, {"earnings": []}])
    rows = list(_src(session).fetch_earnings_calendar(date(2025, 1, 1), date(2025, 1, 31), tickers))
    assert len(rows) == 2
    assert len(session.calls) == 2  # 50 symbols per call
    url, params = session.calls[0]
    assert url.endswith("/calendar/earnings")
    assert params["from"] == "2025-01-01"
    assert params["to"] == "2025-01-31"
    assert params["fmt"] == "json"
    assert len(params["symbols"].split(",")) == 50
    assert len(session.calls[1][1]["symbols"].split(",")) == 10


def test_fetch_earnings_calendar_whole_market_has_no_symbols():
    session = _Session([{"earnings": []}])
    list(_src(session).fetch_earnings_calendar(date(2025, 1, 1), date(2025, 1, 7)))
    assert "symbols" not in session.calls[0][1]


def test_fetch_dividend_calendar_pages_until_short_page():
    first = {"data": [{"date": "2025-11-10", "symbol": f"S{i}.US"} for i in range(1000)]}
    second = {"data": [{"date": "2025-11-11", "symbol": "LAST.US"}], "links": {"next": None}}
    session = _Session([first, second])
    rows = list(_src(session).fetch_dividend_calendar(date(2025, 11, 1), date(2025, 11, 30)))
    assert len(rows) == 1001
    url, params = session.calls[0]
    assert url.endswith("/calendar/dividends")
    assert params["filter[date_from]"] == "2025-11-01"
    assert params["filter[date_to]"] == "2025-11-30"
    assert params["page[limit]"] == "1000"
    assert params["page[offset]"] == "0"
    assert session.calls[1][1]["page[offset]"] == "1000"


def test_fetch_dividend_calendar_per_ticker():
    session = _Session([{"data": []}, {"data": []}])
    list(
        _src(session).fetch_dividend_calendar(
            date(2025, 11, 1), date(2025, 11, 30), ["A.US", "B.US"]
        )
    )
    assert [c[1]["filter[symbol]"] for c in session.calls] == ["A.US", "B.US"]


def test_fetch_economic_events_pages_and_filters_country():
    first = [{"type": "CPI", "country": "US", "date": "2025-03-12 12:30:00"} for _ in range(1000)]
    session = _Session([first, []])
    rows = list(
        _src(session).fetch_economic_events(date(2025, 3, 1), date(2025, 3, 5), countries=["us"])
    )
    assert len(rows) == 1000
    url, params = session.calls[0]
    assert url.endswith("/economic-events")
    assert params["country"] == "US"
    assert params["from"] == "2025-03-01"
    assert params["to"] == "2025-03-05"
    assert params["limit"] == "1000"
    assert params["offset"] == "0"
    assert session.calls[1][1]["offset"] == "1000"


def test_fetch_economic_events_splits_long_windows_by_week():
    """The endpoint pages only up to offset 1000, so a long window is asked
    for a week at a time."""
    session = _Session([[], [], []])
    list(_src(session).fetch_economic_events(date(2025, 3, 1), date(2025, 3, 20)))
    assert [(c[1]["from"], c[1]["to"]) for c in session.calls] == [
        ("2025-03-01", "2025-03-07"),
        ("2025-03-08", "2025-03-14"),
        ("2025-03-15", "2025-03-20"),
    ]
    assert "country" not in session.calls[0][1]
