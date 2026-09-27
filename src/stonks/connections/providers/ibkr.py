"""Interactive Brokers as a connection (roadmap 19.3).

Reads (balances, positions, activities) and trading go through the IBKR
adapter in ``execution/brokers/ibkr`` and an IB Gateway container. Stonks
never sees the IBKR login: the gateway holds it in Docker secret files.
The connection's only "credential" is the name of a gateway configured
under ``[brokers.ibkr.gateways]`` (a setting, not a secret, sealed like
every connection field).

- **Sync** uses the ``sync`` API client id (12). Positions map back to our
  tickers by ``conId`` or by market and symbol; a holding no ticker maps
  to is kept with its raw symbol ("not covered"). Activities are the
  session's executions (about a day back, hand-placed ones included, since
  the sync mirrors the whole account), plus the optional Flex statement
  for older trades, dividends, interest, fees and cash moves.
- **Trading**: :meth:`IbkrConnection.trader` returns an ``IbkrBroker`` on
  the ``tick`` client id (11), bound to the linked account. A ``cash``
  gateway trades long only. A ``margin`` gateway may short, each opening
  sell checked against IBKR's locate (``IbkrBorrowSource``).
- **Ownership.** The account may be shared with the owner's own trading.
  The broker reports the whole account. The tick keeps only what the book
  opened (its fill ledger, ``production/ownership.py``) and never trades
  the rest, and the broker books only executions carrying our ``orderRef``.

Gateway sessions are shared per process and role (one TWS API client id
can hold only one session), so a connection never closes them.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any, ClassVar, Self, TypeVar

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
    ProviderNotConfigured,
    ProviderUnavailable,
    RateLimit,
    mask_number,
)
from stonks.connections.registry import register_provider
from stonks.connections.settings import ConnectionsConfig
from stonks.execution.brokers.base import (
    BrokerError,
    BrokerUnavailableError,
    LiveTradingRefusedError,
)
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbClient, IbContract, IbEndpoint, IbExecution
from stonks.execution.brokers.ibkr.contracts import ticker_for_contract
from stonks.execution.brokers.ibkr.factory import (
    ClientFactory,
    connect_ibkr,
    default_client_factory,
    pick_gateway,
)
from stonks.execution.brokers.ibkr.flex import (
    FlexCashTransaction,
    FlexClient,
    FlexError,
    FlexStatement,
    FlexTrade,
)
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig, IbkrGatewayConfig
from stonks.execution.brokers.ibkr.statements import cached_statements, clear_statement_cache
from stonks.logging import get_logger

_log = get_logger("stonks.connections.providers.ibkr")

T = TypeVar("T")


# ---- shared gateway sessions ----------------------------------------------------------


class _Borrowed:
    """A shared ``IbClient`` a broker may use but never close."""

    def __init__(self, client: IbClient) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def close(self) -> None:
        """The session outlives the broker: nothing to do."""


_SESSIONS: dict[tuple[str, int, int, Any], IbClient] = {}
_SESSIONS_LOCK = threading.Lock()


def _shared(factory: ClientFactory) -> ClientFactory:
    def build(endpoint: IbEndpoint) -> IbClient:
        key = (endpoint.host, endpoint.port, endpoint.client_id, factory)
        with _SESSIONS_LOCK:
            client = _SESSIONS.get(key)
            if client is None:
                client = factory(endpoint)
                _SESSIONS[key] = client
        return _Borrowed(client)  # type: ignore[return-value]

    return build


def reset_sessions() -> None:
    """Close and forget every shared session (tests, shutdown)."""
    with _SESSIONS_LOCK:
        clients = list(_SESSIONS.values())
        _SESSIONS.clear()
    for client in clients:
        close = getattr(client, "close", None)
        try:
            if callable(close):
                close()
            else:
                client.disconnect()
        except Exception as exc:  # a dead session must not block the others
            _log.warning("ibkr.session_close_failed", error=type(exc).__name__)


# ---- Flex statements, cached per query ------------------------------------------------
# The cache lives in ``execution/brokers/ibkr/statements.py``: the sync and the
# reconciliation checks share it.


def _flex_statements(client: FlexClient) -> list[FlexStatement]:
    return cached_statements(client)


def clear_flex_cache() -> None:
    clear_statement_cache()


#: Flex cash transaction types -> our activity kinds (others are ``other``).
_CASH_KINDS: dict[str, ActivityKind] = {
    "dividends": "dividend",
    "payment in lieu of dividends": "dividend",
    "broker interest received": "interest",
    "broker interest paid": "interest",
    "bond interest received": "interest",
    "bond interest paid": "interest",
    "other fees": "fee",
    "commission adjustments": "fee",
    "advisor fees": "fee",
}


# ---- the provider -----------------------------------------------------------------------


@register_provider("ibkr")
class IbkrConnection(BrokerConnection):
    display_name: ClassVar[str] = "Interactive Brokers"
    auth_flow = "api_key"
    #: The name of a ``[brokers.ibkr.gateways]`` entry. Not a secret.
    credential_fields = ("gateway",)
    has_paper: ClassVar[bool] = True
    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {
            Capability.READ_BALANCES,
            Capability.READ_POSITIONS,
            Capability.READ_ACTIVITY,
            Capability.TRADE,
            Capability.SHORT,
        }
    )
    #: The gateway paces itself (``session.py``). These buckets keep a burst
    #: of syncs from crowding the tick.
    rate_limit: ClassVar[RateLimit] = RateLimit(per_minute=600, per_connection_per_minute=120)

    def __init__(
        self,
        gateway: str,
        gw: IbkrGatewayConfig,
        config: IbkrBrokerConfig,
        context: ProviderContext,
        factory: ClientFactory,
        flex: FlexClient | None,
    ) -> None:
        self.gateway = gateway
        self.gw = gw
        self._config = config
        self._context = context
        self._factory = factory
        self._flex = flex
        self._reader: IbkrBroker | None = None

    @classmethod
    def check_configured(cls, config: ConnectionsConfig) -> None:
        if not config.ibkr.gateways:
            raise ProviderNotConfigured(
                "ibkr needs an IB Gateway under [brokers.ibkr.gateways] (deploy/ibkr/README.md)"
            )

    @classmethod
    def open(cls, credentials: Credentials, context: ProviderContext) -> Self:
        config = context.config.ibkr
        name = credentials["gateway"].strip()
        try:
            _, gw = pick_gateway(config, gateway=name)
        except BrokerError as exc:
            raise ProviderError(str(exc), status=404) from None
        factory = _shared(context.transport or default_client_factory)
        flex = context.extra.get("flex")
        if flex is None:
            flex = FlexClient.from_env(config.flex)
        return cls(name, gw, config, context, factory, flex)

    # ---- plumbing -------------------------------------------------------------------

    def _call(self, op: str, fn: Callable[[], T]) -> T:
        if self._context.limiter is not None:
            self._context.limiter.acquire(self._context.connection_id)
        try:
            return fn()
        except BrokerUnavailableError as exc:
            raise ProviderUnavailable(f"ibkr {op}: {exc}") from None
        except LiveTradingRefusedError as exc:
            # a wrong or missing account needs a person, like refused keys
            raise ProviderAuthError(f"ibkr {op}: {exc}") from None
        except BrokerError as exc:
            raise ProviderError(f"ibkr {op}: {exc}") from None

    def _broker(self) -> IbkrBroker:
        if self._reader is None:
            self._reader = connect_ibkr(
                self._config, gateway=self.gateway, role="sync", client_factory=self._factory
            )
        return self._reader

    def _account(self, account_id: str) -> IbkrBroker:
        broker = self._broker()
        found = self._call("account check", lambda: broker.account_id)
        if account_id != found:
            raise ProviderError(f"unknown IBKR account on gateway {self.gateway!r}", status=404)
        return broker

    # ---- reads ----------------------------------------------------------------------

    def accounts(self) -> list[ExternalAccount]:
        broker = self._broker()
        account = self._call("account check", lambda: broker.account_id)
        state = self._call("account values", broker.fetch_account)
        return [
            ExternalAccount(
                id=account,
                name=f"IBKR {self.gw.mode} ({self.gateway})",
                currency=state.currency,
                institution="Interactive Brokers",
                number_mask=mask_number(account),
            )
        ]

    def balances(self, account_id: str) -> AccountBalances:
        broker = self._account(account_id)
        state = self._call("account values", broker.fetch_account)
        return AccountBalances(
            currency=state.currency,
            cash=state.cash,
            buying_power=state.buying_power,
            total_value=state.equity,
        )

    def positions(self, account_id: str) -> list[ExternalPosition]:
        broker = self._account(account_id)
        raw = self._call("positions", lambda: broker.client.positions(account_id))
        out: list[ExternalPosition] = []
        for p in raw:
            if abs(p.position) < 1e-12 or (p.account and p.account != account_id):
                continue
            out.append(
                ExternalPosition(
                    raw_symbol=p.contract.local_symbol or p.contract.symbol,
                    ticker=self._ticker(broker, p.contract),
                    quantity=float(p.position),
                    currency=p.contract.currency,
                )
            )
        return out

    def activities(self, account_id: str, since: date) -> list[Activity]:
        broker = self._account(account_id)
        found: dict[str, Activity] = {}
        for e in self._call("executions", broker.client.executions):
            if e.account and e.account != account_id:
                continue
            act = _execution_activity(e, account_id, self._ticker(broker, e.contract))
            if act.trade_date is None or act.trade_date >= since:
                found[act.provider_activity_id] = act
        for act in self._flex_activities(account_id):
            if act.trade_date is None or act.trade_date >= since:
                found.setdefault(act.provider_activity_id, act)
        return sorted(
            found.values(), key=lambda a: (a.trade_date or date.min, a.provider_activity_id)
        )

    def _flex_activities(self, account_id: str) -> list[Activity]:
        if self._flex is None:
            return []
        try:
            statements = _flex_statements(self._flex)
        except FlexError as exc:
            # optional: a Flex outage never fails the sync
            _log.warning("ibkr.flex_failed", connection_id=self._context.connection_id,
                         error=str(exc))  # fmt: skip
            return []
        out: list[Activity] = []
        for st in statements:
            for t in st.trades:
                if (t.account_id or st.account_id) == account_id:
                    out.append(_flex_trade_activity(t, account_id))
            for c in st.cash_transactions:
                if (c.account_id or st.account_id) == account_id:
                    out.append(_flex_cash_activity(c, account_id))
        return out

    def _ticker(self, broker: IbkrBroker, contract: IbContract) -> str | None:
        return broker.resolver.ticker_for(contract.con_id) or ticker_for_contract(contract)

    # ---- trading --------------------------------------------------------------------

    def trader(self, account_id: str) -> IbkrBroker:
        """An ``IbkrBroker`` on the tick's client id, trading ``account_id``
        only. The contract cache and ``orderRef`` lookup use the state DB
        the caller passes as ``extra["state"]`` (the tick's)."""
        self.require(Capability.TRADE)
        if self.gw.account_id is not None and account_id != self.gw.account_id:
            raise ProviderError(
                f"gateway {self.gateway!r} trades another account than {account_id}", status=404
            )
        extra: Mapping[str, Any] = self._context.extra
        broker = connect_ibkr(
            self._config,
            gateway=self.gateway,
            role="tick",
            state=extra.get("state"),
            client_factory=self._factory,
            borrow_fees=extra.get("borrow_fees"),
        )
        broker.expected_account = account_id
        return broker

    def close(self) -> None:
        """Nothing to release: the gateway session is shared."""
        self._reader = None


# ---- activity mapping ---------------------------------------------------------------------


def _execution_activity(e: IbExecution, account_id: str, ticker: str | None) -> Activity:
    sign = 1.0 if e.side.upper() in ("BOT", "BUY") else -1.0
    qty = sign * float(e.shares)
    fee = abs(e.commission) if e.commission is not None else None
    return Activity(
        provider_activity_id=f"exec:{e.exec_id}",
        account_id=account_id,
        kind="trade",
        trade_date=e.time.date(),
        raw_symbol=e.contract.local_symbol or e.contract.symbol,
        ticker=ticker,
        quantity=qty,
        price=float(e.price),
        amount=-qty * float(e.price) - (fee or 0.0),
        fee=fee,
        currency=e.contract.currency,
    )


def _flex_ticker(symbol: str, listing_exchange: str | None, currency: str | None) -> str | None:
    contract = IbContract(
        con_id=0,
        symbol=symbol,
        sec_type="STK",
        currency=currency or "",
        primary_exchange=listing_exchange,
    )
    return ticker_for_contract(contract)


def _flex_trade_activity(t: FlexTrade, account_id: str) -> Activity:
    ticker = (
        _flex_ticker(t.symbol, t.listing_exchange, t.currency)
        if (t.asset_class or "STK") == "STK"
        else None
    )
    fee = abs(t.commission) if t.commission is not None else None
    amount = t.proceeds if t.proceeds is not None else -t.quantity * t.price
    return Activity(
        provider_activity_id=f"exec:{t.exec_id}" if t.exec_id else f"trade:{t.trade_id}",
        account_id=account_id,
        kind="trade",
        trade_date=t.trade_date,
        raw_symbol=t.symbol or None,
        ticker=ticker,
        quantity=t.quantity,
        price=t.price,
        amount=amount - (fee or 0.0),
        fee=fee,
        currency=t.currency,
        settle_date=t.settle_date,
        description=t.description,
    )


def _flex_cash_activity(c: FlexCashTransaction, account_id: str) -> Activity:
    vendor = c.type.strip().lower()
    kind: ActivityKind = _CASH_KINDS.get(vendor, "other")
    if vendor == "deposits/withdrawals":
        kind = "deposit" if c.amount >= 0 else "withdrawal"
    ticker = _flex_ticker(c.symbol, None, c.currency) if c.symbol else None
    return Activity(
        provider_activity_id=f"cash:{c.transaction_id}",
        account_id=account_id,
        kind=kind,
        trade_date=c.date,
        raw_symbol=c.symbol,
        ticker=ticker,
        amount=c.amount,
        currency=c.currency,
        settle_date=c.settle_date,
        description=c.description or c.type or None,
    )


__all__ = ["IbkrConnection", "clear_flex_cache", "reset_sessions"]
