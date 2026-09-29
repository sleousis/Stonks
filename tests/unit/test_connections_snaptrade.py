"""SnapTrade read-only adapter over plain HTTP (hermetic: httpx2 MockTransport)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import date
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest

from stonks.connections.base import (
    Capability,
    Credentials,
    ProviderAuthError,
    ProviderContext,
    ProviderError,
    ProviderNotConfigured,
    ProviderUnavailable,
    RateLimited,
)
from stonks.connections.providers.snaptrade import SnapTradeClient, SnapTradeConnection
from stonks.connections.settings import ConnectionsConfig, SnapTradeConfig

CLIENT_ID = "STONKS-TEST"
CONSUMER_KEY = "consumer-key-SECRET-123"
USER_ID = "stonks-con_abc"
USER_SECRET = "user-secret-VALUE-456"

ACCOUNTS = [
    {
        "id": "acc-1",
        "brokerage_authorization": "auth-1",
        "name": "Individual",
        "number": "U1234567",
        "institution_name": "Interactive Brokers",
        "balance": {"total": {"amount": 12345.5, "currency": "USD"}},
    }
]
BALANCES = [
    {"currency": {"code": "CAD"}, "cash": 5.0, "buying_power": 5.0},
    {"currency": {"code": "USD"}, "cash": 1000.25, "buying_power": 2000.0},
]
POSITIONS = [
    {
        "symbol": {
            "symbol": {
                "symbol": "AAPL",
                "raw_symbol": "AAPL",
                "description": "Apple Inc",
                "currency": {"code": "USD"},
                "exchange": {"code": "NASDAQ", "mic_code": "XNAS"},
                "type": {"code": "cs"},
            }
        },
        "units": 10,
        "price": 200.0,
    },
    {
        "symbol": {
            "symbol": {
                "symbol": "MYSTERY",
                "raw_symbol": "MYSTERY",
                "currency": {"code": "USD"},
                "exchange": {"code": "ZZZZ"},
                "type": {"code": "cs"},
            }
        },
        "units": 3,
        "price": 5.0,
    },
    {
        "symbol": {
            "symbol": {
                "symbol": "BTC",
                "currency": {"code": "USD"},
                "type": {"code": "crypto"},
            }
        },
        "units": 0.5,
        "price": 60000.0,
    },
]
ACTIVITIES = [
    {
        "id": "act-1",
        "account": {"id": "acc-1"},
        "symbol": {"symbol": "AAPL", "exchange": {"code": "NASDAQ"}, "type": {"code": "cs"}},
        "type": "SELL",
        "units": 2,
        "price": 210.0,
        "amount": 420.0,
        "fee": 1.0,
        "currency": {"code": "USD"},
        "trade_date": "2026-03-02T00:00:00Z",
        "settlement_date": "2026-03-04T00:00:00Z",
        "description": "Sold AAPL",
    },
    {
        "id": "act-2",
        "account": {"id": "acc-1"},
        "symbol": None,
        "type": "CONTRIBUTION",
        "amount": 500.0,
        "currency": {"code": "USD"},
        "trade_date": "2026-03-01",
    },
    {
        "id": "act-3",
        "account": {"id": "acc-1"},
        "symbol": None,
        "type": "SOMETHING_NEW",
        "amount": 1.0,
        "trade_date": None,
    },
]


class FakeSnapTrade:
    """A tiny SnapTrade server: checks the signature on every request."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.override: dict[str, httpx2.Response] = {}

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        url = urlsplit(str(request.url))
        body = json.loads(request.content) if request.content else None
        expected = _sign(url.path, url.query, body)
        assert request.headers["Signature"] == expected
        query = dict(parse_qsl(url.query))
        assert query["clientId"] == CLIENT_ID
        assert query["timestamp"].isdigit()
        route = f"{request.method} {url.path.removeprefix('/api/v1')}"
        if route in self.override:
            return self.override[route]
        if route == "POST /snapTrade/registerUser":
            return httpx2.Response(200, json={"userId": body["userId"], "userSecret": USER_SECRET})
        if route == "DELETE /snapTrade/deleteUser":
            return httpx2.Response(200, json={"status": "deleted"})
        self._check_user(query)
        if route == "POST /snapTrade/login":
            return httpx2.Response(
                200, json={"redirectURI": "https://app.snaptrade.com/portal?x=1", "sessionId": "s"}
            )
        if route == "GET /accounts":
            return httpx2.Response(200, json=ACCOUNTS)
        if route == "GET /accounts/acc-1/balances":
            return httpx2.Response(200, json=BALANCES)
        if route == "GET /accounts/acc-1/positions":
            return httpx2.Response(200, json=POSITIONS)
        if route == "GET /activities":
            assert query["accounts"] == "acc-1"
            return httpx2.Response(200, json=ACTIVITIES)
        return httpx2.Response(404, json={"detail": "not found"})

    @staticmethod
    def _check_user(query: dict[str, str]) -> None:
        assert query["userId"] == USER_ID
        assert query["userSecret"] == USER_SECRET


