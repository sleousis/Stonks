"""The backfill end to end on a fake vendor: phases fill the lake through the
ingest pipeline, progress survives a stop, the budget is never exceeded,
failures retry then give up, and the status adds it up. No network."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
import requests

from stonks.core.interval import Interval
from stonks.ingest.backfill.service import backfill_status, build_runner, make_pipeline_factory
from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.backfill.state import BackfillState
from stonks.ingest.calendar_schemas import DividendEventRow, EarningsEventRow, EconomicEventRow
from stonks.ingest.metadata_bundle import CompanyData, CompanyPart, MetadataBundle
from stonks.ingest.quality_config import DataQualityConfig
from stonks.ingest.schemas import (
    DividendRow,
    ExchangeInfo,
    FinancialStatementsBundle,
    FxRateRow,
    IncomeStatementRow,
    IntradayBar,
    MacroIndicatorRow,
    RawPriceBar,
    StockSplitRow,
    SymbolListing,
    TickerProfile,
)
from stonks.ingest.sources.base import ApiUsage, DataSource, DataSourceError, QuotaExceededError

DAYS = [date(2024, 3, 4) + timedelta(days=i) for i in range(5)]  # Monday to Friday


class _NotFoundResponse:
    status_code = 404
    headers: dict[str, str] = {}


class _RateLimitedResponse:
    status_code = 429
    headers: dict[str, str] = {}


class FakeVendor(DataSource):
    """Canned answers with the EODHD costs, metered like the real source."""

    source_id = "eodhd"

    def __init__(self) -> None:
        self.meter: Any = None
        self.calls = 0
        self.requests: list[str] = []
        self.failing: set[str] = set()
        self.missing: set[str] = set()
        self.quota_on: set[str] = set()
        #: ticker -> how many more requests answer HTTP 429
        self.rate_limited: dict[str, int] = {}
        self.interrupt_on: set[str] = set()
        self.listing_error: BaseException | None = None
        self.vendor_used = 0

    # metering ----------------------------------------------------------------
    def set_request_meter(self, meter: Any) -> None:
        self.meter = meter

    def _request(self, path: str, cost: int) -> None:
        if self.meter is not None:
            self.meter.before_request(path, cost)
        self.calls += cost
        self.requests.append(path)

    def fetch_usage(self) -> ApiUsage | None:
        return ApiUsage(used_today=self.vendor_used, daily_limit=100_000)

    # listings ----------------------------------------------------------------
    def list_tickers(self, exchange: str) -> list[str]:
        return [s.ticker for s in self.list_symbols(exchange)]

    def list_exchanges(self) -> list[ExchangeInfo]:
        self._request("/exchanges-list", 1)
        if self.listing_error is not None:
            raise self.listing_error
        return [ExchangeInfo(code=c) for c in ("US", "CC", "FOREX")]

    def list_symbols(self, exchange: str) -> list[SymbolListing]:
        self._request(f"/exchange-symbol-list/{exchange}", 2)
        if exchange == "US":
            return [
                SymbolListing(ticker="AAA.US", security_type="common_stock", currency="USD"),
                SymbolListing(ticker="BBB.US", security_type="etf", currency="USD"),
                SymbolListing(ticker="OLD.US", security_type="common_stock", is_delisted=True),
            ]
        if exchange == "CC":
            return [SymbolListing(ticker="BTC-USD.CC", asset_class="crypto")]
        if exchange == "FOREX":
            return [SymbolListing(ticker="EURUSD.FOREX"), SymbolListing(ticker="AED.FOREX")]
        return []

    # bars --------------------------------------------------------------------
    def _check(self, key: str) -> None:
        if key in self.interrupt_on:
            raise KeyboardInterrupt
        if self.rate_limited.get(key, 0) > 0:
            self.rate_limited[key] -= 1
            raise requests.HTTPError("429", response=_RateLimitedResponse())  # type: ignore[arg-type]
        if key in self.quota_on:
            raise QuotaExceededError("limit reached")
        if key in self.failing:
            raise DataSourceError(f"boom on {key}")
        if key in self.missing:
            raise requests.HTTPError("404", response=_NotFoundResponse())  # type: ignore[arg-type]

    def fetch_prices(self, ticker: str, since=None, until=None) -> Iterable[RawPriceBar]:
        self._request(f"/eod/{ticker}", 1)
        self._check(ticker)
        return [_bar(ticker, d) for d in DAYS]

    def fetch_bulk_eod(self, exchange: str, day: date) -> list[RawPriceBar]:
        self._request(f"/eod-bulk-last-day/{exchange}", 100)
        return [_bar(t, day) for t in ("AAA.US", "BBB.US", "NEW.US")]

    def fetch_intraday_bars(self, ticker, interval, since=None, until=None):
        self._request(f"/intraday/{ticker}", 5)
        assert since is not None and until is not None
        out = []
        day = since
        while day <= until:
            if day.weekday() < 5:
                out.append(
                    IntradayBar(
                        ticker=ticker,
                        timestamp=datetime(day.year, day.month, day.day, 15, tzinfo=UTC),
                        open=10.0,
                        high=11.0,
                        low=9.0,
                        close=10.5,
                        adj_close=10.5,
                        volume=100,
                    )
                )
            day += timedelta(days=1)
        return out

    # company data ------------------------------------------------------------
    def fetch_fundamentals(self, ticker: str) -> FinancialStatementsBundle:
        raise AssertionError("the backfill asks for company data, not fundamentals")

    def fetch_company_data(self, ticker: str, parts: Collection[CompanyPart]) -> CompanyData:
        wanted = set(parts)
        costs = {"dividends": 1, "splits": 1, "market_cap": 10, "insiders": 10}
        costs.update({"news": 5, "sentiment": 5})
        if wanted & {"profile", "statements", "fund_holdings"}:
            self._request(f"/fundamentals/{ticker}", 10)
        for part in sorted(wanted & set(costs)):
            self._request(f"/{part}/{ticker}", costs[part])
        self._check(ticker)
        meta: dict[str, Any] = {}
        statements = None
        if "profile" in wanted:
            meta["profile"] = TickerProfile(id=ticker, asset_class="equity", name=f"{ticker} Inc")
        if "statements" in wanted:
            statements = FinancialStatementsBundle(
                income=(
                    IncomeStatementRow(
                        ticker=ticker,
                        period_end=date(2023, 12, 31),
                        frequency="A",
                        total_revenue=1e9,
                    ),
                )
            )
        if "dividends" in wanted:
            meta["dividends"] = (DividendRow(ticker=ticker, ex_date=DAYS[2], amount=0.1),)
        if "splits" in wanted:
            meta["splits"] = (StockSplitRow(ticker=ticker, date=DAYS[1], ratio=2.0),)
        return CompanyData(metadata=MetadataBundle(**meta), statements=statements)

    # market wide ---------------------------------------------------------------
    def list_macro_indicators(self) -> Sequence[str]:
        return ("gdp_current_usd",)

    def fetch_macro_indicator(self, country_iso: str, indicator: str):
        self._request(f"/macro-indicator/{country_iso}", 10)
        return [
            MacroIndicatorRow(
                country_iso=country_iso,
                indicator=indicator,
                observation_date=date(2023, 12, 31),
                period="annual",
                value=1.0,
            )
        ]

    def fetch_fx_rates(self, base: str, quote: str, since=None, until=None):
        self._request(f"/eod/{base}{quote}.FOREX", 1)
        return [
            FxRateRow(
                base_currency=base,
                quote_currency=quote,
                observation_date=d,
                rate=1.1,
                source="eodhd",
            )
            for d in DAYS
        ]

    def fetch_earnings_calendar(self, start, end, tickers=None):
        self._request("/calendar/earnings", 1)
        return [EarningsEventRow(ticker="AAA.US", period_end=start, report_date=start)]

    def fetch_dividend_calendar(self, start, end, tickers=None):
        self._request("/calendar/dividends", 1)
        return [DividendEventRow(ticker="AAA.US", ex_date=start)]

    def fetch_economic_events(self, start, end, countries=None):
        self._request("/economic-events", 1)
        return [
            EconomicEventRow(
                country="US",
                event_time=datetime(start.year, start.month, start.day, 12, tzinfo=UTC),
                event_type="CPI",
            )
        ]


def _bar(ticker: str, day: date) -> RawPriceBar:
    return RawPriceBar(
        ticker=ticker,
        date=day,
        open=10.0,
        high=11.0,
        low=9.0,
        close=10.5,
        adj_close=10.5,
        volume=1000,
    )


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)

    slept: list[float]


@pytest.fixture
def clock() -> Clock:
    c = Clock(datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    c.slept = []
    return c


@pytest.fixture
def vendor() -> FakeVendor:
    return FakeVendor()


def _run(lake, state, vendor, clock, settings: BackfillSettings | None = None, **kwargs):
    settings = settings or BackfillSettings(requests_per_minute=1000)
    bstate = BackfillState(state, clock=clock)
    runner, budget = build_runner(
        source=vendor,
        lake=lake,
        state=bstate,
        settings=settings,
        make_pipeline=make_pipeline_factory(lake, DataQualityConfig(), 5e-4, clock=clock),
        clock=clock,
        sleep=clock.sleep,
        **kwargs,
    )
    return runner.run(), bstate, budget


def _count(lake, sql: str, params: list[Any] | None = None) -> int:
    return int(lake.con.execute(sql, params or []).fetchone()[0])


def test_symbols_and_daily_fill_instruments_and_bars(lake, state, vendor, clock):
    report, bstate, _ = _run(
        lake, state, vendor, clock, phases=["symbols", "daily"], until_budget=True
    )
    assert report.stopped == "finished"
    rows = dict(lake.con.execute("SELECT id, asset_class FROM instruments").fetchall())
    # forex pairs are not instruments
    assert rows == {
        "AAA.US": "equity",
        "BBB.US": "equity",
        "OLD.US": "equity",
        "BTC-USD.CC": "crypto",
    }
    assert _count(lake, "SELECT COUNT(*) FROM instruments WHERE is_delisted") == 1
    assert _count(lake, "SELECT COUNT(*) FROM bars WHERE interval = '1d'") == 4 * len(DAYS)
    summary = bstate.summary()
    assert summary["symbols"].done == 2
    assert summary["daily"].done == 4
    assert summary["daily"].calls == 4
    # stocks run first (active, then delisted), then ETFs, then the rest
    order = [
        r["unit_key"]
        for r in state.sql(
            "SELECT unit_key FROM backfill_units WHERE phase = 'daily' ORDER BY priority, unit_key"
        )
    ]
    assert order == ["AAA.US", "OLD.US", "BBB.US", "BTC-USD.CC"]


def test_a_stop_at_the_budget_resumes_the_next_day(lake, state, vendor, clock):
    settings = BackfillSettings(requests_per_minute=1000, daily_budget=7)
    report, bstate, _ = _run(
        lake, state, vendor, clock, settings, phases=["symbols", "daily"], until_budget=True
    )
    assert report.stopped == "budget"
    assert vendor.calls <= 7
    assert bstate.calls_on(clock.now.date()).calls == vendor.calls
    left = bstate.summary()["daily"].remaining
    assert left > 0
    clock.now += timedelta(days=1)
    report, bstate, _ = _run(
        lake, state, vendor, clock, settings, phases=["symbols", "daily"], until_budget=True
    )
    assert report.stopped == "finished"
    assert bstate.summary()["daily"].done == 4
    # nothing was fetched twice
    eod = [r for r in vendor.requests if r.startswith("/eod/")]
    assert len(eod) == len(set(eod)) == 4


def test_without_until_budget_it_sleeps_to_the_reset_and_goes_on(lake, state, vendor, clock):
    settings = BackfillSettings(requests_per_minute=1000, daily_budget=7, reset_margin_minutes=5)
    report, bstate, _ = _run(lake, state, vendor, clock, settings, phases=["symbols", "daily"])
    assert report.stopped == "finished"
    assert clock.slept  # it waited for midnight UTC
    assert clock.now >= datetime(2026, 10, 13, 0, 5, tzinfo=UTC)
    day1 = bstate.calls_on(date(2026, 10, 12)).calls
    assert 0 < day1 <= 7
    assert bstate.summary()["daily"].done == 4


def test_the_run_cap_stops_for_good(lake, state, vendor, clock):
    report, _, budget = _run(lake, state, vendor, clock, phases=["symbols", "daily"], max_calls=6)
    assert report.stopped == "cap"
    assert budget.run_spent <= 6


def test_soft_failures_retry_with_backoff_then_give_up(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, phases=["symbols"], until_budget=True)
    vendor.failing.add("BBB.US")
    settings = BackfillSettings(requests_per_minute=1000, max_attempts=3, retry_backoff_minutes=1)
    report, bstate, _ = _run(lake, state, vendor, clock, settings, phases=["daily"])
    assert report.stopped == "finished"
    summary = bstate.summary()["daily"]
    assert (summary.done, summary.failed) == (3, 1)
    assert vendor.requests.count("/eod/BBB.US") == 3
    assert clock.slept  # it waited for the retries
    vendor.failing.clear()
    assert bstate.retry_failed(["daily"]) == 1
    report, bstate, _ = _run(lake, state, vendor, clock, settings, phases=["daily"])
    assert bstate.summary()["daily"].done == 4


def test_not_found_is_done_and_never_asked_again(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, phases=["symbols"], until_budget=True)
    vendor.missing.add("OLD.US")
    _, bstate, _ = _run(lake, state, vendor, clock, phases=["daily"], until_budget=True)
    assert bstate.summary()["daily"].done == 4
    _run(lake, state, vendor, clock, phases=["daily"], until_budget=True)
    assert vendor.requests.count("/eod/OLD.US") == 1


def test_the_vendor_quota_stops_the_run_and_keeps_the_unit(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, phases=["symbols"], until_budget=True)
    vendor.quota_on.add("BBB.US")
    report, bstate, _ = _run(lake, state, vendor, clock, phases=["daily"], until_budget=True)
    assert report.stopped == "budget"
    summary = bstate.summary()["daily"]
    assert summary.failed == 0
    assert "BBB.US" in {u.key for u in bstate.next_units("daily", 10)}


def test_company_phases_store_statements_profiles_and_actions(lake, state, vendor, clock):
    settings = BackfillSettings(requests_per_minute=1000, insider_exchanges=["US"])
    report, bstate, _ = _run(
        lake,
        state,
        vendor,
        clock,
        settings,
        phases=["symbols", "actions", "fundamentals"],
        until_budget=True,
    )
    assert report.stopped == "finished"
    assert _count(lake, "SELECT COUNT(*) FROM income_statement") == 2  # the two common stocks
    assert _count(lake, "SELECT COUNT(*) FROM income_statement_versions") == 2
    assert _count(lake, "SELECT COUNT(*) FROM dividends") == 3  # equities on US
    assert _count(lake, "SELECT COUNT(*) FROM stock_splits") == 3
    name = lake.con.execute("SELECT name FROM instruments WHERE id = 'AAA.US'").fetchone()[0]
    assert name == "AAA.US Inc"
    # one fundamentals call per ticker, market cap for active stocks, insiders on US
    assert vendor.requests.count("/fundamentals/AAA.US") == 1
    assert "/market_cap/AAA.US" in vendor.requests
    assert "/market_cap/OLD.US" not in vendor.requests
    assert "/insiders/AAA.US" in vendor.requests
    assert "/insiders/BBB.US" not in vendor.requests  # an ETF gets its holdings instead
    assert bstate.summary()["fundamentals"].done == 3


def test_market_wide_phases(lake, state, vendor, clock):
    settings = BackfillSettings(
        requests_per_minute=1000,
        calendar_start=date(2026, 8, 15),
        macro_countries=["USA", "DEU"],
    )
    report, bstate, _ = _run(
        lake, state, vendor, clock, settings, phases=["calendars", "macro", "fx"], until_budget=True
    )
    assert report.stopped == "finished"
    summary = bstate.summary()
    assert summary["calendars"].done == 3
    # August (from the 15th), September, and October to today: three steps each
    assert vendor.requests.count("/calendar/earnings") == 3
    assert _count(lake, "SELECT COUNT(*) FROM earnings_calendar") >= 1
    assert summary["macro"].done == 2
    assert _count(lake, "SELECT COUNT(*) FROM macro_indicators") == 2
    # the three-letter forex code is not a pair
    assert summary["fx"].done == 1
    assert _count(lake, "SELECT COUNT(*) FROM fx_rates") == len(DAYS)


def test_intraday_walks_windows_and_resumes_at_the_cursor(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, phases=["symbols"], until_budget=True)
    settings = BackfillSettings(
        requests_per_minute=1000,
        intraday_start={"1h": date(2026, 9, 28), "5m": date(2026, 9, 28), "1m": date(2026, 9, 28)},
        intraday_window_days={"1h": 7, "5m": 7, "1m": 7},
    )
    # AAA.US and BBB.US are active and listed on US; 15 days take 3 windows each
    report, bstate, _ = _run(
        lake, state, vendor, clock, settings, phases=["intraday_1h"], max_calls=10
    )
    assert report.stopped == "cap"
    cursors = {u.key: u.cursor for u in bstate.next_units("intraday_1h", 10)}
    assert cursors == {"AAA.US": "2026-10-05", "BBB.US": "2026-10-05"}
    report, bstate, _ = _run(
        lake, state, vendor, clock, settings, phases=["intraday_1h"], until_budget=True
    )
    assert report.stopped == "finished"
    assert bstate.summary()["intraday_1h"].done == 2
    stored = _count(lake, "SELECT COUNT(*) FROM bars WHERE interval = ?", [Interval.HOUR_1.code])
    assert stored == 2 * 11  # weekdays from 28 Sep to 12 Oct
    assert vendor.requests.count("/intraday/AAA.US") == 3


def test_topup_stores_recent_days_for_tickers_done_before_them(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, phases=["symbols", "daily"], until_budget=True)
    clock.now = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)  # Thursday
    report, bstate, _ = _run(lake, state, vendor, clock, phases=["daily_topup"], until_budget=True)
    assert report.stopped == "finished"
    # Monday the 12th to Wednesday the 14th, one bulk call each for US and CC
    assert vendor.requests.count("/eod-bulk-last-day/US") == 3
    assert vendor.requests.count("/eod-bulk-last-day/CC") == 3
    assert bstate.summary()["daily_topup"].done == 6
    days = lake.con.execute(
        "SELECT COUNT(*) FROM bars WHERE ticker = 'AAA.US' AND timestamp >= '2026-10-12'"
    ).fetchone()[0]
    assert days == 3
    # a ticker the daily phase never did is left to it
    assert _count(lake, "SELECT COUNT(*) FROM bars WHERE ticker = 'NEW.US'") == 0


def test_status_counts_and_eta(lake, state, vendor, clock):
    settings = BackfillSettings(requests_per_minute=1000, daily_budget=7)
    _run(lake, state, vendor, clock, settings, phases=["symbols", "daily"], until_budget=True)
    bstate = BackfillState(state, clock=clock)
    status = backfill_status(bstate, settings, phases=["symbols", "daily", "news"], today=clock.now)
    by_id = {p.phase: p for p in status.phases}
    assert by_id["symbols"].done == 2
    assert not by_id["news"].listed
    daily = by_id["daily"]
    assert daily.done + daily.remaining == 4
    assert daily.calls_left == daily.remaining  # one call each
    assert status.today.calls == vendor.calls
    assert status.eta_days == pytest.approx(status.calls_left / 7)


# ---- review fixes ------------------------------------------------------------------


def _unit_row(state, phase: str, key: str) -> dict[str, Any]:
    rows = state.sql("SELECT * FROM backfill_units WHERE phase = ? AND unit_key = ?", [phase, key])
    return dict(rows[0])


def test_a_rate_limit_pauses_without_counting_an_attempt(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, phases=["symbols"], until_budget=True)
    vendor.rate_limited["BBB.US"] = 2
    report, bstate, _ = _run(lake, state, vendor, clock, phases=["daily"], until_budget=True)
    assert report.stopped == "finished"
    assert (report.units_retry, report.units_failed) == (0, 0)
    assert bstate.summary()["daily"].done == 4
    row = _unit_row(state, "daily", "BBB.US")
    assert row["attempts"] == 0
    assert row["last_error"] is None
    # it paused a minute, then twice that, not the half hour of a retry
    assert [s for s in clock.slept if s >= 1] == [60.0, 120.0]


def test_a_listing_that_fails_skips_the_phase_and_goes_on(lake, state, vendor, clock):
    vendor.listing_error = DataSourceError("exchanges list is down")
    settings = BackfillSettings(requests_per_minute=1000, macro_countries=["USA"])
    report, bstate, _ = _run(
        lake, state, vendor, clock, settings, phases=["symbols", "macro"], until_budget=True
    )
    assert report.stopped == "finished"
    assert "symbols" in report.detail
    summary = bstate.summary()
    assert summary["macro"].done == 1
    assert bstate.listed_at("symbols") is None  # listed again on the next run
    assert bstate.calls_on(clock.now.date()).calls == vendor.calls


def test_the_quota_while_listing_stops_for_the_day(lake, state, vendor, clock):
    vendor.listing_error = QuotaExceededError("limit reached")
    report, _, _ = _run(lake, state, vendor, clock, phases=["symbols"], until_budget=True)
    assert report.stopped == "budget"


def test_a_write_that_crashes_fails_only_its_unit(lake, state, vendor, clock, monkeypatch):
    _run(lake, state, vendor, clock, phases=["symbols"], until_budget=True)
    upsert = lake.upsert_prices

    def poisoned(frame, *args, **kwargs):
        if "BBB.US" in set(frame["ticker"]):
            raise RuntimeError("the lake refused BBB.US")
        return upsert(frame, *args, **kwargs)

    monkeypatch.setattr(lake, "upsert_prices", poisoned)
    settings = BackfillSettings(requests_per_minute=1000, max_attempts=2, retry_backoff_minutes=1)
    report, bstate, _ = _run(lake, state, vendor, clock, settings, phases=["daily"])
    assert report.stopped == "finished"
    summary = bstate.summary()["daily"]
    assert (summary.done, summary.failed) == (3, 1)
    assert "the lake refused BBB.US" in _unit_row(state, "daily", "BBB.US")["last_error"]


def test_an_interrupt_still_stores_the_calls_spent(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, phases=["symbols"], until_budget=True)
    vendor.interrupt_on.add("BBB.US")
    bstate = BackfillState(state, clock=clock)
    with pytest.raises(KeyboardInterrupt):
        _run(lake, state, vendor, clock, phases=["daily"], until_budget=True)
    assert bstate.calls_on(clock.now.date()).calls == vendor.calls
