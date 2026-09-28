"""Forecast skill survival test (roadmap 23.11).

A strategy that forecasts through the :mod:`stonks.features.forecasters`
seam (it has a ``forecaster()`` method) must show that its model beats
cheap baselines before its backtest counts. In markets the random walk is
very hard to beat, and a model can look good on error metrics while adding
nothing.

On the validation window (embargoed for the strategy), every
``every_bars`` bars and for every ticker, the model and the baselines see
the last ``context_bars`` bars up to that bar (read point in time) and
forecast the close ``horizon`` bars ahead. Scored in log return space:

- Diebold-Mariano on squared errors, HAC errors with at least
  ``horizon - 1`` lags, one-sided, against the random walk and against the
  better of the statistical baselines (``ets`` and ``theta`` by default).
  Losses are averaged across tickers per date first, so the test sees one
  time series.
- MASE (scaled by the context's in-sample ``horizon``-step naive error)
  of the model and of the random walk.
- CRPS from the quantiles, model against the random walk's bands.
- Rank IC across tickers per date, and directional accuracy.

It passes when there are at least ``min_forecasts`` dates, both DM
p-values are below ``alpha``, the model's MASE and CRPS are below the
random walk's, and the rank IC clears ``min_rank_ic`` when one is set.

The test fails when the validation window starts on or before the model's
pretraining cutoff (the preflight refuses such runs too). It judges only
the validation window, so it is safe for the research loop. Baselines are
fixed, never tuned, so they add no trials. A strategy with no forecaster
passes as ``n/a``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from stonks.core.protocols import Strategy, SurvivalReport
from stonks.features.forecasters.base import DEFAULT_LEVELS, MIN_CONTEXT, Forecaster
from stonks.features.forecasters.registry import build_forecaster, get_forecaster_class
from stonks.lab.dataset import scoring_window
from stonks.stats.forecast_tests import crps_from_quantiles, diebold_mariano, mase, rank_ic
from stonks.strategies._common import BarCache

__all__ = ["ForecastSkillOptions", "ForecastSkillTest"]

_HISTORY_START = datetime(1900, 1, 1)
#: Contexts handed to a model per call.
_BATCH = 64
#: Default horizon and context when neither the options nor the strategy say.
_HORIZON = 5
_CONTEXT = 128


class ForecastSkillOptions(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    horizon: int | None = Field(
        default=None,
        ge=1,
        description="Bars ahead to forecast. Default: the strategy's forecast horizon, else 5.",
    )
    every_bars: int = Field(default=5, ge=1, description="Forecast every this many bars.")
    context_bars: int | None = Field(
        default=None,
        ge=MIN_CONTEXT,
        description="Bars each forecast reads. Default: the strategy's context, else 128.",
    )
    alpha: float = Field(
        default=0.05, gt=0.0, lt=0.5, description="Significance level of the DM tests."
    )
    stat_baselines: tuple[str, ...] = Field(
        default=("ets", "theta"),
        description="Statistical baselines; the model must beat the better one.",
    )
    min_forecasts: int = Field(
        default=30, ge=2, description="Fewest forecast dates for a verdict (fewer fails)."
    )
    min_rank_ic: float | None = Field(
        default=None, description="Optional floor on the mean rank IC across tickers."
    )

    @field_validator("stat_baselines")
    @classmethod
    def _baselines(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("stat_baselines must name at least one baseline")
        for name in value:
            forecaster = get_forecaster_class(name)  # raises on an unknown name
            if forecaster.pretrained or name == "random_walk":
                raise ValueError(
                    f"{name!r} is not a statistical baseline (the random walk is always run)"
                )
        return tuple(dict.fromkeys(value))


def _forecaster_of(strategy: Any) -> Forecaster | None:
    hook = getattr(strategy, "forecaster", None)
    if not callable(hook):
        return None
    model = hook()
    return model if isinstance(model, Forecaster) else None


def _int_attr(strategy: Any, name: str) -> int | None:
    value = getattr(strategy, name, None)
    value = value() if callable(value) else value
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


class _Origins:
    """Forecast origins: contexts and outcomes, one row per (date, ticker)."""

    def __init__(self) -> None:
        self.dates: list[date] = []
        self.contexts: list[pd.DataFrame] = []
        self.y: list[float] = []
        self.scale: list[float] = []


def _collect(context: Any, window: tuple[date, date], h: int, n_ctx: int, every: int) -> _Origins:
    cache = BarCache(context.lake)
    interval = context.interval
    out = _Origins()
    for ticker in dict.fromkeys(context.universe):
        # outcomes from bars up to the window end (adjusted as of its end)
        full = cache.bars_between(ticker, interval, _HISTORY_START, window[1])
        if len(full) <= n_ctx + h:
            continue
        stamps = pd.to_datetime(full["timestamp"])
        days = stamps.dt.date.to_numpy()
        closes = full["close"].to_numpy(dtype=float)
        eligible = [
            i for i in range(n_ctx - 1, len(full) - h) if window[0] <= days[i] <= window[1]
        ][::every]
        for i in eligible:
            # the context is read point in time, adjusted as of its own last bar
            ctx = cache.last_n_bars(ticker, interval, stamps.iloc[i].to_pydatetime(), n_ctx)
            if len(ctx) < n_ctx or pd.Timestamp(ctx["timestamp"].iloc[-1]) != stamps.iloc[i]:
                continue
            c = ctx["close"].to_numpy(dtype=float)
            if not np.all(np.isfinite(c)) or np.any(c <= 0) or closes[i] <= 0:
                continue
            y = math.log(closes[i + h] / closes[i])
            if not math.isfinite(y):
                continue
            logs = np.log(c)
            out.dates.append(days[i])
            out.contexts.append(ctx)
            out.y.append(y)
            out.scale.append(float(np.mean(np.abs(logs[h:] - logs[:-h]))))
    return out


def _predict(
    model: Forecaster, contexts: Sequence[pd.DataFrame], h: int
) -> tuple[np.ndarray, np.ndarray]:
    """``(point log returns, quantile log returns n x levels)``."""
    points, quantiles = [], []
    for lo in range(0, len(contexts), _BATCH):
        for f in model.predict_batch(contexts[lo : lo + _BATCH], h, DEFAULT_LEVELS):
            points.append(f.log_return())
            quantiles.append(f.quantile_returns())
    return np.asarray(points, dtype=float), np.asarray(quantiles, dtype=float)


def _per_date(dates: Sequence[date], values: np.ndarray) -> np.ndarray:
    frame = pd.DataFrame({"d": list(dates), "v": values})
    return frame.groupby("d", sort=True)["v"].mean().to_numpy(dtype=float)


class ForecastSkillTest:
    id = "forecast_skill"
    Options = ForecastSkillOptions

    def __init__(self, options: ForecastSkillOptions | None = None, **overrides: Any) -> None:
        base = options or ForecastSkillOptions()
        self.options = (
            ForecastSkillOptions.model_validate({**base.model_dump(), **overrides})
            if overrides
            else base
        )

    @classmethod
    def build(cls, options: ForecastSkillOptions) -> ForecastSkillTest:
        return cls(options)

    def run(self, strategy: Strategy, context: Any) -> SurvivalReport:
        opts = self.options
        model = _forecaster_of(strategy)
        if model is None:
            return SurvivalReport(self.id, True, {}, "n/a: the strategy uses no forecaster")
        window = scoring_window(context, strategy, "val")
        span = f"window {window[0]}..{window[1]}"
        cutoff = type(model).cutoff()
        if cutoff is not None and window[0] <= cutoff:
            return SurvivalReport(
                self.id,
                False,
                {},
                f"the validation window starts {window[0]}, on or before the model's "
                f"pretraining cutoff {cutoff}; {span}",
            )
        h = opts.horizon or _int_attr(strategy, "forecast_horizon") or _HORIZON
        n_ctx = max(
            MIN_CONTEXT,
            opts.context_bars or _int_attr(strategy, "forecast_context_bars") or _CONTEXT,
        )
        origins = _collect(context, window, h, n_ctx, opts.every_bars)
        n_dates = len(set(origins.dates))
        if n_dates < opts.min_forecasts:
            return SurvivalReport(
                self.id,
                False,
                {"n_dates": float(n_dates), "n_forecasts": float(len(origins.y))},
                f"too few forecasts: {n_dates} dates, need {opts.min_forecasts}; {span}",
            )
        return self._score(model, origins, h, window, span)

    def _score(
        self,
        model: Forecaster,
        o: _Origins,
        h: int,
        window: tuple[date, date],
        span: str,
    ) -> SurvivalReport:
        opts = self.options
        y = np.asarray(o.y, dtype=float)
        levels = DEFAULT_LEVELS
        r_model, q_model = _predict(model, o.contexts, h)
        r_rw, q_rw = _predict(build_forecaster("random_walk"), o.contexts, h)
        stat = {
            name: _predict(build_forecaster(name), o.contexts, h)[0] for name in opts.stat_baselines
        }

        def loss(pred: np.ndarray) -> np.ndarray:
            return _per_date(o.dates, (y - pred) ** 2)

        l_model = loss(r_model)
        mse = {name: float(np.mean((y - pred) ** 2)) for name, pred in stat.items()}
        best = min(mse, key=lambda k: mse[k])
        dm_rw = diebold_mariano(loss(r_rw), l_model, horizon=h)
        dm_stat = diebold_mariano(loss(stat[best]), l_model, horizon=h)
        mase_model = mase(y - r_model, o.scale)
        mase_rw = mase(y - r_rw, o.scale)
        crps_model = crps_from_quantiles(levels, q_model, y)
        crps_rw = crps_from_quantiles(levels, q_rw, y)
        ic, ic_dates = rank_ic(o.dates, r_model, y)
        moved = (r_model != 0) & (y != 0)
        direction = (
            float(np.mean(np.sign(r_model[moved]) == np.sign(y[moved])))
            if moved.any()
            else math.nan
        )
        checks = {
            "beats random_walk (DM)": dm_rw.p_value < opts.alpha,
            f"beats {best} (DM)": dm_stat.p_value < opts.alpha,
            "MASE below the random walk": mase_model < mase_rw,
            "CRPS below the random walk": crps_model < crps_rw,
        }
        if opts.min_rank_ic is not None:
            checks[f"rank IC >= {opts.min_rank_ic}"] = math.isfinite(ic) and ic >= opts.min_rank_ic
        passed = all(checks.values())
        metrics = {
            "n_dates": float(len(l_model)),
            "n_forecasts": float(len(y)),
            "horizon": float(h),
            "dm_stat_random_walk": dm_rw.stat,
            "dm_p_random_walk": dm_rw.p_value,
            "dm_stat_stat_baseline": dm_stat.stat,
            "dm_p_stat_baseline": dm_stat.p_value,
            "dm_lags": float(dm_rw.lags),
            "mse_model": float(np.mean((y - r_model) ** 2)),
            "mse_random_walk": float(np.mean((y - r_rw) ** 2)),
            "mse_stat_baseline": mse[best],
            "mase_model": mase_model,
            "mase_random_walk": mase_rw,
            "crps_model": crps_model,
            "crps_random_walk": crps_rw,
            "rank_ic": ic,
            "rank_ic_dates": float(ic_dates),
            "directional_accuracy": direction,
        }
        failed = [name for name, ok in checks.items() if not ok]
        verdict = "passed" if passed else "failed: " + ", ".join(failed)
        notes = (
            f"{verdict}; model {model.name or type(model).__name__} against random_walk and "
            f"{best} (the better of {'/'.join(opts.stat_baselines)}); DM p "
            f"{dm_rw.p_value:.3g} and {dm_stat.p_value:.3g}; {span}"
        )
        return SurvivalReport(self.id, passed, metrics, notes)