def _sign(path: str, query: str, body: object) -> str:
    content = json.dumps(
        {"content": body, "path": path, "query": query}, separators=(",", ":"), sort_keys=True
    )
    digest = hmac.new(CONSUMER_KEY.encode(), content.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _config(**snap) -> ConnectionsConfig:
    base = {
        "client_id": CLIENT_ID,
        "consumer_key": CONSUMER_KEY,
        "base_url": "https://api.snaptrade.test/api/v1",
    }
    base.update(snap)
    return ConnectionsConfig(enabled_providers=("snaptrade",), snaptrade=SnapTradeConfig(**base))


@pytest.fixture
def server() -> FakeSnapTrade:
    return FakeSnapTrade()


@pytest.fixture
def context(server) -> ProviderContext:
    return ProviderContext(
        config=_config(), connection_id="con_abc", transport=httpx2.MockTransport(server)
    )


def _creds() -> Credentials:
    return Credentials({"user_id": USER_ID, "user_secret": USER_SECRET})


def test_is_read_only():
    assert not SnapTradeConnection.supports(Capability.TRADE)
    assert SnapTradeConnection.supports(Capability.READ_POSITIONS)
    assert SnapTradeConnection.auth_flow == "portal"


def test_check_configured_requires_partner_keys():
    with pytest.raises(ProviderNotConfigured, match="STONKS_SNAPTRADE_CONSUMER_KEY"):
        SnapTradeConnection.check_configured(ConnectionsConfig())
    SnapTradeConnection.check_configured(_config())


def test_register_user_returns_sealable_credentials(context, server):
    creds = SnapTradeConnection.register_user(context, USER_ID)
    assert creds["user_id"] == USER_ID
    assert creds["user_secret"] == USER_SECRET
    assert json.loads(server.requests[0].content) == {"userId": USER_ID}


def test_portal_url_asks_for_a_read_only_connection(context, server):
    url = SnapTradeConnection.portal_url(
        context, USER_ID, _creds(), "https://stonks.example/cb?s=1"
    )
    assert url == "https://app.snaptrade.com/portal?x=1"
    body = json.loads(server.requests[-1].content)
    assert body["connectionType"] == "read"
    assert body["customRedirect"] == "https://stonks.example/cb?s=1"


def test_portal_url_refuses_a_non_https_link(context, server):
    server.override["POST /snapTrade/login"] = httpx2.Response(
        200, json={"redirectURI": "http://evil.example/"}
    )
    with pytest.raises(ProviderError, match="portal"):
        SnapTradeConnection.portal_url(context, USER_ID, _creds(), "https://stonks.example/cb")


def test_unregister_user_deletes_the_snaptrade_user(context, server):
    SnapTradeConnection.unregister_user(context, USER_ID, _creds())
    req = server.requests[-1]
    assert req.method == "DELETE"
    assert dict(parse_qsl(urlsplit(str(req.url)).query))["userId"] == USER_ID


def test_accounts_balances_positions_are_mapped_to_our_types(context):
    conn = SnapTradeConnection.open(_creds(), context)
    [acc] = conn.accounts()
    assert (acc.id, acc.name, acc.currency, acc.institution) == (
        "acc-1", "Individual", "USD", "Interactive Brokers",
    )  # fmt: skip
    assert acc.number_mask == "…4567"
    bal = conn.balances("acc-1")
    assert (bal.currency, bal.cash, bal.buying_power, bal.total_value) == (
        "USD", 1000.25, 2000.0, 12345.5,
    )  # fmt: skip
    positions = {p.raw_symbol: p for p in conn.positions("acc-1")}
    assert positions["AAPL"].ticker == "AAPL.US"
    assert positions["AAPL"].market_value == 2000.0
    assert positions["MYSTERY"].ticker is None  # kept, reported as not covered
    assert positions["MYSTERY"].quantity == 3
    assert positions["BTC"].ticker == "BTC-USD.CC"


def test_activities_are_normalised(context):
    conn = SnapTradeConnection.open(_creds(), context)
    acts = {a.provider_activity_id: a for a in conn.activities("acc-1", date(2026, 1, 1))}
    sell = acts["act-1"]
    assert (sell.kind, sell.ticker, sell.quantity, sell.price, sell.fee) == (
        "trade", "AAPL.US", -2.0, 210.0, 1.0,
    )  # fmt: skip
    assert (sell.trade_date, sell.settle_date) == (date(2026, 3, 2), date(2026, 3, 4))
    assert acts["act-2"].kind == "deposit"
    assert acts["act-3"].kind == "other"


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (500, ProviderUnavailable),
        (400, ProviderError),
    ],
)
def test_errors_are_typed_and_never_carry_secrets(context, server, status, error):
    server.override["GET /accounts"] = httpx2.Response(
        status, json={"detail": f"bad user {USER_SECRET}", "code": "1076"}
    )
    conn = SnapTradeConnection.open(_creds(), context)
    with pytest.raises(error) as info:
        conn.accounts()
    text = f"{info.value} {info.value!r} {info.value.__cause__!r} {info.value.__context__!r}"
    for secret in (USER_SECRET, CONSUMER_KEY):
        assert secret not in text
    assert info.value.status == status


