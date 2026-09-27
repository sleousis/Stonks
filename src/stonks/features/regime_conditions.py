"""Regime conditions for :class:`~stonks.strategies.regime.RegimeFilter` (BL-42).

A condition answers one question on a day: is this risk-off sign showing?
``triggered(as_of, ctx)`` returns ``True`` (risk off), ``False`` (risk on)
or ``None`` (can't tell: no data, too little history, stale data). Every
condition reads only data dated on or before ``as_of`` (P12).

Built in:

- ``macro``: one ``macro_indicators`` series, its level or change, against
  a threshold, counted from its publication date (reuses the loaders of
  :mod:`stonks.strategies.macro_regime`).
- ``price_trend``: a benchmark's close below its ``sma``-bar average, with
  an optional hysteresis band (Clenow, Faber).
- ``realized_vol``: a benchmark's realised volatility above its own
  ``pct`` quantile over the last ``lookback_years``.
- ``yield_curve``: long minus short yield (``bond_yield_history``) below
  ``threshold``, so an inverted curve by default.
- ``higher_timeframe``: a benchmark's close below the EMA of its weekly
  closes (Elder's Triple Screen). Weekly bars are built here from daily
  bars dated on or before ``as_of``, so the current week is partial and
  never holds a future close.

Registry: every subclass of :class:`RegimeCondition` that sets ``kind``
registers itself. Modules named ``stonks.features.regime_*`` are imported
on first lookup, so a new condition is one new file and no list is edited.
"""

from __future__ import annotations

import importlib
import math
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from stonks.core.interval import Interval

__all__ = [
    "ConditionContext",
    "RegimeCondition",
    "build_condition",
    "condition_kinds",
]

_REGISTRY: dict[str, type[RegimeCondition]] = {}
_DISCOVERED = False


