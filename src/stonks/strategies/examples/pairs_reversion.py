"""PairsReversion: trade the spread of cointegrated pairs back to its mean
(roadmap 16.3; Gatev, Goetzmann and Rouwenhorst 2006; Chan, *Algorithmic
Trading* ch. 2-4; Vidyamurthy, *Pairs Trading*).

Rules, per configured pair ``A:B``, on adjusted daily closes up to as_of:

1. Over the last ``formation_bars`` bars fit the hedge ratio ``beta`` (OLS
   of ``log A`` on ``log B``) and form the spread ``s = log A - beta log B``.
2. The pair trades only while the spread mean-reverts: its AR(1)
   half-life must exist and be at most ``max_half_life`` bars (a cheap
   stand-in for an Engle-Granger test that works on any window).
3. Standardise the spread over the same window. Replay the z-scores from
   flat: enter at ``|z| >= entry_z``, exit at ``|z| <= exit_z``, stop out
   at ``|z| >= stop_z`` (the relationship broke). Replaying the window
   makes the state a pure function of the bars, with no memory between
   calls.
4. ``z`` high means A is rich: short A, long B. ``z`` low: long A, short B.

Scores: the long leg scores ``+|z|`` and the short leg ``-|z|``. The short
leg only exists with ``short_mode = "short"`` (off by default); a long-only
book keeps just the long legs. ``decide`` puts ``allocation / (2 x pairs)``
of equity on every active leg, so an active pair is dollar neutral.

Pairs are given as ``"A.US:B.US,C.US:D.US"``. With no pairs the strategy
never trades.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import numpy as np

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import AssetClass, Features, Order, Portfolio
from stonks.features.pairs import half_life, hedge_ratio, pair_state, spread, spread_zscores
from stonks.portfolio.orders import orders_from_targets
from stonks.strategies._common import LakeBarCaches
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import CrossSectionMemo, is_fresh, session_cutoff

#: Smallest score magnitude, so an active leg always clears threshold 0.
MIN_SCORE = 1e-6


def parse_pairs(spec: str) -> list[tuple[str, str]]:
    """``"A:B,C:D"`` as ``[("A", "B"), ("C", "D")]``; blank items are skipped."""
    pairs: list[tuple[str, str]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        a, sep, b = item.partition(":")
        if not sep or not a.strip() or not b.strip() or a.strip() == b.strip():
            raise ValueError(f"a pair is two different tickers as 'A:B', got {item!r}")
        pairs.append((a.strip(), b.strip()))
    return list(dict.fromkeys(pairs))


class PairsReversion(BaseStrategy):
    id = "pairs_reversion"
    hypothesis = (
        "Two close economic substitutes share a common price driver, so when "
        "their spread stretches it tends to come back: liquidity demanders "
        "push one leg away from fair value and we are paid for providing "
        "the other side (Gatev, Goetzmann and Rouwenhorst). Long the cheap "
        "leg and short the rich one. Fails when the relationship breaks "
        "(a merger, a new business line), which the stop and the half-life "
        "check try to catch."
    )
    alpha_family = "reversion"
    premise = "mean_reversion"
    label_horizon_bars = 10
    required_history_bars = 120
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = ("equity", "crypto", "commodity")
    short_capable: ClassVar[bool] = True

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        p = self.params
        if not 0.0 <= float(p["exit_z"]) < float(p["entry_z"]) < float(p["stop_z"]):
            raise ValueError("need 0 <= exit_z < entry_z < stop_z")
        self.pairs = parse_pairs(str(p["pairs"]))
        self._bar_caches = LakeBarCaches()
        self._memo = CrossSectionMemo()

    def param_metadata(self) -> dict[str, int]:
        return {"required_history_bars": int(self.params["formation_bars"])}

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="pairs",
                kind="categorical",
                default="",
                bounds=None,
                tunable=False,
                description="Pairs as 'A.US:B.US,C.US:D.US'; empty trades nothing.",
            ),
            ParameterSpec(
                name="formation_bars",
                kind="int",
                default=120,
                bounds=(40, 252),
                description="Bars used for the hedge ratio, the half-life and the z-score.",
            ),
            ParameterSpec(
                name="entry_z",
                kind="float",
                default=2.0,
                bounds=(1.0, 3.0),
                description="Spread z-score that opens a trade.",
            ),
            ParameterSpec(
                name="exit_z",
                kind="float",
                default=0.5,
                bounds=(0.0, 0.9),
                description="Spread z-score that closes it.",
            ),
            ParameterSpec(
                name="stop_z",
                kind="float",
                default=4.0,
                bounds=(3.0, 6.0),
                tunable=False,
                description="Spread z-score that stops a trade out (the pair broke).",
            ),
            ParameterSpec(
                name="max_half_life",
                kind="int",
                default=30,
                bounds=(5, 120),
                tunable=False,
                description="Longest spread half-life, in bars, a pair may have to trade.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.1, 2.0),
                tunable=False,
                description="Gross exposure across every pair when all are active.",
            ),
        ]

    # ---- signals -----------------------------------------------------------------

    def pair_view(self, a: str, b: str, as_of: Any, lake: Any) -> tuple[int, float] | None:
        """``(state, last z)`` of pair ``a:b`` as of ``as_of``; ``None``
        when there is not enough history or the spread does not revert."""
        p = self.params
        n = int(p["formation_bars"])
        _, cutoff = session_cutoff(as_of)
        cache = self._bar_caches.for_lake(lake)
        if not (is_fresh(cache, a, cutoff) and is_fresh(cache, b, cutoff)):
            return None
        ca = cache.last_n_closes(a, Interval.DAY_1, cutoff, n)
        cb = cache.last_n_closes(b, Interval.DAY_1, cutoff, n)
        if len(ca) < n or len(cb) < n:
            return None
        ok = np.isfinite(ca) & np.isfinite(cb) & (ca > 0) & (cb > 0)
        if ok.sum() < max(10, n // 2):
            return None
        la, lb = np.log(ca[ok]), np.log(cb[ok])
        beta = hedge_ratio(la, lb)
        if beta is None:
            return None
        s = spread(la, lb, beta)
        hl = half_life(s)
        z = spread_zscores(s)
        if hl is None or hl > int(p["max_half_life"]) or z is None:
            return None
        state = pair_state(z, float(p["entry_z"]), float(p["exit_z"]), float(p["stop_z"]))
        return state, float(z[-1])

    def scores(self, as_of: Any, lake: Any) -> dict[str, float]:
        """``ticker -> signed score`` of every active leg (module doc);
        short legs only with ``short_mode = "short"``."""
        out: dict[str, float] = {}
        for a, b in self.pairs:
            view = self.pair_view(a, b, as_of, lake)
            if view is None or view[0] == 0:
                continue
            state, z = view
            size = max(abs(z), MIN_SCORE)
            long_leg, short_leg = (a, b) if state > 0 else (b, a)
            out[long_leg] = out.get(long_leg, 0.0) + size
            out[short_leg] = out.get(short_leg, 0.0) - size
        return {
            t: v
            for t, v in out.items()
            if math.isfinite(v) and (v > 0 or (v < 0 and self.supports_short))
        }

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        values: dict[str, float] = {}
        for a, b in self.pairs:
            if ticker not in (a, b) or lake is None:
                continue
            view = self.pair_view(a, b, as_of, lake)
            if view is not None:
                values[f"z_{a}_{b}"] = view[1]
                values[f"state_{a}_{b}"] = float(view[0])
        return Features(values=values)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None or not self.pairs:
            return None
        day, _ = session_cutoff(as_of)
        scores = self._memo.get(lake, day, lambda: self.scores(as_of, lake))
        return scores.get(ticker)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day, _ = session_cutoff(as_of)
        shorts = self.supports_short
        leg = float(self.params["allocation"]) / (2.0 * max(len(self.pairs), 1))
        targets = {t: (leg if r > 0 else -leg) for r, t in my_picks if r > 0 or (shorts and r < 0)}
        return orders_from_targets(
            targets,
            portfolio,
            prices,
            buffer_fraction=0.1,
            as_of=day,
            strategy_id=self.id,
            allow_short=shorts,
        )
