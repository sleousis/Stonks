"""EarningsDrift: post-earnings announcement drift (Ball and Brown 1968,
Bernard and Thomas 1989).

After a company reports, buy it when its standardized surprise is in the
top bucket, and hold it for ``hold_days`` weekdays.

- **Surprise.** Reported EPS less the consensus estimate, from the lake's
  ``earnings_calendar`` (``stonks calendars refresh``). It is scaled by
  the spread of the company's own last ``history`` surprises (at least
  four), which gives the standardized surprise (SUE).
- **Top bucket.** The SUE is compared with the SUEs of every report from
  the ``peer_days`` calendar days before the report day (the companies in
  ``universe``, or every company in the calendar when it is empty). It is a
  pick when it reaches the top one of ``buckets`` buckets and is positive.
  At least ``min_peers`` peer reports are needed. Peers are reports from
  earlier days only, so the bucket never changes after the report.
- **Point in time (P12).** A report counts from the latest moment its
  timing allows (:mod:`stonks.calendars.known`): a daily decision on the
  report day sees it and fills at the next open. Reports without an actual
  EPS (upcoming ones) are ignored.
- **Hold.** Counted in weekdays from the first session that can react: the
  report day for a report before the open or in the session, the next
  weekday for one after the close or with no timing.

A held name scores its SUE, so the biggest surprises rank first. Decide:
equal weight across the current picks, bought with the free cash, and sell
names that stop being picks. Long only.
"""

from __future__ import annotations

import weakref
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.calendars.known import known_moments
from stonks.core.interval import decision_reach
from stonks.core.params import ParameterSpec
from stonks.core.timeutil import as_datetime
from stonks.core.types import AssetClass, Order, Portfolio
from stonks.strategies._common import (
    current_decision_interval,
    equal_weight_new_picks,
    memo_scope,
)
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import parse_universe

#: Fewest earlier surprises of the same company that scale a surprise.
MIN_PRIOR_REPORTS = 4
#: Smallest surprise spread, in EPS units (one cent), so a company whose
#: past surprises were all equal does not divide by zero.
MIN_SPREAD = 0.01
_REACTS_SAME_DAY = ("before", "during")


def _numbers(column: Any) -> np.ndarray:
    """A column as floats, NaN where it is not a number."""
    return np.asarray(pd.to_numeric(column, errors="coerce"), dtype=float)


def _weekday_on_or_after(day: Any) -> np.datetime64:
    return np.busday_offset(np.datetime64(day, "D"), 0, roll="forward")


def first_session(report_date: date, timing: str | None) -> np.datetime64:
    """The first weekday whose prices can react to the report."""
    if timing in _REACTS_SAME_DAY:
        return _weekday_on_or_after(report_date)
    return _weekday_on_or_after(report_date + timedelta(days=1))


