"""SEC EDGAR source (roadmap 23.13): recorded responses, parsing into the
vendor-neutral rows, acceptance times as known_at, and fair access (a named
User-Agent, request spacing, backoff). Hermetic: a stub session serves the
fixtures in tests/fixtures/edgar, recorded from EDGAR on 2026-09-28."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from stonks.config import EdgarSourceConfig, load_settings
from stonks.ingest.sources.base import UnsupportedCapabilityError
from stonks.ingest.sources.edgar import (
    EdgarDataSource,
    EdgarError,
    acceptance_time,
    parse_submissions,
    symbol_of,
)

FIX = Path(__file__).parent.parent / "fixtures" / "edgar"
AGENT = "Stonks tests admin@example.com"

ROUTES = {
    "https://www.sec.gov/files/company_tickers.json": "company_tickers.json",
    "https://data.sec.gov/submissions/CIK0000320193.json": "submissions_CIK0000320193.json",
    "https://data.sec.gov/submissions/CIK0001067983.json": "submissions_CIK0001067983.json",
    "https://www.sec.gov/Archives/edgar/data/320193/000114036126037584/form4.xml": (
        "form4_0001140361-26-037584.xml"
    ),
    "https://www.sec.gov/Archives/edgar/data/1067983/000119312526352200/index.json": (
        "index_0001193125-26-352200.json"
    ),
    "https://www.sec.gov/Archives/edgar/data/1067983/000119312526352200/56757.xml": (
        "infotable_0001193125-26-352200.xml"
    ),
}


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status_code = status
        self.content = body

    def json(self) -> Any:
        return json.loads(self.content)


class _Session:
    """Serves recorded files; ``fail`` queues statuses to answer first."""

    def __init__(self, fail: list[int] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.fail = list(fail or [])

    def get(self, url: str, headers: dict[str, str], timeout: float) -> _Response:
        self.calls.append((url, headers))
        if self.fail:
            return _Response(self.fail.pop(0), b"")
        name = ROUTES.get(url)
        if name is None:
            return _Response(404, b"")
        return _Response(200, (FIX / name).read_bytes())


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _source(session: _Session | None = None, clock: _Clock | None = None) -> EdgarDataSource:
    clock = clock or _Clock()
    return EdgarDataSource(
        user_agent=AGENT,
        session=session or _Session(),
        sleep=clock.sleep,
        clock=clock.time,
        retry_backoff_seconds=0.5,
    )


def test_a_user_agent_with_a_contact_is_required():
    with pytest.raises(EdgarError, match="User-Agent"):
        EdgarDataSource(user_agent="")
    with pytest.raises(EdgarError, match="contact"):
        EdgarDataSource(user_agent="stonks")
    EdgarDataSource(user_agent=AGENT)


def test_config_and_env(monkeypatch, tmp_path):
    cfg = EdgarSourceConfig()
    assert cfg.user_agent == "" and cfg.min_request_interval_seconds >= 0.1
    root = Path(__file__).resolve().parents[2]
    assert load_settings(root / "config" / "default.toml").sources.edgar == cfg
    monkeypatch.setenv("STONKS_EDGAR_USER_AGENT", AGENT)
    settings = load_settings(root / "config" / "default.toml")
    assert settings.sources.edgar.user_agent == AGENT
    assert EdgarDataSource.from_config(settings.sources.edgar)._agent == AGENT


def test_symbols_and_times():
    assert symbol_of("AAPL.US") == "AAPL"
    assert symbol_of("brk-b.us") == "BRK-B"
    assert symbol_of("MSFT") == "MSFT"
    with pytest.raises(EdgarError, match="US listings"):
        symbol_of("VOD.LSE")
    assert acceptance_time("2026-07-30T20:30:28.000Z") == datetime(2026, 7, 30, 20, 30, 28)


def test_filings_carry_acceptance_times_and_items():
    session = _Session()
    rows = _source(session).fetch_filings("AAPL.US")
    assert [r.form for r in rows] == ["8-K", "8-K", "8-K", "10-Q", "4", "4"]
    earnings = next(r for r in rows if "2.02" in r.items)
    assert earnings.items == ("2.02", "9.01")
    assert earnings.issuer_cik == "0000320193"
    # 20:30 UTC is 16:30 in New York: the release after the close
    assert earnings.known_at == datetime(2026, 7, 30, 20, 30, 28)
    assert earnings.filing_date == date(2026, 7, 30)
    assert earnings.url is not None and earnings.url.endswith("/aapl-20260730.htm")
    # every request named the client
    assert all(h["User-Agent"] == AGENT for _, h in session.calls)


def test_filings_filter_by_day_and_form():
    rows = _source().fetch_filings(
        "AAPL.US", since=date(2026, 4, 1), until=date(2026, 8, 31), forms=["8-K"]
    )
    assert [r.items for r in rows] == [("5.02",), ("2.02", "9.01")]


def test_insider_trades_from_form4_xml():
    rows = _source().fetch_insider_filings("AAPL.US", since=date(2026, 9, 20))
    (trade,) = rows
    assert trade.ticker == "AAPL.US"
    assert trade.transaction_date == date(2026, 9, 22)
    assert trade.filing_date == date(2026, 9, 24)
    assert trade.known_at == datetime(2026, 9, 24, 22, 30, 7)
    assert trade.owner_name == "Newstead Jennifer"
    assert trade.owner_relation == "officer"
    assert trade.owner_title == "SVP, GC and Government Affairs"
    assert trade.transaction_code == "S" and trade.acquired_disposed == "D"
    assert trade.shares == 2399 and trade.price == pytest.approx(340.06)
    assert trade.value == pytest.approx(2399 * 340.06)
    assert trade.post_transaction_amount == 44391
    assert trade.sec_link is not None and trade.sec_link.endswith("/form4.xml")
    assert "xsl" not in trade.sec_link


def test_institutional_holdings_from_13f():
    rows = _source().fetch_institutional_holdings(
        "1067983", since=date(2026, 8, 1), tickers={"037833100": "AAPL.US"}
    )
    assert len(rows) == 3
    ally, apple, apple2 = rows
    assert ally.cusip == "02005N100" and ally.ticker is None
    assert apple.ticker == "AAPL.US" and apple.issuer_name == "APPLE INC"
    assert apple.amount == 692000 and apple.amount_type == "shares"
    assert apple.value_usd == 200237120  # dollars since 2023
    assert apple.investment_discretion == "defined"
    assert apple.report_period == date(2026, 6, 30)
    assert apple.known_at == datetime(2026, 8, 14, 20, 5, 4)
    assert apple.filer_cik == "0001067983"
    assert apple.filer_name == "BERKSHIRE HATHAWAY INC"
    assert [r.line for r in rows] == [0, 1, 2]
    assert apple2.amount == 3840000


def test_old_13f_values_were_thousands():
    from stonks.ingest.filing_schemas import CorporateFilingRow
    from stonks.ingest.sources.edgar import parse_information_table

    filing = CorporateFilingRow(
        accession_number="x",
        ticker="x",
        issuer_cik="0000000001",
        form="13F-HR",
        filing_date=date(2022, 11, 14),
        known_at=datetime(2022, 11, 14, 21),
        period_of_report=date(2022, 9, 30),
    )
    xml = (FIX / "infotable_0001193125-26-352200.xml").read_bytes()
    rows = parse_information_table(xml, filing=filing)
    assert rows[1].value_usd == 200237120 * 1000


def test_requests_are_spaced_under_the_sec_limit():
    clock = _Clock()
    source = _source(clock=clock)
    source.fetch_filings("AAPL.US")
    source.fetch_filings("AAPL.US")
    assert clock.slept and all(s <= 0.125 + 1e-9 for s in clock.slept)
    assert len(clock.slept) >= 2  # the ticker map is read once, each request waits its turn


def test_backoff_on_429_then_success():
    clock = _Clock()
    session = _Session(fail=[429, 503])
    rows = _source(session, clock).fetch_filings("AAPL.US")
    assert rows
    assert 0.5 in clock.slept and 1.0 in clock.slept


def test_errors_are_soft_fail_types():
    with pytest.raises(EdgarError, match="no company"):
        _source().fetch_filings("NOPE.US")
    session = _Session(fail=[403])
    with pytest.raises(EdgarError, match="403"):
        _source(session).fetch_filings("AAPL.US")
    with pytest.raises(UnsupportedCapabilityError):
        list(_source().fetch_prices("AAPL.US"))
    with pytest.raises(EdgarError, match="filing table"):
        parse_submissions({"filings": {"recent": {}}}, "X")


def test_network_errors_retry_then_raise():
    class Broken(_Session):
        def get(self, url, headers, timeout):
            raise requests.ConnectionError("down")

    with pytest.raises(requests.ConnectionError):
        _source(Broken()).fetch_filings("AAPL.US")
