"""EODHD reference endpoints map to vendor-neutral rows: index members and
history, exchange hours and holidays, Treasury rates, IPO and split
calendars, identifiers. Payload shapes were captured live on 2026-10-12.
Hermetic: a fake getter answers each request."""

from __future__ import annotations

from datetime import date, time
from typing import Any

import pytest

from stonks.ingest.sources import eodhd_reference as ref
from stonks.ingest.sources.base import DataSourceError

GSPC = {
    "General": {"Code": "GSPC", "Type": "INDEX", "Name": "S&P 500 Index", "CountryISO": "US"},
    "Components": {
        "0": {"Code": "AIZ", "Exchange": "US", "Name": "Assurant, Inc.", "Weight": 0.0002},
        "1": {"Code": "BRK-B", "Exchange": "US", "Name": "Berkshire", "Weight": 0.016},
    },
    "HistoricalTickerComponents": {
        "0": {
            "Code": "AIZ",
            "StartDate": "2007-04-10",
            "EndDate": None,
            "IsActiveNow": 1,
            "IsDelisted": 0,
        },
        "1": {
            "Code": "AAL",
            "StartDate": "2015-03-23",
            "EndDate": "2024-09-23",
            "IsActiveNow": 0,
            "IsDelisted": 0,
        },
        "2": {"Code": "BAD", "StartDate": None, "EndDate": None},
    },
}


class _Getter:
    def __init__(self, routes: dict[str, Any]):
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, path: str, params: dict[str, str] | None = None) -> Any:
        self.calls.append((path, dict(params or {})))
        item = self.routes[path]
        if isinstance(item, list) and item and isinstance(item[0], dict) and "_page" in item[0]:
            return item.pop(0)["_page"]
        return item


def test_index_components_become_members_and_dated_changes():
    row = ref.parse_index_components(GSPC, "GSPC", as_of=date(2026, 10, 12))
    assert row.index_id == "sp500"  # the canonical code the S&P 500 already has
    assert row.name == "S&P 500 Index"
    # the history's open spans: components can lag the history by days
    assert row.members == ("AIZ.US",)
    assert set(row.changes) == {
        ref.IndexChangeRow(ticker="AIZ.US", change_date=date(2007, 4, 10), action="add"),
        ref.IndexChangeRow(ticker="AAL.US", change_date=date(2015, 3, 23), action="add"),
        ref.IndexChangeRow(ticker="AAL.US", change_date=date(2024, 9, 23), action="remove"),
    }


def test_an_index_with_no_components_is_empty_not_an_error():
    payload = {"General": {"Code": "000906", "CountryISO": "CN"}, "Components": {}}
    row = ref.parse_index_components(payload, "000906", as_of=date(2026, 10, 12))
    assert row.index_id == "000906"
    assert row.members == () and row.changes == ()


def test_without_a_history_the_components_are_the_members():
    payload = {"Components": {"0": {"Code": "VOD", "Exchange": "LSE"}}}
    row = ref.parse_index_components(payload, "FTSE", as_of=date(2026, 10, 12))
    assert row.members == ("VOD.LSE",) and row.changes == ()


def test_past_members_take_the_exchange_most_members_trade_on():
    payload = {
        "General": {"Code": "FTSE", "CountryISO": "GB"},
        "Components": {"0": {"Code": "VOD", "Exchange": "LSE"}},
        "HistoricalTickerComponents": {"0": {"Code": "OLD", "StartDate": "2001-01-02"}},
    }
    row = ref.parse_index_components(payload, "FTSE", as_of=date(2026, 10, 12))
    assert row.changes[0].ticker == "OLD.LSE"


def test_index_codes_map_to_canonical_ids_and_back():
    assert ref.index_id_for("GSPC") == "sp500"
    assert ref.index_id_for("5SP2550") == "5sp2550"
    assert ref.index_id_for("BUK100P.X") == "buk100p_x"
    assert ref.vendor_index_code("sp500") == "GSPC"
    assert ref.vendor_index_code("ftse") == "FTSE"


def test_a_payload_that_is_not_an_index_raises():
    with pytest.raises(DataSourceError):
        ref.parse_index_components(["nope"], "GSPC", as_of=date(2026, 10, 12))


EXCHANGE = {
    "Name": "London Exchange",
    "Code": "LSE",
    "OperatingMIC": "XLON, XLSE",
    "Country": "UK",
    "Currency": "GBP",
    "Timezone": "Europe/London",
    "ExchangeHolidays": {
        "0": {"Holiday": "New Year's Day", "Date": "2000-01-03", "Type": "bank"},
        "1": {"Holiday": "Christmas Day", "Date": "2026-12-25", "Type": "official"},
    },
    "ExchangeEarlyCloseDays": {
        "0": {"Holiday": "Christmas Eve", "Date": "2023-12-22", "EarlyClose": "12:30"}
    },
    "TradingHours": {
        "Open": "08:00:00",
        "Close": "16:30:00",
        "OpenUTC": "07:00:00",
        "CloseUTC": "15:30:00",
        "WorkingDays": "Mon,Tue,Wed,Thu,Fri",
    },
}


def test_exchange_details_keep_hours_zone_and_holidays():
    row = ref.parse_exchange_details(EXCHANGE, "LSE")
    assert row.exchange == "LSE"
    assert row.operating_mics == ("XLON", "XLSE")
    assert row.timezone == "Europe/London"
    assert (row.open_local, row.close_local) == (time(8, 0), time(16, 30))
    assert (row.open_utc, row.close_utc) == (time(7, 0), time(15, 30))
    assert row.working_days == (1, 2, 3, 4, 5)
    by_day = {h.holiday_date: h for h in row.holidays}
    assert by_day[date(2000, 1, 3)].kind == "bank"
    assert by_day[date(2026, 12, 25)].kind == "official"
    assert by_day[date(2023, 12, 22)].early_close == time(12, 30)


