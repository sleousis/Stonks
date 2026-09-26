"""Hawkes-process volatility breakout.

Source: neurotrader888/VolatilityHawkes, ``hawkes.py`` (MIT License, (c) 2023
neurotrader888). Stonks' own implementation; no code is copied. The Hawkes
accumulator is :func:`stonks.features.library.hawkes_process`.

Indicator: ``norm_range = (log H - log L) / ATR_rma(log H, log L, log C,
norm_lookback)``, smoothed into ``v = hawkes_process(norm_range, kappa)``,
with ``q05`` / ``q95`` the rolling ``quantile_lookback``-bar 5% / 95%
quantiles of ``v``.

Rule (state machine over bars): when ``v < q05`` remember the bar as
``last_below`` and go flat. When ``v`` crosses above ``q95`` (above now,
at or below on the previous bar) after at least one calm bar, go long if
``close > close[last_below]``; otherwise the original goes short, which is
flat here. The position is held until the next dip below ``q05``.

Deliberate deviations from the original:

- Short -> flat (long-only broker).
- The state machine is replayed causally over the trailing
  ``2 * norm_lookback + 4 * quantile_lookback`` bars on every call, so the
  ATR / Hawkes warm-up starts at the start of that window instead of at the
  start of the whole history. Values are a pure function of the bars up to
  ``as_of`` (look-ahead safe) but can differ slightly from a full-history
  run while the Wilder ATR is still converging.
- A calm bar at window index 0 counts (``last_below >= 0``; the original
  requires ``> 0``, an off-by-one on its full-history array).
- ``estimate_return`` while long is ``close / close[last_below] - 1``, the
  move since the calm bar, floored at a tiny positive value.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.features.indicators import atr
from stonks.features.library import hawkes_process
from stonks.strategies._common import BarCache
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat, common_specs


def vol_breakout_states(
    close: np.ndarray, v: np.ndarray, q05: np.ndarray, q95: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Replay the long/flat state machine. Returns ``(signal, last_below)``
    per bar: ``signal`` is 1 (long) or 0 (flat), ``last_below`` the index of
    the latest bar with ``v < q05`` so far (-1 before the first). NaNs never
    satisfy a comparison, so warm-up bars keep the previous state."""
    n = len(close)
    signal = np.zeros(n, dtype=int)
    last_below = np.full(n, -1, dtype=int)
    lb, state = -1, 0
    for i in range(n):
        if v[i] < q05[i]:
            lb, state = i, 0
        if i > 0 and lb >= 0 and v[i] > q95[i] and v[i - 1] <= q95[i - 1]:
            state = 1 if close[i] > close[lb] else 0
        signal[i] = state
        last_below[i] = lb
    return signal, last_below


class VolatilityHawkesStrategy(SingleTickerLongFlat):
    id = "volatility_hawkes"
    hypothesis = (
        "A burst of volatility after calm often starts a new move, and "
        "joining in the direction of that move pays. Fails when bursts "
        "come from news that reverses at once."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 24
    required_history_bars = 506

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "required_history_bars": int(p["norm_lookback"]) + int(p["quantile_lookback"]) + 2,
        }

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="kappa",
                kind="float",
                default=0.1,
                bounds=(0.01, 0.5),
                description="Hawkes decay rate; larger forgets faster.",
            ),
            ParameterSpec(
                name="quantile_lookback",
                kind="int",
                default=168,
                bounds=(24, 336),
                description="Bars in the rolling 5%/95% quantile window of the Hawkes volatility.",
            ),
            ParameterSpec(
                name="norm_lookback",
                kind="int",
                default=336,
                bounds=(50, 500),
                tunable=False,
                description="ATR length (bars) that normalises the log range.",
            ),
            *common_specs("BTC-USD.CC"),
        ]

    def _evaluate(self, cache: BarCache, ticker: str, as_of: Any) -> dict[str, float] | None:
        norm_lb = int(self.params["norm_lookback"])
        q_lb = int(self.params["quantile_lookback"])
        df = cache.last_n_bars(ticker, self.interval, as_of, 2 * norm_lb + 4 * q_lb)
        if len(df) < norm_lb + q_lb + 2:
            return None

        def _log(col: str) -> pd.Series:
            x = df[col].astype(float)
            return np.log(x.where(x > 0))

        log_h, log_l, log_c = _log("high"), _log("low"), _log("close")
        a = atr(log_h, log_l, log_c, norm_lb, method="rma")
        norm_range = (log_h - log_l) / a.where(a > 0)
        v = hawkes_process(norm_range, float(self.params["kappa"]))
        q05 = v.rolling(q_lb).quantile(0.05)
        q95 = v.rolling(q_lb).quantile(0.95)
        if pd.isna(q95.iloc[-1]) or pd.isna(v.iloc[-1]):
            return None

        close = df["close"].to_numpy(dtype=float)
        signal, last_below = vol_breakout_states(
            close, v.to_numpy(), q05.to_numpy(), q95.to_numpy()
        )
        lb = int(last_below[-1])
        base = close[lb] if lb >= 0 else float("nan")
        long = bool(signal[-1] == 1)
        return {
            "v": float(v.iloc[-1]),
            "q05": float(q05.iloc[-1]),
            "q95": float(q95.iloc[-1]),
            "close": float(close[-1]),
            "close_at_last_below": float(base),
            "signal": 1.0 if long else 0.0,
            "score": float(close[-1] / base - 1.0) if long and base > 0 else 0.0,
        }
