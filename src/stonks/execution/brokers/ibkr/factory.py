"""Settings onto an ``IbkrBroker`` (roadmap 19.2).

:func:`connect_ibkr` picks the gateway (by name, by the portfolio it
serves, or the only one), gives the process role its fixed API client id
(``[brokers.ibkr] client_ids``: tick 11, sync 12, health 13,
reconcile 14), and wires the
contract cache and the ``orderRef`` lookup to the state DB when one is
given. Nothing connects until the broker is first used, so a gateway that
is down fails that call, nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Literal

from stonks.core.clock import SYSTEM_CLOCK, Clock
from stonks.execution.borrow import BorrowSource
from stonks.execution.brokers.base import AccountType, BrokerError
from stonks.execution.brokers.ibkr.borrow import IbkrBorrowSource
from stonks.execution.brokers.ibkr.broker import IbkrBroker
from stonks.execution.brokers.ibkr.client import IbClient, IbEndpoint
from stonks.execution.brokers.ibkr.contracts import (
    ContractResolver,
    InstrumentLookup,
    MemoryContractCache,
    SqliteContractCache,
)
from stonks.execution.brokers.ibkr.settings import IbkrBrokerConfig, IbkrGatewayConfig
from stonks.store.state import SqliteState

Role = Literal["tick", "sync", "health", "reconcile"]
ClientFactory = Callable[[IbEndpoint], IbClient]


def pick_gateway(
    config: IbkrBrokerConfig, *, gateway: str | None = None, portfolio_id: str | None = None
) -> tuple[str, IbkrGatewayConfig]:
    """The gateway named ``gateway``, else the one listing ``portfolio_id``,
    else the only gateway. Anything else is a configuration error."""
    if not config.gateways:
        raise BrokerError("no IB Gateway is configured ([brokers.ibkr.gateways])")
    if gateway is not None:
        found = config.gateways.get(gateway)
        if found is None:
            raise BrokerError(f"no IB Gateway named {gateway!r} in [brokers.ibkr.gateways]")
        return gateway, found
    if portfolio_id is not None:
        for name, gw in sorted(config.gateways.items()):
            if portfolio_id in gw.portfolios:
                return name, gw
    if len(config.gateways) == 1:
        return next(iter(config.gateways.items()))
    raise BrokerError(
        f"several IB Gateways are configured and none lists portfolio {portfolio_id!r}"
    )


def endpoint_for(config: IbkrBrokerConfig, gateway: IbkrGatewayConfig, role: Role) -> IbEndpoint:
    """The endpoint of a process role. The health probe tries once, within
    ``[brokers.ibkr.health] probe_timeout_seconds``, and only reads."""
    health = role == "health"
    probe_timeout = config.health.probe_timeout_seconds
    return IbEndpoint(
        host=gateway.host,
        port=gateway.port,
        client_id=getattr(config.client_ids, role),
        connect_timeout=probe_timeout if health else config.connect_timeout_seconds,
        request_timeout=probe_timeout if health else config.request_timeout_seconds,
        reconnect_deadline=0.0 if health else config.reconnect_deadline_seconds,
        readonly=health,
    )


def default_client_factory(endpoint: IbEndpoint) -> IbClient:
    from stonks.execution.brokers.ibkr.ib_async_client import IbAsyncClient

    return IbAsyncClient(endpoint)


def order_ref_lookup(state: SqliteState) -> Callable[[str], str | None]:
    """Our client id for an ``orderRef`` sent earlier (``orders.broker_ref``)."""

    def lookup(ref: str) -> str | None:
        rows = state.sql("SELECT client_id FROM orders WHERE broker_ref = ? LIMIT 1", [ref])
        return str(rows[0]["client_id"]) if rows else None

    return lookup


def connect_ibkr(
    config: IbkrBrokerConfig,
    *,
    gateway: str | None = None,
    portfolio_id: str | None = None,
    role: Role = "tick",
    state: SqliteState | None = None,
    lookup: InstrumentLookup | None = None,
    account_type: AccountType | None = None,
    clock: Clock = SYSTEM_CLOCK,
    client_factory: ClientFactory = default_client_factory,
    borrow_fees: BorrowSource | None = None,
) -> IbkrBroker:
    """``account_type`` defaults to the gateway's. A margin account may
    short: its broker checks each opening sell against IBKR's locate
    (``IbkrBorrowSource``), with fees from ``borrow_fees`` (the lake's
    ``borrow_rates``, say) when given."""
    _, gw = pick_gateway(config, gateway=gateway, portfolio_id=portfolio_id)
    kind: AccountType = account_type or gw.account_type
    client = client_factory(endpoint_for(config, gw, role))
    cache = SqliteContractCache(state) if state is not None else MemoryContractCache()
    resolver = ContractResolver(
        client,
        cache=cache,
        lookup=lookup,
        clock=clock,
        max_age=timedelta(days=config.contract_max_age_days),
    )
    broker = IbkrBroker(
        client,
        mode=gw.mode,
        account_id=gw.account_id,
        allow_live=config.allow_live,
        resolver=resolver,
        order_settings=config.orders,
        account_type=kind,
        allow_short=kind == "margin",
        ref_lookup=order_ref_lookup(state) if state is not None else None,
        clock=clock,
    )
    if kind == "margin":
        broker.borrow = IbkrBorrowSource(broker, fees=borrow_fees)
    return broker
