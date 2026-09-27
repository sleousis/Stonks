"""Corporate actions (splits, cash dividends) as vendor-agnostic values.

Two consumers need them:

- **signals** read price history back-adjusted so a split or dividend
  doesn't look like a crash (``stonks.features.price_adjustment``);
- **backtest accounting** trades on raw prices and applies each event to
  the held position on its ex-date (``stonks.backtest.corporate_actions``).

Both read events through :class:`CorporateActionsProvider`, so a new vendor
or asset class plugs in by supplying events, never by special-casing the
engine loop.

Data contract
-------------
- ``Split.ratio`` is new shares per old share: ``10.0`` for a 10:1 forward
  split, ``0.1`` for a 1:10 reverse split.
- ``Dividend.amount`` is the cash paid per share **as the share existed on
  the ex-date** (unadjusted for later splits), in the instrument's quote
  currency.
- ``Dividend.declared_on`` is the day the company announced it, or
  ``None`` when the source does not say. Anything that looks ahead to an
  upcoming dividend reads only the ones declared by the decision day (P12).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

#: Which price series a bar accessor returns. ``adjusted`` back-adjusts
#: history for splits and dividends (for signals); ``raw`` is what the
#: market actually quoted (for fills and marks).
PriceBasis = Literal["adjusted", "raw"]

CorporateActionKind = Literal["split", "dividend"]


@dataclass(frozen=True)
class Split:
    ticker: str
    ex_date: date
    #: New shares per old share (10.0 = 10:1 forward, 0.1 = 1:10 reverse).
    ratio: float

    def __post_init__(self) -> None:
        if not self.ratio > 0:
            raise ValueError(f"Split.ratio must be positive, got {self.ratio}")


@dataclass(frozen=True)
class Dividend:
    ticker: str
    ex_date: date
    #: Cash per share on the ex-date (not adjusted for later splits).
    amount: float
    #: The declaration day, or ``None`` when unknown. Not part of equality,
    #: so the same payment compares equal whether or not a source knows it.
    declared_on: date | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError(f"Dividend.amount must be non-negative, got {self.amount}")


CorporateAction = Split | Dividend


@dataclass(frozen=True)
class CorporateActions:
    """Events per ticker, each list sorted by ex-date (splits before
    dividends on the same ex-date)."""

    by_ticker: dict[str, tuple[CorporateAction, ...]] = field(
        default_factory=dict[str, tuple[CorporateAction, ...]]
    )

    @classmethod
    def from_events(cls, events: Iterable[CorporateAction]) -> CorporateActions:
        grouped: dict[str, list[CorporateAction]] = {}
        for event in events:
            grouped.setdefault(event.ticker, []).append(event)
        return cls(
            {
                ticker: tuple(sorted(evs, key=lambda e: (e.ex_date, isinstance(e, Dividend))))
                for ticker, evs in grouped.items()
            }
        )

    def for_ticker(self, ticker: str) -> tuple[CorporateAction, ...]:
        return self.by_ticker.get(ticker, ())

    def __bool__(self) -> bool:
        return any(self.by_ticker.values())


class CorporateActionsProvider(Protocol):
    """Loads every known corporate action for ``tickers`` in one call."""

    def load(self, tickers: Sequence[str]) -> CorporateActions: ...


class NoCorporateActions:
    """Provider for sources with no corporate-action data (always empty)."""

    def load(self, tickers: Sequence[str]) -> CorporateActions:
        return CorporateActions()
