"""TimeSeriesMomentum — each instrument against its own past (BL-40;
Moskowitz, Ooi & Pedersen; Hurst, Ooi & Pedersen; Clenow, *Trading
Evolved*).

Rules, per ticker, on adjusted daily closes:

1. For each lookback ``L`` in ``lookbacks`` (125 and 250 sessions, about
   six and twelve months) take the log return ``r_L = ln(C_t / C_{t-L})``.
2. Rule forecast:
   - ``mode="sign"``: ``10 * sign(r_L)``, the classic TSMOM bet;
   - ``mode="scaled"``: ``r_L / (sigma_t sqrt(L))`` (the return in units of
     its own expected std, so a quiet steady climb counts more than a
     noisy one) times ``10 / sqrt(2/pi)``, which averages 10 on a random
     walk; capped at +/-20.
3. Combine: the equal-weight mean times ``fdm`` (1.1), capped at +/-20.
4. ``rebalance="monthly"`` (default) holds the forecast set on the last
   session of each month (about every 21 bars) until the next one; sizing
   still tracks the day's volatility through ``vol_target``, and the 10%
   no-trade buffer absorbs small drifts. ``"daily"`` re-reads it every bar.
5. Long only: a negative forecast is flat (the short leg once shorts land
   in Phase 16).

Applies to equities, crypto and commodities. Sizing, look-ahead and order
rules are in :mod:`stonks.strategies.examples._forecast_trend`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.features.forecast import DEFAULT_VOL_SPAN, TSMOM_SCALED_SCALAR, cap_forecast, tsmom_raw
from stonks.portfolio.signals import FORECAST_TARGET
from stonks.strategies.examples._forecast_trend import (
    MIN_ESTIMATION_BARS,
    ForecastTrendStrategy,
    forecast_specs,
    period_end_mask,
    rule_scalar,
    sizing_specs,
)

LOOKBACK_SETS = ["125,250", "63,125,250", "21,63,125,250", "250", "125"]


class TimeSeriesMomentum(ForecastTrendStrategy):
    id = "tsmom"
    hypothesis = (
        "An instrument's own past 6-12 month return predicts its next month's "
        "return across equities, commodities and crypto: news diffuses slowly "
        "and flows chase performance (Moskowitz, Ooi & Pedersen). We are paid "
        "by contrarians and forced sellers. Fails at trend reversals, where "
        "the long lookback is late, and in sideways markets."
    )
    label_horizon_bars = 21
    required_history_bars = 251

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="lookbacks",
                kind="categorical",
                default=LOOKBACK_SETS[0],
                bounds=LOOKBACK_SETS,
                description="Return lookbacks in sessions; one rule each.",
            ),
            ParameterSpec(
                name="mode",
                kind="categorical",
                default="sign",
                bounds=["sign", "scaled"],
                tunable=False,
                description="'sign': 10 * sign(r); 'scaled': r over its std, scaled to mean |f| 10.",
            ),
            ParameterSpec(
                name="vol_span",
                kind="int",
                default=DEFAULT_VOL_SPAN,
                bounds=(10, 100),
                tunable=False,
                description="EWMA span of the daily return sigma in 'scaled' mode.",
            ),
            ParameterSpec(
                name="rebalance",
                kind="categorical",
                default="monthly",
                bounds=["monthly", "daily"],
                tunable=False,
                description="Refresh the forecast on each month's last session, or every bar.",
            ),
            *forecast_specs(),
            *sizing_specs(),
        ]

    def lookbacks(self) -> list[int]:
        return [int(x) for x in str(self.params["lookbacks"]).split(",")]

    def _history_bars(self) -> int:
        return max(self.lookbacks()) + 1 + 2 * MIN_ESTIMATION_BARS

    def _min_bars(self) -> int:
        return max(self.lookbacks()) + 1

    def rule_forecasts(self, closes: pd.Series) -> pd.DataFrame:
        """Each lookback's capped forecast, one column per lookback
        (``tsmom125``, ...), aligned to ``closes``."""
        mode = str(self.params["mode"])
        fixed = FORECAST_TARGET if mode == "sign" else TSMOM_SCALED_SCALAR
        rules = {}
        for lookback in self.lookbacks():
            raw = tsmom_raw(closes, lookback, mode, vol_span=int(self.params["vol_span"]))
            scalar = rule_scalar(raw, fixed, str(self.params["scalar_mode"]))
            rules[f"tsmom{lookback}"] = cap_forecast(raw * scalar)
        return pd.DataFrame(rules)

    def _signed_forecast(self, bars: pd.DataFrame, asset_class: str) -> float | None:
        if self.params["rebalance"] == "monthly":
            # the forecast as it stood at the latest month end: bars after it
            # are not read at all
            days = [ts.date() for ts in pd.to_datetime(bars["timestamp"])]
            ends = np.flatnonzero(period_end_mask(days, asset_class, "month"))
            if not ends.size:
                return None
            bars = bars.iloc[: int(ends[-1]) + 1]
        combined = self._combine(self.rule_forecasts(bars["close"].astype(float)))
        value = combined.iloc[-1] if len(combined) else np.nan
        return None if pd.isna(value) else float(value)
