"""EODHD options adapter (roadmap 17.1): vendor rows map to our columns,
zeros become missing, pagination follows ``meta.total``, and only US
underlyings are served. No network: a recording session answers."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from stonks.ingest.sources.base import (
    DataSource,
    DataSourceError,
    UnsupportedCapabilityError,
)
from stonks.ingest.sources.eodhd import EodhdDataSource
from stonks.ingest.sources.eodhd_options import (
    EOD_PATH,
    parse_eod_page,
    parse_eod_row,
    vendor_symbol,
)


def attrs(**kw: Any) -> dict[str, Any]:
    base = {
        "contract": "AAPL260116C00150000",
        "underlying_symbol": "AAPL",
        "exp_date": "2026-01-16",
        "type": "call",
        "strike": 150,
        "bid": 12.1,
        "ask": 12.4,
        "last": 12.3,
        "volume": 812,
        "open_interest": 23000,
        "volatility": 0.27,
        "delta": 0.61,
        "gamma": 0.012,
        "theta": -0.04,
        "vega": 0.33,
        "rho": 0.21,
        "tradetime": "2025-10-01",
    }
    base.update(kw)
    return base


def page(items: list[dict[str, Any]], offset: int = 0, total: int | None = None) -> dict:
    meta: dict[str, Any] = {"offset": offset, "limit": 1000}
    if total is not None:
        meta["total"] = total
    return {"meta": meta, "data": [{"attributes": a} for a in items], "links": {"next": None}}


def test_row_maps_to_canonical_columns():
    row = parse_eod_row("AAPL.US", attrs())
    assert row is not None
    assert row.contract_id == "AAPL.US:2026-01-16:C:150"
    assert (row.bid, row.ask, row.last, row.volume, row.open_interest) == (
        12.1,
        12.4,
        12.3,
        812.0,
        23000.0,
    )
    assert (row.iv, row.delta, row.gamma, row.theta, row.vega, row.rho) == (
        0.27,
        0.61,
        0.012,
        -0.04,
        0.33,
        0.21,
    )
    assert row.as_of == date(2025, 10, 1)
    assert row.occ_symbol == "AAPL260116C00150000"
    assert row.currency == "USD"


def test_zero_vol_means_no_greeks_and_zero_bid_is_missing():
    row = parse_eod_row(
        "AAPL.US", attrs(volatility=0, delta=0, gamma=0, bid=0, ask=0.01, last=None, volume=None)
    )
    assert row is not None
    assert row.iv is None and row.delta is None and row.rho is None
    assert row.bid is None and row.ask == 0.01 and row.last is None and row.volume is None


def test_missing_fields_fall_back_to_the_occ_symbol():
    row = parse_eod_row("AAPL.US", attrs(type=None, exp_date=None, strike=None))
    assert row is not None and row.contract_id == "AAPL.US:2026-01-16:C:150"


@pytest.mark.parametrize(
    "bad",
    [
        {"type": None, "contract": "garbage"},
        {"tradetime": None},
        {"tradetime": "not a date"},
        {"type": "straddle", "contract": None},
        {"strike": -5, "contract": None},
    ],
)
def test_unreadable_rows_are_dropped(bad):
    assert parse_eod_row("AAPL.US", attrs(**bad)) is None


def test_page_parsing_and_paging_flags():
    rows, n, more = parse_eod_page("AAPL.US", page([attrs(), attrs(tradetime=None)], 0, 5))
    assert len(rows) == 1 and n == 2 and more
    _, _, more = parse_eod_page("AAPL.US", page([attrs()], 4, 5))
    assert not more
    # without meta.total the next link decides
    body = page([attrs()])
    body["links"]["next"] = "https://..."
    assert parse_eod_page("AAPL.US", body)[2]
    assert not parse_eod_page("AAPL.US", page([]))[2]
    with pytest.raises(DataSourceError):
        parse_eod_page("AAPL.US", {"error": "no subscription"})
    rows, _, _ = parse_eod_page("AAPL.US", {"data": ["junk", {"attributes": "x"}]})
    assert rows == []


def test_vendor_symbol():
    assert vendor_symbol("AAPL.US") == "AAPL"
    assert vendor_symbol("brk-b") == "BRK-B"
    with pytest.raises(UnsupportedCapabilityError):
        vendor_symbol("VOD.LSE")


class _Response:
    def __init__(self, body: Any):
        self.status_code = 200
        self._body = body

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self) -> Any:
        return self._body

    def raise_for_status(self) -> None:
        return None


class _Session:
    def __init__(self, pages: list[Any]):
        self._pages = list(pages)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return _Response(self._pages.pop(0))


def test_source_pages_through_the_eod_endpoint():
    first = page([attrs(strike=150), attrs(strike=155)], 0, 3)
    second = page([attrs(strike=160)], 2, 3)
    session = _Session([first, second])
    src = EodhdDataSource("KEY", session=session)  # type: ignore[arg-type]
    rows = list(src.fetch_option_quotes("AAPL.US", date(2025, 10, 1), date(2025, 10, 3)))
    assert [r.strike for r in rows] == [150.0, 155.0, 160.0]
    url, params = session.calls[0]
    assert url.endswith(EOD_PATH)
    assert params["filter[underlying_symbol]"] == "AAPL"
    assert params["filter[tradetime_from]"] == "2025-10-01"
    assert params["filter[tradetime_to]"] == "2025-10-03"
    assert params["page[offset]"] == "0" and params["page[limit]"] == "1000"
    assert session.calls[1][1]["page[offset]"] == "2"


def test_other_sources_do_not_serve_options():
    class Plain(DataSource):
        source_id = "plain"

        def list_tickers(self, exchange):  # pragma: no cover - unused
            return []

        def fetch_prices(self, ticker, since=None, until=None):  # pragma: no cover - unused
            return []

        def fetch_fundamentals(self, ticker):  # pragma: no cover - unused
            raise NotImplementedError

    with pytest.raises(UnsupportedCapabilityError):
        Plain().fetch_option_quotes("AAPL.US")
