"""``IbkrBorrowSource``: today's locate from IBKR, the fee from the lake
(roadmap 19.3, ``docs/design/live-trading.md`` section 2).

A short sale needs two answers:

- **Can we borrow, and how much?** IBKR's shortable ticks (generic tick
  236) for today: the indicator (easy, hard or not shortable) and the
  shares it can lend. Read live through the adapter's ``IbClient``.
- **At what fee?** The daily short stock file in the lake's
  ``borrow_rates`` (``LakeBorrowSource``), passed in as ``fees``.

The broker's status wins over the file. A hard to borrow name without a
known fee gets no quote (its cost is unknown), an easy one falls back to
``general_fee_rate``. A day other than today reads ``fees`` alone, since
IBKR only answers for now. Any failure (gateway down, unknown ticker, no
answer) is no quote, and no quote means no short.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, ClassVar

from stonks.core.clock import today
from stonks.core.types import AssetClass
from stonks.execution.borrow import BorrowQuote, BorrowSource, BorrowStatus
from stonks.execution.brokers.base import BrokerError, UnsupportedTickerError
from stonks.execution.brokers.ibkr.client import IbApiError, IbShortableClient
from stonks.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from stonks.execution.brokers.ibkr.broker import IbkrBroker

_log = get_logger("stonks.execution.brokers.ibkr.borrow")

#: IBKR's shortable indicator thresholds (generic tick 236).
EASY_ABOVE = 2.5
HARD_ABOVE = 1.5


def borrow_status(indicator: float | None) -> BorrowStatus | None:
    """Our status for IBKR's shortable indicator, ``None`` without one."""
    if indicator is None or indicator != indicator:
        return None
    if indicator > EASY_ABOVE:
        return "easy"
    if indicator > HARD_ABOVE:
        return "hard"
    return "none"


class IbkrBorrowSource(BorrowSource):
    has_history: ClassVar[bool] = True

    def __init__(
        self,
        broker: IbkrBroker,
        *,
        fees: BorrowSource | None = None,
        general_fee_rate: float = 0.005,
    ) -> None:
        self._broker = broker
        self.fees = fees
        self.general_fee_rate = general_fee_rate
        #: (day, ticker) -> (status, shares) or None for "no answer"
        self._live: dict[tuple[date, str], tuple[BorrowStatus, float | None] | None] = {}

    def quote(
        self, ticker: str, day: date, asset_class: AssetClass = "equity"
    ) -> BorrowQuote | None:
        fee_quote = self.fees.quote(ticker, day, asset_class) if self.fees is not None else None
        if day != today(self._broker.clock):
            return fee_quote
        live = self._live_answer(ticker, day)
        if live is None:
            return None
        status, shares = live
        if status == "none":
            return BorrowQuote("none", 0.0, 0.0 if shares is None else shares)
        if fee_quote is not None:
            fee = fee_quote.fee_rate_annual
        elif status == "easy":
            fee = self.general_fee_rate
        else:
            _log.info("ibkr.borrow.hard_without_fee", ticker=ticker)
            return None
        available = shares if shares is not None else (
            fee_quote.available_shares if fee_quote is not None else None
        )  # fmt: skip
        return BorrowQuote(status, fee, available)

    def _live_answer(self, ticker: str, day: date) -> tuple[BorrowStatus, float | None] | None:
        key = (day, ticker)
        if key not in self._live:
            self._live[key] = self._ask(ticker)
        return self._live[key]

    def _ask(self, ticker: str) -> tuple[BorrowStatus, float | None] | None:
        broker = self._broker
        client = broker.client
        if not isinstance(client, IbShortableClient):
            return None
        try:
            broker.ensure_ready()
            resolved = broker.resolver.resolve(ticker)
            answers = client.shortability([resolved.contract])
        except (
            BrokerError,
            UnsupportedTickerError,
            IbApiError,
            ConnectionError,
            TimeoutError,
        ) as exc:
            _log.warning("ibkr.borrow.no_answer", ticker=ticker, error=str(exc))
            return None
        for answer in answers:
            if answer.con_id != resolved.con_id:
                continue
            status = borrow_status(answer.indicator)
            if status is None:
                return None
            shares = answer.shares
            if shares is not None and (shares != shares or shares < 0):
                shares = None
            return status, shares
        return None
