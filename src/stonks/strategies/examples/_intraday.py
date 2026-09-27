"""Shared base of the intraday reference strategies (roadmap 21.3.1).

An intraday strategy decides on the close of a minute bar and only ever
reads closed bars (P12, RS-03). Each decision looks at the bars of the
current regular session of the ticker's exchange, from the open up to the
decision bar, and replays its rule over them. The strategies keep no state
between calls, so the answer at a bar is the same whatever came before it
in the run, and a restart or a replay gives the same decision.

Sessions come from the exchange calendar (``features.sessions``):
pre-market and after-hours bars stay outside, and early closes and
daylight saving move the open and the close. A ticker with no known
calendar never trades. Every strategy is flat ``exit_minutes_before_close``
before the close and flat at the first bar of a new session, so it holds
nothing overnight.

The strategies trade one ``ticker`` long only, like the other single
ticker examples. They run on today's bar-based backtester at a minute
interval. The event driver of roadmap 21.2 will call the same code.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import Features, Order, Portfolio
from stonks.features.sessions import (
    RegularSession,
    previous_regular_session,
    regular_session,
)
from stonks.strategies._common import LakeBarCaches, as_datetime, long_only_decide, visible_cutoff
from stonks.strategies.base import BaseStrategy

#: Minute intervals an intraday strategy may read.
INTRADAY_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "30m")
#: Minutes in a regular US session, for the bar counts in the metadata.
US_SESSION_MINUTES = 390
#: Floor of a positive score, so a live trade always ranks.
_MIN_SCORE = 1e-6


@dataclass(frozen=True)
class SessionBars:
    """The closed bars of the current session at a decision."""

    session: RegularSession
    #: Bars of this session up to the decision bar, oldest first, finite closes.
    bars: pd.DataFrame
    #: Minutes from the open to each bar's close.
    elapsed: np.ndarray
    #: Minutes from the decision bar's close to the session close.
    minutes_to_close: float
    #: Last close of the previous session, when it was asked for and exists.
    previous_close: float | None = None

    @property
    def close(self) -> np.ndarray:
        return self.bars["close"].to_numpy(dtype=float)

    @property
    def minutes_open(self) -> float:
        return float(self.elapsed[-1])


def bars_for(minutes: float, interval: str) -> int:
    """Bars of ``interval`` that cover ``minutes``."""
    per_bar = Interval.parse(interval).seconds / 60
    return max(1, math.ceil(round(minutes / per_bar, 9)))


def _naive_utc(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when
    return when.astimezone(UTC).replace(tzinfo=None)


def _stamps(frame: pd.DataFrame) -> pd.Series:
    ts = pd.to_datetime(frame["timestamp"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    return ts


class IntradayStrategy(BaseStrategy):
    """Base of the intraday examples (see the module doc). Subclasses give
    their own params in ``rule_spec`` and implement ``_score``."""

    id: ClassVar[str] = "intraday_base"
    applicable_asset_classes = ("equity",)
    #: The rule needs the previous session's last close.
    needs_previous_session: ClassVar[bool] = False
    #: Default of ``exit_minutes_before_close``.
    default_exit_minutes: ClassVar[int] = 5

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()

    @classmethod
    def rule_spec(cls) -> list[ParameterSpec]:
        return []

    @classmethod
    def parameter_spec(cls) -> list[ParameterSpec]:
        return [
            *cls.rule_spec(),
            ParameterSpec(
                name="exit_minutes_before_close",
                kind="int",
                default=cls.default_exit_minutes,
                bounds=(1, 120),
                tunable=False,
                description="Flat this many minutes before the session close, "
                "and no position in that last stretch.",
            ),
            ParameterSpec(
                name="interval",
                kind="categorical",
                default="1m",
                bounds=list(INTRADAY_INTERVALS),
                tunable=False,
                description="Minute bars to read from the lake.",
            ),
            ParameterSpec(
                name="ticker",
                kind="categorical",
                default="SPY.US",
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
                description="Fraction of cash deployed on an entry.",
            ),
        ]

    # ---- Strategy Protocol -----------------------------------------------------

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        if ticker != self.params["ticker"]:
            return None
        view = self.session_bars(ticker, as_of, lake)
        if view is None:
            return None
        return self._final_score(view)

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        view = self.session_bars(ticker, as_of, lake)
        if view is None:
            return Features(values={})
        score = self._final_score(view)
        values = {
            "close": float(view.close[-1]),
            "minutes_open": view.minutes_open,
            "minutes_to_close": float(view.minutes_to_close),
            "signal": 0.0 if score is None else 1.0,
        }
        values.update(self._features(view))
        return Features(values={k: v for k, v in values.items() if math.isfinite(v)})

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        return long_only_decide(
            self.id,
            self.params["ticker"],
            float(self.params["allocation"]),
            my_picks,
            portfolio,
            prices,
            as_of,
        )

    # ---- rule hooks ---------------------------------------------------------------

    def _score(self, view: SessionBars) -> float | None:
        """The score while the rule is long at the decision bar, else None."""
        raise NotImplementedError

    def _features(self, view: SessionBars) -> dict[str, float]:
        return {}

    # ---- the session at a decision -------------------------------------------------

    def _final_score(self, view: SessionBars) -> float | None:
        if view.minutes_to_close <= int(self.params["exit_minutes_before_close"]):
            return None
        score = self._score(view)
        if score is None or not math.isfinite(score):
            return None
        return max(float(score), _MIN_SCORE)

    def session_bars(self, ticker: str, as_of: Any, lake: Any) -> SessionBars | None:
        """The closed bars of the session the decision bar starting at
        ``as_of`` belongs to, or ``None`` when there is no such session or
        it has no closed bar yet."""
        if lake is None:
            return None
        at = _naive_utc(as_datetime(as_of))
        if not isinstance(at, datetime):
            return None
        session = regular_session(ticker, at)
        if session is None:
            return None
        interval = Interval.parse(self.params["interval"])
        cutoff = visible_cutoff(at, interval)
        decision_close = min(cutoff + interval.to_timedelta(), session.close)
        start = session.open
        previous = None
        if self.needs_previous_session:
            previous = previous_regular_session(ticker, session)
            if previous is None:
                return None
            start = previous.open
        cache = self._bar_caches.for_lake(lake)
        frame = cache.bars_between(ticker, interval, start, cutoff)
        if frame.empty:
            return None
        ts = _stamps(frame)
        finite = np.isfinite(frame["close"].to_numpy(dtype=float))
        today = finite & (ts >= session.open).to_numpy() & (ts < session.close).to_numpy()
        if not today.any():
            return None
        bars = frame.loc[today].reset_index(drop=True)
        bar_minutes = interval.seconds / 60
        elapsed = (ts[today] - session.open).dt.total_seconds().to_numpy(
            dtype=float
        ) / 60 + bar_minutes
        prev_close = None
        if previous is not None:
            mask = finite & (ts >= previous.open).to_numpy() & (ts < previous.close).to_numpy()
            if mask.any():
                prev_close = float(frame["close"].to_numpy(dtype=float)[mask][-1])
        return SessionBars(
            session=session,
            bars=bars,
            elapsed=elapsed,
            minutes_to_close=(session.close - decision_close) / timedelta(minutes=1),
            previous_close=prev_close,
        )
