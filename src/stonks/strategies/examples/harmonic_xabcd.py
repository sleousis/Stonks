"""HarmonicXABCDStrategy — long bullish harmonic (XABCD) patterns.

Ported from ``harmonic_patterns.py`` in neurotrader888's MIT-licensed
``TechnicalAnalysisAutomation`` repository
(https://github.com/neurotrader888/TechnicalAnalysisAutomation, Copyright (c)
neurotrader888). Independent re-implementation of the published algorithm;
the pattern ratio templates are taken from that file.

Rule:

- Swing points come from a percentage directional change
  (:class:`stonks.features.extremes.DirectionalChange`, threshold
  ``sigma``) over highs / lows / closes; each is usable only from its
  confirmation bar on.
- When the last confirmed extreme is a top, the last four extremes are
  X (bottom), A, B, C (top) and price is on the CD leg. On every bar whose
  low is the lowest since C was confirmed, that low is the candidate D.
- The four ratios AB/XA, BC/AB, CD/BC and AD/XA are scored against each
  template (Gartley, Bat, Butterfly, Crab, Deep Crab, Cypher, Shark). A
  template ratio is either a target (error = |log(actual / target)|) or a
  range (error 0 inside, twice the log distance to the nearest edge
  outside); a missing ratio costs nothing. A template's error is the sum.
- Enter at the close of D when the best template's error is within
  ``err_thresh``; hold until the next directional-change extreme confirms.
  One entry per CD leg.

The live trade is rebuilt on every call by replaying a fixed window of the
latest bars from scratch, so a fresh production instance matches a backtest
that stepped through every bar.

Deliberate deviations from the original:

- Long-only: bearish patterns are not traded (the Stonks broker cannot
  short), so that leg is flat.
- Strictly causal replay: the original's loop peeked at the *next*
  extreme's confirmation index to stop early; here extremes are produced
  bar by bar.
- ``estimate_return`` needs a magnitude, which the original never defined:
  it uses a 0.382 retracement of CD as the target.
- History is limited to the last ``history_bars()`` (2000) bars, and the
  directional change restarts at the start of that window.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.extremes import DirectionalChange, Extreme
from stonks.strategies._common import LakeBarCaches, iso
from stonks.strategies.base import BaseStrategy

Ratio = float | tuple[float, float] | None

# Range misses are punished harder: a range is already lenient.
_RANGE_PENALTY = 2.0
# Share of the CD leg used as the take-profit proxy in estimate_return.
_TARGET_RETRACE = 0.382


@dataclass(frozen=True)
class HarmonicTemplate:
    name: str
    ab_xa: Ratio
    bc_ab: Ratio
    cd_bc: Ratio
    ad_xa: Ratio


TEMPLATES: tuple[HarmonicTemplate, ...] = (
    HarmonicTemplate("Gartley", 0.618, (0.382, 0.886), (1.13, 1.618), 0.786),
    HarmonicTemplate("Bat", (0.382, 0.50), (0.382, 0.886), (1.618, 2.618), 0.886),
    HarmonicTemplate("Butterfly", 0.786, (0.382, 0.886), (1.618, 2.24), (1.27, 1.41)),
    HarmonicTemplate("Crab", (0.382, 0.618), (0.382, 0.886), (2.618, 3.618), 1.618),
    HarmonicTemplate("Deep Crab", 0.886, (0.382, 0.886), (2.0, 3.618), 1.618),
    HarmonicTemplate("Cypher", (0.382, 0.618), (1.13, 1.41), (1.27, 2.00), 0.786),
    HarmonicTemplate("Shark", None, (1.13, 1.618), (1.618, 2.24), (0.886, 1.13)),
)


def ratio_error(actual: float, template: Ratio) -> float:
    """Log-distance of ``actual`` to a template ratio (see module doc)."""
    if template is None:
        return 0.0
    log_actual = math.log(actual)
    if isinstance(template, tuple):
        lo, hi = math.log(template[0]), math.log(template[1])
        if lo <= log_actual <= hi:
            return 0.0
        return _RANGE_PENALTY * min(abs(log_actual - lo), abs(log_actual - hi))
    return abs(log_actual - math.log(template))


@dataclass(frozen=True)
class HarmonicTrade:
    """A live bullish XABCD trade; indices relative to the replayed window."""

    entry_index: int
    name: str
    error: float
    x_index: int
    a_index: int
    b_index: int
    c_index: int
    c_price: float
    d_price: float


def _best_template(x: float, a: float, b: float, c: float, d: float) -> tuple[str, float] | None:
    xa, ab, bc = abs(a - x), abs(b - a), abs(c - b)
    cd, ad = abs(c - d), abs(a - d)
    if min(xa, ab, bc, cd, ad) <= 0:
        return None
    ratios = (ab / xa, bc / ab, cd / bc, ad / xa)
    best: tuple[str, float] | None = None
    for tpl in TEMPLATES:
        err = sum(
            ratio_error(r, t)
            for r, t in zip(ratios, (tpl.ab_xa, tpl.bc_ab, tpl.cd_bc, tpl.ad_xa), strict=True)
        )
        if best is None or err < best[1]:
            best = (tpl.name, err)
    return best


def replay_bull_harmonic(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    sigma: float,
    err_thresh: float,
) -> HarmonicTrade | None:
    """Replay the bullish XABCD rule bar by bar and return the trade live at
    the last bar, or ``None`` when flat."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    dc = DirectionalChange(sigma)
    extremes: list[Extreme] = []
    trade: HarmonicTrade | None = None
    for i in range(len(close)):
        new = dc.update(i, float(high[i]), float(low[i]), float(close[i]))
        if new is not None:
            extremes.append(new)
            trade = None  # a new swing point ends the trade (and the leg)
        if trade is not None or len(extremes) < 4:
            continue
        last = extremes[-1]
        if last.kind != "top":
            continue
        d = float(low[i])
        if i > last.conf_index and low[last.conf_index : i].min() < d:
            continue  # not the lowest low since C confirmed
        x, a, b, c = extremes[-4:]
        best = _best_template(x.price, a.price, b.price, c.price, d)
        if best is None or best[1] > err_thresh:
            continue
        trade = HarmonicTrade(
            entry_index=i,
            name=best[0],
            error=float(best[1]),
            x_index=x.ext_index,
            a_index=a.ext_index,
            b_index=b.ext_index,
            c_index=c.ext_index,
            c_price=c.price,
            d_price=d,
        )
    return trade


