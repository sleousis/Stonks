"""Gray & Vogel momentum features (*Quantitative Momentum*, BL-38).

- :func:`trailing_return_skip`: the 12-2 return ``C[t-skip] / C[t-formation]
  - 1``, which skips the most recent month (its one-month reversal);
- :func:`information_discreteness`: Da, Gurun & Warachka's "frog in the pan"
  ``ID = sign(r) * (%neg - %pos)`` over the daily returns of the same
  window. A low (negative) ID means the move came as many small steps, which
  investors underreact to; a high ID means a few jumps, which get priced;
- :func:`is_quarter_rebalance_day`: the last session of a rebalance month.

All inputs are closes up to the decision bar (oldest first); nothing reads
past the last element.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

import numpy as np

from stonks.features.sessions import last_session_of_month

__all__ = [
    "formation_window",
    "information_discreteness",
    "is_quarter_rebalance_day",
    "trailing_return_skip",
]

#: Gray & Vogel rebalance at the end of Feb, May, Aug and Nov, a month
#: before the quarter-end window dressing and tax-loss selling.
QUARTER_REBALANCE_MONTHS: tuple[int, ...] = (2, 5, 8, 11)


def _check(formation_bars: int, skip_bars: int) -> None:
    if skip_bars < 0 or formation_bars <= skip_bars:
        raise ValueError(
            f"need 0 <= skip_bars < formation_bars, got {skip_bars=} {formation_bars=}"
        )


def formation_window(closes: np.ndarray, formation_bars: int, skip_bars: int) -> np.ndarray:
    """Closes ``C[t-formation] .. C[t-skip]`` (``formation - skip + 1`` of
    them), where ``t`` is the last element. Shorter input gives what exists."""
    _check(formation_bars, skip_bars)
    closes = np.asarray(closes, dtype=float)
    end = len(closes) - skip_bars
    return closes[max(0, end - (formation_bars - skip_bars) - 1) : max(0, end)]


def trailing_return_skip(
    closes: np.ndarray, formation_bars: int = 252, skip_bars: int = 21
) -> float | None:
    """``C[t-skip] / C[t-formation] - 1`` with ``t`` the last close; ``None``
    with fewer than ``formation_bars + 1`` closes or a non-positive price."""
    _check(formation_bars, skip_bars)
    closes = np.asarray(closes, dtype=float)
    if len(closes) < formation_bars + 1:
        return None
    start, end = closes[-1 - formation_bars], closes[-1 - skip_bars]
    if not (start > 0 and end > 0):
        return None
    return float(end / start - 1.0)


def information_discreteness(closes: np.ndarray) -> float | None:
    """``sign(r) * (%neg - %pos)`` over the daily returns of ``closes``, with
    ``r`` its total return; zero-return days count as neither. In ``[-1, 1]``;
    ``None`` with fewer than two closes."""
    closes = np.asarray(closes, dtype=float)
    if len(closes) < 2:
        return None
    rets = np.diff(closes)
    n = len(rets)
    pct_neg = float(np.sum(rets < 0)) / n
    pct_pos = float(np.sum(rets > 0)) / n
    sign = float(np.sign(closes[-1] - closes[0]))
    return sign * (pct_neg - pct_pos)


def is_quarter_rebalance_day(day: date, months: Iterable[int] = QUARTER_REBALANCE_MONTHS) -> bool:
    """True on the last session of a month in ``months``. Answered from the
    exchange calendar, so it never needs the next bar."""
    return day.month in set(months) and day == last_session_of_month(day.year, day.month)