def test_exchange_details_ask_for_every_holiday_in_the_window():
    get = _Getter({"/exchange-details/LSE": EXCHANGE})
    ref.fetch_exchange_details(get, "LSE", date(2000, 1, 1), date(2030, 12, 31))
    assert get.calls == [
        ("/exchange-details/LSE", {"from": "2000-01-01", "to": "2030-12-31", "fmt": "json"})
    ]


def test_treasury_rates_are_normalized_per_curve():
    bills = {
        "data": [
            {
                "date": "2024-01-02",
                "tenor": "4WK",
                "discount": 5.28,
                "coupon": 5.39,
                "avg_discount": 5.28,
                "avg_coupon": 5.39,
                "maturity_date": "2024-01-30",
                "cusip": "912797JB4",
            }
        ],
        "links": {"next": None},
    }
    rows = ref.parse_treasury("bill", bills)
    assert {r.measure for r in rows} == {
        "bank_discount",
        "coupon_equivalent",
        "bank_discount_avg",
        "coupon_equivalent_avg",
    }
    assert {r.tenor for r in rows} == {"4W"}
    assert rows[0].maturity_date == date(2024, 1, 30) and rows[0].cusip == "912797JB4"

    long_term = {
        "data": [
            {"date": "2024-01-02", "rate_type": "BC_20year", "rate": 4.25},
            {"date": "2024-01-02", "rate_type": "Over_10_Years", "rate": 4.2},
            {"date": "2024-01-02", "rate_type": "Real_Rate", "rate": 1.98},
        ]
    }
    got = {(r.curve, r.tenor): r.rate for r in ref.parse_treasury("long_term", long_term)}
    assert got == {
        ("long_term", "20Y"): 4.25,
        ("long_term", "10Y+"): 4.2,
        ("long_term_real", "10Y+"): 1.98,
    }

    par = {"data": [{"date": "1990-01-02", "tenor": "3M", "rate": 7.83}]}
    (row,) = ref.parse_treasury("par_nominal", par)
    assert (row.curve, row.tenor, row.measure, row.rate) == ("par_nominal", "3M", "yield", 7.83)


def test_treasury_years_follow_the_next_links():
    first = {"data": [{"date": "2024-01-02", "tenor": "1M", "rate": 5.5}], "links": {"next": "x"}}
    second = {"data": [{"date": "2024-01-03", "tenor": "1M", "rate": 5.4}], "links": {"next": None}}
    get = _Getter({"/ust/yield-rates": [{"_page": first}, {"_page": second}]})
    rows = ref.fetch_treasury(get, "par_nominal", 2024)
    assert [r.observation_date for r in rows] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert get.calls[0][1]["filter[year]"] == "2024"
    assert get.calls[1][1]["page[offset]"] == "1"


def test_ipos_map_status_and_drop_zero_prices():
    payload = {
        "ipos": [
            {
                "code": "2498.HK",
                "name": "RoboSense",
                "exchange": "HKSE",
                "currency": "HKD",
                "start_date": "2024-01-02",
                "filing_date": "2021-08-12",
                "amended_date": "2023-12-27",
                "price_from": 0,
                "price_to": 0,
                "offer_price": 43,
                "shares": 0,
                "deal_type": "Priced",
            },
            {"code": None, "start_date": "2024-01-02"},
        ]
    }
    (row,) = ref.parse_ipos(payload)
    assert row.ticker == "2498.HK"
    assert row.status == "priced"
    assert row.price_low is None and row.shares is None and row.offer_price == 43
    assert row.listing_date == date(2024, 1, 2)


def test_splits_keep_the_ratio_and_optionable_flag():
    payload = {
        "splits": [
            {
                "code": "TBLT.US",
                "split_date": "2024-01-02",
                "optionable": "N",
                "old_shares": 65,
                "new_shares": 1,
            }
        ]
    }
    (row,) = ref.parse_splits(payload)
    assert (row.ticker, row.old_shares, row.new_shares, row.optionable) == (
        "TBLT.US",
        65,
        1,
        False,
    )


def test_id_mapping_pages_carry_identifiers_and_the_total():
    payload = {
        "meta": {"limit": 1000, "offset": 0, "total": 2},
        "data": [
            {
                "symbol": "AAPL.US",
                "isin": "US0378331005",
                "figi": "BBG000B9XRY4",
                "lei": "HWUPKR0MPOU8FGXBT394",
                "cusip": "037833100",
                "cik": "320193",
            },
            {"symbol": "X.US", "isin": None, "figi": None, "lei": None, "cusip": None, "cik": None},
        ],
        "links": {"next": None},
    }
    get = _Getter({"/id-mapping": payload})
    page = ref.fetch_identifiers(get, "US", offset=0, limit=1000)
    assert page.total == 2
    apple = page.rows[0]
    assert apple.cik == "0000320193"  # ten digits, like EDGAR
    assert apple.open_figi == "BBG000B9XRY4"
    assert len(page.rows) == 1  # a row with no identifier says nothing
    assert get.calls[0][1] == {
        "filter[ex]": "US",
        "page[limit]": "1000",
        "page[offset]": "0",
        "fmt": "json",
    }
