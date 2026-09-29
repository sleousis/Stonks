"""TrailingStopWrapper — a volatility-scaled trailing stop around any
strategy (BL-40; Carver, Clenow, Wilcox & Crittenden, Kaufman).

Wraps an inner :class:`Strategy` (params carry ``inner_class_path`` +
``inner_params``; the inner's fitted state is saved under ``inner/``, see
:mod:`stonks.strategies._wrapping`). For each ticker it replays the inner
strategy's own long signal over the last ``replay_bars`` bars (dated on or
before ``as_of``): the inner is "in" on a bar when its ``estimate_return``
is not ``None``. A trade opens when the inner turns in and closes when it
turns out or when the close falls below

    stop = HWM_since_entry - k_atr * ATR(atr_period)    (never lowered)

(see :mod:`stonks.features.trailing_stop`). After a stop-out the ticker
sits out ``cooldown_bars`` bars, then re-enters if the inner is still in.
While stopped out ``estimate_return`` is ``None`` and ``decide`` sells the
position and drops the inner's orders on that ticker. Everything is rebuilt
from bars on every call, so the wrapper keeps no trading state.

Deviations and limits:

- A trade already open on the replay window's first bar is treated as
  entered there (its true entry is out of view), so its high-water mark
  starts later and the stop can be looser than a full replay's. Keep
  ``replay_bars`` well above a typical holding period.
- The ATR is Wilder's (``rma``) over the window; the first ``atr_period``
  bars carry no stop.
- Inner signals are memoised per lake and bar, so stepping bar by bar costs
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
from stonks.features.indicators import atr
from stonks.features.trailing_stop import Trade, open_trade, replay_trades
from stonks.strategies._common import LakeBarCaches, iso, memo_scope
from stonks.strategies._wrapping import INTERVALS, InnerStrategyWrapper, inner_param_specs


class TrailingStopWrapper(InnerStrategyWrapper):
    id = "trailing_stop"
    title = "Trailing stop"
    summary = (
        "Wraps another strategy and sells a position once it falls a set distance below its "
        "best close."
    )
    id_suffix = "trailing_stop"
    #: The replayed trade log is long-only (see the module doc).
    long_only = True
    hypothesis = (
        "Cutting a position once it falls k ATRs below its best close since "
        "entry keeps the trend's gains and caps the loss when it breaks, at "
        "the cost of whipsaws in noisy markets. Adds no alpha of its own."
    )

    @classmethod
    def parameter_spec(cls):
        return [
            *inner_param_specs(),
            ParameterSpec(
                name="k_atr",
                kind="float",
                default=3.0,
                bounds=(1.0, 12.0),
                description="Stop distance below the high-water mark, in ATRs.",
            ),
            ParameterSpec(
                name="atr_period",
                kind="int",
                default=20,
                bounds=(5, 100),
                description="Wilder ATR period.",
            ),
            ParameterSpec(
                name="cooldown_bars",
                kind="int",
                default=5,
                bounds=(0, 60),
                tunable=False,
                description="Bars to sit out after a stop-out before re-entering.",
            ),
            ParameterSpec(
                name="replay_bars",
                kind="int",
                default=500,
                bounds=(30, 20_000),
                tunable=False,
                description="Bars of history the inner signal is replayed over.",
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

    # ---- replay ---------------------------------------------------------------------

    def _inner_on(self, ticker: str, ts: Any, lake: Any, memo: dict | None) -> bool:
        key = (ticker, ts)
        if memo is not None and key in memo:
            return memo[key]
        on = self._inner.estimate_return(ticker, pd.Timestamp(ts).to_pydatetime(), lake) is not None
        if memo is not None:
            memo[key] = on
        return on

    def _memo(self, lake: Any) -> dict | None:
        try:
            return self._signals.setdefault(memo_scope(lake), {})
        except TypeError:
            return None

    def trade_log(self, ticker: str, as_of: Any, lake: Any) -> list[Trade]:
        """The wrapper's trades over the replay window (bar indices into the
        window), from bars dated on or before ``as_of``."""
        interval = Interval.parse(self.params["interval"])
        bars = self._bar_caches.for_lake(lake).last_n_bars(
            ticker, interval, as_of, int(self.params["replay_bars"])
        )
        if bars.empty:
            return []
        memo = self._memo(lake)
        on = [self._inner_on(ticker, ts, lake, memo) for ts in bars["timestamp"]]
        atrs = atr(
            bars["high"].astype(float),
            bars["low"].astype(float),
            bars["close"].astype(float),
            int(self.params["atr_period"]),
        ).to_numpy()
        return replay_trades(
            bars["close"].to_numpy(dtype=float),
            atrs,
            on,
            float(self.params["k_atr"]),
            holds=on,
            cooldown_bars=int(self.params["cooldown_bars"]),
        )

    def is_stopped(self, ticker: str, as_of: Any, lake: Any) -> bool:
        """True when the wrapper holds no trade at ``as_of`` (stopped out,
        cooling down, or the inner is out)."""
        return open_trade(self.trade_log(ticker, as_of, lake)) is None

    # ---- Strategy Protocol ----------------------------------------------------------

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        values = dict(self._inner.extract_features(ticker, as_of, lake).values)
        if lake is not None:
            values["trailing_stop_out"] = 1.0 if self.is_stopped(ticker, as_of, lake) else 0.0
        return Features(values=values)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if lake is None:
            return self._inner.estimate_return(ticker, as_of, lake)
        self._remember_lake(lake)
        if self.is_stopped(ticker, as_of, lake):
            return None
        return self._inner.estimate_return(ticker, as_of, lake)

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        lake = self._recall_lake()
        if lake is None:
            return self._inner.decide(my_picks, portfolio, prices, as_of)
        tickers = {t for _, t in my_picks} | {t for t, q in portfolio.positions.items() if q > 0}
        stopped = {t for t in tickers if self.is_stopped(t, as_of, lake)}
        picks = [(s, t) for s, t in my_picks if t not in stopped]
        orders = [
            o
            for o in self._inner.decide(picks, portfolio, prices, as_of)
            if o.ticker not in stopped
        ]
        for ticker in sorted(stopped):
            qty = portfolio.positions.get(ticker, 0.0)
            if qty > 0:
                orders.append(
                    Order(
                        client_id=f"{self.id}:sell:{ticker}:{iso(as_of)}",
                        ticker=ticker,
                        side="sell",
                        quantity=qty,
                        order_type="market",
                        strategy_id=self.id,
                    )
                )
        return orders
