"""In-memory providers for tests and demos: ``fake`` (API-key flow) and
``fake_portal`` (hosted-login flow). Like every provider they work only when
enabled in ``[connections].enabled_providers``.

Books are looked up by the credential ``token``; an unknown token gets a
fresh copy of the demo book (one account with two covered holdings and one
that no ticker maps to). Tests install their own :class:`FakeBook` in
:data:`FAKE_BOOKS` to script balances, positions, activities and failures.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar, Self
from urllib.parse import quote

from stonks.connections.base import (
    AccountBalances,
    Activity,
    BrokerConnection,
    Capability,
    Credentials,
    ExternalAccount,
    ExternalPosition,
    PortalFlow,
    ProviderAuthError,
    ProviderContext,
    ProviderError,
    RateLimit,
)
from stonks.connections.registry import register_provider
from stonks.execution.brokers.symbols import to_canonical_ticker


def fake_position(
    symbol: str,
    quantity: float,
    price: float,
    *,
    exchange: str | None = "NASDAQ",
    asset_type: str | None = None,
    currency: str = "USD",
) -> ExternalPosition:
    return ExternalPosition(
        raw_symbol=symbol,
        ticker=to_canonical_ticker(
            symbol, exchange=exchange, asset_type=asset_type, currency=currency
        ),
        quantity=quantity,
        price=price,
        market_value=quantity * price,
        currency=currency,
    )


@dataclass
class FakeBook:
    accounts: list[ExternalAccount]
    balances: dict[str, AccountBalances]
    positions: dict[str, list[ExternalPosition]]
    activities: dict[str, list[Activity]] = field(default_factory=dict)
    #: Raised by every call while set (auth errors, rate limits, outages).
    fail: ProviderError | None = None
    calls: list[str] = field(default_factory=list)
    registered_users: set[str] = field(default_factory=set)


def demo_book() -> FakeBook:
    acc = ExternalAccount(
        id="fake-acc-1", name="Demo brokerage", currency="USD", institution="Fake Broker",
        number_mask="…0001",
    )  # fmt: skip
    return FakeBook(
        accounts=[acc],
        balances={acc.id: AccountBalances(currency="USD", cash=10_000.0, buying_power=10_000.0)},
        positions={
            acc.id: [
                fake_position("AAPL", 10, 200.0),
                fake_position("BRK.B", 2, 450.0, exchange="NYSE"),
                fake_position("XYZ123", 3, 10.0, exchange="PRIVATE"),
            ]
        },
        activities={
            acc.id: [
                Activity(
                    "fake-act-1",
                    acc.id,
                    "trade",
                    date(2026, 1, 5),
                    raw_symbol="AAPL",
                    ticker="AAPL.US",
                    quantity=10,
                    price=190.0,
                    amount=-1900.0,
                    currency="USD",
                ),
                Activity(
                    "fake-act-2",
                    acc.id,
                    "dividend",
                    date(2026, 2, 12),
                    raw_symbol="AAPL",
                    ticker="AAPL.US",
                    amount=2.5,
                    currency="USD",
                ),
            ]  # fmt: skip
        },
    )


FAKE_BOOKS: dict[str, FakeBook] = {}
_LOCK = threading.Lock()


def book_for(token: str) -> FakeBook:
    with _LOCK:
        if token not in FAKE_BOOKS:
            FAKE_BOOKS[token] = copy.deepcopy(demo_book())
        return FAKE_BOOKS[token]


class _FakeBase(BrokerConnection):
    display_name: ClassVar[str] = "Fake broker"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READ_BALANCES, Capability.READ_POSITIONS, Capability.READ_ACTIVITY}
    )
    rate_limit: ClassVar[RateLimit] = RateLimit(per_minute=6000, per_connection_per_minute=6000)

    def __init__(self, book: FakeBook, context: ProviderContext) -> None:
        self._book = book
        self._context = context

    @classmethod
    def open(cls, credentials: Credentials, context: ProviderContext) -> Self:
        token = credentials["token"]
        if token.startswith("bad"):
            raise ProviderAuthError("fake broker refused the credentials")
        return cls(book_for(token), context)

    def _call(self, op: str) -> None:
        if self._context.limiter is not None:
            self._context.limiter.acquire(self._context.connection_id)
        self._book.calls.append(op)
        if self._book.fail is not None:
            raise self._book.fail

    def _account(self, account_id: str) -> None:
        if account_id not in {a.id for a in self._book.accounts}:
            raise ProviderError(f"unknown account {account_id!r}", status=404)

    def accounts(self) -> list[ExternalAccount]:
        self._call("accounts")
        return list(self._book.accounts)

    def balances(self, account_id: str) -> AccountBalances:
        self._call("balances")
        self._account(account_id)
        return self._book.balances[account_id]

    def positions(self, account_id: str) -> list[ExternalPosition]:
        self._call("positions")
        self._account(account_id)
        return list(self._book.positions.get(account_id, []))

    def activities(self, account_id: str, since: date) -> list[Activity]:
        self._call("activities")
        self._account(account_id)
        return [
            a
            for a in self._book.activities.get(account_id, [])
            if a.trade_date is None or a.trade_date >= since
        ]


@register_provider("fake")
class FakeConnection(_FakeBase):
    auth_flow = "api_key"
    credential_fields = ("token",)


@register_provider("fake_portal")
class FakePortalConnection(_FakeBase, PortalFlow):
    display_name: ClassVar[str] = "Fake aggregator"
    auth_flow = "portal"

    @classmethod
    def register_user(cls, context: ProviderContext, external_user_id: str) -> Credentials:
        token = f"portal-{external_user_id}"
        book_for(token).registered_users.add(external_user_id)
        return Credentials({"token": token})

    @classmethod
    def portal_url(
        cls,
        context: ProviderContext,
        external_user_id: str,
        credentials: Credentials,
        redirect_uri: str,
    ) -> str:
        return (
            "https://fake-broker.invalid/portal?user="
            f"{quote(external_user_id, safe='')}&redirect={quote(redirect_uri, safe='')}"
        )

    @classmethod
    def unregister_user(
        cls, context: ProviderContext, external_user_id: str, credentials: Credentials
    ) -> None:
        book_for(credentials["token"]).registered_users.discard(external_user_id)