@dataclass
class ConditionContext:
    """What a condition reads on one lake: the lake, a look-ahead-safe bar
    reader (anything with ``last_n_bars(ticker, interval, as_of, n)``) and a
    memo for per-lake caches (a macro series, a yield history)."""

    lake: Any
    bars: Any = None
    memo: dict[Any, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.bars is None and self.lake is not None:
            from stonks.strategies._common import BarCache

            self.bars = BarCache(self.lake)


class RegimeCondition(BaseModel, ABC):
    """One risk-off sign. Subclasses set ``kind`` and ``triggered``.

    ``shared_defaults`` maps a field to a :class:`RegimeFilter` param that
    supplies it when a condition spec leaves it out, so the filter's
    tunable knobs reach every condition of that kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ClassVar[str] = ""
    shared_defaults: ClassVar[Mapping[str, str]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        kind = cls.__dict__.get("kind")
        if isinstance(kind, str) and kind:
            _REGISTRY[kind] = cls

    @abstractmethod
    def triggered(self, as_of: Any, ctx: ConditionContext) -> bool | None:
        """``True`` risk off, ``False`` risk on, ``None`` unknown."""

    def spec(self) -> dict[str, Any]:
        return {"kind": self.kind, **self.model_dump(mode="json")}


def _discover() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    import stonks.features as features

    for info in pkgutil.iter_modules(features.__path__):
        if info.name.startswith("regime_") and info.name != "regime_conditions":
            importlib.import_module(f"{features.__name__}.{info.name}")


def condition_kinds() -> dict[str, type[RegimeCondition]]:
    """``kind -> class`` for every registered condition."""
    _discover()
    return dict(sorted(_REGISTRY.items()))


def build_condition(
    spec: Mapping[str, Any], shared: Mapping[str, Any] | None = None
) -> RegimeCondition:
    """A condition from ``{"kind": ..., **fields}``. Fields the spec leaves
    out come from ``shared`` (the filter's params) where the condition
    declares a shared default. Raises ``ValueError`` on a bad spec."""
    if not isinstance(spec, Mapping):
        raise ValueError(f"a regime condition must be a mapping, got {type(spec).__name__}")
    kinds = condition_kinds()
    kind = spec.get("kind")
    cls = kinds.get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise ValueError(f"unknown regime condition {kind!r}; choose one of {sorted(kinds)}")
    fields = {k: v for k, v in spec.items() if k != "kind"}
    for name, param in cls.shared_defaults.items():
        if name not in fields and shared is not None and param in shared:
            fields[name] = shared[param]
    try:
        return cls(**fields)
    except PydanticValidationError as exc:
        raise ValueError(f"regime condition {kind!r}: {exc}") from None


# ---- helpers -------------------------------------------------------------------------


def _closes(ctx: ConditionContext, ticker: str, interval: str, as_of: Any, n: int) -> pd.Series:
    """The last ``n`` closes dated on or before ``as_of``, indexed by
    timestamp, oldest first (empty when there are none)."""
    bars = ctx.bars.last_n_bars(ticker, Interval.parse(interval), as_of, n)
    if bars is None or bars.empty:
        return pd.Series(dtype=float)
    return pd.Series(bars["close"].to_numpy(dtype=float), index=pd.DatetimeIndex(bars["timestamp"]))


def _day(as_of: Any) -> date:
    """The last day whose day-stamped rows the decision may read: ``as_of``'s
    day for a daily decision, the day before for an intraday one, whose
    day has not closed yet (BE-20)."""
    from stonks.strategies._common import known_day

    return known_day(as_of)


# ---- conditions ----------------------------------------------------------------------


class MacroCondition(RegimeCondition):
    """A macro series' level or change crosses ``threshold``."""

    kind: ClassVar[str] = "macro"

    country_iso: str = "USA"
    indicator: str = "unemployment_total_percent"
    transform: Literal["level", "change"] = "change"
    change_periods: int = Field(default=1, ge=1, le=24)
    threshold: float = 0.5
    risk_off_when: Literal["above", "below"] = "above"
    publication_lag_days: int = Field(default=180, ge=0, le=730)
    observation_stamp: Literal["period_start", "period_end"] = "period_start"
    max_staleness_days: int = Field(default=730, ge=1, le=3650)

    def triggered(self, as_of: Any, ctx: ConditionContext) -> bool | None:
        from stonks.strategies.macro_regime import load_macro_observations, macro_signal

        key = (
            "macro",
            self.country_iso,
            self.indicator,
            self.publication_lag_days,
            self.observation_stamp,
        )
        observations = ctx.memo.get(key)
        if observations is None:
            observations = load_macro_observations(
                ctx.lake,
                self.country_iso,
                self.indicator,
                publication_lag_days=self.publication_lag_days,
                observation_stamp=self.observation_stamp,
            )
            ctx.memo[key] = observations
        signal = macro_signal(
            _day(as_of),
            observations,
            transform=self.transform,
            change_periods=self.change_periods,
            max_staleness_days=self.max_staleness_days,
        )
        if signal is None:
            return None
        if self.risk_off_when == "above":
            return signal > self.threshold
        return signal < self.threshold


class PriceTrendCondition(RegimeCondition):
    """The benchmark's close is below its ``sma``-bar simple average.

    With ``hysteresis`` h the state only flips to risk off below
    ``SMA * (1 - h)`` and back to risk on above ``SMA * (1 + h)``; inside
    the band it keeps its last state. The state is replayed over the last
    ``replay_bars`` bars, so it needs no memory between calls."""

    kind: ClassVar[str] = "price_trend"
    shared_defaults: ClassVar[Mapping[str, str]] = {
        "sma": "trend_sma",
        "hysteresis": "trend_hysteresis",
    }

    ticker: str = "SPY.US"
    sma: int = Field(default=200, ge=2, le=1000)
    hysteresis: float = Field(default=0.0, ge=0.0, le=0.03)
    interval: str = "1d"
    replay_bars: int = Field(default=252, ge=0, le=5000)

    def triggered(self, as_of: Any, ctx: ConditionContext) -> bool | None:
        closes = _closes(ctx, self.ticker, self.interval, as_of, self.sma + self.replay_bars)
        if len(closes) < self.sma:
            return None
        values = closes.to_numpy()
        averages = closes.rolling(self.sma, min_periods=self.sma).mean().to_numpy()
        start = self.sma - 1
        off = bool(values[start] < averages[start])
        lower, upper = 1.0 - self.hysteresis, 1.0 + self.hysteresis
        for close, avg in zip(values[start + 1 :], averages[start + 1 :], strict=True):
            if close < avg * lower:
                off = True
            elif close > avg * upper:
                off = False
        return off


class RealizedVolCondition(RegimeCondition):
    """The benchmark's realised volatility (std of ``window`` log returns)
    is above its own ``pct`` quantile over the last ``lookback_years``.
    Needs at least a year (or the whole lookback, if shorter) of history."""

    kind: ClassVar[str] = "realized_vol"
    shared_defaults: ClassVar[Mapping[str, str]] = {
        "window": "vol_window",
        "pct": "vol_pct",
    }

    ticker: str = "SPY.US"
    window: int = Field(default=21, ge=5, le=252)
    pct: float = Field(default=0.8, ge=0.5, le=0.99)
    lookback_years: float = Field(default=5.0, gt=0.0, le=30.0)
    bars_per_year: int = Field(default=252, ge=1, le=100_000)
    interval: str = "1d"

    def triggered(self, as_of: Any, ctx: ConditionContext) -> bool | None:
        history = int(math.ceil(self.lookback_years * self.bars_per_year))
        closes = _closes(ctx, self.ticker, self.interval, as_of, history + self.window + 1)
        log_returns = np.log(closes).diff()
        vol = log_returns.rolling(self.window, min_periods=self.window).std(ddof=1).dropna()
        if len(vol) < min(history, self.bars_per_year):
            return None
        vol = vol.iloc[-history:]
        return bool(vol.iloc[-1] > vol.quantile(self.pct))


class YieldCurveCondition(RegimeCondition):
    """Long minus short yield (``bond_yield_history``) is below
    ``threshold`` (default 0: an inverted curve). Each leg is its latest
    yield dated on or before ``as_of``; one older than
    ``max_staleness_days`` means unknown."""

    kind: ClassVar[str] = "yield_curve"

    long_ticker: str = "US10Y.GBOND"
    short_ticker: str = "US3M.GBOND"
    threshold: float = 0.0
    max_staleness_days: int = Field(default=10, ge=1, le=3650)

    def _latest(self, ticker: str, day: date, ctx: ConditionContext) -> float | None:
        key = ("yield", ticker)
        series = ctx.memo.get(key)
        if series is None:
            df = ctx.lake.get_bond_yields(ticker)
            df = df[df["yield_to_maturity"].notna()]
            series = (list(df["date"]), df["yield_to_maturity"].to_numpy(dtype=float))
            ctx.memo[key] = series
        dates, values = series
        i = int(
            np.searchsorted(np.array(dates, dtype="datetime64[D]"), np.datetime64(day), "right")
        )
        if i == 0 or (day - dates[i - 1]).days > self.max_staleness_days:
            return None
        return float(values[i - 1])

    def triggered(self, as_of: Any, ctx: ConditionContext) -> bool | None:
        day = _day(as_of)
        long_yield = self._latest(self.long_ticker, day, ctx)
        short_yield = self._latest(self.short_ticker, day, ctx)
        if long_yield is None or short_yield is None:
            return None
        return long_yield - short_yield < self.threshold


_RESAMPLE_RULES = {"1w": "W-SUN"}


class HigherTimeframeCondition(RegimeCondition):
    """The benchmark's latest close is below the ``ema``-period EMA of its
    ``interval`` closes, built from ``base_interval`` bars dated on or
    before ``as_of`` (the current period is partial, never future)."""

    kind: ClassVar[str] = "higher_timeframe"
    shared_defaults: ClassVar[Mapping[str, str]] = {"ema": "htf_ema"}

    ticker: str = "SPY.US"
    interval: Literal["1w"] = "1w"
    base_interval: Literal["1d"] = "1d"
    ema: int = Field(default=26, ge=2, le=200)

    def triggered(self, as_of: Any, ctx: ConditionContext) -> bool | None:
        # Enough daily bars for the EMA's warm-up (4x span) in weeks.
        closes = _closes(ctx, self.ticker, self.base_interval, as_of, 4 * self.ema * 7)
        if closes.empty:
            return None
        weekly = closes.resample(_RESAMPLE_RULES[self.interval]).last().dropna()
        if len(weekly) < self.ema:
            return None
        ema = weekly.ewm(span=self.ema, adjust=False, min_periods=self.ema).mean()
        return bool(weekly.iloc[-1] < ema.iloc[-1])
