"""Shared plumbing for the time-series trend strategies of BL-40 (EWMAC,
time-series momentum, all-time-high trend).

Each ticker is judged on its own history, so there is no cross section to
rank: a subclass turns one ticker's adjusted daily bars into a signed,
capped Carver forecast (mean |f| about 10, capped at +/-20), and this base
does the rest the same way for all three:

- **Look-ahead.** Bars are read through the look-ahead-safe ``BarCache`` on
  the adjusted basis, only up to ``as_of``'s session. A ticker whose last
  bar is stale (delisted, halted) gets no forecast.
- **Long only by default.** ``estimate_return`` is the forecast when it is
  positive and ``None`` otherwise: a short forecast goes flat. With
  ``short_mode = "short"`` (roadmap 16.3) a negative forecast is returned
  as is, so a book that may short holds the down trend short, sized the
  same way. A long-only book still drops the short legs.
- **Sizing** goes through the ``vol_target`` constructor (Carver:
  ``w = tau * IDM * F / 10 / sigma / N``) over every ticker evaluated that
  day, flat ones included, so a single long name is not levered up to the
  whole risk budget. Gross is capped at 1.0 and buys never spend more than
  cash plus sale proceeds (``orders_from_targets``), with Carver's 10%
  no-trade buffer.
- ``decide`` gets no lake, so it sizes over every ticker this instance was
  asked about on the last lake it saw, evaluated on ``decide``'s own day
  (the rows ``estimate_return`` cached, recomputed if another day was
  evaluated in between). A fresh instance that was never asked anything
  makes no buys and never resizes; it still sells held names that are no
  longer picked.
"""

from __future__ import annotations

import math
import weakref
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from stonks.core.interval import Interval
from stonks.core.params import ParameterSpec
from stonks.core.types import AssetClass, Features, Order, Portfolio
from stonks.features.forecast import combine_forecasts
from stonks.features.momentum import is_quarter_rebalance_day
from stonks.features.sessions import is_session, week_index
from stonks.features.volatility import annualize, ewma_vol, periods_per_year
from stonks.portfolio.base import ConstructionInput, get_constructor
from stonks.portfolio.constructors import diversification_multiplier
from stonks.portfolio.orders import orders_from_targets
from stonks.portfolio.signals import estimate_forecast_scalar
from stonks.strategies._common import BarCache, LakeBarCaches, visible_cutoff
from stonks.strategies.base import BaseStrategy
from stonks.strategies.examples._cross_section import (
    is_fresh,
    orders_from_constructor,
    session_cutoff,
)

#: Rows of rule forecasts needed before the FDM or a forecast scalar is
#: estimated from a ticker's own history (about a year of daily bars).
MIN_ESTIMATION_BARS = 250
#: EWMA span of the daily return sigma used for sizing (Carver: 35 days).
SIZING_VOL_SPAN = 35
#: Trailing daily returns kept per ticker for the instrument
#: diversification multiplier.
IDM_RETURNS_BARS = 250
_HISTORY_START = pd.Timestamp("1900-01-01").to_pydatetime()
_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()


def forecast_specs(*, fdm_default: float = 1.1) -> list[ParameterSpec]:
    """How rule forecasts are scaled and combined."""
    return [
        ParameterSpec(
            name="scalar_mode",
            kind="categorical",
            default="fixed",
            bounds=["fixed", "estimate"],
            tunable=False,
            description="'fixed': the published forecast scalars; 'estimate': "
            "10 / mean|raw| over the ticker's own history up to as_of (falls "
            "back to fixed with under a year of it).",
        ),
        ParameterSpec(
            name="fdm_mode",
            kind="categorical",
            default="fixed",
            bounds=["fixed", "estimate"],
            tunable=False,
            description="'fixed': use fdm; 'estimate': 1/sqrt(w'Hw) from the "
            "correlation of the rules' past forecasts up to as_of (falls back "
            "to fdm with under a year of them).",
        ),
        ParameterSpec(
            name="fdm",
            kind="float",
            default=fdm_default,
            bounds=(1.0, 2.5),
            tunable=False,
            description="Forecast diversification multiplier across the rules.",
        ),
    ]


