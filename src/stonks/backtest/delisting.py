"""Delisted holdings in a backtest (P14).

A name that stops trading must leave the book. Left alone it sits frozen at
its last mark for the rest of the run, and the trade ledger never closes
its lot. So the engine settles it for cash on the first bar after its last
bar, once the name is

- on or past ``instruments.delisted_date``, or
- past the end of its membership of ``BacktestConfig.universe_id`` with no
  later bar in the window.

The position settles at ``last_close x (1 + delisting_return)``. The return
is ``[backtest.delisting] return`` (default 0.0, the last close). A negative
value such as ``-0.3`` models the haircut a forced delisting often brings.
Orders still queued for the name are dropped. Each event is listed in
``BacktestReport.delistings``, and ``stonks.backtest.trades`` closes the
lots at its price.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DelistingReason = Literal["delisted_date", "membership"]


class DelistingSettings(BaseModel):
    """``[backtest.delisting]``."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    #: Return applied to the last close when a held name delists (``-1``: a
    #: total loss, ``0``: the last close).
    return_: float = Field(default=0.0, ge=-1.0, alias="return")


@dataclass(frozen=True)
class DelistingRecord:
    """One held position settled because its name delisted."""

    timestamp: datetime
    ticker: str
    #: Signed quantity settled (negative for a short).
    quantity: float
    last_close: float
    delisting_return: float
    #: ``last_close x (1 + delisting_return)``.
    price: float
    #: Cash credited (debited for a short): ``quantity x price``.
    cash_delta: float
    reason: DelistingReason
    #: Client ids of the queued orders for the name that were dropped.
    dropped_orders: tuple[str, ...] = ()
