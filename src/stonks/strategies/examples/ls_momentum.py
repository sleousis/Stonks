"""LongShortMomentum: the classic cross-sectional momentum spread
(roadmap 16.3; Jegadeesh and Titman 1993; Asness, Moskowitz and Pedersen,
*Value and Momentum Everywhere*; Daniel and Moskowitz on momentum crashes).

Rules, on adjusted daily closes:

1. **12-1 momentum.** ``r = C[t-skip] / C[t-formation] - 1``: the past
   year's return without the most recent month (the one-month reversal).
2. **Winners and losers.** Rank the universe by ``r``. The top ``quantile``
   are winners (at least one name), the bottom ``quantile`` losers; a name
   is never both.
3. **Scores.** A winner scores ``r - median(r)`` and a loser the same
   (negative) value, floored away from 0 so the sign always survives. The
   loser leg exists only with ``short_mode = "short"`` (off by default), so
   the long-only strategy holds just the winners.
4. **Book.** On the last session of each month in ``rebalance_months``
   ``decide`` sets equal weights inside each leg: ``gross / 2`` per leg
   when both exist (a dollar-neutral spread), else the one leg at ``gross``
   (capped at 1.0 for a long-only book). Other days hold.

Universe: the ``universe`` param, or every daily-bar ticker in the lake. A
ticker ranks only with ``formation_bars + 1`` bars of history and a fresh
last bar.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.momentum import is_quarter_rebalance_day, trailing_return_skip
from stonks.portfolio.orders import orders_from_targets
from stonks.strategies._common import LakeBarCaches
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import (
    CrossSectionMemo,
    is_fresh,
    lake_universe,
    parse_universe,
    session_cutoff,
)

_MONTHLY = "1,2,3,4,5,6,7,8,9,10,11,12"
_REBALANCE_MONTHS = [_MONTHLY, "3,6,9,12", "2,5,8,11"]
_EPS = 1e-9
#: Smallest score magnitude, so a winner or loser always keeps its sign.
MIN_SCORE = 1e-6


def select_legs(returns: Mapping[str, float], quantile: float) -> tuple[list[str], list[str]]:
    """``(winners, losers)`` from ``{ticker: r}``: the top and bottom
    ``quantile`` (at least one winner; losers never overlap winners).
    Ties break by ticker."""
    if not returns:
        return [], []
    n = max(1, math.ceil(quantile * len(returns) - _EPS))
    ranked = sorted(returns, key=lambda t: (-returns[t], t))
    winners = ranked[:n]
    losers = [t for t in reversed(ranked) if t not in winners][:n]
    return winners, losers


class LongShortMomentum(BaseStrategy):
    id = "ls_momentum"
    hypothesis = (
        "Past 12-1 month winners keep beating past losers for months: "
        "investors underreact to news and then chase it (Jegadeesh and "
        "Titman; Asness, Moskowitz and Pedersen). Long the winners and, when "
        "the book may short, short the losers, so the spread earns the "
        "momentum premium with little market beta. We are paid by investors "
        "anchored on old prices. Fails in momentum crashes, when a market "
        "rebound makes the beaten-down losers rally hardest (Daniel and "
        "Moskowitz), which hits the short leg."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 21
    required_history_bars = 253
    short_capable: ClassVar[bool] = True

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        if int(self.params["skip_bars"]) >= int(self.params["formation_bars"]):
            raise ValueError("skip_bars must be shorter than formation_bars")
        self._bar_caches = LakeBarCaches()
        self._memo = CrossSectionMemo()

    def param_metadata(self) -> dict[str, int]:
        return {"required_history_bars": int(self.params["formation_bars"]) + 1}

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="formation_bars",
                kind="int",
                default=252,
                bounds=(126, 504),
                description="Bars back to the start of the momentum window (t-252).",
            ),
            ParameterSpec(
                name="skip_bars",
                kind="int",
                default=21,
                bounds=(0, 63),
                description="Most recent bars skipped (the one-month reversal).",
            ),
            ParameterSpec(
                name="quantile",
                kind="float",
                default=0.2,
                bounds=(0.05, 0.5),
                description="Share of the universe in each leg (winners, losers).",
            ),
            ParameterSpec(
                name="gross",
                kind="float",
                default=1.0,
                bounds=(0.2, 2.0),
                tunable=False,
                description="Gross exposure of the book; a long-only book caps it at 1.0.",
            ),
            ParameterSpec(
                name="rebalance_months",
                kind="categorical",
                default=_MONTHLY,
                bounds=_REBALANCE_MONTHS,
                tunable=False,
                description="Months whose last session rebalances the book.",
            ),
            ParameterSpec(
                name="universe",
                kind="categorical",
                default="",
                bounds=None,
                tunable=False,
                description="Comma-separated tickers ranked together; empty = the lake's.",
            ),
        ]

    # ---- signals -----------------------------------------------------------------

    def score_universe(self, tickers: Sequence[str], as_of: Any, lake: Any) -> dict[str, float]:
        """Signed scores of the winners (and losers, when shorting) of
        ``tickers`` as of ``as_of`` (see the module doc)."""
        returns = {}
        for ticker in dict.fromkeys(tickers):
            r = self._momentum(ticker, as_of, lake)
            if r is not None and math.isfinite(r):
                returns[ticker] = r
        winners, losers = select_legs(returns, float(self.params["quantile"]))
        if not winners:
            return {}
        ordered = sorted(returns.values())
        mid = len(ordered) // 2
        median = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
        out = {t: max(returns[t] - median, MIN_SCORE) for t in winners}
        if self.supports_short:
            out.update({t: min(returns[t] - median, -MIN_SCORE) for t in losers})
        return out

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        r = self._momentum(ticker, as_of, lake) if lake is not None else None
        return Features(values={} if r is None or not math.isfinite(r) else {"momentum": r})

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None:
            return None
        day, _ = session_cutoff(as_of)
        scores = self._memo.get(
            lake, day, lambda: self.score_universe(self._universe(lake), as_of, lake)
        )
        return scores.get(ticker)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day, _ = session_cutoff(as_of)
        months = [int(m) for m in str(self.params["rebalance_months"]).split(",")]
        if not is_quarter_rebalance_day(day, months):
            return []
        shorts = self.supports_short
        longs = [t for r, t in my_picks if r > 0]
        losers = [t for r, t in my_picks if r < 0] if shorts else []
        gross = float(self.params["gross"])
        if not shorts:
            gross = min(gross, 1.0)
        leg = gross / 2.0 if longs and losers else gross
        targets = {t: leg / len(longs) for t in longs}
        targets.update({t: -leg / len(losers) for t in losers})
        return orders_from_targets(
            targets,
            portfolio,
            prices,
            buffer_fraction=0.0,
            as_of=day,
            strategy_id=self.id,
            allow_short=shorts,
        )

    # ---- internals ---------------------------------------------------------------

    def _universe(self, lake: Any) -> list[str]:
        explicit = parse_universe(str(self.params["universe"]))
        if explicit:
            return explicit
        return self._memo.universe(lake, lambda: lake_universe(lake, self.applicable_asset_classes))

    def _momentum(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        formation, skip = int(self.params["formation_bars"]), int(self.params["skip_bars"])
        _, cutoff = session_cutoff(as_of)
        cache = self._bar_caches.for_lake(lake)
        if not is_fresh(cache, ticker, cutoff):
            return None
        closes = cache.last_n_closes(ticker, Interval.DAY_1, cutoff, formation + 1)
        return trailing_return_skip(closes, formation, skip)
