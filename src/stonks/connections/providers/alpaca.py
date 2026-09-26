"""Alpaca as a read-only connection (API-key flow).

Reuses the ``execution.brokers`` Alpaca pieces: the same ``alpaca-py``
``TradingClient`` in ``raw_data`` mode, the account mapping and the symbol
mapper. Plain Alpaca API keys *can* trade, so the UI must say so before a
user pastes them (least privilege, design section 6); this adapter only
reads. Trading through a connection (``trader()`` returning the existing
``AlpacaBroker``) arrives with step S6 and adds ``Capability.TRADE``.

Credentials: ``api_key``, ``secret_key`` and optional ``paper``
(``"true"`` default, ``"false"`` for the live endpoint; reads only).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Any, ClassVar, Self

import requests
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient

from stonks.connections.base import (
    AccountBalances,
    Activity,
    ActivityKind,
    BrokerConnection,
    Capability,
    Credentials,
    ExternalAccount,
    ExternalPosition,
    ProviderAuthError,
    ProviderContext,
    ProviderError,
    ProviderUnavailable,
    RateLimit,
    RateLimited,
    mask_number,
)
from stonks.connections.registry import register_provider
from stonks.execution.brokers.alpaca import _to_account
from stonks.execution.brokers.symbols import alpaca_symbol_to_ticker

_PAGE_SIZE = 100
_MAX_PAGES = 50
_ACTIVITY_KINDS: dict[str, ActivityKind] = {
    "FILL": "trade",
    "DIV": "dividend",
    "DIVCGL": "dividend",
    "DIVCGS": "dividend",
    "DIVNRA": "dividend",
    "DIVROC": "dividend",
    "DIVTXEX": "dividend",
    "CSD": "deposit",
    "JNLC": "deposit",
    "CSW": "withdrawal",
    "INT": "interest",
    "FEE": "fee",
    "CFEE": "fee",
    "SPLIT": "split",
    "SSO": "split",
    "SSP": "split",
    "REORG": "other",
}

ClientFactory = Callable[[str, str, bool], Any]


def _default_factory(api_key: str, secret_key: str, paper: bool) -> Any:
    return TradingClient(api_key, secret_key, paper=paper, raw_data=True)


@register_provider("alpaca")
class AlpacaConnection(BrokerConnection):
    display_name: ClassVar[str] = "Alpaca"
    has_paper: ClassVar[bool] = True
    auth_flow = "api_key"
    credential_fields = ("api_key", "secret_key")
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READ_BALANCES, Capability.READ_POSITIONS, Capability.READ_ACTIVITY}
    )
    #: Alpaca allows 200 requests a minute per key; one key = one connection.
    rate_limit: ClassVar[RateLimit] = RateLimit(per_minute=1000, per_connection_per_minute=150)

    def __init__(self, client: Any, credentials: Credentials, context: ProviderContext) -> None:
        self._client = client
        self._credentials = credentials
        self._context = context
        self.paper = (credentials.get("paper") or "true").lower() != "false"
        self._account_id: str | None = None

    @classmethod
    def open(cls, credentials: Credentials, context: ProviderContext) -> Self:
        factory: ClientFactory = context.transport or _default_factory
        paper = (credentials.get("paper") or "true").lower() != "false"
        client = factory(credentials["api_key"], credentials["secret_key"], paper)
        return cls(client, credentials, context)

    def _call(self, op: str, fn: Callable[..., Any], *args: Any) -> Any:
        if self._context.limiter is not None:
            self._context.limiter.acquire(self._context.connection_id)
        try:
            return fn(*args)
        except APIError as exc:
            status = _status(exc)
            message = self._credentials.redact(f"alpaca {op} failed (HTTP {status})")
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise ProviderUnavailable(f"alpaca {op} failed: {type(exc).__name__}") from None
        if status in (401, 403):
            raise ProviderAuthError(message, status=status)
        if status == 429:
            raise RateLimited(message)
        if status is not None and status >= 500:
            raise ProviderUnavailable(message, status=status)
        raise ProviderError(message, status=status)

    def _raw_account(self) -> dict[str, Any]:
        raw = self._call("get_account", self._client.get_account)
        self._account_id = str(raw.get("id") or "")
        return raw

    def _check_account(self, account_id: str) -> None:
        if self._account_id is None:
            self._raw_account()
        if account_id != self._account_id:
            raise ProviderError(f"unknown Alpaca account {account_id!r}", status=404)

    def accounts(self) -> list[ExternalAccount]:
        raw = self._raw_account()
        return [
            ExternalAccount(
                id=str(raw["id"]),
                name=f"Alpaca ({'paper' if self.paper else 'live'})",
                currency=str(raw.get("currency") or "USD"),
                institution="Alpaca",
                number_mask=mask_number(raw.get("account_number")),
            )
        ]

    def balances(self, account_id: str) -> AccountBalances:
        self._check_account(account_id)
        account = _to_account(self._raw_account())
        return AccountBalances(
            currency=account.currency,
            cash=account.cash,
            buying_power=account.buying_power,
            total_value=account.equity,
        )

    def positions(self, account_id: str) -> list[ExternalPosition]:
        self._check_account(account_id)
        out: list[ExternalPosition] = []
        for raw in self._call("get_all_positions", self._client.get_all_positions):
            qty = float(raw.get("qty") or 0.0)
            if raw.get("side") == "short" and qty > 0:
                qty = -qty
            symbol = str(raw.get("symbol") or "")
            out.append(
                ExternalPosition(
                    raw_symbol=symbol,
                    ticker=alpaca_symbol_to_ticker(symbol, asset_class=raw.get("asset_class")),
                    quantity=qty,
                    price=_num(raw.get("current_price")),
                    market_value=_num(raw.get("market_value")),
                    currency="USD",
                )
            )
        return out

    def activities(self, account_id: str, since: date) -> list[Activity]:
        self._check_account(account_id)
        out: list[Activity] = []
        page_token: str | None = None
        for _ in range(_MAX_PAGES):
            params = {"after": since.isoformat(), "direction": "asc", "page_size": _PAGE_SIZE}
            if page_token:
                params["page_token"] = page_token
            page = self._call("get_activities", self._client.get, "/account/activities", params)
            if not isinstance(page, list) or not page:
                break
            out.extend(a for a in (_activity(raw, account_id) for raw in page) if a)
            if len(page) < _PAGE_SIZE:
                break
            page_token = str(page[-1].get("id") or "") or None
            if page_token is None:
                break
        return out


def _activity(raw: dict[str, Any], account_id: str) -> Activity | None:
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    vendor = str(raw.get("activity_type") or "").upper()
    kind = _ACTIVITY_KINDS.get(vendor, "other")
    symbol = raw.get("symbol") or None
    qty = _num(raw.get("qty"))
    if kind == "trade" and qty is not None:
        qty = -abs(qty) if str(raw.get("side") or "").lower().startswith("sell") else abs(qty)
    when = raw.get("transaction_time") or raw.get("date")
    return Activity(
        provider_activity_id=str(raw["id"]),
        account_id=account_id,
        kind=kind,
        trade_date=_date(when),
        raw_symbol=symbol,
        ticker=alpaca_symbol_to_ticker(symbol) if symbol else None,
        quantity=qty,
        price=_num(raw.get("price")),
        amount=_num(raw.get("net_amount")),
        currency="USD",
        description=raw.get("description"),
    )


def _status(exc: APIError) -> int | None:
    try:
        return exc.status_code
    except Exception:
        return None


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None
