"""Alpaca read-only connection adapter (fake TradingClient, no network)."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
import requests
from alpaca.common.exceptions import APIError

from stonks.connections.base import (
    Capability,
    CapabilityMissing,
    Credentials,
    ProviderAuthError,
    ProviderContext,
    ProviderUnavailable,
    RateLimited,
)
from stonks.connections.providers.alpaca import AlpacaConnection
from stonks.connections.settings import ConnectionsConfig

KEY = "AKFAKEKEY123456"
SECRET = "fake-secret-abcdef"


def api_error(status: int, message: str = "boom") -> APIError:
    body = json.dumps({"code": 0, "message": message})
    http_error = SimpleNamespace(response=SimpleNamespace(status_code=status), request=None)
    return APIError(body, http_error)


class FakeTradingClient:
    def __init__(self) -> None:
        self.fail: Exception | None = None
        self.activity_pages: list[list[dict]] = [
            [
                {
                    "id": "20260302000000000::1",
                    "activity_type": "FILL",
                    "transaction_time": "2026-03-02T15:00:00Z",
                    "symbol": "AAPL",
                    "side": "sell",
                    "qty": "2",
                    "price": "210",
                },
                {
                    "id": "20260303000000000::2",
                    "activity_type": "DIV",
                    "date": "2026-03-03",
                    "symbol": "MSFT",
                    "net_amount": "1.5",
                },
            ],
        ]
        self.gets: list[tuple[str, dict]] = []

    def _maybe_fail(self):
        if self.fail is not None:
            raise self.fail

    def get_account(self):
        self._maybe_fail()
        return {
            "id": "acct-uuid",
            "account_number": "PA3ABCDEF1",
            "cash": "1000.5",
            "equity": "5000",
            "buying_power": "2000",
            "currency": "USD",
            "status": "ACTIVE",
        }

    def get_all_positions(self):
        self._maybe_fail()
        return [
            {"symbol": "AAPL", "asset_class": "us_equity", "qty": "10", "side": "long",
             "current_price": "200", "market_value": "2000"},
            {"symbol": "BTCUSD", "asset_class": "crypto", "qty": "0.1", "side": "long",
             "current_price": "60000", "market_value": "6000"},
            {"symbol": "AAPL260116C00200000", "asset_class": "us_option", "qty": "1",
             "side": "long", "current_price": "5", "market_value": "500"},
        ]  # fmt: skip

    def get(self, path, data=None):
        self._maybe_fail()
        self.gets.append((path, dict(data or {})))
        return self.activity_pages.pop(0) if self.activity_pages else []


@pytest.fixture
def client() -> FakeTradingClient:
    return FakeTradingClient()


@pytest.fixture
def conn(client) -> AlpacaConnection:
    made = []

    def factory(api_key, secret_key, paper):
        made.append((api_key, secret_key, paper))
        return client

    ctx = ProviderContext(config=ConnectionsConfig(), connection_id="con_a", transport=factory)
    c = AlpacaConnection.open(Credentials({"api_key": KEY, "secret_key": SECRET}), ctx)
    assert made == [(KEY, SECRET, True)]
    return c


def test_is_read_only_in_this_step():
    assert AlpacaConnection.auth_flow == "api_key"
    assert AlpacaConnection.credential_fields == ("api_key", "secret_key")
    assert not AlpacaConnection.supports(Capability.TRADE)


def test_accounts_and_balances(conn):
    [acc] = conn.accounts()
    assert (acc.id, acc.name, acc.number_mask) == ("acct-uuid", "Alpaca (paper)", "…DEF1")
    bal = conn.balances("acct-uuid")
    assert (bal.cash, bal.buying_power, bal.total_value, bal.currency) == (
        1000.5, 2000.0, 5000.0, "USD",
    )  # fmt: skip


def test_positions_keep_unmapped_symbols(conn):
    pos = {p.raw_symbol: p for p in conn.positions("acct-uuid")}
    assert pos["AAPL"].ticker == "AAPL.US"
    assert pos["BTCUSD"].ticker == "BTC-USD.CC"
    assert pos["AAPL260116C00200000"].ticker is None
    assert pos["AAPL"].market_value == 2000.0


def test_activities_are_normalised(conn, client):
    acts = {a.provider_activity_id: a for a in conn.activities("acct-uuid", date(2026, 1, 1))}
    fill = acts["20260302000000000::1"]
    assert (fill.kind, fill.ticker, fill.quantity, fill.price, fill.trade_date) == (
        "trade", "AAPL.US", -2.0, 210.0, date(2026, 3, 2),
    )  # fmt: skip
    div = acts["20260303000000000::2"]
    assert (div.kind, div.amount) == ("dividend", 1.5)
    assert client.gets[0][0] == "/account/activities"
    assert client.gets[0][1]["after"] == "2026-01-01"


def test_unknown_account_is_refused(conn):
    with pytest.raises(Exception, match="account"):
        conn.balances("other")


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (api_error(401, f"bad key {SECRET}"), ProviderAuthError),
        (api_error(403), ProviderAuthError),
        (api_error(429), RateLimited),
        (api_error(503), ProviderUnavailable),
        (requests.ConnectionError(f"https://x?key={SECRET}"), ProviderUnavailable),
    ],
)
def test_errors_are_typed_and_redacted(conn, client, exc, error):
    client.fail = exc
    with pytest.raises(error) as info:
        conn.accounts()
    assert SECRET not in str(info.value)
    assert info.value.__cause__ is None


def test_trader_is_not_available_yet(conn):
    with pytest.raises(CapabilityMissing):
        conn.trader("acct-uuid")


def test_live_flag_is_passed_through(client):
    made = []
    ctx = ProviderContext(
        config=ConnectionsConfig(),
        transport=lambda k, s, paper: made.append(paper) or client,
    )
    conn = AlpacaConnection.open(
        Credentials({"api_key": KEY, "secret_key": SECRET, "paper": "false"}), ctx
    )
    assert made == [False]
    assert conn.accounts()[0].name == "Alpaca (live)"