def sizing_specs() -> list[ParameterSpec]:
    """Knobs of the ``vol_target`` sizing and the order buffer."""
    return [
        ParameterSpec(
            name="tau",
            kind="float",
            default=0.20,
            bounds=(0.05, 0.40),
            tunable=False,
            description="Annual volatility target of the book (vol_target).",
        ),
        ParameterSpec(
            name="buffer_fraction",
            kind="float",
            default=0.10,
            bounds=(0.0, 0.5),
            tunable=False,
            description="No-trade band around each target, as a fraction of it.",
        ),
    ]


def forecast_diversification_multiplier(rules: pd.DataFrame, fallback: float) -> float:
    """FDM from the correlation of the rules' past forecasts (equal
    weights; negative correlations floored at 0; capped at 2.5). ``fallback``
    with fewer than :data:`MIN_ESTIMATION_BARS` rows where every rule has a
    value. A single rule has an FDM of 1."""
    if rules.shape[1] < 2:
        return 1.0
    overlap = rules.dropna()
    if len(overlap) < MIN_ESTIMATION_BARS:
        return fallback
    weights = dict.fromkeys(rules.columns, 1.0 / rules.shape[1])
    return diversification_multiplier(overlap, weights, min_observations=MIN_ESTIMATION_BARS)


def rule_scalar(raw: pd.Series, fixed: float, mode: str) -> float:
    """``fixed``, or in ``"estimate"`` mode ``10 / mean|raw|`` over ``raw``
    (the ticker's history up to as_of) once it has a year of values."""
    if mode != "estimate":
        return fixed
    history = raw.dropna()
    if len(history) < MIN_ESTIMATION_BARS or not (history.abs() > 0).any():
        return fixed
    return estimate_forecast_scalar(history)


def _last_bar_of_open_period(day: date, asset_class: str, period: str) -> bool:
    """Whether ``day``'s bar closes its week / month, from the calendar
    (never from the next bar): crypto trades every day; other classes on
    US exchange sessions."""
    if asset_class == "crypto":
        nxt = day + timedelta(days=1)
        return nxt.weekday() == 0 if period == "week" else nxt.month != day.month
    if period == "month":
        return is_quarter_rebalance_day(day, range(1, 13))
    d = day + timedelta(days=1)
    while week_index(d) == week_index(day):
        if is_session(d):
            return False
        d += timedelta(days=1)
    return True


def period_end_mask(days: Any, asset_class: str, period: str) -> np.ndarray:
    """``True`` for each bar (``days``: dates or timestamps, oldest first)
    that closes its week (``period="week"``, Monday-anchored as
    :func:`~stonks.features.sessions.week_index`) or month. A bar followed by
    one in a later period is the period's last; the latest bar is judged
    from the calendar."""
    if period not in ("week", "month"):
        raise ValueError(f"period must be 'week' or 'month', got {period!r}")
    index = pd.DatetimeIndex(days)
    if index.empty:
        return np.zeros(0, dtype=bool)
    if period == "week":
        epoch_days = index.to_numpy(dtype="datetime64[D]").astype(np.int64)
        keys = (epoch_days + _EPOCH_ORDINAL - 1) // 7  # == week_index(day)
    else:
        keys = index.to_numpy(dtype="datetime64[M]").astype(np.int64)
    mask = np.zeros(len(index), dtype=bool)
    mask[:-1] = keys[1:] != keys[:-1]
    mask[-1] = _last_bar_of_open_period(index[-1].date(), asset_class, period)
    return mask


@dataclass(frozen=True)
class _Row:
    forecast: float  # signed
    sigma_annual: float | None
    returns: pd.Series


@dataclass
class _Day:
    day: date
    rows: dict[str, _Row | None] = field(default_factory=dict)


