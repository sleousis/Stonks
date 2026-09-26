"""Volume Spread Analysis strategy.

Source: neurotrader888/VSAIndicator, ``vsa.py`` (MIT License, (c) 2023
neurotrader888). Stonks' own implementation; no code is copied. The
indicator lives in :func:`stonks.features.vsa.range_volume_deviation`.

Indicator: regress the ATR-normalised bar range on the median-normalised
volume over the previous ``norm_lookback`` bars and take the current bar's
residual (``dev``); 0 when the fit is not a positive relation (slope <= 0
or r < 0.2).

**Trading rule (a Stonks addition — the original publishes the indicator
only, with no entry or exit rule):**

- ``mode="absorption"`` (default): a bar with ``dev < -threshold`` (a small
  range on heavy volume, read as supply/demand being absorbed) triggers a
  long entry.
- ``mode="expansion"``: a bar with ``dev > threshold`` (a wide range on
  light volume) triggers instead.
- The position is held for ``hold_bars`` bars counted from the latest
  trigger (the trigger bar included), then flattened. A new trigger while
  long restarts the count.

Deliberate deviations from the original indicator:

- The regression for bar ``i`` excludes bar ``i`` itself (fit on
  ``[i-n, i-1]``), so the deviation is out-of-sample; see
  :mod:`stonks.features.vsa`.
- Recomputed on every call over the trailing ``2 * norm_lookback +
  hold_bars`` bars (look-ahead safe; the Wilder ATR warm-up starts at the
  window start).
- ``estimate_return`` while long is ``|dev|`` of the triggering bar, the
  strength of the anomaly, not a forecast return.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from stonks.core.params import ParameterSpec
from stonks.features.vsa import range_volume_deviation
from stonks.strategies._common import BarCache
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat, common_specs


class VSAStrategy(SingleTickerLongFlat):
    id = "vsa"
    hypothesis = (
        "A small range on heavy volume shows large players absorbing "
        "selling, so price turns up next. Fails when volume is noise or "
        "the selling wins."
    )
    alpha_family = "reversion"
    premise = "mean_reversion"
    label_horizon_bars = 24

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="norm_lookback",
                kind="int",
                default=168,
                bounds=(48, 504),
                description="Bars for the ATR, the volume median and the rolling "
                "range-on-volume regression.",
            ),
            ParameterSpec(
                name="threshold",
                kind="float",
                default=1.0,
                bounds=(0.5, 2.0),
                description="|deviation| that triggers an entry (Stonks rule).",
            ),
            ParameterSpec(
                name="hold_bars",
                kind="int",
                default=24,
                bounds=(1, 48),
                description="Bars held after the latest trigger (Stonks rule).",
            ),
            ParameterSpec(
                name="mode",
                kind="categorical",
                default="absorption",
                bounds=["absorption", "expansion"],
                tunable=False,
                description="absorption: enter on dev < -threshold; expansion: "
                "enter on dev > threshold.",
            ),
            *common_specs("BTC-USD.CC"),
        ]

    def _evaluate(self, cache: BarCache, ticker: str, as_of: Any) -> dict[str, float] | None:
        n = int(self.params["norm_lookback"])
        hold = int(self.params["hold_bars"])
        threshold = float(self.params["threshold"])
        df = cache.last_n_bars(ticker, self.interval, as_of, 2 * n + hold)
        if len(df) < 2 * n + 1:
            return None
        dev = range_volume_deviation(df["high"], df["low"], df["close"], df["volume"], n)
        if not np.isfinite(dev[-1]):
            return None

        recent = dev[-hold:]
        with np.errstate(invalid="ignore"):
            if self.params["mode"] == "absorption":
                triggers = recent < -threshold
            else:
                triggers = recent > threshold
        hits = np.flatnonzero(triggers)
        long = len(hits) > 0
        trigger_dev = float(recent[hits[-1]]) if long else float("nan")
        return {
            "dev": float(dev[-1]),
            "trigger_dev": trigger_dev,
            "bars_since_trigger": float(len(recent) - 1 - hits[-1]) if long else float("nan"),
            "close": float(df["close"].iloc[-1]),
            "signal": 1.0 if long else 0.0,
            "score": abs(trigger_dev) if long else 0.0,
        }
