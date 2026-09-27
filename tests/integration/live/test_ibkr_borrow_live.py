"""Live contract tests for roadmap 19.3: IBKR's public short stock file, the
shortable ticks through a PAPER gateway, the ``ibkr`` provider's reads, and
the optional Flex statement.

Gated behind ``STONKS_RUN_LIVE_TESTS=1``. The short stock file needs
nothing else (IBKR's shared public login). The gateway tests also need
``STONKS_IBKR_HOST``, ``STONKS_IBKR_PORT`` and a paper ``STONKS_IBKR_ACCOUNT``
(``DU...``). The Flex test needs ``STONKS_IBKR_FLEX_TOKEN`` and
``STONKS_IBKR_FLEX_QUERY_ID``. No IBKR login is read: the gateway holds it.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("STONKS_RUN_LIVE_TESTS") != "1",
        reason="set STONKS_RUN_LIVE_TESTS=1 to enable live API tests",
    ),
]

ACCOUNT = os.environ.get("STONKS_IBKR_ACCOUNT", "")
needs_paper_gateway = pytest.mark.skipif(
    not (
        os.environ.get("STONKS_IBKR_HOST")
        and os.environ.get("STONKS_IBKR_PORT")
        and ACCOUNT.upper().startswith("DU")
    ),
    reason="STONKS_IBKR_HOST / STONKS_IBKR_PORT and a paper (DU) STONKS_IBKR_ACCOUNT needed",
)


def test_live_short_stock_file_parses():
    from stonks.ingest.sources.ibkr_borrow import IbkrBorrowDataSource

    rows = list(IbkrBorrowDataSource().fetch_borrow_rates("usa"))
    assert len(rows) > 1000
    by = {r.ticker: r for r in rows}
    assert "AAPL.US" in by
    assert 0 <= by["AAPL.US"].fee_rate_annual < 1
    assert len({r.as_of for r in rows}) == 1


@pytest.fixture(scope="module")
def paper_broker():
    from stonks.execution.brokers.ibkr.broker import IbkrBroker
    from stonks.execution.brokers.ibkr.client import IbEndpoint
    from stonks.execution.brokers.ibkr.ib_async_client import IbAsyncClient

    endpoint = IbEndpoint(
        host=os.environ["STONKS_IBKR_HOST"],
        port=int(os.environ["STONKS_IBKR_PORT"]),
        client_id=int(os.environ.get("STONKS_IBKR_CLIENT_ID", "92")),
        request_timeout=15.0,
    )
    client = IbAsyncClient(endpoint)
    broker = IbkrBroker(client, mode="paper", account_id=ACCOUNT)
    yield broker
    broker.close()


@needs_paper_gateway
def test_live_shortable_indicator(paper_broker):
    from datetime import UTC, datetime

    from stonks.execution.brokers.ibkr.borrow import IbkrBorrowSource

    quote = IbkrBorrowSource(paper_broker).quote("AAPL.US", datetime.now(UTC).date())
    if quote is None:
        pytest.skip("IBKR sent no shortable ticks (market data subscription or hours)")
    assert quote.status in ("easy", "hard", "none")


@needs_paper_gateway
def test_live_provider_reads_the_paper_account():
    from datetime import UTC, datetime, timedelta

    from stonks.connections.base import Credentials, ProviderContext
    from stonks.connections.providers.ibkr import IbkrConnection, reset_sessions
    from stonks.connections.settings import ConnectionsConfig

    config = ConnectionsConfig(
        enabled_providers=("ibkr",),
        ibkr={
            "client_ids": {"sync": int(os.environ.get("STONKS_IBKR_CLIENT_ID_SYNC", "93"))},
            "gateways": {
                "paper": {
                    "host": os.environ["STONKS_IBKR_HOST"],
                    "port": int(os.environ["STONKS_IBKR_PORT"]),
                    "mode": "paper",
                    "account_id": ACCOUNT,
                }
            },
        },
    )
    conn = IbkrConnection.open(Credentials({"gateway": "paper"}), ProviderContext(config=config))
    try:
        (account,) = conn.accounts()
        assert account.id == ACCOUNT
        assert conn.balances(ACCOUNT).total_value
        conn.positions(ACCOUNT)
        conn.activities(ACCOUNT, datetime.now(UTC).date() - timedelta(days=7))
    finally:
        conn.close()
        reset_sessions()


@pytest.mark.skipif(
    not (os.environ.get("STONKS_IBKR_FLEX_TOKEN") and os.environ.get("STONKS_IBKR_FLEX_QUERY_ID")),
    reason="STONKS_IBKR_FLEX_TOKEN / STONKS_IBKR_FLEX_QUERY_ID not set",
)
def test_live_flex_statement():
    from stonks.execution.brokers.ibkr.flex import FlexClient
    from stonks.execution.brokers.ibkr.settings import IbkrFlexSettings

    client = FlexClient.from_env(
        IbkrFlexSettings(query_id=os.environ["STONKS_IBKR_FLEX_QUERY_ID"], poll_seconds=5.0)
    )
    assert client is not None
    statements = client.fetch()
    assert statements
    assert all(s.account_id for s in statements)
