"""StocksOnTheMove — Andreas Clenow's *Stocks on the Move* (BL-39).

Rules, on adjusted daily bars:

1. **Score.** Fit ``ln(close)`` on time over ``lookback`` (90) sessions; the
   score is the annualised slope ``exp(b)^250 - 1`` times the fit's R².
2. **Rank** the universe by score and keep the top ``top_fraction`` (20%).
3. **Filters.** A kept name must close above its ``ma_filter`` (100) day
   average, have no close-to-close move of ``gap_limit`` (15%) or more in
   the last ``lookback`` sessions, and score above 0. Names that fail still
   take their rank slot (the book ranks first, then filters).
4. **Index filter.** No new buys while ``index_ticker`` (SPY.US) closes
   below its ``index_ma`` (200) day average; held names are still managed.
   Missing index history blocks new buys too (fail closed); set
   ``index_ticker`` to ``""`` to switch the filter off.
5. **Sizing** by ATR risk parity through the ``atr_parity`` constructor:
   ``shares = equity * risk_factor / ATR(atr_period)``, so each position
   moves the book by ``risk_factor`` (10 bps) on an average day. Held names
   keep their shares and are resized every ``resize_every_weeks`` (2) weeks
   when more than ``resize_tolerance`` off; new names are bought best first
   while cash lasts.
6. **Weekly trading** on ``trade_weekday`` (Wednesday), or the next session
   of that week when it is a holiday. Names that leave the selection are
   sold on that day; other days hold. Long only.

Universe, history and staleness rules as in
:mod:`stonks.strategies.examples.quant_momentum` (the index ticker is never
part of the default universe).

``decide`` needs the ATRs and the index state of its ``as_of``, which
``estimate_return`` computes. A fresh instance that never evaluated that day
(a production tick loads one per step) makes no new buys and never resizes;
it still sells names that dropped out of the picks.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Order, Portfolio
from stonks.features.indicators import atr
from stonks.features.sessions import weekly_session
from stonks.features.trend import max_gap, regression_momentum
from stonks.logging import get_logger
from stonks.portfolio.atr_parity import AtrConstructionInput
from stonks.portfolio.base import get_constructor
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

_log = get_logger("stonks.strategies.stocks_on_the_move")
_EPS = 1e-9


@dataclass(frozen=True)
class TrendStats:
    score: float
    above_ma: bool
    max_gap: float
    atr: float


@dataclass(frozen=True)
class _Evaluation:
    day: date
    scores: dict[str, float]
    atrs: dict[str, float]
    risk_on: bool


class StocksOnTheMove(BaseStrategy):
    id = "stocks_on_the_move"
    hypothesis = (
        "Stocks in steady, low-noise uptrends keep rising for weeks because "
        "investors underreact and institutions buy gradually; holding the "
        "strongest smooth trends with equal risk per name, only in a rising "
        "market, earns the trend premium from late sellers. Fails in choppy, "
        "trendless markets and sharp reversals, and lags in V-shaped recoveries "
        "while the index filter blocks buying."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 5
    required_history_bars = 200

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()
        self._memo = CrossSectionMemo()
        self._last: _Evaluation | None = None

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec("lookback", "int", 90, (60, 250), description="Regression window."),
            ParameterSpec("ma_filter", "int", 100, (20, 250), description="Trend-filter average."),
            ParameterSpec(
                "gap_limit", "float", 0.15, (0.05, 0.5), description="Largest allowed daily move."
            ),
            ParameterSpec(
                "top_fraction",
                "float",
                0.2,
                (0.05, 1.0),
                description="Fraction of the ranked universe that may be held.",
            ),
            ParameterSpec(
                "annualization",
                "int",
                250,
                (200, 365),
                tunable=False,
                description="Sessions per year for the annualised slope.",
            ),
            ParameterSpec("atr_period", "int", 20, (5, 100), description="ATR window for sizing."),
            ParameterSpec(
                "risk_factor",
                "float",
                0.001,
                (0.0001, 0.01),
                description="Daily equity move per position (ATR risk parity).",
            ),
            ParameterSpec(
                "index_ticker",
                "categorical",
                "SPY.US",
                None,
                tunable=False,
                description="Benchmark for the index filter; empty disables it.",
            ),
            ParameterSpec("index_ma", "int", 200, (50, 250), description="Index-filter average."),
            ParameterSpec(
                "trade_weekday",
                "int",
                2,
                (0, 4),
                tunable=False,
                description="Weekday to trade (0 = Monday).",
            ),
            ParameterSpec(
                "resize_every_weeks",
                "int",
                2,
                (1, 8),
                tunable=False,
                description="Weeks between position resizes.",
            ),
            ParameterSpec(
                "resize_tolerance",
                "float",
                0.10,
                (0.0, 0.5),
                tunable=False,
                description="Relative size drift tolerated before a resize.",
            ),
            ParameterSpec(
                "universe",
                "categorical",
                "",
                None,
                tunable=False,
                description="Comma-separated tickers ranked together; empty = the lake's.",
            ),
        ]

    # ---- signals -----------------------------------------------------------------

    def score_universe(self, tickers: Sequence[str], as_of: Any, lake: Any) -> dict[str, float]:
        """``{ticker: score}`` for the names held from ``tickers`` as of ``as_of``."""
        return self._evaluate(tickers, as_of, lake).scores

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None:
            return None
        day, _ = session_cutoff(as_of)
        ev = self._memo.get(lake, day, lambda: self._evaluate(self._universe(lake), as_of, lake))
        self._last = ev
        return ev.scores.get(ticker)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day, _ = session_cutoff(as_of)
        if weekly_session(day, int(self.params["trade_weekday"])) != day:
            return []
        ev = self._last if self._last is not None and self._last.day == day else None
        if ev is None:
            _log.warning("stocks_on_the_move.no_evaluation", as_of=day.isoformat())
        signals = {t: float(r) for r, t in my_picks if r > 0}
        if ev is None or not ev.risk_on:
            signals = {t: v for t, v in signals.items() if portfolio.positions.get(t, 0.0) > 0}
        inp = AtrConstructionInput(
            signals={self.id: signals},
            portfolio=portfolio,
            prices=prices,
            as_of=day,
            atrs=ev.atrs if ev is not None else {},
        )
        constructor = get_constructor(
            "atr_parity",
            risk_factor=float(self.params["risk_factor"]),
            resize_every_weeks=int(self.params["resize_every_weeks"]),
            resize_tolerance=float(self.params["resize_tolerance"]),
        )
        return orders_from_constructor(constructor, inp, strategy_id=self.id)

    # ---- internals ---------------------------------------------------------------

    def _universe(self, lake: Any) -> list[str]:
        explicit = parse_universe(str(self.params["universe"]))
        if explicit:
            return explicit
        index = str(self.params["index_ticker"])
        return self._memo.universe(
            lake,
            lambda: [t for t in lake_universe(lake, self.applicable_asset_classes) if t != index],
        )

    def _evaluate(self, tickers: Sequence[str], as_of: Any, lake: Any) -> _Evaluation:
        day, _ = session_cutoff(as_of)
        stats = {}
        for ticker in dict.fromkeys(tickers):
            s = self._stats(ticker, as_of, lake)
            if s is not None:
                stats[ticker] = s
        n_top = max(1, math.ceil(float(self.params["top_fraction"]) * len(stats) - _EPS))
        ranked = sorted(stats, key=lambda t: (-stats[t].score, t))[:n_top]
        gap_limit = float(self.params["gap_limit"])
        kept = [
            t
            for t in ranked
            if stats[t].score > 0 and stats[t].above_ma and stats[t].max_gap < gap_limit
        ]
        return _Evaluation(
            day=day,
            scores={t: stats[t].score for t in kept},
            atrs={t: stats[t].atr for t in kept},
            risk_on=self._index_ok(as_of, lake),
        )

    def _stats(self, ticker: str, as_of: Any, lake: Any) -> TrendStats | None:
        """Score, filters and ATR for ``ticker`` from bars dated on or before
        ``as_of``; ``None`` without enough recent history."""
        lookback, ma = int(self.params["lookback"]), int(self.params["ma_filter"])
        atr_period = int(self.params["atr_period"])
        n = max(lookback + 1, ma, atr_period + 1)
        _, cutoff = session_cutoff(as_of)
        cache = self._bar_caches.for_lake(lake)
        if not is_fresh(cache, ticker, cutoff):
            return None
        bars = cache.last_n_bars(ticker, Interval.DAY_1, cutoff, n)
        if len(bars) < n:
            return None
        closes = bars["close"].to_numpy(dtype=float)
        fit = regression_momentum(closes, lookback, int(self.params["annualization"]))
        gap = max_gap(closes, lookback)
        atr_now = float(
            atr(bars["high"], bars["low"], bars["close"], atr_period, method="sma").iloc[-1]
        )
        if fit is None or gap is None or not math.isfinite(atr_now) or atr_now <= 0:
            return None
        return TrendStats(
            score=fit.score,
            above_ma=bool(closes[-1] > closes[-ma:].mean()),
            max_gap=gap,
            atr=atr_now,
        )

    def _index_ok(self, as_of: Any, lake: Any) -> bool:
        index = str(self.params["index_ticker"])
        if not index:
            return True
        n = int(self.params["index_ma"])
        _, cutoff = session_cutoff(as_of)
        cache = self._bar_caches.for_lake(lake)
        closes = cache.last_n_closes(index, Interval.DAY_1, cutoff, n)
        if len(closes) < n or not is_fresh(cache, index, cutoff):
            _log.warning("stocks_on_the_move.index_unavailable", index=index, bars=len(closes))
            return False
        return bool(closes[-1] > closes.mean())
