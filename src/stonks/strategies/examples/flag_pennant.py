"""FlagPennantStrategy — long the breakout of a bull flag or pennant.

Ported from ``flags_pennants.py`` in neurotrader888's MIT-licensed
``TechnicalAnalysisAutomation`` repository
(https://github.com/neurotrader888/TechnicalAnalysisAutomation, Copyright (c)
neurotrader888). Independent re-implementation of the published algorithm.

Two detectors (``variant``), both on log prices with counts in bars and
swing points from :func:`stonks.features.extremes.rw_extremes` (usable only
from their confirmation bar on):

``pips``
    The pole starts at the last confirmed bottom and runs to the highest
    close since. The flag is everything after the pole tip; it must be at
    least ``max(5, order / 2)`` bars long, at most half the pole's width and
    at most half its height. Five perceptually important points (vertical
    distance) of the flag must zig-zag (the middle one above its two
    neighbours); resistance runs through PIPs 0 and 2, support through PIPs
    1 and 3. Lines that cross inside the flag, or diverge sharply (cross
    less than one flag width behind the tip), are rejected. Confirmed when
    the close (the last PIP) is above resistance.

``trendline``
    When a top confirms, the pole runs from the last confirmed bottom to
    that top. Every later bar is a candidate breakout: the flag (bars from
    the tip up to the previous bar) may not exceed the tip, must be at most
    half the pole's width and three quarters of its height; support and
    resistance lines are fitted to the flag's highs and lows
    (:func:`stonks.features.library.fit_trendlines_high_low`). Confirmed
    when the close is above resistance projected to the current bar.

A confirmed pattern opens a long held for ``int(flag_width * hold_mult)``
bars. The live trade is rebuilt on every call by replaying a fixed window of
the latest bars from scratch, so a fresh production instance matches a
backtest that stepped through every bar.

Deliberate deviations from the original:

- Long-only: bear flags / pennants are not traded (the Stonks broker cannot
  short), so that leg is flat.
- The trendline variant fits the flag's highs and lows
  (``fit_trendlines_high_low``) rather than closes only, and projects
  resistance to the current bar (x = flag width); the original projected
  one bar further (``flag_width + 1``).
- Breakouts must be strictly above resistance (the original accepted
  equality in the PIP variant).
- While a trade is live no new pattern is taken (one position at a time).
- The hold period (``int(flag_width * hold_mult)``, at least one bar) was a
  research measurement in the original; here it is the exit rule.
- ``estimate_return`` uses the classic measured move (breakout close plus
  pole height) as its target; the original defined no target.
- History is limited to the last ``60 * order + 200`` bars; a pattern whose
  bars would slide out of that window before its hold ends is skipped, so a
  trade never vanishes mid-life.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.extremes import find_pips, rw_extremes
from stonks.features.library import fit_trendlines_high_low
from stonks.strategies._common import LakeBarCaches, iso
from stonks.strategies.base import BaseStrategy

VARIANTS = ["pips", "trendline"]


@dataclass(frozen=True)
class FlagTrade:
    """A live bull flag / pennant trade. Prices are logs; indices are
    relative to the replayed window."""

    entry_index: int
    base_index: int
    tip_index: int
    flag_width: int
    pole_width: int
    pole_height: float
    resist_at_entry: float
    pennant: bool
    hold: int


def _hold(flag_width: int, hold_mult: float) -> int:
    return max(1, int(flag_width * hold_mult))


def _check_pips(
    logc: np.ndarray, i: int, base: int, order: int, hold_mult: float
) -> FlagTrade | None:
    tip = base + int(np.argmax(logc[base : i + 1]))
    flag_width = i - tip
    if flag_width < max(5, order * 0.5):
        return None
    pole_width = tip - base
    if flag_width > 0.5 * pole_width:
        return None
    pole_height = logc[tip] - logc[base]
    flag_height = logc[tip] - logc[tip : i + 1].min()
    if pole_height <= 0 or flag_height > 0.5 * pole_height:
        return None

    px, py = find_pips(logc[tip : i + 1], 5, 3)
    if len(px) < 5 or not (py[2] > py[1] and py[2] > py[3]):
        return None
    r_slope = (py[2] - py[0]) / (px[2] - px[0])
    r_int = py[0]
    s_slope = (py[3] - py[1]) / (px[3] - px[1])
    s_int = py[1] - px[1] * s_slope
    # parallel lines never cross: treat as crossing far behind the tip
    parallel = r_slope == s_slope
    cross = -100.0 * flag_width if parallel else (s_int - r_int) / (r_slope - s_slope)
    if 0 <= cross <= px[4]:  # lines meet inside the flag
        return None
    if -flag_width < cross < 0:  # sharply diverging
        return None
    resist = r_int + r_slope * px[4]
    if py[4] <= resist:
        return None
    return FlagTrade(
        entry_index=i,
        base_index=base,
        tip_index=tip,
        flag_width=flag_width,
        pole_width=pole_width,
        pole_height=float(pole_height),
        resist_at_entry=float(resist),
        pennant=bool(s_slope > 0),
        hold=_hold(flag_width, hold_mult),
    )


def _check_trendline(
    log_high: np.ndarray,
    log_low: np.ndarray,
    logc: np.ndarray,
    i: int,
    base: int,
    tip: int,
    hold_mult: float,
) -> FlagTrade | None:
    flag_width = i - tip
    if flag_width < 2:  # need two flag bars to fit a line
        return None
    if logc[tip + 1 : i].max() > logc[tip]:
        return None
    pole_height = logc[tip] - logc[base]
    pole_width = tip - base
    flag_height = logc[tip] - logc[tip:i].min()
    if flag_width > 0.5 * pole_width:
        return None
    if pole_height <= 0 or flag_height > 0.75 * pole_height:
        return None
    support, resist = fit_trendlines_high_low(log_high[tip:i], log_low[tip:i], logc[tip:i])
    resist_now = resist[1] + resist[0] * flag_width
    if logc[i] <= resist_now:
        return None
    return FlagTrade(
        entry_index=i,
        base_index=base,
        tip_index=tip,
        flag_width=flag_width,
        pole_width=pole_width,
        pole_height=float(pole_height),
        resist_at_entry=float(resist_now),
        pennant=bool(support[0] > 0),
        hold=_hold(flag_width, hold_mult),
    )


def replay_bull_flag(
    log_high: np.ndarray,
    log_low: np.ndarray,
    log_close: np.ndarray,
    order: int,
    variant: str,
    hold_mult: float,
    window_bars: int | None = None,
) -> FlagTrade | None:
    """Replay the bull flag rule bar by bar and return the trade live at the
    last bar, or ``None`` when flat. Only extremes confirmed at or before
    each bar are consulted.

    With ``window_bars`` (the size of the caller's sliding replay window), a
    pattern is skipped when the bars it depends on (from the pole base's
    rolling-window neighbourhood on) would leave that window before its hold
    ends, so a trade never vanishes mid-life as the window slides.
    """
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, got {variant!r}")
    log_high = np.asarray(log_high, dtype=float)
    log_low = np.asarray(log_low, dtype=float)
    logc = np.asarray(log_close, dtype=float)
    extremes = rw_extremes(logc, order)
    ptr = 0
    pending: tuple[int, int] | None = None  # (base, tip); tip unused for pips
    last_bottom: int | None = None
    trade: FlagTrade | None = None
    for i in range(len(logc)):
        while ptr < len(extremes) and extremes[ptr].conf_index == i:
            ext = extremes[ptr]
            ptr += 1
            if ext.kind == "bottom":
                last_bottom = ext.ext_index
                if variant == "pips":
                    pending = (ext.ext_index, -1)
            elif variant == "trendline" and last_bottom is not None:
                pending = (last_bottom, ext.ext_index)
        if trade is not None:
            if i - trade.entry_index < trade.hold:
                continue
            trade = None
        if pending is None:
            continue
        base, tip = pending
        if variant == "pips":
            found = _check_pips(logc, i, base, order, hold_mult)
        else:
            found = _check_trendline(log_high, log_low, logc, i, base, tip, hold_mult)
        if found is None:
            continue
        span = i - (found.base_index - order) + found.hold
        if window_bars is not None and span > window_bars:
            continue
        trade = found
        pending = None
    return trade


class FlagPennantStrategy(BaseStrategy):
    id = "flag_pennant"
    applicable_asset_classes = ("crypto", "equity")

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="variant",
                kind="categorical",
                default="pips",
                bounds=list(VARIANTS),
                description="Flag detector: perceptually important points or fitted trendlines.",
            ),
            ParameterSpec(
                name="order",
                kind="int",
                default=12,
                bounds=(3, 48),
                description="Rolling-window extreme order in bars used to find pole "
                "bases (and tips for the trendline variant).",
            ),
            ParameterSpec(
                name="hold_mult",
                kind="float",
                default=1.0,
                bounds=(0.5, 3.0),
                description="Hold period as a multiple of the flag width (in bars).",
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
        trade, logc = state
        close = float(np.exp(logc[-1]))
        if trade is None:
            return Features(values={"close": close, "in_trade": 0.0})
        return Features(
            values={
                "close": close,
                "in_trade": 1.0,
                "bars_in_trade": float(len(logc) - 1 - trade.entry_index),
                "flag_width": float(trade.flag_width),
                "pole_width": float(trade.pole_width),
                "pole_height": trade.pole_height,
                "pennant": float(trade.pennant),
            }
        )

    def estimate_return(self, ticker: str, as_of, lake: Any) -> float | None:
        if ticker != self.params["ticker"]:
            return None
        state = self._state(ticker, as_of, lake)
        if state is None or state[0] is None:
            return None
        trade, logc = state
        target = logc[trade.entry_index] + trade.pole_height
        return max(float(np.exp(target - logc[-1]) - 1.0), 1e-6)

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
        return 60 * int(self.params["order"]) + 200

    def _state(self, ticker: str, as_of, lake: Any) -> tuple[FlagTrade | None, np.ndarray] | None:
        if lake is None:
            return None
        order = int(self.params["order"])
        interval = Interval.parse(self.params["interval"])
        bars = self._bar_caches.for_lake(lake).last_n_bars(
            ticker, interval, as_of, self.history_bars()
        )
        if len(bars) < 2 * order + 6:
            return None
        cols = [bars[c].to_numpy(dtype=float) for c in ("high", "low", "close")]
        if any(np.any(~(c > 0)) for c in cols):
            return None
        log_high, log_low, logc = (np.log(c) for c in cols)
        trade = replay_bull_flag(
            log_high,
            log_low,
            logc,
            order,
            self.params["variant"],
            float(self.params["hold_mult"]),
            window_bars=self.history_bars(),
        )
        return trade, logc
