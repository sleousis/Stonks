"""EodhdDataSource.list_symbols and fetch_bulk_eod (roadmap 10.5), over a
stub session: no network."""

from __future__ import annotations

from datetime import date

import pytest

from stonks.ingest.sources.base import DataSourceError
from stonks.ingest.sources.eodhd import EodhdDataSource, parse_bulk_eod_response
from tests.unit.test_eodhd_list_tickers import _DelistedAwareSession, _Response


def test_list_symbols_normalises_type_and_marks_delisted():
    active = [
        {
            "Code": "AAPL",
            "Name": "Apple Inc",
            "Exchange": "NASDAQ",
            "Currency": "USD",
            "Country": "USA",
            "Type": "Common Stock",
            "Isin": "US0378331005",
        },
        {"Code": "SPY", "Name": "SPDR S&P 500", "Exchange": "NYSE ARCA", "Type": "ETF"},
    ]
    delisted = [{"Code": "ENRN", "Name": "Enron", "Exchange": "NYSE", "Type": "Common Stock"}]
    source = EodhdDataSource(
        api_key="k",
        session=_DelistedAwareSession(active=active, delisted=delisted),  # type: ignore[arg-type]
    )

    symbols = source.list_symbols("US")

    assert [s.ticker for s in symbols] == ["AAPL.US", "SPY.US", "ENRN.US"]
    aapl, spy, enrn = symbols
    assert aapl.security_type == "common_stock" and aapl.isin == "US0378331005"
    assert aapl.exchange == "NASDAQ" and aapl.currency == "USD" and not aapl.is_delisted
    assert spy.security_type == "etf"
    assert enrn.is_delisted
    # list_tickers stays the ticker view of the same call
    assert source.list_tickers("US") == ["AAPL.US", "SPY.US", "ENRN.US"]


def test_list_symbols_keeps_security_type_off_non_equities():
    active = [{"Code": "BTC-USD", "Name": "Bitcoin", "Type": "Currency"}]
    source = EodhdDataSource(
        api_key="k",
        session=_DelistedAwareSession(active=active, delisted=[]),  # type: ignore[arg-type]
    )
    (btc,) = source.list_symbols("CC")
    assert btc.asset_class == "crypto" and btc.security_type is None


def test_parse_bulk_eod_response():
    payload = [
        {
            "code": "AAPL",
            "exchange_short_name": "US",
            "date": "2025-01-02",
            "open": 1,
            "high": 2,
            "low": 0.5,
            "close": 1.5,
            "adjusted_close": 1.4,
            "volume": 100,
        },
        {"code": "BAD", "date": "not a date"},
    ]
    rows = parse_bulk_eod_response("US", payload)
    assert len(rows) == 1
    (row,) = rows
    assert row.ticker == "AAPL.US" and row.date == date(2025, 1, 2)
    assert row.adj_close == 1.4 and row.volume == 100


class _BulkSession:
    def __init__(self, body):
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return _Response(200, self.body)


def test_fetch_bulk_eod_calls_the_bulk_endpoint_once():
    session = _BulkSession(
        [
            {
                "code": "MSFT",
                "date": "2025-01-02",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "adjusted_close": 1,
                "volume": 1,
            }
        ]
    )
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]
    rows = source.fetch_bulk_eod("US", date(2025, 1, 2))
    assert [r.ticker for r in rows] == ["MSFT.US"]
    ((url, params),) = session.calls
    assert url.endswith("/eod-bulk-last-day/US")
    assert params["date"] == "2025-01-02"


def test_fetch_bulk_eod_on_the_free_tier_is_a_soft_error():
    session = _BulkSession("Only EOD data allowed for free users")
    source = EodhdDataSource(api_key="k", session=session)  # type: ignore[arg-type]
    with pytest.raises(DataSourceError):
        source.fetch_bulk_eod("US", date(2025, 1, 2))