class EarningsDrift(BaseStrategy):
    id = "earnings_drift"
    title = "Earnings surprise drift"
    summary = (
        "Buys a company after earnings that beat the estimate by much more than usual "
        "and holds it for about two months."
    )
    hypothesis = (
        "Prices underreact to earnings news, so after a big positive surprise "
        "they keep drifting up for weeks (post-earnings announcement drift, "
        "Ball and Brown 1968, Bernard and Thomas 1989). Investors who anchor "
        "on the old view sell to us too early. Fails when the market prices "
        "news fast, as the drift has shrunk in large US stocks since the "
        "1990s, and when estimates in the data were revised after the fact."
    )
    alpha_family = "sentiment"
    premise = "trend"
    label_horizon_bars = 40
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = ("equity",)

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._memo: weakref.WeakKeyDictionary[Any, dict[Any, Any]] = weakref.WeakKeyDictionary()

    def param_metadata(self) -> dict[str, int]:
        return {"label_horizon_bars": int(self.params["hold_days"])}

    @classmethod
    def parameter_spec(cls):
        return [
            ParameterSpec(
                name="hold_days",
                kind="int",
                default=40,
                bounds=(5, 90),
                description="Weekdays a pick is held from the first session after the report.",
            ),
            ParameterSpec(
                name="buckets",
                kind="int",
                default=10,
                bounds=(3, 20),
                tunable=False,
                description="Buckets the surprises are split into; only the top one is bought.",
            ),
            ParameterSpec(
                name="history",
                kind="int",
                default=8,
                bounds=(MIN_PRIOR_REPORTS, 20),
                tunable=False,
                description="Earlier reports of the company that scale its surprise.",
            ),
            ParameterSpec(
                name="peer_days",
                kind="int",
                default=90,
                bounds=(30, 365),
                tunable=False,
                description="Calendar days of other reports the surprise is ranked against.",
            ),
            ParameterSpec(
                name="min_peers",
                kind="int",
                default=20,
                bounds=(5, 1000),
                tunable=False,
                description="Fewest peer reports needed to rank a surprise.",
            ),
            ParameterSpec(
                name="universe",
                kind="categorical",
                default="",
                bounds=None,
                tunable=False,
                description=(
                    "Companies whose reports set the buckets, comma separated. "
                    "Empty: every company in the earnings calendar."
                ),
            ),
        ]

    # ---- point-in-time reads -------------------------------------------------------

    def _reach(self, as_of: Any, lake: Any) -> datetime:
        """The close of the decision: nothing after it is known."""
        own = decision_reach(as_datetime(as_of), current_decision_interval())
        view = getattr(lake, "reach", None)
        return min(own, view) if isinstance(view, datetime) else own

    def _slot(self, lake: Any) -> dict[Any, Any]:
        scope = memo_scope(lake)
        try:
            slot = self._memo.get(scope)
            if slot is None:
                slot = {}
                self._memo[scope] = slot
        except TypeError:  # a lake that cannot be weakly referenced
            return {}
        return slot

    def _surprises(self, lake: Any, reach: datetime) -> pd.DataFrame | None:
        """Every report out by ``reach`` with an actual and an estimate, and
        its SUE (NaN with too little history), oldest first."""
        reader = getattr(lake, "get_earnings_calendar", None)
        if not callable(reader):
            return None
        slot = self._slot(lake)
        if ("reach", reach) in slot:
            return slot[("reach", reach)]
        universe = parse_universe(str(self.params["universe"]))
        frame: Any = reader(universe or None)
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            table = None
        else:
            known = known_moments(frame)
            visible = frame.loc[(known.notna() & (known <= pd.Timestamp(reach))).to_numpy()]
            key = ("rows", len(visible))
            table = slot.get(key)
            if table is None:
                table = self._score(visible)
                slot[key] = table
        slot[("reach", reach)] = table
        return table

    def _score(self, visible: pd.DataFrame) -> pd.DataFrame:
        df = pd.DataFrame(
            {
                "ticker": visible["ticker"].astype(str),
                "report_date": pd.to_datetime(visible["report_date"]),
                "timing": visible["before_after_market"].astype(object),
                "surprise": _numbers(visible["eps_actual"]) - _numbers(visible["eps_estimate"]),
            }
        )
        df = df.loc[np.isfinite(df["surprise"].to_numpy(dtype=float))]
        df = df.sort_values(["ticker", "report_date"], kind="stable").reset_index(drop=True)
        history = int(self.params["history"])
        spread = df.groupby("ticker")["surprise"].transform(
            lambda s: s.shift(1).rolling(history, min_periods=MIN_PRIOR_REPORTS).std()
        )
        df["sue"] = df["surprise"] / np.maximum(spread.to_numpy(dtype=float), MIN_SPREAD)
        df.loc[spread.isna().to_numpy(), "sue"] = np.nan
        return df

    def _edge(self, table: pd.DataFrame, report_date: pd.Timestamp) -> float | None:
        """The SUE that starts the top bucket for a report on ``report_date``."""
        start = report_date - pd.Timedelta(days=int(self.params["peer_days"]))
        days = table["report_date"]
        peers = table.loc[
            ((days >= start) & (days < report_date)).to_numpy() & table["sue"].notna().to_numpy(),
            "sue",
        ].to_numpy(dtype=float)
        if len(peers) < int(self.params["min_peers"]):
            return None
        return float(np.quantile(peers, 1.0 - 1.0 / int(self.params["buckets"])))

    # ---- Strategy Protocol -----------------------------------------------------------

    def estimate_return(self, ticker: str, as_of: date, lake: Any) -> float | None:
        universe = parse_universe(str(self.params["universe"]))
        if universe and ticker not in universe:
            return None
        reach = self._reach(as_of, lake)
        table = self._surprises(lake, reach)
        if table is None:
            return None
        rows = table.loc[(table["ticker"] == ticker).to_numpy()]
        if rows.empty:
            return None
        last = rows.iloc[-1]
        sue = float(last["sue"])
        if not np.isfinite(sue) or sue <= 0:
            return None
        report_day = pd.Timestamp(last["report_date"])
        if not isinstance(report_day, pd.Timestamp) or pd.isna(report_day):
            return None
        start = first_session(date.fromordinal(report_day.toordinal()), last["timing"])
        day = as_datetime(as_of).date()
        if int(np.busday_count(start, np.datetime64(day, "D"))) >= int(self.params["hold_days"]):
            return None
        slot = self._slot(lake)
        if ("edge", report_day) not in slot:
            # peers are earlier days only, so the edge is the same from any later view
            slot[("edge", report_day)] = self._edge(table, report_day)
        edge = slot[("edge", report_day)]
        if edge is None or sue < edge:
            return None
        return sue

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: date,
    ) -> list[Order]:
        return equal_weight_new_picks(self.id, my_picks, portfolio, prices, as_of)
