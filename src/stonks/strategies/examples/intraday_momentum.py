"""Intraday time-series momentum on minute bars (roadmap 21.3.1).

After Gao, Han, Li and Zhou (2018), "Market intraday momentum": the return
from the previous close to the end of the first half hour predicts the
return of the last half hour.

Rule, over the closed bars of today's regular session:

- the signal is the return from the previous session's last close to the
  last close within ``signal_minutes`` of the open
- from ``entry_minutes_before_close`` before the close, a signal above
  ``threshold_bps`` buys
- flat ``exit_minutes_before_close`` before the close (see ``_intraday``)

Score while long: the signal return.
"""

from __future__ import annotations

from stonks.core.params import ParameterSpec
from stonks.strategies.examples._intraday import (
    US_SESSION_MINUTES,
    IntradayStrategy,
    SessionBars,
    bars_for,
)


class IntradayMomentum(IntradayStrategy):
    id = "intraday_momentum"
    title = "Intraday momentum"
    summary = "Buys near the close after a strong first half hour, expecting the move to carry on."
    applicable_asset_classes = ("equity", "commodity")
    needs_previous_session = True
    hypothesis = (
        "Overnight news is priced by the end of the first half hour, but "
        "slow traders, and dealers who hedge their gamma late in the day, "
        "trade in the same direction near the close. So the first half "
        "hour's return, from the previous close, predicts the last half "
        "hour's return in liquid index products and futures. We buy after "
        "a strong start and supply the late flow's other side at the close. "
        "Fails on days with a news shock in the afternoon, on thin markets, "
        "and where the late flow is small against costs."
    )
    alpha_family = "trend"
    premise = "trend"
    label_horizon_bars = 30
    required_history_bars = US_SESSION_MINUTES + 30

    @classmethod
    def rule_spec(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec(
                name="signal_minutes",
                kind="int",
                default=30,
                bounds=(15, 90),
                description="Minutes after the open that end the signal return.",
            ),
            ParameterSpec(
                name="entry_minutes_before_close",
                kind="int",
                default=30,
                bounds=(10, 90),
                description="Buy from this many minutes before the close.",
            ),
            ParameterSpec(
                name="threshold_bps",
                kind="float",
                default=0.0,
                bounds=(0.0, 100.0),
                tunable=False,
                description="The signal return must beat this, in basis points.",
            ),
        ]

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "label_horizon_bars": bars_for(int(p["entry_minutes_before_close"]), p["interval"]),
            "required_history_bars": bars_for(
                US_SESSION_MINUTES + int(p["signal_minutes"]), p["interval"]
            ),
        }

    def _signal(self, view: SessionBars) -> float | None:
        prev = view.previous_close
        window = float(self.params["signal_minutes"])
        if prev is None or prev <= 0 or view.minutes_open < window:
            return None
        inside = view.close[view.elapsed <= window]
        if inside.size == 0:
            return None
        return float(inside[-1] / prev - 1.0)

    def _score(self, view: SessionBars) -> float | None:
        if view.minutes_to_close > int(self.params["entry_minutes_before_close"]):
            return None
        signal = self._signal(view)
        if signal is None or signal * 1e4 <= float(self.params["threshold_bps"]):
            return None
        return signal

    def _features(self, view: SessionBars) -> dict[str, float]:
        signal = self._signal(view)
        return {} if signal is None else {"signal_return": signal}
