"""HeadShouldersStrategy — long the inverse head & shoulders breakout.

Ported from ``head_shoulders.py`` in neurotrader888's MIT-licensed
``TechnicalAnalysisAutomation`` repository
(https://github.com/neurotrader888/TechnicalAnalysisAutomation, Copyright (c)
neurotrader888). Independent re-implementation of the published algorithm.

Rule (on log closes, all counts in bars):

- Swing points are rolling-window extremes of order ``order``
  (:func:`stonks.features.extremes.rw_extremes`), each usable only from its
  confirmation bar on.
- Candidate: the last four alternating extremes ending with a top —
  left shoulder (bottom), left armpit (top), head (bottom), right armpit
  (top). The right shoulder is the lowest close since the right armpit
  (excluding the current bar).
- Valid when the head is below both shoulders; balance: each shoulder is
  below the midpoint of the other shoulder and its armpit; symmetry: the
  shoulder-to-head times are within 2.5x of each other; and the price
  before the left shoulder was above the (extended) neckline within one
  head width.
- Neckline through the two armpits. Entry on the first bar whose close is
  above the neckline projected to that bar (``early_find``: above the
  midpoint of the right shoulder and right armpit instead).
- Exit when the close reaches ``target = neckline + head height`` or falls
  to the right-shoulder price (stop), or after
  ``round(head_width * hold_mult)`` bars, whichever comes first.

The live trade is reconstructed on every call by replaying a fixed window of
the latest bars from scratch, so a fresh production instance and a
backtest that stepped through every bar reach the same state for the same
``as_of``.

Deliberate deviations from the original:

- Long-only: the regular (bearish) head & shoulders is not traded; the
  Stonks broker cannot short, so that leg is simply flat.
- Entry requires the close to be strictly above the trigger level (the
  original accepted equality).
- While a trade is live no new pattern is taken (one position at a time);
  the original catalogued every pattern independently.
- The stop and target were used by the original only to measure pattern
  returns; here they drive the exit, and the max hold is scaled by
  ``hold_mult`` (the original held at most one head width).
- The pattern R-squared attribute (unused as a filter upstream) is dropped.
- History is limited to the last ``100 * order + 200`` bars, and a pattern
  is only taken when every bar it depends on stays inside that sliding
  window for its whole max hold (so a trade never vanishes mid-life as the
  window moves on).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from stonks.core.params import ParameterSpec
from stonks.features.extremes import Extreme, rw_extremes
from stonks.strategies._common import BarCache
from stonks.strategies.examples._nt888_base import SingleTickerLongFlat, common_specs


@dataclass(frozen=True)
class IHSTrade:
    """A live inverse head & shoulders trade. Prices are log closes;
    indices are relative to the replayed window."""

    entry_index: int
    neckline: float
    stop: float
    target: float
    max_hold: int
    head_width: int


def _check_inverse_hs(
    logc: np.ndarray,
    i: int,
    confirmed: list[Extreme],
    order: int,
    early_find: bool,
    hold_mult: float,
    window_bars: int | None,
) -> IHSTrade | None:
    if len(confirmed) < 4:
        return None
    if confirmed[-1].kind == "top":
        quad = confirmed[-4:]
    elif len(confirmed) >= 5:
        quad = confirmed[-5:-1]
    else:
        return None
    if [e.kind for e in quad] != ["bottom", "top", "bottom", "top"]:
        return None
    ls, la, hd, ra = (e.ext_index for e in quad)
    if i - ra < 2:
        return None

    rs = ra + 1 + int(np.argmin(logc[ra + 1 : i]))
    ls_p, la_p, hd_p, ra_p, rs_p = logc[ls], logc[la], logc[hd], logc[ra], logc[rs]
    if hd_p >= min(ls_p, rs_p):
        return None
    r_mid = 0.5 * (rs_p + ra_p)
    l_mid = 0.5 * (ls_p + la_p)
    if ls_p > r_mid or rs_p > l_mid:
        return None
    r_time, l_time = rs - hd, hd - ls
    if r_time > 2.5 * l_time or l_time > 2.5 * r_time:
        return None

    head_width = ra - la
    slope = (ra_p - la_p) / head_width

    def neck(x: int) -> float:
        return la_p + (x - la) * slope

    trigger = r_mid if early_find else neck(i)
    if logc[i] <= trigger:
        return None

    # the pattern must start from above the neckline: somewhere within one
    # head width before the left shoulder the price was above it
    for j in range(1, head_width):
        x = ls - j
        if x < 0:
            return None
        if logc[x] > neck(x):
            break
    else:
        return None

    # Window stability: the replay window slides one bar per evaluation, so
    # every bar this pattern depends on (its start, and the left shoulder's
    # rolling-window neighbourhood) must stay inside a window of
    # ``window_bars`` for the whole max hold; otherwise the trade would
    # vanish mid-life on a later bar. The span is measured in bars, so the
    # check gives the same answer wherever the window sits.
    max_hold = max(1, round(head_width * hold_mult))
    if window_bars is not None and i - min(x, ls - order) + max_hold > window_bars:
        return None

    head_height = neck(hd) - hd_p
    return IHSTrade(
        entry_index=i,
        neckline=neck(i),
        stop=float(rs_p),
        target=neck(i) + head_height,
        max_hold=max_hold,
        head_width=head_width,
    )


def replay_inverse_hs(
    log_close: np.ndarray,
    order: int,
    early_find: bool,
    hold_mult: float,
    window_bars: int | None = None,
) -> IHSTrade | None:
    """Replay the inverse H&S rule bar by bar over ``log_close`` and return
    the trade live at the last bar, or ``None`` when flat.

    Only extremes confirmed at or before each bar are consulted. With
    ``window_bars`` (the size of the caller's sliding replay window),
    patterns whose bars would leave that window before their max hold ends
    are skipped.
    """
    logc = np.asarray(log_close, dtype=float)
    extremes = rw_extremes(logc, order)
    confirmed: list[Extreme] = []
    ptr = 0
    locked = False  # the pattern ending at the last top was already taken
    trade: IHSTrade | None = None
    for i in range(len(logc)):
        while ptr < len(extremes) and extremes[ptr].conf_index == i:
            ext = extremes[ptr]
            confirmed.append(ext)
            if ext.kind == "top":
                locked = False
            ptr += 1
        if trade is not None:
            expired = i - trade.entry_index >= trade.max_hold
            if expired or logc[i] >= trade.target or logc[i] <= trade.stop:
                trade = None
            else:
                continue
        if locked:
            continue
        found = _check_inverse_hs(logc, i, confirmed, order, early_find, hold_mult, window_bars)
        if found is not None:
            locked = True
            trade = found
    return trade


class HeadShouldersStrategy(SingleTickerLongFlat):
    id = "head_shoulders"
    title = "Inverse head and shoulders"
    summary = "Buys when price breaks up through the neckline of an inverse head and shoulders."
    hypothesis = (
        "An inverse head and shoulders marks the end of a downtrend. A "
        "close through the neckline traps short sellers and starts a "
        "rally. Fails when the pattern is noise or the downtrend resumes."
    )
    alpha_family = "reversion"
    premise = "mean_reversion"
    label_horizon_bars = 10
    required_history_bars = 27

    def param_metadata(self) -> dict[str, int]:
        p = self.params
        return {
            "required_history_bars": 4 * int(p["order"]) + 3,
        }

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="order",
                kind="int",
                default=6,
                bounds=(2, 48),
                description="Rolling-window extreme order in bars (a swing point must be "
                "the extreme of +/- order bars; confirmed order bars later).",
            ),
            ParameterSpec(
                name="early_find",
                kind="bool",
                default=False,
                description="Enter when price clears the right-shoulder midpoint instead "
                "of the neckline.",
            ),
            ParameterSpec(
                name="hold_mult",
                kind="float",
                default=1.0,
                bounds=(0.5, 3.0),
                description="Maximum hold as a multiple of the head width (in bars).",
            ),
            *common_specs("BTC-USD.CC"),
        ]

    def history_bars(self) -> int:
        """Bars replayed per evaluation."""
        return 100 * int(self.params["order"]) + 200

    def _evaluate(self, cache: BarCache, ticker: str, as_of: Any) -> dict[str, float] | None:
        order = int(self.params["order"])
        closes = cache.last_n_closes(ticker, self.interval, as_of, self.history_bars())
        if len(closes) < 4 * order + 3 or not np.all(closes > 0):
            return None
        logc = np.log(closes)
        trade = replay_inverse_hs(
            logc,
            order,
            bool(self.params["early_find"]),
            float(self.params["hold_mult"]),
            window_bars=self.history_bars(),
        )
        close = float(closes[-1])
        if trade is None:
            return {"close": close, "in_trade": 0.0, "signal": 0.0, "score": 0.0}
        return {
            "close": close,
            "in_trade": 1.0,
            "bars_in_trade": float(len(logc) - 1 - trade.entry_index),
            "neckline": float(np.exp(trade.neckline)),
            "stop": float(np.exp(trade.stop)),
            "target": float(np.exp(trade.target)),
            "signal": 1.0,
            # distance to the pattern's price target
            "score": float(np.exp(trade.target - logc[-1]) - 1.0),
        }
