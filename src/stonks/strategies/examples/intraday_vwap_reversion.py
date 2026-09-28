"""VWAP reversion on minute bars (roadmap 21.3.1).

Rule, over the closed bars of today's regular session, with the session
VWAP from the open (``features.intraday.session_vwap``):

- no entries before ``warmup_minutes`` after the open (VWAP is unstable)
- flat, a close at least ``entry_bps`` below VWAP (and above the stop
  line) buys
- long, a close at or above VWAP exits (the reversion is done)
- long, a close ``entry_bps * stop_multiple`` or more below VWAP exits
  (the stop), and the strategy does not buy again that session
- flat ``exit_minutes_before_close`` before the close (see ``_intraday``)

Score while long: the gap back up to VWAP, ``vwap / close - 1``.
"""

from __future__ import annotations

from stonks.core.params import ParameterSpec
from stonks.features.intraday import session_vwap
from stonks.strategies.examples._intraday import (
    US_SESSION_MINUTES,
    IntradayStrategy,
    SessionBars,
    bars_for,
)


class VwapReversion(IntradayStrategy):
    id = "intraday_vwap_reversion"
    title = "Back to the day's average"
    summary = (
        "Buys a liquid stock that falls well below its average traded price of the day and "
        "sells as it comes back."
    )
    applicable_asset_classes = ("equity",)
    hypothesis = (
        "Large orders worked through the day push a liquid stock away from "
        "the session VWAP for a while, and the price comes back once the "
        "pressure passes. Buying a stretch below VWAP supplies liquidity to "
        "that impatient seller, who pays us the spread and the temporary "
        "impact. Expected sign: a positive return from the entry back to "
        "VWAP. Fails on trend days and on news, when the move below VWAP is "
        "information, not pressure: the stop caps that loss."
    )
    alpha_family = "reversion"
    premise = "mean_reversion"
    label_horizon_bars = US_SESSION_MINUTES - 15
    required_history_bars = 15

    @classmethod
    def rule_spec(cls) -> list[ParameterSpec]:
        return [
            ParameterSpec(
                name="entry_bps",
                kind="float",
                default=30.0,
                bounds=(5.0, 200.0),
                description="Distance below VWAP, in basis points, that buys.",
            ),
            ParameterSpec(
                name="stop_multiple",
                kind="float",
                default=3.0,
                bounds=(1.5, 6.0),
                description="The stop sits this many entry distances below VWAP.",
            ),
            ParameterSpec(
                name="warmup_minutes",
                kind="int",
                default=15,
                bounds=(5, 60),
                tunable=False,
                description="No entries in the first minutes of the session.",
            ),
        ]

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        warmup = int(p["warmup_minutes"])
        return {
            "label_horizon_bars": bars_for(US_SESSION_MINUTES - warmup, p["interval"]),
            "required_history_bars": bars_for(warmup, p["interval"]),
        }

    def _vwap(self, view: SessionBars):
        b = view.bars
        return session_vwap(
            b["high"].to_numpy(dtype=float),
            b["low"].to_numpy(dtype=float),
            view.close,
            b["volume"].to_numpy(dtype=float),
        )

    def _score(self, view: SessionBars) -> float | None:
        vwap = self._vwap(view)
        close = view.close
        entry = float(self.params["entry_bps"]) / 1e4
        stop = entry * float(self.params["stop_multiple"])
        warmup = float(self.params["warmup_minutes"])
        long = stopped = False
        for i in range(close.size):
            if vwap[i] <= 0:
                continue
            gap = close[i] / vwap[i] - 1.0
            if not long:
                if not stopped and view.elapsed[i] >= warmup and -stop < gap <= -entry:
                    long = True
            elif gap >= 0.0:
                long = False
            elif gap <= -stop:
                long, stopped = False, True
        if not long:
            return None
        return float(vwap[-1] / close[-1] - 1.0)

    def _features(self, view: SessionBars) -> dict[str, float]:
        vwap = float(self._vwap(view)[-1])
        if vwap <= 0:
            return {}
        return {"vwap": vwap, "vwap_gap_bps": (float(view.close[-1]) / vwap - 1.0) * 1e4}
