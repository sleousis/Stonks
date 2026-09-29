"""LastTradeFilter — take an inner strategy's entry only after a loser (or winner).

Wraps an inner :class:`Strategy` (params carry ``inner_class_path`` +
``inner_params``; inner fitted state is saved under ``inner/``). To judge
an entry it replays a shadow trade log of the inner strategy's own signal
over the last ``replay_bars`` bars of the ticker (``timestamp <= as_of``,
so causal): the inner strategy is "in" on a bar when its
``estimate_return`` is not ``None``; an off -> on step opens a shadow trade
at that bar's close and on -> off closes it at that bar's close (a winner
when the exit close is above the entry close, a loser when below, neither
when equal). A new entry is admitted only if the previous completed shadow
round-trip matches ``require_last`` (default ``"loser"``); the decision is
fixed at the entry bar and holds for the whole trade. Exits always pass
through: ``decide`` is the inner strategy's own.

Source: ``last_trade_adj_signal`` in neurotrader888's MIT licensed
``TradeDependenceRunsTest`` repo. Own implementation, no code copied.
Deviations:

- Long-only: the original's input was a stop-and-reverse long/short signal
  where a long was admitted on the outcome of the preceding *short*. Here a
  long entry is judged on the preceding *long* round-trip.
- The replay is bounded to ``replay_bars`` bars, and a trade already open
  at the replay's first bar is ignored (its entry is unknown), so early in
  the window there may be no completed trade -> not admitted (the original
  also refuses the first trade).
- Entry / exit prices are signal-bar closes (as in the original); the
  backtester fills at the next bar's open.

Inner signals are memoized per lake and bar, so stepping bar by bar costs
one new inner call per bar.
"""

from __future__ import annotations

import weakref
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.strategies._common import LakeBarCaches, memo_scope
from stonks.strategies._wrapping import INTERVALS, InnerStrategyWrapper, inner_param_specs


class LastTradeFilter(InnerStrategyWrapper):
    id = "last_trade_filter"
    title = "After a losing trade"
    summary = "Wraps another strategy and takes its entries only after a losing trade."
    hypothesis = (
        "For some systems a losing trade is often followed by a winner, "
        "as noise shakes out and then the move comes. Taking entries only "
        "after a loser keeps the better trades. Adds no alpha of its own."
    )
    id_suffix = "last_trade"
    #: The replayed trade log is long-only (see the module doc).
    long_only = True

    @classmethod
    def parameter_spec(cls):
        return [
            *inner_param_specs(),
            ParameterSpec(
                name="require_last",
                kind="categorical",
                default="loser",
                bounds=["loser", "winner"],
                tunable=False,
                description="Admit an entry only after a losing / winning round-trip.",
            ),
            ParameterSpec(
                name="replay_bars",
                kind="int",
                default=500,
                bounds=(3, 20_000),
                tunable=False,
                description="Bars of history the shadow trade log is replayed over.",
            ),
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1d",
                bounds=list(INTERVALS),
                tunable=False,
                description="Bar interval to read from the lake.",
            ),
        ]

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()
        # per lake: (ticker, bar timestamp) -> inner strategy "in" on that bar
        self._signals: weakref.WeakKeyDictionary[Any, dict] = weakref.WeakKeyDictionary()

    # ---- shadow trade log -----------------------------------------------------

    def _inner_on(self, ticker: str, ts: Any, lake: Any, memo: dict | None) -> bool:
        key = (ticker, ts)
        if memo is not None and key in memo:
            return memo[key]
        on = self._inner.estimate_return(ticker, pd.Timestamp(ts).to_pydatetime(), lake) is not None
        if memo is not None:
            memo[key] = on
        return on

    def is_admitted(self, ticker: str, as_of: Any, lake: Any) -> bool:
        """Whether the inner strategy's current trade (as of ``as_of``) was
        admitted at its entry bar. ``False`` when the inner is not in."""
        interval = Interval.parse(self.params["interval"])
        bars = self._bar_caches.for_lake(lake).last_n_bars(
            ticker, interval, as_of, int(self.params["replay_bars"])
        )
        if bars.empty:
            return False
        memo = self._lake_signals(lake)
        timestamps = list(bars["timestamp"])
        closes = bars["close"].astype(float).tolist()
        required = self.params["require_last"]

        in_trade = False
        known_entry: float | None = None  # None while in a trade of unknown entry
        admitted = False
        last_outcome: str | None = None
        for k, (ts, close) in enumerate(zip(timestamps, closes, strict=True)):
            on = self._inner_on(ticker, ts, lake, memo)
            if on and not in_trade:
                in_trade = True
                if k == 0:
                    known_entry, admitted = None, False  # opened before the replay
                else:
                    known_entry, admitted = close, last_outcome == required
            elif not on and in_trade:
                in_trade = False
                if known_entry is not None:
                    if close > known_entry:
                        last_outcome = "winner"
                    elif close < known_entry:
                        last_outcome = "loser"
                    else:
                        last_outcome = "flat"
        return in_trade and admitted

    def _lake_signals(self, lake: Any) -> dict | None:
        try:
            scope = memo_scope(lake)
            memo = self._signals.get(scope)
            if memo is None:
                memo = {}
                self._signals[scope] = memo
        except TypeError:
            return None
        return memo

    # ---- Strategy Protocol ---------------------------------------------------

    def fit(self, dataset: Any) -> None:
        super().fit(dataset)
        # a refitted inner strategy may signal differently on the same bars
        self._signals = weakref.WeakKeyDictionary()

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        values = dict(self._inner.extract_features(ticker, as_of, lake).values)
        if lake is not None:
            values["last_trade_admitted"] = 1.0 if self.is_admitted(ticker, as_of, lake) else 0.0
        return Features(values=values)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        estimate = self._inner.estimate_return(ticker, as_of, lake)
        if estimate is None or lake is None:
            return None
        return estimate if self.is_admitted(ticker, as_of, lake) else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        return self._inner.decide(my_picks, portfolio, prices, as_of)
