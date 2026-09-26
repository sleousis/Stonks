"""Applying corporate actions to a simulated portfolio.

The backtest trades and marks on raw prices, so it must do what a broker
does on each ex-date:

- **split** of ratio ``r``: held quantity is multiplied by ``r`` (the raw
  price divides by ``r`` on the same bar, so value is unchanged), and
  orders still queued for that ticker have their quantity multiplied by
  ``r`` (limit price divided by ``r``), as brokers adjust open orders;
- **cash dividend** ``D``: ``quantity x D x (1 - withholding_rate)`` is
  credited to cash (debited for a short position).

An event takes effect on the first bar of its ticker dated on or after the
ex-date, before that bar's fills, which is also where the signal-side
price adjustment starts (``stonks.features.price_adjustment``). A buy that
fills at the ex-date open therefore gets no dividend, and a sell at that
open still does. On one ex-date, splits apply before dividends.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

from stonks.core.corporate_actions import (
    CorporateAction,
    CorporateActionKind,
    CorporateActions,
    Split,
)
from stonks.core.types import Order, Portfolio


@dataclass(frozen=True)
class CorporateActionRecord:
    """One corporate action applied to a held position, for the report."""

    timestamp: datetime
    ticker: str
    kind: CorporateActionKind
    #: Split ratio, or cash dividend per share.
    value: float
    quantity_before: float
    quantity_after: float
    #: Cash credited (negative: debited) after withholding; 0 for splits.
    cash_delta: float


class CorporateActionSchedule:
    """Hands out each ticker's events once, on its first bar dated on or
    after the ex-date."""

    def __init__(self, actions: CorporateActions) -> None:
        self._pending: dict[str, list[CorporateAction]] = {
            ticker: list(events) for ticker, events in actions.by_ticker.items() if events
        }

    def due(self, ticker: str, bar_date: date) -> list[CorporateAction]:
        queue = self._pending.get(ticker)
        if not queue:
            return []
        n = 0
        while n < len(queue) and queue[n].ex_date <= bar_date:
            n += 1
        due, self._pending[ticker] = queue[:n], queue[n:]
        return due


def apply_to_portfolio(
    portfolio: Portfolio,
    action: CorporateAction,
    as_of: datetime,
    *,
    withholding_rate: float = 0.0,
) -> CorporateActionRecord | None:
    """Apply ``action`` to ``portfolio`` in place; ``None`` when the
    portfolio has no position in the ticker."""
    held = portfolio.positions.get(action.ticker, 0.0)
    if held == 0:
        return None
    if isinstance(action, Split):
        after = held * action.ratio
        portfolio.positions[action.ticker] = after
        return CorporateActionRecord(
            timestamp=as_of,
            ticker=action.ticker,
            kind="split",
            value=action.ratio,
            quantity_before=held,
            quantity_after=after,
            cash_delta=0.0,
        )
    cash = held * action.amount * (1.0 - withholding_rate)
    portfolio.cash += cash
    return CorporateActionRecord(
        timestamp=as_of,
        ticker=action.ticker,
        kind="dividend",
        value=action.amount,
        quantity_before=held,
        quantity_after=held,
        cash_delta=cash,
    )


def adjust_orders_for_split(orders: Sequence[Order], split: Split) -> list[Order]:
    """Queued orders for the split ticker re-expressed in post-split shares."""
    out: list[Order] = []
    for order in orders:
        if order.ticker != split.ticker:
            out.append(order)
            continue
        limit = None if order.limit_price is None else order.limit_price / split.ratio
        out.append(replace(order, quantity=order.quantity * split.ratio, limit_price=limit))
    return out
