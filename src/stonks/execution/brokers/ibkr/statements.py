"""IBKR Flex statements as vendor-free broker statements (roadmap 19.15).

Reconciliation compares the broker's official record with the ledger
(``execution/drift.py``). It reads :class:`~stonks.execution.drift.BrokerStatement`
values, never Flex types, so another broker's statement plugs in the same
way. This module maps a :class:`FlexStatement` to one and keeps the one
statement cache that the ``ibkr`` connection's sync and the checks share
(Flex is slow and rate limited, and a statement changes once a day).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from stonks.execution.brokers.ibkr.flex import (
    FlexCashTransaction,
    FlexClient,
    FlexStatement,
    FlexTrade,
)
from stonks.execution.drift import BrokerStatement, StatementCash, StatementExecution

__all__ = [
    "cached_statements",
    "clear_statement_cache",
    "statement_source",
    "to_broker_statement",
]

#: Flex cash transaction types that are dividends (lower case).
_DIVIDENDS = frozenset({"dividends", "payment in lieu of dividends"})

_CACHE: dict[str, tuple[float, list[FlexStatement]]] = {}
_LOCK = threading.Lock()


def to_broker_statement(s: FlexStatement) -> BrokerStatement:
    executions = tuple(e for e in (_execution(t) for t in s.trades) if e is not None)
    return BrokerStatement(
        account_id=s.account_id,
        from_date=s.from_date,
        to_date=s.to_date,
        executions=executions,
        cash=tuple(_cash(c) for c in s.cash_transactions),
    )


def _execution(t: FlexTrade) -> StatementExecution | None:
    if not t.exec_id:
        return None
    cash = t.proceeds if t.proceeds is not None else -t.quantity * t.price
    return StatementExecution(
        exec_id=t.exec_id,
        quantity=t.quantity,
        symbol=t.symbol,
        order_ref=t.order_ref,
        trade_date=t.trade_date,
        settle_date=t.settle_date,
        cash=cash,
        # IBKR reports a cost as a negative commission
        commission=-t.commission if t.commission is not None else None,
        commission_currency=t.commission_currency,
        currency=t.currency,
    )


def _cash(c: FlexCashTransaction) -> StatementCash:
    kind = "dividend" if c.type.strip().lower() in _DIVIDENDS else "other"
    return StatementCash(
        kind=kind,
        amount=c.amount,
        date=c.date,
        settle_date=c.settle_date,
        currency=c.currency,
        symbol=c.symbol,
    )


def cached_statements(client: FlexClient) -> list[FlexStatement]:
    """The query's statements, reused for ``refresh_hours``. Raises
    ``FlexError`` when a fetch fails."""
    key = str(client.settings.query_id)
    ttl = client.settings.refresh_hours * 3600.0
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]
    statements = client.fetch()
    with _LOCK:
        _CACHE[key] = (time.monotonic(), statements)
    return statements


def clear_statement_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def statement_source(client: FlexClient) -> Callable[[], list[BrokerStatement]]:
    """A callable the reconciliation checks read statements from."""

    def read() -> list[BrokerStatement]:
        return [to_broker_statement(s) for s in cached_statements(client)]

    return read
