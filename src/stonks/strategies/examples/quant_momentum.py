"""QuantMomentum — Gray & Vogel's *Quantitative Momentum* (BL-38).

Rules, on adjusted daily closes:

1. **12-2 momentum.** ``r = C[t-21] / C[t-252] - 1``: the past year's
   return skipping the most recent month (the one-month reversal).
2. **Top decile.** Keep the best ``top_pct`` of the universe by ``r``.
3. **Frog in the pan.** Among those winners keep the ``id_keep_pct`` with the
   lowest information discreteness ``ID = sign(r) * (%neg - %pos)`` over the
   same window: momentum built from many small moves, which investors
   underreact to, rather than a few headline jumps.
4. **Equal weight, long only**, through the ``equal_weight_top_n``
   constructor.
5. **Quarterly rebalance** on the last session of February, May, August and
   November, ahead of quarter-end window dressing and tax-loss selling. On
   other days ``decide`` holds.

Optional ``absolute_momentum`` drops winners whose own ``r <= 0``. There
is no market filter here: compose one with a regime wrapper.

Universe: ``estimate_return`` ranks the whole universe (the ``universe``
param, or every daily-bar ticker in the lake) once per day and answers
1.0 for the selected names, ``None`` otherwise, so ``decide`` only needs the
picks. :meth:`score_universe` is the same selection over explicit tickers.
A ticker is ranked only with ``formation_bars + 1`` bars of history (names
listed mid-window wait) and a last bar no older than
:data:`~stonks.strategies.examples._cross_section.MAX_STALENESS_DAYS`
(delisted names drop out). Run it on daily bars with
``rebalance_every_bars = 1`` so the rebalance session is not skipped.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.features.momentum import (
    formation_window,
    information_discreteness,
    is_quarter_rebalance_day,
    trailing_return_skip,
)
from stonks.portfolio.base import ConstructionInput, get_constructor
from stonks.strategies._common import LakeBarCaches
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import (
    CrossSectionMemo,
    is_fresh,
    lake_universe,
    orders_from_constructor,
    parse_universe,
    session_cutoff,
)

_REBALANCE_MONTHS = ["2,5,8,11", "1,4,7,10", "3,6,9,12", "1,2,3,4,5,6,7,8,9,10,11,12"]
_EPS = 1e-9


def select_quant_momentum(
    stats: Mapping[str, tuple[float, float]],
    top_pct: float,
    id_keep_pct: float,
    *,
    absolute_momentum: bool = False,
) -> list[str]:
    """Tickers kept from ``{ticker: (r, ID)}``: the top ``top_pct`` by ``r``
    (at least one), optionally only those with ``r > 0``, then the
    ``id_keep_pct`` of those with the lowest ID (at least one). Sorted by
    ticker; ties break by ticker."""
    if not stats:
        return []
    n_top = max(1, math.ceil(top_pct * len(stats) - _EPS))
    winners = sorted(stats, key=lambda t: (-stats[t][0], t))[:n_top]
    if absolute_momentum:
        winners = [t for t in winners if stats[t][0] > 0]
    if not winners:
        return []
    n_keep = max(1, math.ceil(id_keep_pct * len(winners) - _EPS))
    return sorted(sorted(winners, key=lambda t: (stats[t][1], -stats[t][0], t))[:n_keep])


class QuantMomentum(BaseStrategy):
    id = "quant_momentum"
    hypothesis = (
        "Investors underreact to information that arrives gradually, so stocks "
        "with the strongest 12-2 month returns built from many small moves keep "
        "outperforming for the next quarter; the losers are those who anchor on "
        "old prices. Fails in sharp momentum crashes (market rebounds after a "
        "bear market, when past losers rally hardest)."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 63
    # C[t-252] needs 252 bars before today's: 253 closes. The skipped month
    # lies inside that window, so it adds nothing (the backlog's 273 counted
    # it twice), and Barroso vol scaling (126 bars) is not implemented.
    required_history_bars = 253

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "required_history_bars": int(p["formation_bars"]) + 1,
        }

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        if int(self.params["skip_bars"]) >= int(self.params["formation_bars"]):
            raise ValueError("skip_bars must be shorter than formation_bars")
        self._bar_caches = LakeBarCaches()
        self._memo = CrossSectionMemo()

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
                name="top_pct",
                kind="float",
                default=0.10,
                bounds=(0.02, 0.5),
                description="Fraction of the universe kept by momentum.",
            ),
            ParameterSpec(
                name="id_keep_pct",
                kind="float",
                default=0.5,
                bounds=(0.1, 1.0),
                description="Fraction of the winners kept by lowest information discreteness.",
            ),
            ParameterSpec(
                name="absolute_momentum",
                kind="bool",
                default=False,
                tunable=False,
                description="Only hold winners whose own 12-2 return is positive.",
            ),
            ParameterSpec(
                name="rebalance_months",
                kind="categorical",
                default=_REBALANCE_MONTHS[0],
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
        """``{ticker: 1.0}`` for the names held from ``tickers`` as of ``as_of``."""
        stats = {}
        for ticker in dict.fromkeys(tickers):
            s = self._stats(ticker, as_of, lake)
            if s is not None:
                stats[ticker] = s
        kept = select_quant_momentum(
            stats,
            float(self.params["top_pct"]),
            float(self.params["id_keep_pct"]),
            absolute_momentum=bool(self.params["absolute_momentum"]),
        )
        return dict.fromkeys(kept, 1.0)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None:
            return None
        day, _ = session_cutoff(as_of)
        selected = self._memo.get(
            lake, day, lambda: self.score_universe(self._universe(lake), as_of, lake)
        )
        return selected.get(ticker)

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
        signals = {t: 1.0 for r, t in my_picks if r > 0}
        inp = ConstructionInput(
            signals={self.id: signals}, portfolio=portfolio, prices=prices, as_of=day
        )
        constructor = get_constructor("equal_weight_top_n", n=max(1, len(signals)))
        return orders_from_constructor(constructor, inp, strategy_id=self.id)

    # ---- internals ---------------------------------------------------------------

    def _universe(self, lake: Any) -> list[str]:
        explicit = parse_universe(str(self.params["universe"]))
        if explicit:
            return explicit
        return self._memo.universe(lake, lambda: lake_universe(lake, self.applicable_asset_classes))

    def _stats(self, ticker: str, as_of: Any, lake: Any) -> tuple[float, float] | None:
        """``(r, ID)`` for ``ticker`` from bars dated on or before ``as_of``."""
        formation, skip = int(self.params["formation_bars"]), int(self.params["skip_bars"])
        _, cutoff = session_cutoff(as_of)
        cache = self._bar_caches.for_lake(lake)
        if not is_fresh(cache, ticker, cutoff):
            return None
        closes = cache.last_n_closes(ticker, Interval.DAY_1, cutoff, formation + 1)
        r = trailing_return_skip(closes, formation, skip)
        if r is None:
            return None
        discreteness = information_discreteness(formation_window(closes, formation, skip))
        return None if discreteness is None else (r, discreteness)
