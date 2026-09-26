"""ATR risk-parity constructor (Clenow, *Stocks on the Move*; BL-39).

Every position is sized so a typical day moves it by the same slice of the
book: ``shares = equity * risk_factor / ATR``, i.e. the target weight is
``risk_factor * price / ATR``. With the default ``risk_factor = 0.001`` a
one-ATR day moves each position by 10 bps of equity.

Clenow's book management, stateless:

- a held name that still has a positive signal keeps its current shares;
  on resize weeks (every ``resize_every_weeks`` Monday-anchored weeks) it is
  reset to parity when it is more than ``resize_tolerance`` off;
- held names are funded first; new names are then bought best score first
  while the book has room (``max_gross``), top-down as the book does it. A
  name that doesn't fit the room left is skipped (a smaller one further
  down may still fit) and reported in ``meta["unfunded"]``;
- a held name without a positive signal gets no target, so it is sold.

Signals are used raw (``signal_method = "raw"``), for ranking and sign only.
ATRs come from :class:`AtrConstructionInput`; given a plain
:class:`ConstructionInput` the constructor approximates the daily move from
the annual volatility, ``price * sigma / sqrt(252)``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import Field

from stonks.features.sessions import week_index
from stonks.portfolio.base import (
    ConstructionInput,
    ConstructorSettings,
    PortfolioConstructor,
    TargetBook,
    Ticker,
    combine_signals,
    register_constructor,
)

_TRADING_DAYS = 252.0


@dataclass(frozen=True, kw_only=True)
class AtrConstructionInput(ConstructionInput):
    """A :class:`ConstructionInput` with each ticker's average true range
    (price units, same basis as ``prices``)."""

    atrs: Mapping[Ticker, float] = field(default_factory=dict)


class AtrParitySettings(ConstructorSettings):
    risk_factor: float = Field(default=0.001, gt=0.0, le=0.1)
    resize_every_weeks: int = Field(default=2, ge=1)
    resize_tolerance: float = Field(default=0.10, ge=0.0)


def _positive(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


@register_constructor("atr_parity")
class AtrParity(PortfolioConstructor):
    """Equal daily risk per position: ``w_i = risk_factor * price_i / ATR_i``."""

    signal_method = "raw"
    Settings = AtrParitySettings

    def target_weights(self, inp: ConstructionInput) -> TargetBook:
        s: AtrParitySettings = self.settings  # type: ignore[assignment]
        combined, attribution = combine_signals(inp)
        equity = inp.portfolio.total_value(inp.prices)
        resize = week_index(inp.as_of) % s.resize_every_weeks == 0
        meta: dict = {"resize": resize, "unfunded": []}
        if not equity > 0:
            return self.finalize({}, meta=meta)

        def parity(ticker: Ticker) -> float | None:
            price = inp.prices[ticker]
            atr = self._atr(inp, ticker)
            return None if atr is None else s.risk_factor * price / atr

        wanted = [t for t, v in combined.items() if v > 0 and inp.tradable(t)]
        held = sorted(t for t in wanted if inp.portfolio.positions.get(t, 0.0) > 0)
        weights: dict[Ticker, float] = {}
        for ticker in held:
            current = inp.portfolio.positions[ticker] * inp.prices[ticker] / equity
            target = parity(ticker)
            if (
                resize
                and target is not None
                and abs(current - target) > s.resize_tolerance * target
            ):
                current = target
            weights[ticker] = current

        room = s.max_gross - sum(weights.values())
        new = sorted((t for t in wanted if t not in weights), key=lambda t: (-combined[t], t))
        for ticker in new:
            target = parity(ticker)
            if target is None:
                continue
            if target > room + 1e-12:
                meta["unfunded"].append(ticker)
                continue
            weights[ticker] = target
            room -= target
        return self.finalize(weights, attribution, meta)

    @staticmethod
    def _atr(inp: ConstructionInput, ticker: Ticker) -> float | None:
        atr = _positive(getattr(inp, "atrs", {}).get(ticker))
        if atr is not None:
            return atr
        sigma = inp.vol(ticker)
        if sigma is None:
            return None
        return inp.prices[ticker] * sigma / math.sqrt(_TRADING_DAYS)