def test_rate_limited_carries_retry_after(context, server):
    server.override["GET /accounts"] = httpx2.Response(429, headers={"Retry-After": "12"})
    conn = SnapTradeConnection.open(_creds(), context)
    with pytest.raises(RateLimited) as info:
        conn.accounts()
    assert info.value.retry_after == 12.0


def test_network_errors_do_not_leak_the_url(server):
    def boom(request):
        raise httpx2.ConnectError(f"cannot reach {request.url}")

    ctx = ProviderContext(config=_config(), connection_id="c", transport=httpx2.MockTransport(boom))
    conn = SnapTradeConnection.open(_creds(), ctx)
    with pytest.raises(ProviderUnavailable) as info:
        conn.accounts()
    assert USER_SECRET not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_malformed_json_is_a_provider_error(context, server):
    server.override["GET /accounts"] = httpx2.Response(200, content=b"<html>")
    with pytest.raises(ProviderError, match="accounts"):
        SnapTradeConnection.open(_creds(), context).accounts()


def test_client_uses_the_rate_limiter(context):
    seen = []

    class Limiter:
        def acquire(self, connection_id, *, max_wait=30.0):
            seen.append(connection_id)

    context.limiter = Limiter()  # type: ignore[assignment]
    SnapTradeConnection.open(_creds(), context).accounts()
    assert seen == ["con_abc"]


def test_client_repr_hides_keys(context):
    client = SnapTradeClient(context.config.snaptrade, transport=context.transport)
    assert CONSUMER_KEY not in repr(client)


def test_the_http_request_log_never_carries_the_user_secret(context, caplog):
    """httpx2 logs every request URL at INFO, and SnapTrade's user secret
    travels in the query. The logged URL keeps its path, never its query."""
    import logging

    conn = SnapTradeConnection.open(_creds(), context)
    with caplog.at_level(logging.INFO, logger="httpx2"):
        conn.accounts()
    lines = [r.getMessage() for r in caplog.records if r.name == "httpx2"]
    assert lines and "/accounts" in lines[0]
    for secret in (USER_SECRET, CONSUMER_KEY):
        assert all(secret not in line for line in lines)
    assert all("userSecret" not in line for line in lines)


def test_a_degiro_account_is_never_synced(context, server):
    """SnapTrade reaches DEGIRO over an unofficial route that DEGIRO's
    terms forbid: the account is left out, the others come through."""
    from stonks.connections.route_policy import refused_route

    degiro = {**ACCOUNTS[0], "id": "acc-9", "institution_name": "DEGIRO", "name": "Degiro"}
    server.override["GET /accounts"] = httpx2.Response(200, json=[degiro, ACCOUNTS[0]])
    conn = SnapTradeConnection.open(_creds(), context)
    assert [a.id for a in conn.accounts()] == ["acc-1"]
    assert refused_route("snaptrade", "flatexDEGIRO Bank") is not None
    assert refused_route("snaptrade", "Interactive Brokers") is None
    assert refused_route("alpaca", "DEGIRO") is None
