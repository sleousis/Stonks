"""SnapTrade aggregator, read-only (decision 2026-09-26: aggregator first).

Plain signed HTTP calls through ``httpx2`` instead of the SnapTrade SDK (a
large generated package): the handful of endpoints Stonks reads fit in one
small client, and vendor JSON never leaves this module.

Auth model:

- App level: the partner ``clientId`` and ``consumerKey``
  (``STONKS_SNAPTRADE_CLIENT_ID`` / ``STONKS_SNAPTRADE_CONSUMER_KEY``). Every
  request carries ``clientId`` and ``timestamp`` in the query and an
  HMAC-SHA256 ``Signature`` header over ``{content, path, query}``.
- Per connection: a SnapTrade user (``userId`` = ``stonks-<connection id>``,
  no email or name) and its ``userSecret``. The secret is what Stonks seals
  in ``broker_credentials``.
- The user links brokerages in SnapTrade's connection portal, requested
  with ``connectionType = read``.

Errors never carry the request URL (the user secret travels in the query)
nor any response text that echoes a secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any, ClassVar, Self
from urllib.parse import quote, urlencode, urlsplit

import httpx2

from stonks.connections.base import (
    AccountBalances,
    Activity,
    ActivityKind,
    BrokerConnection,
    Capability,
    Credentials,
    ExternalAccount,
    ExternalPosition,
    PortalFlow,
    ProviderAuthError,
    ProviderContext,
    ProviderError,
    ProviderNotConfigured,
    ProviderUnavailable,
    RateLimit,
    RateLimited,
    mask_number,
)
from stonks.connections.registry import register_provider
from stonks.connections.settings import SNAPTRADE_CONSUMER_KEY_ENV, SnapTradeConfig
from stonks.execution.brokers.symbols import to_canonical_ticker
from stonks.ingest.redact import redact_secrets

_MAX_DETAIL = 160

#: SnapTrade security type codes -> our normalized asset types.
_TYPE_CODES: dict[str, str] = {
    "cs": "equity",
    "ps": "equity",
    "ad": "adr",
    "et": "etf",
    "etf": "etf",
    "oef": "fund",
    "cef": "fund",
    "mf": "fund",
    "crypto": "crypto",
    "bnd": "bond",
    "op": "option",
    "opt": "option",
    "wt": "other",
    "rt": "other",
    "ut": "other",
}
_ACTIVITY_KINDS: dict[str, ActivityKind] = {
    "BUY": "trade",
    "SELL": "trade",
    "DIVIDEND": "dividend",
    "STOCK_DIVIDEND": "dividend",
    "REI": "dividend",
    "CONTRIBUTION": "deposit",
    "DEPOSIT": "deposit",
    "WITHDRAWAL": "withdrawal",
    "INTEREST": "interest",
    "FEE": "fee",
    "SPLIT": "split",
}


class SnapTradeClient:
    """Signed JSON calls against the SnapTrade API. Knows nothing about our
    types; :class:`SnapTradeConnection` does the mapping."""

    def __init__(
        self,
        config: SnapTradeConfig,
        *,
        transport: Any = None,
        limiter: Any = None,
        connection_key: str = "",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not config.configured:
            raise ProviderNotConfigured(
                "SnapTrade is not configured: set STONKS_SNAPTRADE_CLIENT_ID and "
                f"{SNAPTRADE_CONSUMER_KEY_ENV}"
            )
        self._config = config
        self._client_id = str(config.client_id)
        self._consumer_key = config.consumer_key.get_secret_value()  # type: ignore[union-attr]
        base = urlsplit(config.base_url.rstrip("/"))
        self._origin = f"{base.scheme}://{base.netloc}"
        self._prefix = base.path
        self._limiter = limiter
        self._connection_key = connection_key
        self._clock = clock
        self._http = httpx2.Client(transport=transport, timeout=config.timeout_seconds)
        self._extra_secrets: list[str] = []

    def close(self) -> None:
        self._http.close()

    def __repr__(self) -> str:
        return f"SnapTradeClient(base={self._origin}{self._prefix!s})"

    def request(
        self,
        op: str,
        method: str,
        path: str,
        *,
        user: tuple[str, str] | None = None,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        if self._limiter is not None:
            self._limiter.acquire(self._connection_key)
        params: list[tuple[str, str]] = [
            ("clientId", self._client_id),
            ("timestamp", str(int(self._clock()))),
        ]
        secrets = [self._consumer_key]
        if user is not None:
            params += [("userId", user[0]), ("userSecret", user[1])]
            secrets.append(user[1])
        params += [(k, v) for k, v in (query or {}).items() if v is not None]
        query_string = urlencode(params, quote_via=quote)
        full_path = f"{self._prefix}{path}"
        content = json.dumps(body, separators=(",", ":"), sort_keys=True) if body else None
        headers = {
            "Signature": self._sign(full_path, query_string, body),
            "Accept": "application/json",
        }
        if content is not None:
            headers["Content-Type"] = "application/json"
        url = f"{self._origin}{full_path}?{query_string}"
        try:
            response = self._http.request(method, url, content=content, headers=headers)
        except httpx2.TimeoutException:
            raise ProviderUnavailable(f"snaptrade {op} timed out") from None
        except httpx2.HTTPError as exc:
            raise ProviderUnavailable(f"snaptrade {op} failed: {type(exc).__name__}") from None
        return self._decode(op, response, secrets)

    def _sign(self, path: str, query: str, body: Mapping[str, Any] | None) -> str:
        payload = json.dumps(
            {"content": dict(body) if body else None, "path": path, "query": query},
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hmac.new(self._consumer_key.encode(), payload.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    @staticmethod
    def _decode(op: str, response: httpx2.Response, secrets: list[str]) -> Any:
        status = response.status_code
        if 200 <= status < 300:
            try:
                return response.json() if response.content else None
            except ValueError:
                raise ProviderError(
                    f"snaptrade {op}: response is not JSON", status=status
                ) from None
        detail = _detail(response, secrets)
        message = f"snaptrade {op} failed (HTTP {status}){': ' + detail if detail else ''}"
        if status in (401, 403):
            raise ProviderAuthError(message, status=status)
        if status == 429:
            raise RateLimited(message, retry_after=_retry_after(response))
        if status >= 500:
            raise ProviderUnavailable(message, status=status)
        raise ProviderError(message, status=status)


def _detail(response: httpx2.Response, secrets: list[str]) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    text = " ".join(
        str(data[k]) for k in ("code", "detail", "message") if data.get(k) not in (None, "")
    )
    return redact_secrets(text, secrets)[:_MAX_DETAIL]


def _retry_after(response: httpx2.Response) -> float | None:
    try:
        return float(response.headers.get("Retry-After", ""))
    except ValueError:
        return None


@register_provider("snaptrade")
class SnapTradeConnection(BrokerConnection, PortalFlow):
    display_name: ClassVar[str] = "SnapTrade"
    auth_flow = "portal"
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READ_BALANCES, Capability.READ_POSITIONS, Capability.READ_ACTIVITY}
    )
    #: SnapTrade allows 250 requests a minute per partner; keep a margin.
    rate_limit: ClassVar[RateLimit] = RateLimit(per_minute=200, per_connection_per_minute=60)

    def __init__(self, client: SnapTradeClient, credentials: Credentials) -> None:
        self._client = client
        self._user = (credentials["user_id"], credentials["user_secret"])
        self._accounts: dict[str, dict[str, Any]] = {}

    # ---- construction and the portal flow -----------------------------------

    @classmethod
    def check_configured(cls, config: Any) -> None:
        if not config.snaptrade.configured:
            raise ProviderNotConfigured(
                "SnapTrade is enabled but not configured: set STONKS_SNAPTRADE_CLIENT_ID "
                f"and {SNAPTRADE_CONSUMER_KEY_ENV}"
            )

    @classmethod
    def _client_for(cls, context: ProviderContext) -> SnapTradeClient:
        return SnapTradeClient(
            context.config.snaptrade,
            transport=context.transport,
            limiter=context.limiter,
            connection_key=context.connection_id,
        )

    @classmethod
    def open(cls, credentials: Credentials, context: ProviderContext) -> Self:
        return cls(cls._client_for(context), credentials)

    @classmethod
    def register_user(cls, context: ProviderContext, external_user_id: str) -> Credentials:
        client = cls._client_for(context)
        try:
            data = client.request(
                "register_user",
                "POST",
                "/snapTrade/registerUser",
                body={"userId": external_user_id},
            )
        finally:
            client.close()
        secret = data.get("userSecret") if isinstance(data, dict) else None
        if not isinstance(secret, str) or not secret:
            raise ProviderError("snaptrade register_user: no user secret in the response")
        return Credentials({"user_id": external_user_id, "user_secret": secret})

    @classmethod
    def portal_url(
        cls,
        context: ProviderContext,
        external_user_id: str,
        credentials: Credentials,
        redirect_uri: str,
    ) -> str:
        client = cls._client_for(context)
        try:
            data = client.request(
                "login",
                "POST",
                "/snapTrade/login",
                user=(credentials["user_id"], credentials["user_secret"]),
                body={
                    "connectionType": "read",
                    "customRedirect": redirect_uri,
                    "immediateRedirect": True,
                },
            )
        finally:
            client.close()
        url = data.get("redirectURI") if isinstance(data, dict) else None
        if not isinstance(url, str) or urlsplit(url).scheme != "https":
            raise ProviderError("snaptrade login: the portal link is missing or not https")
        return url

    @classmethod
    def unregister_user(
        cls, context: ProviderContext, external_user_id: str, credentials: Credentials
    ) -> None:
        client = cls._client_for(context)
        try:
            client.request(
                "delete_user",
                "DELETE",
                "/snapTrade/deleteUser",
                query={"userId": credentials["user_id"]},
            )
        finally:
            client.close()

    def close(self) -> None:
        self._client.close()

    # ---- reads --------------------------------------------------------------

    def _get(self, op: str, path: str, query: Mapping[str, str] | None = None) -> Any:
        return self._client.request(op, "GET", path, user=self._user, query=query)

    def accounts(self) -> list[ExternalAccount]:
        data = _as_list(self._get("accounts", "/accounts"), "accounts")
        out: list[ExternalAccount] = []
        for raw in data:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            self._accounts[str(raw["id"])] = raw
            total = (raw.get("balance") or {}).get("total") or {}
            out.append(
                ExternalAccount(
                    id=str(raw["id"]),
                    name=str(raw.get("name") or raw.get("institution_name") or "Account"),
                    currency=str(total.get("currency") or "USD").upper(),
                    institution=raw.get("institution_name"),
                    number_mask=mask_number(raw.get("number")),
                )
            )
        return out

    def balances(self, account_id: str) -> AccountBalances:
        if account_id not in self._accounts:
            self.accounts()
        account = self._accounts.get(account_id, {})
        total = (account.get("balance") or {}).get("total") or {}
        currency = str(total.get("currency") or "USD").upper()
        rows = _as_list(self._get("balances", f"/accounts/{_seg(account_id)}/balances"), "balances")
        by_currency = {
            str((r.get("currency") or {}).get("code") or "").upper(): r
            for r in rows
            if isinstance(r, dict)
        }
        row = by_currency.get(currency) or (next(iter(by_currency.values())) if by_currency else {})
        return AccountBalances(
            currency=currency,
            cash=_num(row.get("cash")) or 0.0,
            buying_power=_num(row.get("buying_power")),
            total_value=_num(total.get("amount")),
        )

    def positions(self, account_id: str) -> list[ExternalPosition]:
        rows = _as_list(
            self._get("positions", f"/accounts/{_seg(account_id)}/positions"), "positions"
        )
        out: list[ExternalPosition] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            sym = _universal_symbol(raw.get("symbol"))
            raw_symbol = str(sym.get("symbol") or sym.get("raw_symbol") or "").strip()
            units = _num(raw.get("units"))
            if units is None:
                units = _num(raw.get("fractional_units"))
            if not raw_symbol or units is None:
                continue
            currency = _code(sym.get("currency"))
            price = _num(raw.get("price"))
            out.append(
                ExternalPosition(
                    raw_symbol=raw_symbol,
                    ticker=_ticker(sym, raw_symbol, currency),
                    quantity=units,
                    price=price,
                    market_value=price * units if price is not None else None,
                    currency=currency,
                    description=sym.get("description"),
                )
            )
        return out

    def activities(self, account_id: str, since: date) -> list[Activity]:
        rows = _as_list(
            self._get(
                "activities",
                "/activities",
                {
                    "startDate": since.isoformat(),
                    "endDate": date.today().isoformat(),
                    "accounts": account_id,
                },
            ),
            "activities",
        )
        out: list[Activity] = []
        for raw in rows:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            vendor_type = str(raw.get("type") or "").upper()
            kind = _ACTIVITY_KINDS.get(vendor_type, "other")
            sym = raw.get("symbol") if isinstance(raw.get("symbol"), dict) else {}
            raw_symbol = str(sym.get("symbol") or sym.get("raw_symbol") or "").strip() or None
            currency = _code(raw.get("currency"))
            quantity = _num(raw.get("units"))
            if quantity is not None and vendor_type in ("BUY", "SELL"):
                quantity = abs(quantity) if vendor_type == "BUY" else -abs(quantity)
            out.append(
                Activity(
                    provider_activity_id=str(raw["id"]),
                    account_id=str((raw.get("account") or {}).get("id") or account_id),
                    kind=kind,
                    trade_date=_date(raw.get("trade_date")),
                    raw_symbol=raw_symbol,
                    ticker=_ticker(sym, raw_symbol, currency) if raw_symbol else None,
                    quantity=quantity,
                    price=_num(raw.get("price")),
                    amount=_num(raw.get("amount")),
                    fee=_num(raw.get("fee")),
                    currency=currency,
                    settle_date=_date(raw.get("settlement_date")),
                    description=raw.get("description"),
                )
            )
        return out


# ---- JSON helpers ---------------------------------------------------------------


def _as_list(data: Any, op: str) -> list[Any]:
    if not isinstance(data, list):
        raise ProviderError(f"snaptrade {op}: unexpected response shape")
    return data


def _seg(value: str) -> str:
    return quote(str(value), safe="")


def _universal_symbol(value: Any) -> dict[str, Any]:
    """Positions nest the universal symbol as ``symbol.symbol``."""
    if isinstance(value, dict) and isinstance(value.get("symbol"), dict):
        return value["symbol"]
    return value if isinstance(value, dict) else {}


def _ticker(sym: Mapping[str, Any], raw_symbol: str, currency: str | None) -> str | None:
    exchange = sym.get("exchange") if isinstance(sym.get("exchange"), dict) else {}
    type_code = str(
        ((sym.get("type") or {}) if isinstance(sym.get("type"), dict) else {}).get("code") or ""
    ).lower()
    asset_type = _TYPE_CODES.get(type_code, "other") if type_code else None
    for code in (exchange.get("mic_code"), exchange.get("code")):
        ticker = to_canonical_ticker(
            raw_symbol, exchange=code, asset_type=asset_type, currency=currency
        )
        if ticker is not None or asset_type == "crypto":
            return ticker
    if not exchange:
        return to_canonical_ticker(raw_symbol, asset_type=asset_type, currency=currency)
    return None


def _code(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("code")
    return str(value).upper() if value else None


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
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