class HarmonicXABCDStrategy(BaseStrategy):
    id = "harmonic_xabcd"
    applicable_asset_classes = ("crypto", "equity")

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="sigma",
                kind="float",
                default=0.02,
                bounds=(0.005, 0.06),
                description="Directional-change threshold (fraction of price) that "
                "confirms a swing point.",
            ),
            ParameterSpec(
                name="err_thresh",
                kind="float",
                default=0.2,
                bounds=(0.1, 0.75),
                description="Maximum summed log-ratio error to the best pattern template.",
            ),
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1d",
                bounds=["1m", "5m", "15m", "30m", "1h", "4h", "12h", "1d", "1w"],
                tunable=False,
                description="Bar interval to read from the lake.",
            ),
            ParameterSpec(
                name="ticker",
                kind="categorical",
                default="AAPL.US",
                bounds=None,
                tunable=False,
                description="Ticker the strategy trades.",
            ),
            ParameterSpec(
                name="allocation",
                kind="float",
                default=1.0,
                bounds=(0.0, 1.0),
                tunable=False,
                description="Fraction of cash deployed on a fresh long entry.",
            ),
        ]

    # ---- Strategy Protocol -------------------------------------------------

    def extract_features(self, ticker: str, as_of, lake: Any) -> Features:
        state = self._state(ticker, as_of, lake)
        if state is None:
            return Features(values={})
        trade, close = state
        if trade is None:
            return Features(values={"close": close, "in_trade": 0.0})
        return Features(
            values={
                "close": close,
                "in_trade": 1.0,
                "pattern_error": trade.error,
                "d_price": trade.d_price,
                "c_price": trade.c_price,
            }
        )

    def estimate_return(self, ticker: str, as_of, lake: Any) -> float | None:
        if ticker != self.params["ticker"]:
            return None
        state = self._state(ticker, as_of, lake)
        if state is None or state[0] is None:
            return None
        trade, close = state
        target = trade.d_price + _TARGET_RETRACE * (trade.c_price - trade.d_price)
        return max(target / close - 1.0, 1e-6)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of,
    ) -> list[Order]:
        target = self.params["ticker"]
        price = prices.get(target)
        holding = portfolio.positions.get(target, 0.0)
        if my_picks and price and price > 0 and holding <= 0 and portfolio.cash > 0:
            qty = (portfolio.cash * float(self.params["allocation"])) / price
            if qty <= 0:
                return []
            return [
                Order(
                    client_id=f"{self.id}:buy:{target}:{iso(as_of)}",
                    ticker=target,
                    side="buy",
                    quantity=qty,
                    order_type="market",
                    strategy_id=self.id,
                )
            ]
        if not my_picks and holding > 0:
            return [
                Order(
                    client_id=f"{self.id}:sell:{target}:{iso(as_of)}",
                    ticker=target,
                    side="sell",
                    quantity=holding,
                    order_type="market",
                    strategy_id=self.id,
                )
            ]
        return []

    # ---- internals ---------------------------------------------------------

    def history_bars(self) -> int:
        """Bars replayed per evaluation."""
        return 2000

    def _state(self, ticker: str, as_of, lake: Any) -> tuple[HarmonicTrade | None, float] | None:
        if lake is None:
            return None
        interval = Interval.parse(self.params["interval"])
        bars = self._bar_caches.for_lake(lake).last_n_bars(
            ticker, interval, as_of, self.history_bars()
        )
        if len(bars) < 5:
            return None
        high, low, close = (bars[c].to_numpy(dtype=float) for c in ("high", "low", "close"))
        if np.any(~(low > 0)) or np.any(~(close > 0)):
            return None
        trade = replay_bull_harmonic(
            high, low, close, float(self.params["sigma"]), float(self.params["err_thresh"])
        )
        return trade, float(close[-1])