class ForecastTrendStrategy(BaseStrategy):
    """Base of the BL-40 trend strategies (see the module docstring).
    Subclasses implement :meth:`_signed_forecast` and :meth:`_history_bars`."""

    alpha_family = "trend"
    premise = "trend"
    applicable_asset_classes: ClassVar[tuple[AssetClass, ...]] = ("equity", "crypto", "commodity")
    short_capable: ClassVar[bool] = True

    def __init__(self, params: Any) -> None:
        super().__init__(params)
        self._bar_caches = LakeBarCaches()
        self._days: weakref.WeakKeyDictionary[Any, _Day] = weakref.WeakKeyDictionary()
        self._classes: weakref.WeakKeyDictionary[Any, dict[str, str]] = weakref.WeakKeyDictionary()
        # per lake: every ticker asked (an ordered set), the instruments
        # ``decide`` spreads the risk budget over
        self._seen: weakref.WeakKeyDictionary[Any, dict[str, None]] = weakref.WeakKeyDictionary()
        self._last_lake: weakref.ref | None = None

    # ---- subclass hooks ------------------------------------------------------------

    def _history_bars(self) -> int | None:
        """Trailing daily bars read per evaluation (``None``: all of them)."""
        raise NotImplementedError

    def _min_bars(self) -> int:
        """Bars needed before any forecast is made."""
        return max(1, int(type(self).required_history_bars))

    def param_metadata(self) -> dict[str, int]:
        return {"required_history_bars": self._min_bars()}

    def _signed_forecast(self, bars: pd.DataFrame, asset_class: str) -> float | None:
        """The capped forecast at the last of ``bars`` (adjusted daily bars
        dated on or before as_of, oldest first); ``None`` when undefined."""
        raise NotImplementedError

    # ---- shared helpers for subclasses ---------------------------------------------

    def _combine(self, rules: pd.DataFrame) -> pd.Series:
        """Combined forecast per bar of the rules' capped forecasts, with the
        fixed or estimated FDM."""
        fallback = float(self.params["fdm"])
        if self.params["fdm_mode"] == "estimate":
            fdm = forecast_diversification_multiplier(rules, fallback)
        else:
            fdm = fallback if rules.shape[1] > 1 else 1.0
        return combine_forecasts(rules, fdm=fdm)

    # ---- evaluation ------------------------------------------------------------------

    def forecast(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        """The signed forecast for ``ticker`` from bars dated on or before
        ``as_of`` (negative = short, which the long-only book holds flat)."""
        row = self._row(ticker, as_of, lake)
        return None if row is None else row.forecast

    def _row(self, ticker: str, as_of: Any, lake: Any) -> _Row | None:
        if lake is None:
            return None
        day, cutoff = session_cutoff(as_of)
        state = self._day_state(lake, day, ticker)
        if state is not None and ticker in state.rows:
            return state.rows[ticker]
        row = self._evaluate(ticker, cutoff, lake)
        if state is not None:
            state.rows[ticker] = row
        return row

    def _day_state(self, lake: Any, day: date, ticker: str | None = None) -> _Day | None:
        """The row cache of ``day`` (one day per lake: a new day replaces
        it). Also remembers ``lake`` and the tickers asked on it, which
        :meth:`decide` sizes over."""
        try:
            state = self._days.get(lake)
            if state is None or state.day != day:
                state = _Day(day)
                self._days[lake] = state
            if ticker is not None:
                self._seen.setdefault(lake, {})[ticker] = None
            self._last_lake = weakref.ref(lake)
        except TypeError:  # a lake that can't be weakly referenced
            return None
        return state

    def _asset_class(self, ticker: str, lake: Any) -> str:
        try:
            known = self._classes.setdefault(lake, {})
        except TypeError:
            known = {}
        if ticker not in known:
            try:
                known[ticker] = lake.get_asset_classes([ticker]).get(ticker, "equity")
            except AttributeError:
                known[ticker] = "equity"
        return known[ticker]

    def _bars(self, cache: BarCache, ticker: str, cutoff: Any) -> pd.DataFrame:
        n = self._history_bars()
        if n is None:
            # an explicit range: apply the visibility rule here (RS-03)
            end = visible_cutoff(cutoff, Interval.DAY_1)
            return cache.bars_between(ticker, Interval.DAY_1, _HISTORY_START, end)
        return cache.last_n_bars(ticker, Interval.DAY_1, cutoff, n)

    def _evaluate(self, ticker: str, cutoff: Any, lake: Any) -> _Row | None:
        cache = self._bar_caches.for_lake(lake)
        if not is_fresh(cache, ticker, cutoff):
            return None
        bars = self._bars(cache, ticker, cutoff)
        if len(bars) < self._min_bars():
            return None
        asset_class = self._asset_class(ticker, lake)
        forecast = self._signed_forecast(bars.reset_index(drop=True), asset_class)
        if forecast is None or not math.isfinite(forecast):
            return None
        closes = bars["close"].astype(float)
        returns = np.log(closes).diff()
        returns.index = pd.to_datetime(bars["timestamp"])
        sigma = ewma_vol(returns, span=SIZING_VOL_SPAN).iloc[-1]
        sigma_annual = (
            float(annualize(sigma, periods_per_year(asset_class)))  # type: ignore[arg-type]
            if math.isfinite(sigma) and sigma > 0
            else None
        )
        return _Row(float(forecast), sigma_annual, returns.iloc[-IDM_RETURNS_BARS:].dropna())

    # ---- Strategy Protocol ------------------------------------------------------------

    def extract_features(self, ticker: str, as_of: Any, lake: Any) -> Features:
        row = self._row(ticker, as_of, lake)
        if row is None:
            return Features(values={})
        values = {"forecast": row.forecast}
        if row.sigma_annual is not None:
            values["sigma_annual"] = row.sigma_annual
        return Features(values=values)

    def estimate_return(self, ticker: str, as_of: Any, lake: Any) -> float | None:
        f = self.forecast(ticker, as_of, lake)
        if f is None or f == 0:
            return None
        return f if f > 0 or self.supports_short else None

    def decide(
        self,
        my_picks: Sequence[tuple[float, str]],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        as_of: Any,
    ) -> list[Order]:
        day, _ = session_cutoff(as_of)
        shorts = self.supports_short
        picked = {t for r, t in my_picks if r > 0 or (shorts and r < 0)}
        lake = self._last_lake() if self._last_lake is not None else None
        seen = list(self._seen.get(lake, {})) if lake is not None else []
        if not seen:
            return self._exits_only(picked, portfolio, prices, day)
        # the rows of decide's own day (cached unless an evaluation of another
        # day came in between, e.g. a wrapper replaying past bars)
        evaluated = {t: self._row(t, as_of, lake) for t in seen}
        rows = {t: r for t, r in evaluated.items() if r is not None}
        signals = {
            t: ((r.forecast if shorts else max(r.forecast, 0.0)) if t in picked else 0.0)
            for t, r in rows.items()
        }
        vols = {t: r.sigma_annual for t, r in rows.items() if r.sigma_annual is not None}
        returns = {t: r.returns for t, r in rows.items() if not r.returns.empty}
        inp = ConstructionInput(
            signals={self.id: signals},
            portfolio=portfolio,
            prices=prices,
            as_of=day,
            vols_annual=vols,  # type: ignore[arg-type]
            returns_history=pd.DataFrame(returns) if returns else None,
        )
        constructor = get_constructor(
            "vol_target", tau=float(self.params["tau"]), long_only=not shorts
        )
        return orders_from_constructor(
            constructor,
            inp,
            strategy_id=self.id,
            buffer_fraction=float(self.params["buffer_fraction"]),
            allow_short=shorts,
        )

    def _exits_only(
        self,
        picked: set[str],
        portfolio: Portfolio,
        prices: Mapping[str, float],
        day: date,
    ) -> list[Order]:
        """Without the day's forecasts: keep picked holdings as they are and
        sell the rest."""
        equity = portfolio.total_value(prices)
        if not equity > 0:
            return []
        shorts = self.supports_short
        keep = {
            t: q * prices[t] / equity
            for t, q in portfolio.positions.items()
            if (q > 0 or (shorts and q < 0)) and t in picked and prices.get(t)
        }
        return orders_from_targets(
            keep, portfolio, prices, as_of=day, strategy_id=self.id, allow_short=shorts
        )
