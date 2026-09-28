"""EWMACTrend — Carver's exponentially weighted moving-average crossover
at several speeds (BL-40; *Systematic Trading*, *Leveraged Trading*).

Rules, per ticker, on adjusted daily closes:

1. For each fast span ``f`` in ``speeds`` (8, 16, 32, 64) the raw signal is
   ``(EMA_f - EMA_4f) / sigma_price``, with ``sigma_price`` the zero-mean
   EWMA (span ``vol_span`` = 36) std of daily price changes.
2. Scale each rule to a forecast averaging 10 in absolute value with
   Carver's scalars ``{8: 5.3, 16: 3.75, 32: 2.65, 64: 1.87}`` (or, with
   ``scalar_mode="estimate"``, ``10 / mean|raw|`` over the ticker's own
   history up to ``as_of``), and cap each at +/-20.
3. Combine: the equal-weight mean times the forecast diversification
   multiplier (``fdm`` 1.1, or estimated from the rules' past forecasts),
   capped at +/-20.
4. Long only: a positive forecast is held, sized by ``vol_target``; a
   negative one is flat (it becomes the short leg once shorts land in
   Phase 16).

Each evaluation reads the last ``16 * max(speeds)`` bars (so the slowest
average's seed weighs under 0.1%) and needs at least ``4 * max(speeds)``.
Applies to equities, crypto and commodities. Sizing, look-ahead and
order rules are in :mod:`stonks.strategies.examples._forecast_trend`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from stonks.core.params import ParameterSpec
from stonks.features.forecast import (
    DEFAULT_VOL_SPAN,
    EWMAC_FORECAST_SCALARS,
    cap_forecast,
    ewmac_raw,
)
from stonks.strategies._vectorized import forecast_weights
from stonks.strategies.examples._forecast_trend import (
    ForecastTrendStrategy,
    forecast_specs,
    require_fixed_modes,
    rule_scalar,
    sizing_specs,
)

SPEED_SETS = ["8,16,32,64", "16,32,64", "4,8,16,32", "2,4,8,16,32,64", "32,64", "16"]
#: Bars read per evaluation, as a multiple of the slowest EMA span.
HISTORY_MULTIPLE = 4


class EWMACTrend(ForecastTrendStrategy):
    id = "ewmac_trend"
    title = "Moving average trend"
    summary = (
        "Follows trends in any market by comparing a fast and a slow moving average, sized by "
        "how strong the trend is."
    )
    hypothesis = (
        "Prices underreact to news and then overshoot as trend followers and "
        "hedgers pile in, so the sign of a fast-minus-slow moving average "
        "predicts the next weeks' returns across asset classes (Carver; Hurst, "
        "Ooi & Pedersen). We are paid by those who fade trends or must trade "
        "against them (hedgers, rebalancers). Fails in choppy, range-bound "
        "markets and at sharp reversals."
    )
    label_horizon_bars = 21
    required_history_bars = 256

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="speeds",
                kind="categorical",
                default=SPEED_SETS[0],
                bounds=SPEED_SETS,
                description="Fast EMA spans; each rule's slow span is 4x its fast one.",
            ),
            ParameterSpec(
                name="vol_span",
                kind="int",
                default=DEFAULT_VOL_SPAN,
                bounds=(10, 100),
                tunable=False,
                description="EWMA span of the daily price-change sigma.",
            ),
            *forecast_specs(),
            *sizing_specs(),
        ]

    def speeds(self) -> list[int]:
        return [int(s) for s in str(self.params["speeds"]).split(",")]

    def _history_bars(self) -> int:
        return HISTORY_MULTIPLE * 4 * max(self.speeds())

    def _min_bars(self) -> int:
        return 4 * max(self.speeds())

    def rule_forecasts(self, closes: pd.Series) -> pd.DataFrame:
        """Each speed's scaled and capped forecast, one column per speed
        (``ewmac8``, ...), aligned to ``closes``."""
        vol_span = int(self.params["vol_span"])
        mode = str(self.params["scalar_mode"])
        rules = {}
        for fast in self.speeds():
            raw = ewmac_raw(closes, fast, vol_span=vol_span)
            scalar = rule_scalar(raw, EWMAC_FORECAST_SCALARS[fast], mode)
            rules[f"ewmac{fast}"] = cap_forecast(raw * scalar)
        return pd.DataFrame(rules)

    def _signed_forecast(self, bars: pd.DataFrame, asset_class: str) -> float | None:
        rules = self.rule_forecasts(bars["close"].astype(float))
        value = self._combine(rules).iloc[-1]
        return None if pd.isna(value) else float(value)

    # ---- vectorised fast path (lab/vectorized.py) ---------------------------

    @classmethod
    def target_positions(cls, closes: pd.DataFrame, params: Mapping[str, Any]) -> pd.DataFrame:
        """Each ticker's combined forecast sized the ``vol_target`` way, for
        a whole table of daily closes. Close to the event engine, not exact:
        no 10% no-trade buffer, an IDM of 1, and the EMAs seeded at the
        table's first row rather than a trailing window. Only the fixed
        scalar and FDM modes are supported."""
        strategy = cls(dict(params))
        require_fixed_modes(strategy.params)
        return forecast_weights(
            closes,
            lambda column: strategy._combine(strategy.rule_forecasts(column)),
            tau=float(strategy.params["tau"]),
            allow_short=strategy.supports_short,
        )
