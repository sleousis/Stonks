"""Live contract test for roadmap 23.13: SEC EDGAR filings, Form 4 insider
trades and 13F holdings.

Gated behind ``STONKS_RUN_LIVE_TESTS=1`` and ``STONKS_EDGAR_USER_AGENT`` (a
name and a contact address, as the SEC's fair access rules ask). EDGAR is
free and keyless.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
    pytest.mark.skipif(
        not os.environ.get("STONKS_EDGAR_USER_AGENT"),
        reason="set STONKS_EDGAR_USER_AGENT to 'Your Name you@example.com'",
    ),
]


@pytest.fixture(scope="module")
def source():
    from stonks.ingest.sources.edgar import EdgarDataSource

    return EdgarDataSource(user_agent=os.environ["STONKS_EDGAR_USER_AGENT"])


def test_live_filings_have_acceptance_times_and_items(source):
    since = datetime.now(UTC).date() - timedelta(days=400)
    rows = source.fetch_filings("AAPL.US", since=since, forms=["8-K", "10-Q", "10-K"])
    assert rows
    assert all(r.issuer_cik == "0000320193" for r in rows)
    assert any("2.02" in r.items for r in rows)  # quarterly earnings releases
    assert all(r.known_at.date() >= since for r in rows)


def test_live_form4_trades(source):
    since = datetime.now(UTC).date() - timedelta(days=120)
    rows = source.fetch_insider_filings("AAPL.US", since=since)
    assert rows, "Apple insiders file Form 4s every few weeks"
    assert all(r.known_at is not None and r.known_at.date() >= r.transaction_date for r in rows)
    assert all(r.acquired_disposed in ("A", "D", None) for r in rows)


def test_live_13f_holdings(source):
    since = datetime.now(UTC).date() - timedelta(days=200)
    rows = source.fetch_institutional_holdings("1067983", since=since)
    assert rows
    assert all(len(r.cusip) == 9 for r in rows)
    assert any(r.cusip == "037833100" for r in rows)  # Berkshire's Apple stake
