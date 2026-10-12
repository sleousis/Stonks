"""The reference phases end to end on a fake vendor (index members and
history, exchange details, Treasury rates, IPO and split calendars,
identifiers) and the free-source phases (EDGAR, borrow, TVL), which spend
no budget and keep running while the day's budget waits for its reset.
No network."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from stonks.ingest.backfill.settings import BackfillSettings
from stonks.ingest.filing_schemas import CorporateFilingRow
from stonks.ingest.reference_schemas import (
    ExchangeDetailsRow,
    ExchangeHolidayRow,
    IdentifierPage,
    IndexChangeRow,
    IndexComponentsRow,
    InstrumentIdentifiersRow,
    IpoEventRow,
    SplitEventRow,
    TreasuryRateRow,
)
from stonks.ingest.schemas import BorrowRateRow, DefiTvlRow, SymbolListing
from stonks.ingest.sources.base import DataSource, DataSourceError, UnknownSymbolError
from stonks.universes.store import UniverseStore
from tests.integration.test_backfill_runner import Clock, FakeVendor, _count, _run


class RefVendor(FakeVendor):
    """The runner tests' vendor plus the reference endpoints, with the
    costs measured on 2026-10-12."""

    def list_symbols(self, exchange: str) -> list[SymbolListing]:
        if exchange == "INDX":
            self._request("/exchange-symbol-list/INDX", 2)
            return [SymbolListing(ticker="GSPC.INDX"), SymbolListing(ticker="EMPTY.INDX")]
        return super().list_symbols(exchange)

    def fetch_index_components(self, index_code: str, as_of: date) -> IndexComponentsRow:
        self._request(f"/fundamentals/{index_code}.INDX", 10)
        if index_code == "EMPTY":
            return IndexComponentsRow(index_id="empty", as_of=as_of)
        return IndexComponentsRow(
            index_id="sp500",
            as_of=as_of,
            members=("AAA.US",),
            changes=(
                IndexChangeRow(ticker="AAA.US", change_date=date(2010, 1, 4), action="add"),
                IndexChangeRow(ticker="OLD.US", change_date=date(2005, 1, 3), action="add"),
                IndexChangeRow(ticker="OLD.US", change_date=date(2020, 6, 1), action="remove"),
            ),
        )

    def fetch_exchange_details(self, exchange: str, start: date, end: date) -> ExchangeDetailsRow:
        self._request(f"/exchange-details/{exchange}", 1)
        self._check(exchange)
        return ExchangeDetailsRow(
            exchange=exchange,
            timezone="America/New_York",
            open_local=time(9, 30),
            close_local=time(16),
            holidays=(ExchangeHolidayRow(holiday_date=date(2026, 12, 25), kind="official"),),
        )

    def fetch_treasury_rates(self, curve, year):
        self._request(f"/ust/{curve}", 1)
        if year < 2025:
            return []
        return [
            TreasuryRateRow(
                curve=curve if curve != "long_term" else "long_term",
                tenor="10Y" if curve != "bill" else "4W",
                measure="yield" if curve != "bill" else "bank_discount",
                observation_date=date(year, 1, 2),
                rate=4.0,
            )
        ]

    def fetch_ipo_calendar(self, start, end):
        self._request("/calendar/ipos", 1)
        return [IpoEventRow(ticker="NEW.US", filing_date=start, status="priced")]

    def fetch_split_calendar(self, start, end):
        self._request("/calendar/splits", 1)
        return [SplitEventRow(ticker="AAA.US", split_date=start, old_shares=1, new_shares=2)]

    def fetch_identifiers(self, exchange, offset, limit):
        self._request(f"/id-mapping/{exchange}", 1)
        names = ["AAA.US", "BBB.US", "OLD.US"] if exchange == "US" else []
        chunk = names[offset : offset + 2]  # a page of two, so the cursor moves
        return IdentifierPage(
            rows=tuple(
                InstrumentIdentifiersRow(ticker=t, cusip=f"C{i + offset}")
                for i, t in enumerate(chunk)
            ),
            size=len(chunk),
            total=len(names),
        )


class FreeSource(DataSource):
    """EDGAR, the short stock files and DefiLlama in one fake. Counts its
    requests, which no budget ever sees."""

    source_id = "free"

    def __init__(self) -> None:
        self.requests: list[str] = []

    def list_tickers(self, exchange):  # pragma: no cover - unused
        return []

    def fetch_prices(self, ticker, since=None, until=None):  # pragma: no cover
        return []

    def fetch_fundamentals(self, ticker):  # pragma: no cover
        raise NotImplementedError

    def fetch_filings(self, ticker, since=None, until=None, forms=None):
        self.requests.append(f"filings:{ticker}")
        if ticker != "AAA.US":
            raise UnknownSymbolError(f"EDGAR lists no company with ticker {ticker!r}")
        return [
            CorporateFilingRow(
                accession_number="0000000001-26-000001",
                ticker=ticker,
                issuer_cik="0000000001",
                form="8-K",
                filing_date=date(2026, 1, 5),
                known_at=datetime(2026, 1, 5, 21, 30),
            )
        ]

    def fetch_insider_filings(self, ticker, since=None, until=None):
        self.requests.append(f"insiders:{ticker}")
        return []

    def fetch_borrow_rates(self, market):
        self.requests.append(f"borrow:{market}")
        return [
            BorrowRateRow(
                ticker="AAA.US",
                as_of=date(2026, 10, 12),
                source="ibkr_borrow",
                fee_rate_annual=0.01,
                available_shares=1000,
            )
        ]

    def list_tvl_chains(self):
        self.requests.append("chains")
        return ["ethereum", "solana"]

    def fetch_chain_tvl(self, chain, since=None):
        self.requests.append(f"tvl:{chain}")
        return [
            DefiTvlRow(
                chain=chain, observation_date=date(2026, 10, 11), tvl_usd=1e9, source="defillama"
            )
        ]


@pytest.fixture
def clock() -> Clock:
    c = Clock(datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
    c.slept = []
    return c


@pytest.fixture
def vendor() -> RefVendor:
    return RefVendor()


def _settings(**kwargs: Any) -> BackfillSettings:
    base: dict[str, Any] = {
        "requests_per_minute": 1000,
        "calendar_start": date(2026, 9, 15),
        "treasury_start_year": 2024,
        "exchange_holidays_start": date(2026, 1, 1),
    }
    base.update(kwargs)
    return BackfillSettings(**base)


def test_index_members_and_history_feed_a_stored_index_universe(lake, state, vendor, clock):
    report, bstate, _ = _run(
        lake, state, vendor, clock, _settings(), phases=["index_constituents"], until_budget=True
    )
    assert report.stopped == "finished"
    summary = bstate.summary()["index_constituents"]
    assert summary.done == 2
    assert summary.calls == 20  # one /fundamentals call each, the listing aside
    history = UniverseStore(lake).index_history("sp500")
    assert history is not None
    assert history.constituents == ("AAA.US",)
    assert {(c.ticker, c.action) for c in history.changes} == {
        ("AAA.US", "add"),
        ("OLD.US", "add"),
        ("OLD.US", "remove"),
    }
    assert UniverseStore(lake).index_history("empty") is None  # nothing to store


def test_exchange_details_treasury_and_listing_calendars(lake, state, vendor, clock):
    report, bstate, _ = _run(
        lake,
        state,
        vendor,
        clock,
        _settings(),
        phases=["exchange_details", "treasury", "calendar_extra"],
        until_budget=True,
    )
    assert report.stopped == "finished"
    summary = bstate.summary()
    # US and CC; FOREX is skipped
    assert summary["exchange_details"].done == 2
    assert _count(lake, "SELECT COUNT(*) FROM exchange_details") == 2
    assert _count(lake, "SELECT COUNT(*) FROM exchange_holidays") == 2
    # four curves for 2024, 2025 and 2026; 2024 is empty
    assert summary["treasury"].done == 12
    assert summary["treasury"].calls == 12
    assert _count(lake, "SELECT COUNT(*) FROM treasury_rates") == 8
    assert _count(lake, "SELECT COUNT(*) FROM treasury_rates_versions") == 8
    # the current year is a unit a month, past years one unit each
    keys = {
        r["unit_key"]
        for r in state.sql("SELECT unit_key FROM backfill_units WHERE phase = 'treasury'")
    }
    assert "bill:2025" in keys and "bill:2026@2026-10" in keys
    # September from the 15th, then October to today: two steps each
    assert vendor.requests.count("/calendar/ipos") == 2
    assert vendor.requests.count("/calendar/splits") == 2
    assert _count(lake, "SELECT COUNT(*) FROM ipo_calendar") == 2
    assert _count(lake, "SELECT COUNT(*) FROM split_calendar") == 2


def test_identifiers_page_through_an_exchange_onto_known_instruments(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, _settings(), phases=["symbols"], until_budget=True)
    report, bstate, _ = _run(
        lake, state, vendor, clock, _settings(), phases=["id_mapping"], until_budget=True
    )
    assert report.stopped == "finished"
    assert vendor.requests.count("/id-mapping/US") == 2  # three names, two a page
    cusips = dict(
        lake.con.execute("SELECT id, cusip FROM instruments WHERE cusip IS NOT NULL").fetchall()
    )
    assert cusips == {"AAA.US": "C0", "BBB.US": "C1", "OLD.US": "C2"}
    assert bstate.summary()["id_mapping"].done == 2  # US and CC


def _free(source: FreeSource, *, edgar_ready: bool = True):
    def build(source_id: str) -> DataSource:
        if source_id == "edgar" and not edgar_ready:
            raise DataSourceError("set [sources.edgar] user_agent")
        return source

    return build


def test_free_phases_spend_no_budget_and_skip_edgar_until_it_is_set_up(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, _settings(), phases=["symbols"], until_budget=True)
    free = FreeSource()
    before = vendor.calls
    report, bstate, _ = _run(
        lake,
        state,
        vendor,
        clock,
        _settings(borrow_markets=["usa", "uk"]),
        phases=["edgar", "borrow", "tvl"],
        until_budget=True,
        sources=_free(free, edgar_ready=False),
    )
    assert report.stopped == "finished"
    assert vendor.calls == before  # not one metered call
    summary = bstate.summary()
    assert "edgar" not in summary or summary["edgar"].total == 0  # skipped, not listed
    assert summary["borrow"].done == 2
    assert summary["tvl"].done == 2
    assert _count(lake, "SELECT COUNT(*) FROM defi_tvl") == 2
    assert _count(lake, "SELECT COUNT(*) FROM borrow_rates") == 1


def test_edgar_stores_filings_and_marks_unknown_tickers_done(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, _settings(), phases=["symbols"], until_budget=True)
    free = FreeSource()
    report, bstate, _ = _run(
        lake,
        state,
        vendor,
        clock,
        _settings(),
        phases=["edgar"],
        until_budget=True,
        sources=_free(free),
    )
    assert report.stopped == "finished"
    # active US stocks only: AAA (known) and no delisted OLD
    assert free.requests == ["filings:AAA.US", "insiders:AAA.US"]
    assert _count(lake, "SELECT COUNT(*) FROM corporate_filings") == 1
    assert bstate.summary()["edgar"].done == 1


def test_free_phases_run_while_the_budget_waits_for_the_reset(lake, state, vendor, clock):
    _run(lake, state, vendor, clock, _settings(), phases=["symbols"], until_budget=True)
    free = FreeSource()
    # the daily phase needs 4 calls, the budget allows 2 today
    report, bstate, _ = _run(
        lake,
        state,
        vendor,
        clock,
        _settings(daily_budget=2, tvl_chains=["ethereum"]),
        phases=["daily", "tvl"],
        until_budget=True,
        sources=_free(free),
    )
    assert report.stopped == "budget"
    assert bstate.summary()["daily"].remaining > 0
    assert bstate.summary()["tvl"].done == 1  # ran in the wait
    assert "tvl:ethereum" in free.requests


def test_a_borrow_unit_from_a_day_that_passed_fetches_nothing(lake, state, vendor, clock):
    free = FreeSource()
    settings = _settings(borrow_markets=["usa"])
    from stonks.ingest.backfill.state import BackfillState, UnitSpec

    # yesterday's run listed its unit and stopped before fetching it
    yesterday = clock.now - timedelta(days=1)
    BackfillState(state, clock=lambda: yesterday).add_units(
        "borrow", [UnitSpec(f"usa:{yesterday.date().isoformat()}")]
    )
    report, bstate, _ = _run(
        lake, state, vendor, clock, settings, phases=["borrow"], until_budget=True,
        sources=_free(free),
    )  # fmt: skip
    assert report.stopped == "finished"
    assert free.requests == ["borrow:usa"]  # today's unit only
    assert bstate.summary()["borrow"].done == 2
